#!/usr/bin/env python3
"""
mcrsync.py - A Midnight-Commander-style rsync/rclone queue manager.

Targets Python 3.4 (Synology chroot). Avoids f-strings, subprocess.run,
os.scandir and other 3.5+-only features on purpose.

Either pane can be switched to a remote SSH host (key O); rsync then runs
over a shared ssh ControlMaster connection. A pane can also show an rclone
remote (cloud/object storage from the user's own rclone config); items
involving one are copied with rclone instead of rsync.

Views and keys are documented in HELP_TEXT.
"""

import curses
import curses.textpad
import os
import posixpath
import sys
import json
import time
import shutil
import stat
import tempfile
import getpass
import threading
import subprocess
import re
import shlex
import signal
import textwrap
import queue as pyqueue
from collections import OrderedDict

# Semantic versioning. A release is made by pushing the tag "v" + VERSION.
VERSION = "2.1.1"

DEFAULT_RSYNC_OPTS = ["-a", "-u", "--stats", "--itemize-changes"]
DEFAULT_RCLONE_OPTS = ["--update", "--links"]

TOOL_RSYNC = "rsync"
TOOL_RCLONE = "rclone"

# ----------------------------------------------------------------------
# Data model
# ----------------------------------------------------------------------

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_OK = "ok"
STATUS_FAIL = "fail"
STATUS_INCOMPLETE = "incomplete"
# Dry-run results must not look like SYNCED OK, or a later real run would
# skip those items.
STATUS_DRY_OK = "dry_ok"
STATUS_DRY_INCOMPLETE = "dry_incomplete"

# Queue files store the ids above; these are only for display.
STATUS_LABELS = {
    STATUS_PENDING: "NOT SYNCED",
    STATUS_RUNNING: "IN PROGRESS",
    STATUS_OK: "SYNCED OK",
    STATUS_FAIL: "SYNC FAIL",
    STATUS_INCOMPLETE: "INCOMPLETE",
    STATUS_DRY_OK: "DRY RUN OK",
    STATUS_DRY_INCOMPLETE: "DRY RUN INCOMPLETE",
}


class Conflict:
    """A file that exists in both source and dest, dest is newer. Paths are
    on the item's source/dest side (local or remote)."""
    def __init__(self, relpath, src_full, dst_full, dry_run=False):
        self.relpath = relpath
        self.src_full = src_full
        self.dst_full = dst_full
        self.dry_run = dry_run
        self.resolution = None      # None / "skip" / "rename" / "autorename"
        if dry_run:
            self.remark = "target newer (dry run - nothing was copied)"
        else:
            self.remark = "target newer - needs attention"


class ConnSpec:
    """Identifies a remote SSH endpoint. Never holds a password - ssh asks
    for that itself when the connection is opened.

    Specs also come from queue files, so the constructor validates: a user
    or host starting with '-' would otherwise be taken as an ssh option
    (e.g. -oProxyCommand=... runs a local command)."""

    # \Z, not $: $ also matches before a trailing newline
    NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*\Z")
    kind = "ssh"
    tool = TOOL_RSYNC

    def __init__(self, user, host, port=22, keyfile=""):
        if not isinstance(user, str) or not ConnSpec.NAME_RE.match(user):
            raise ValueError("invalid user name {!r} (letters, digits, . _ - only)".format(user))
        if not isinstance(host, str) or not ConnSpec.NAME_RE.match(host):
            raise ValueError("invalid host {!r} (host name or IPv4 address)".format(host))
        try:
            port = int(port)
        except (TypeError, ValueError):
            port = 0
        if not 1 <= port <= 65535:
            raise ValueError("port must be a number from 1 to 65535")
        keyfile = keyfile or ""
        if not isinstance(keyfile, str) or "\n" in keyfile or "\0" in keyfile:
            raise ValueError("invalid key file path")
        if "%" in keyfile:
            # OpenSSH versions differ in whether -i expands %-tokens
            raise ValueError("key file path can't contain %")
        self.user = user
        self.host = host
        self.port = port
        self.keyfile = keyfile

    @staticmethod
    def from_form(user, host, port, keyfile):
        """Validate connect-form input. Returns (ConnSpec, "") or
        (None, error_text for the user)."""
        if not user:
            return None, "User is required."
        if not host:
            return None, "Host is required."
        if ":" in host:
            return None, "IPv6 addresses aren't supported - use a host name."
        if keyfile and not os.path.isfile(os.path.expanduser(keyfile)):
            return None, "Key file not found: {}".format(keyfile)
        try:
            return ConnSpec(user, host, port or "22", keyfile), ""
        except ValueError as e:
            return None, str(e)[0].upper() + str(e)[1:] + "."

    def key(self):
        return "{}@{}:{}".format(self.user, self.host, self.port)

    def display_path(self, path):
        return "{}:{}".format(self.host, path)

    def to_dict(self):
        return {
            "type": "ssh",
            "user": self.user,
            "host": self.host,
            "port": self.port,
            "keyfile": self.keyfile,
        }

    @staticmethod
    def from_dict(d):
        if not d:
            return None
        return ConnSpec(d["user"], d["host"], d.get("port", 22), d.get("keyfile", ""))


class RcloneSpec:
    """Identifies a remote from the user's own rclone config, by name only:
    mcrsync never reads or stores its credentials.

    The name ends up in front of a ':' on rclone's command line, so it is
    restricted to characters that can't make it an option ('-' first), a
    connection string (',', '=') or a path."""

    NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]*\Z")
    kind = "rclone"
    tool = TOOL_RCLONE

    def __init__(self, remote):
        if not isinstance(remote, str) or not RcloneSpec.NAME_RE.match(remote):
            raise ValueError("invalid rclone remote name {!r} (letters, digits, _ - only)".format(
                remote))
        self.remote = remote

    def key(self):
        return "rclone:" + self.remote

    def display_path(self, path):
        return "{}:{}".format(self.remote, path)

    def arg(self, path):
        """rclone's 'remote:path' argument. Paths are kept absolute ('/'
        first) - local and sftp backends would read anything else relative
        to the current or home directory."""
        return "{}:{}".format(self.remote, path)

    def to_dict(self):
        return {"type": "rclone", "remote": self.remote}


def conn_from_dict(d):
    """ConnSpec / RcloneSpec / None from a queue file. Entries without a
    type are SSH (queue files written before rclone support)."""
    if not d:
        return None
    if not isinstance(d, dict):
        raise ValueError("invalid connection in queue: {!r}".format(d))
    kind = d.get("type", "ssh")
    if kind == "ssh":
        return ConnSpec.from_dict(d)
    if kind == "rclone":
        return RcloneSpec(d["remote"])
    raise ValueError("unknown connection type in queue: {!r}".format(kind))


def format_size_si(n_bytes):
    """Format a byte count as e.g. '400.0 M', picking the largest SI unit
    that keeps the value >= 1.0. Returns 'nan' if n_bytes is None (size not
    yet known/estimated)."""
    if n_bytes is None:
        return "nan"
    n = float(n_bytes)
    for factor, suffix in ((1024.0 ** 4, "T"), (1024.0 ** 3, "G"),
                            (1024.0 ** 2, "M"), (1024.0, "K")):
        if n >= factor:
            return "{:3.1f} {}".format(n / factor, suffix)
    return "{:3.1f} B".format(n)


def format_duration(total_seconds):
    """Format a duration in seconds as 'Xd Yh Zm' (omitting leading zero
    units), for ETA display."""
    total_seconds = int(total_seconds)
    if total_seconds < 0:
        total_seconds = 0
    days, rem = divmod(total_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    mins, _ = divmod(rem, 60)
    parts = []
    if days:
        parts.append("{}d".format(days))
    if days or hours:
        parts.append("{}h".format(hours))
    parts.append("{}m".format(mins))
    return " ".join(parts)


def atomic_write(target, text):
    """Write text to target via a temp file in the same folder + rename, so
    a crash never leaves a half-written file. mkstemp creates the temp file
    exclusively under a random name: a predictable name like 'x.tmp' could
    be a symlink planted by another user in a shared folder, and open()
    would follow it and overwrite whatever it points to."""
    folder = os.path.dirname(os.path.abspath(target))
    fd, tmp = tempfile.mkstemp(prefix=".mcrsync-", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class QueueItem:
    STATUS_BY_LABEL = {label: status for status, label in STATUS_LABELS.items()}

    def __init__(self, src, dst, is_dir=True, src_conn=None, dst_conn=None):
        self.src = src
        self.dst = dst
        self.is_dir = is_dir
        # ConnSpec of the remote side, or None for local
        self.src_conn = src_conn
        self.dst_conn = dst_conn
        self.status = STATUS_PENDING
        self.conflicts = []          # list[Conflict]
        self.copied_files = 0
        self.error_text = ""
        self.copied_list = []        # list of (src, dst, remark)
        self.errors_list = []        # list of (src, dst, remark)

        # Size estimate, computed lazily in the background: total bytes and
        # number of regular files (what rsync -a copies as file data).
        self.size_bytes = None
        self.size_files = None
        # "unknown" | "pending" | "computing" | "done" | "error" | "offline"
        # ("offline" = remote source whose connection isn't open yet)
        self.size_state = "unknown"
        # Not persisted to JSON - used to abort an in-flight/queued
        # background size calculation if this item gets deleted.
        self.cancel_event = threading.Event()
        # Wall-clock seconds the rsync transfer took, recorded ONLY for
        # items that finish with SYNCED OK (used for ETA rate estimates).
        self.transfer_seconds = None
        # Progress of the current/last run; not persisted.
        self.progress = None

    def conn_specs(self):
        return [s for s in (self.src_conn, self.dst_conn) if s is not None]

    def tool(self):
        """rclone if either side is an rclone remote, else rsync."""
        if any(s.tool == TOOL_RCLONE for s in self.conn_specs()):
            return TOOL_RCLONE
        return TOOL_RSYNC

    def src_display(self, path=None):
        path = self.src if path is None else path
        return self.src_conn.display_path(path) if self.src_conn else path

    def dst_display(self, path=None):
        path = self.dst if path is None else path
        return self.dst_conn.display_path(path) if self.dst_conn else path

    def status_label(self):
        return STATUS_LABELS[self.status]

    def to_dict(self):
        return {
            "src": self.src,
            "dst": self.dst,
            "is_dir": self.is_dir,
            "src_conn": self.src_conn.to_dict() if self.src_conn else None,
            "dst_conn": self.dst_conn.to_dict() if self.dst_conn else None,
            "status": self.status,
            "size_bytes": self.size_bytes,
            "size_files": self.size_files,
            "transfer_seconds": self.transfer_seconds,
        }

    @staticmethod
    def check_path(path):
        """Queue paths are always absolute; this also guarantees rsync can
        never read one as an option or a host:path spec."""
        if not isinstance(path, str) or not path.startswith("/") or "\0" in path:
            raise ValueError("invalid path in queue: {!r} (must be absolute)".format(path))
        return path

    @staticmethod
    def from_dict(d):
        QueueItem.check_path(d["src"])
        QueueItem.check_path(d["dst"])
        if not isinstance(d.get("is_dir", True), bool):
            raise ValueError("invalid is_dir in queue: {!r}".format(d.get("is_dir")))
        src_conn = conn_from_dict(d.get("src_conn"))
        dst_conn = conn_from_dict(d.get("dst_conn"))
        # the same combinations the UI allows - see App.pane_conflict
        kinds = sorted(s.kind for s in (src_conn, dst_conn) if s is not None)
        if kinds in (["ssh", "ssh"], ["rclone", "ssh"]):
            raise ValueError("invalid item in queue: {} can't be combined".format(
                " and ".join(kinds)))
        it = QueueItem(d["src"], d["dst"], d.get("is_dir", True), src_conn, dst_conn)
        status = d.get("status", STATUS_PENDING)
        # version 1 files stored the display labels
        status = QueueItem.STATUS_BY_LABEL.get(status, status)
        if status not in STATUS_LABELS:
            raise ValueError("invalid status in queue: {!r}".format(status))
        it.status = status
        # never resume as "in progress", and a dry run copied nothing
        if it.status in (STATUS_RUNNING, STATUS_DRY_OK, STATUS_DRY_INCOMPLETE):
            it.status = STATUS_PENDING
        # cached numbers: anything odd is just recomputed
        it.size_bytes = QueueItem.count_or_none(d.get("size_bytes"))
        it.size_files = QueueItem.count_or_none(d.get("size_files"))
        seconds = d.get("transfer_seconds")
        it.transfer_seconds = seconds if isinstance(seconds, (int, float)) and seconds > 0 else None
        it.size_state = "done" if it.has_size() else "unknown"
        return it

    @staticmethod
    def count_or_none(value):
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
        return None

    def has_size(self):
        return self.size_bytes is not None and self.size_files is not None


class QueueFile:
    """Saved queue (.json). Versions: none = a bare list of items (upstream),
    1 = object with flags (no version field), 2 = status ids instead of labels."""
    VERSION = 2

    @staticmethod
    def save(path, items, rsync_opts, rclone_opts):
        data = {
            "version": QueueFile.VERSION,
            "rsync_opts": rsync_opts,
            "rclone_opts": rclone_opts,
            "items": [it.to_dict() for it in items],
        }
        atomic_write(path, json.dumps(data, indent=2))

    @staticmethod
    def check_opts(data, key):
        """A queue file's flag list for one tool, or None if absent."""
        if key not in data:
            return None
        opts = data[key]
        if (not isinstance(opts, list) or not opts or
                not all(isinstance(o, str) and "\0" not in o for o in opts)):
            raise ValueError("invalid {} in queue file".format(key))
        return opts

    @staticmethod
    def read(path):
        """Parse and validate a queue file. Returns (items, rsync_opts,
        rclone_opts), each opts None if the file doesn't set them; raises
        ValueError/KeyError/OSError. Nothing is applied, so a bad file
        changes nothing."""
        with open(path, "r") as f:
            data = json.load(f)
        if isinstance(data, list):
            return [QueueItem.from_dict(d) for d in data], None, None
        if not isinstance(data, dict):
            raise ValueError("not a queue file")
        version = data.get("version", 1)
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise ValueError("invalid version in queue file: {!r}".format(version))
        if version > QueueFile.VERSION:
            raise ValueError("the queue file was made by a newer mcrsync (format version {}) "
                             "- update mcrsync to load it".format(version))
        rsync_opts = QueueFile.check_opts(data, "rsync_opts")
        rclone_opts = QueueFile.check_opts(data, "rclone_opts")
        for flags, opts in ((RsyncFlags, rsync_opts), (RcloneFlags, rclone_opts)):
            if opts is not None and flags.problems(opts):
                raise ValueError("{} flags in queue file: {}".format(
                    flags.TOOL, flags.problems(opts)))
        return [QueueItem.from_dict(d) for d in data.get("items", [])], rsync_opts, rclone_opts


# ----------------------------------------------------------------------
# Settings (~/.mcrsync.yaml)
# ----------------------------------------------------------------------

class SettingsError(Exception):
    pass


class Settings:
    """User settings stored as a small YAML subset (PyYAML usually isn't
    available on NAS Python installs): '#' comments, 'key: value' scalars
    and nested mappings (by indentation). Keys are kept as strings, also
    all-digit ones. Values are integers or strings (plain, 'single' or
    "double" quoted). Anything else is rejected with a line number instead
    of being silently misread."""

    DEFAULT_PATH = os.path.join(os.path.expanduser("~"), ".mcrsync.yaml")
    KEY_RE = re.compile(r"^([A-Za-z0-9_][A-Za-z0-9_]*):(?:\s+(.*))?$")
    INT_RE = re.compile(r"^-?[0-9]+$")
    # Plain scalars that real YAML would read as something other than a
    # string - require quotes so both readings agree.
    AMBIGUOUS_PLAIN = ("~", "null", "true", "false", "yes", "no", "on", "off")

    def __init__(self, path=None):
        self.path = path or Settings.DEFAULT_PATH
        self.data = OrderedDict()
        self.error = ""

    def load(self):
        self.data = OrderedDict()
        self.error = ""
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                text = f.read()
        except FileNotFoundError:
            return
        except (IOError, OSError, ValueError) as e:
            # unreadable (permissions, not UTF-8, ...) - keep it untouched
            self.error = "{}: {}".format(self.path, e)
            return
        try:
            self.data = Settings.parse(text)
        except SettingsError as e:
            self.error = "{}: {}".format(self.path, e)

    def save(self):
        """Returns an error string, or "" on success. A file that failed to
        parse is never overwritten, so hand edits aren't lost."""
        if self.error:
            return "not saving settings - fix this first: " + self.error
        # realpath keeps a symlinked settings file a symlink
        try:
            atomic_write(os.path.realpath(self.path), Settings.dump(self.data))
        except (IOError, OSError) as e:
            return "could not save settings: {}".format(e)
        return ""

    def update(self, change):
        """Read the file again, apply change(data) and save, so another
        mcrsync instance's changes since this one started aren't lost.
        change may return an error text to cancel without saving. Returns
        an error string, or "" on success."""
        self.load()
        if self.error:
            return "not saving settings - fix this first: " + self.error
        problem = change(self.data)
        if problem:
            return problem
        return self.save()

    def section(self, name):
        value = self.data.get(name)
        return value if isinstance(value, dict) else OrderedDict()

    def set_section(self, name, values):
        self.data[name] = values

    @staticmethod
    def parse(text):
        data = OrderedDict()
        # (indent of the mapping's keys, mapping), outermost first
        stack = [(0, data)]
        opened = None   # (indent of its key, mapping) of the last 'key:' without value
        for n, raw in enumerate(text.splitlines(), 1):
            line = Settings._strip_comment(raw, n).rstrip()
            if not line.strip():
                continue
            stripped = line.lstrip(" ")
            if stripped.startswith("\t"):
                raise SettingsError("line {}: tabs are not allowed for indentation".format(n))
            indent = len(line) - len(stripped)
            m = Settings.KEY_RE.match(stripped)
            if not m:
                raise SettingsError("line {}: unsupported syntax, expected 'key: value'".format(n))
            if opened is not None and indent > opened[0]:
                stack.append((indent, opened[1]))
            opened = None
            while stack[-1][0] > indent:
                stack.pop()
            if stack[-1][0] != indent:
                raise SettingsError("line {}: unexpected indentation".format(n))
            current = stack[-1][1]
            key, value = m.group(1), m.group(2)
            if value is None:
                current[key] = OrderedDict()
                opened = (indent, current[key])
            else:
                current[key] = Settings._parse_scalar(value, n)
        return data

    @staticmethod
    def _strip_comment(line, n):
        quote = None
        for i, ch in enumerate(line):
            if quote:
                if ch == "\\" and quote == '"':
                    continue
                if ch == quote and not (quote == '"' and i > 0 and line[i - 1] == "\\"):
                    quote = None
            elif ch in ("'", '"'):
                quote = ch
            elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
                return line[:i]
        return line

    @staticmethod
    def _parse_scalar(value, n):
        if value.startswith('"'):
            if len(value) < 2 or not value.endswith('"'):
                raise SettingsError("line {}: unterminated \"string\"".format(n))
            out = []
            body = value[1:-1]
            i = 0
            while i < len(body):
                ch = body[i]
                if ch == "\\":
                    if i + 1 >= len(body) or body[i + 1] not in '\\"':
                        raise SettingsError("line {}: only \\\\ and \\\" escapes are supported".format(n))
                    out.append(body[i + 1])
                    i += 2
                    continue
                if ch == '"':
                    raise SettingsError("line {}: unescaped \" inside string".format(n))
                out.append(ch)
                i += 1
            return "".join(out)
        if value.startswith("'"):
            if len(value) < 2 or not value.endswith("'"):
                raise SettingsError("line {}: unterminated 'string'".format(n))
            return value[1:-1].replace("''", "'")
        if Settings.INT_RE.match(value):
            return int(value)
        if value.lower() in Settings.AMBIGUOUS_PLAIN or value[0] in "[]{}&*!|>%@`-?:,":
            raise SettingsError("line {}: put quotes around {}".format(n, value))
        return value

    @staticmethod
    def _dump_scalar(value):
        if isinstance(value, int):
            return str(value)
        return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'

    @staticmethod
    def dump(data):
        lines = ["# mcrsync settings - written by mcrsync, safe to edit by hand.",
                 "# Supported: comments, 'key: value' and nested mappings."]
        Settings._dump_mapping(data, "", lines)
        return "\n".join(lines) + "\n"

    @staticmethod
    def _dump_mapping(data, indent, lines):
        for key, value in data.items():
            if isinstance(value, dict):
                lines.append("{}{}:".format(indent, key))
                Settings._dump_mapping(value, indent + "  ", lines)
            else:
                lines.append("{}{}: {}".format(indent, key, Settings._dump_scalar(value)))


# Bookmarks (stored in the settings)
# ----------------------------------------------------------------------

class Bookmark:
    """A saved pane location: connection (None = local) and folder. An
    entry that can't be used keeps its error and raw data, so it can be
    shown and deleted, and is saved back unchanged."""

    def __init__(self, key, name="", spec=None, path="", error=""):
        self.key = key          # key in the settings, the number as text
        self.name = name
        self.spec = spec
        self.path = path
        self.error = error

    def number(self):
        """The bookmark's number, or None if the key isn't one."""
        if self.key.isdigit() and self.key == str(int(self.key)) and int(self.key) > 0:
            return int(self.key)
        return None

    def location(self):
        return (self.spec.key() if self.spec else "local", self.path)

    def display_location(self):
        return self.spec.display_path(self.path) if self.spec else self.path

    @staticmethod
    def from_entry(key, entry):
        bm = Bookmark(key)
        if bm.number() is None:
            bm.error = "{!r} is not a bookmark number (1, 2, ...)".format(key)
            return bm
        if not isinstance(entry, dict):
            bm.error = "not a mapping of name, type and path"
            return bm
        name = entry.get("name")
        path = entry.get("path")
        kind = entry.get("type", "local")
        if not isinstance(name, str) or not name.strip():
            bm.error = "name is missing"
        elif not isinstance(path, str) or not path or "\0" in path or "\n" in path:
            bm.error = "path is missing or invalid"
        elif kind not in ("local", "ssh", "rclone"):
            bm.error = "unknown type {!r}".format(kind)
        else:
            try:
                bm.spec = conn_from_dict(entry) if kind != "local" else None
            except KeyError as e:
                bm.error = "{} is missing".format(e)
            except (TypeError, ValueError) as e:
                bm.error = str(e)
        if not bm.error:
            bm.name = name.strip()
            bm.path = path
        return bm

    @staticmethod
    def to_entry(name, spec, path):
        entry = OrderedDict([("name", name)])
        if spec is None:
            entry["type"] = "local"
        else:
            entry.update(spec.to_dict())
        entry["path"] = path
        return entry

    @staticmethod
    def suggest_name(spec, path):
        folder = posixpath.basename(path.rstrip("/"))
        if spec is None:
            return folder or "/"
        where = spec.host if spec.kind == "ssh" else spec.remote
        return "{} ({})".format(folder, where) if folder else where


class Bookmarks:
    """The bookmark list in the settings ('bookmarks:' with one numbered
    entry each). Every change reads the file again and saves straight
    away, so instances running side by side don't drop each other's."""

    SECTION = "bookmarks"
    QUICK_MAX = 9       # numbers with a quick-jump key

    def __init__(self, settings):
        self.settings = settings
        self.items = []
        self.error = ""

    def load(self):
        self.settings.load()
        self.items = []
        self.error = self.settings.error
        if self.error:
            return
        raw = self.settings.data.get(Bookmarks.SECTION, OrderedDict())
        if not isinstance(raw, dict):
            self.error = "'{}' in {} must contain numbered entries".format(
                Bookmarks.SECTION, self.settings.path)
            return
        self.items = [Bookmark.from_entry(k, v) for k, v in raw.items()]
        # numbered first, in order; broken keys after them
        self.items.sort(key=lambda b: (b.number() is None, b.number() or 0, b.key))

    def get(self, number):
        for bm in self.items:
            if bm.number() == number:
                return bm
        return None

    def find(self, spec, path):
        location = Bookmark(None, spec=spec, path=path).location()
        for bm in self.items:
            if not bm.error and bm.location() == location:
                return bm
        return None

    def _section(self, data):
        raw = data.get(Bookmarks.SECTION)
        if raw is None:
            raw = data[Bookmarks.SECTION] = OrderedDict()
        if not isinstance(raw, dict):
            return None
        return raw

    def add(self, name, spec, path):
        """Returns (number, error); number is None if not added."""
        added = []

        def change(data):
            raw = self._section(data)
            if raw is None:
                return "'{}' in the settings must contain numbered entries".format(
                    Bookmarks.SECTION)
            location = Bookmark(None, spec=spec, path=path).location()
            for key, entry in raw.items():
                bm = Bookmark.from_entry(key, entry)
                if not bm.error and bm.location() == location:
                    return "Already bookmarked as [{}] {}.".format(key, bm.name)
            number = 1
            while str(number) in raw:
                number += 1
            raw[str(number)] = Bookmark.to_entry(name, spec, path)
            added.append(number)
            return ""

        err = self.settings.update(change)
        self.load()
        if err:
            return None, err
        return added[0], ""

    def rename(self, key, name):
        def change(data):
            raw = self._section(data)
            entry = raw.get(key) if raw is not None else None
            if not isinstance(entry, dict):
                return "Bookmark [{}] no longer exists.".format(key)
            entry["name"] = name
            return ""

        err = self.settings.update(change)
        self.load()
        return err

    def delete(self, key):
        def change(data):
            raw = self._section(data)
            if raw is not None:
                raw.pop(key, None)
            return ""

        err = self.settings.update(change)
        self.load()
        return err


# ----------------------------------------------------------------------
# rsync option helpers
# ----------------------------------------------------------------------

class FlagSet:
    """Flag checks on a user-editable option list (rsync's or rclone's).
    Short flags are also found inside combined clusters like '-avu'."""

    SHORT_CLUSTER_RE = re.compile(r"^-[A-Za-z]+$")
    TOOL = ""
    DEFAULTS = []
    # Checkboxes on the F8 screen: (short or None, long, description, risky)
    CHOICES = []

    @classmethod
    def has(cls, opts, short, long_name):
        for o in opts:
            if o == long_name:
                return True
            if short and cls.SHORT_CLUSTER_RE.match(o) and short in o[1:]:
                return True
        return False

    @classmethod
    def without(cls, opts, short, long_name):
        out = []
        for o in opts:
            if o == long_name:
                continue
            if short and cls.SHORT_CLUSTER_RE.match(o) and short in o[1:]:
                o = "-" + o[1:].replace(short, "")
                if o == "-":
                    continue
            out.append(o)
        return out

    @classmethod
    def is_dry_run(cls, opts):
        return cls.has(opts, "n", "--dry-run")

    @classmethod
    def has_update(cls, opts):
        return cls.has(opts, "u", "--update")

    @classmethod
    def toggle(cls, opts, choice):
        short, long_name = choice[0], choice[1]
        if cls.has(opts, short, long_name):
            return cls.without(opts, short, long_name)
        return list(opts) + ["-" + short if short else long_name]

    @classmethod
    def others(cls, opts):
        """The flags no checkbox covers."""
        for choice in cls.CHOICES:
            opts = cls.without(opts, choice[0], choice[1])
        return opts

    # Classification, filled by the subclasses. Long names without '='.
    SAFE_LONG = ()          # known to be harmless for mcrsync's copies
    SAFE_SHORT = ""         # short flags without a value
    RISKY_LONG = ()         # delete, overwrite, write elsewhere, run commands
    RISKY_PREFIXES = ()
    RISKY_SHORT = ""
    # Path-valued flags: harmless with a path inside the target, risky with
    # an absolute one or '..' (written elsewhere).
    PATH_LONG = ()

    @classmethod
    def _parse(cls, o):
        """(long name or None, short letters) of a token. For '-T/tmp' or
        '-aT/x' the letters are 'T' / 'aT': a short flag's value follows it
        directly, so a letter run may include value letters - which only
        makes the checks more cautious."""
        if o.startswith("--"):
            return o.split("=", 1)[0], ""
        m = re.match(r"-([A-Za-z]+)", o)
        return None, (m.group(1) if m else "")

    @classmethod
    def is_risky(cls, o):
        name, letters = cls._parse(o)
        if name is None:
            return any(ch in cls.RISKY_SHORT for ch in letters)
        if name in cls.PATH_LONG:
            value = o.split("=", 1)[1] if "=" in o else ""
            return value.startswith(("/", "~")) or ".." in value.split("/")
        return (name in cls.RISKY_LONG or name.startswith(cls.RISKY_PREFIXES) or
                name.endswith("-command"))

    @classmethod
    def risky(cls, opts):
        """Flags that delete or overwrite data, write outside the target,
        or run other commands."""
        return [o for o in opts if cls.is_risky(o)]

    @classmethod
    def unchecked(cls, opts):
        """Flags mcrsync knows neither as harmless nor as risky. They can
        do anything (e.g. a backend flag can change where data goes), so
        they are pointed out wherever flags are shown."""
        out = []
        for o in opts:
            if cls.is_risky(o):
                continue
            name, letters = cls._parse(o)
            if name is not None:
                known = name in cls.SAFE_LONG or name in cls.PATH_LONG
            else:
                known = (cls.SHORT_CLUSTER_RE.match(o) is not None and
                         all(ch in cls.SAFE_SHORT for ch in letters))
            if not known:
                out.append(o)
        return out

    @classmethod
    def problems(cls, opts):
        """Text saying why opts can't be used at all, or "". A token that
        isn't a flag would be taken as an extra path - e.g. one more
        source to copy."""
        bad = [o for o in opts if not o.startswith("-") or o in ("-", "--")]
        if bad:
            return ("every flag must start with '-' and have its value attached with '=' "
                    "(e.g. --exclude='*.tmp') - not usable: " +
                    " ".join(shlex.quote(o) for o in bad))
        return ""


class RsyncFlags(FlagSet):
    TOOL = TOOL_RSYNC
    DEFAULTS = DEFAULT_RSYNC_OPTS
    CHOICES = [
        ("a", "--archive", "Archive mode (recurse, perms, times, links, ...)", False),
        ("u", "--update", "Skip files that are newer on the target (safety net)", False),
        ("n", "--dry-run", "Show what would happen, change nothing", False),
        ("z", "--compress", "Compress data during transfer", False),
        ("c", "--checksum", "Compare by checksum, not size + time (slow)", False),
        ("H", "--hard-links", "Preserve hard links", False),
        ("A", "--acls", "Preserve ACLs", False),
        ("X", "--xattrs", "Preserve extended attributes", False),
        (None, "--partial", "Keep partially copied files (resume big files)", False),
        (None, "--stats", "Transfer statistics (fills the Verify view)", False),
        (None, "--itemize-changes", "Per-file change list", False),
        (None, "--delete", "Delete target files that aren't in the source", True),
    ]
    SAFE_SHORT = "arlptgoDuznchHAXvWxSiPOJmsE"
    SAFE_LONG = (
        "--archive", "--recursive", "--links", "--perms", "--times", "--group", "--owner",
        "--devices", "--specials", "--update", "--dry-run", "--compress", "--compress-level",
        "--checksum", "--hard-links", "--acls", "--xattrs", "--partial", "--stats",
        "--itemize-changes", "--whole-file", "--no-whole-file", "--no-perms", "--no-owner",
        "--no-group", "--no-times", "--no-devices", "--no-specials", "--verbose",
        "--human-readable", "--progress", "--exclude", "--include", "--filter",
        "--exclude-from", "--include-from", "--cvs-exclude", "--bwlimit", "--timeout",
        "--contimeout", "--size-only", "--ignore-existing", "--existing", "--one-file-system",
        "--sparse", "--numeric-ids", "--chmod", "--modify-window", "--max-size", "--min-size",
        "--prune-empty-dirs", "--no-inc-recursive", "--safe-links", "--munge-links",
        "--omit-dir-times", "--omit-link-times", "--protect-args", "--no-motd", "--fuzzy",
        "--executability", "--info", "--debug", "--checksum-choice", "--mkpath")
    RISKY_LONG = (
        "--del", "--remove-source-files", "--force", "--inplace", "--rsync-path", "--rsh",
        "--ignore-times", "--backup", "--suffix", "--log-file", "--write-batch",
        "--only-write-batch", "--read-batch", "--keep-dirlinks", "--copy-links",
        "--copy-unsafe-links", "--remote-option", "--super")
    RISKY_PREFIXES = ("--delete", "--append")
    # e: remote shell, I: ignore times, K: write through target symlinks,
    # L: follow source symlinks, M: remote options, T: temp dir, b: backups
    RISKY_SHORT = "eIKLMTb"
    PATH_LONG = ("--partial-dir", "--temp-dir", "--backup-dir")


class RcloneFlags(FlagSet):
    """mcrsync runs 'rclone copy'/'copyto', which never delete from the
    target, unless SYNC_FLAG is set; the risky flags are those that
    overwrite, move or delete anyway, weaken the --update check, or run
    commands."""

    TOOL = TOOL_RCLONE
    DEFAULTS = DEFAULT_RCLONE_OPTS
    # mcrsync's own flag, never passed to rclone: folder items run 'rclone
    # sync' instead of 'copy'. Matches RISKY_PREFIXES.
    SYNC_FLAG = "--delete"
    CHOICES = [
        ("u", "--update", "Skip files that are newer on the target (safety net)", False),
        ("n", "--dry-run", "Show what would happen, change nothing", False),
        ("l", "--links", "Upload symlinks as .rclonelink files (restorable)", False),
        ("c", "--checksum", "Compare by checksum, not size + time (slow)", False),
        ("M", "--metadata", "Keep permissions/owners (only some backends)", False),
        (None, "--ignore-existing", "Never touch files that exist on the target", False),
        ("L", "--copy-links", "Follow symlinks, copy what they point to", True),
        ("I", "--ignore-times", "Copy every file again, overwriting the target", True),
        (None, SYNC_FLAG, "Delete target files that aren't in the source (sync)", True),
    ]
    SAFE_SHORT = "unlcM"
    SAFE_LONG = (
        "--update", "--dry-run", "--links", "--checksum", "--metadata", "--ignore-existing",
        "--exclude", "--include", "--filter", "--exclude-from", "--include-from",
        "--filter-from", "--files-from", "--files-from-raw", "--exclude-if-present",
        "--bwlimit", "--transfers", "--checkers", "--retries", "--retries-sleep",
        "--low-level-retries", "--contimeout", "--timeout", "--tpslimit", "--tpslimit-burst",
        "--buffer-size", "--size-only", "--max-age", "--min-age", "--max-size", "--min-size",
        "--max-depth", "--fast-list", "--no-traverse", "--create-empty-src-dirs",
        "--order-by", "--immutable", "--multi-thread-streams", "--multi-thread-cutoff",
        "--use-mmap", "--skip-links", "--one-file-system", "--check-first",
        "--max-transfer", "--cutoff-mode", "--ignore-case")
    # (names ending in -command, which run a program, are always risky)
    RISKY_LONG = ("--ignore-times", "--copy-links", "--no-check-dest", "--backup-dir",
                  "--suffix", "--compare-dest", "--copy-dest", "--track-renames",
                  "--use-server-modtime", "--modify-window", "--config", "--rc", "--rc-addr",
                  "--max-delete", "--inplace", "--metadata-mapper", "--ignore-size")
    RISKY_PREFIXES = ("--delete",)
    RISKY_SHORT = "IL"
    # mcrsync reads rclone's JSON log from stderr: anything that moves,
    # reformats or silences it would break progress and conflict detection.
    OUTPUT_NAMES = ("--log-file", "--log-level", "--log-format", "--use-json-log",
                    "--verbose", "--quiet", "--progress", "--stats", "--stats-one-line",
                    "--stats-log-level", "--stats-unit", "--syslog", "--ask-password",
                    "--interactive")
    OUTPUT_SHORT = "vqPi"

    @classmethod
    def is_sync(cls, opts):
        return cls.has(opts, None, cls.SYNC_FLAG)

    @classmethod
    def problems(cls, opts):
        problem = super(RcloneFlags, cls).problems(opts)
        if problem:
            return problem
        bad = []
        for o in opts:
            name = o.split("=", 1)[0]
            if (name in cls.OUTPUT_NAMES or name.startswith("--stats") or
                    (cls.SHORT_CLUSTER_RE.match(o) and
                     any(ch in cls.OUTPUT_SHORT for ch in o[1:]))):
                bad.append(o)
        if bad:
            return ("mcrsync sets rclone's logging and progress output itself - remove: " +
                    " ".join(shlex.quote(o) for o in bad))
        follow = [o for o in opts if cls._parse(o)[0] == "--copy-links" or "L" in cls._parse(o)[1]]
        if follow and cls.is_sync(opts):
            return ("--delete can't be combined with -L/--copy-links: rclone would follow "
                    "symlinks in the target and delete files outside it - remove: " +
                    " ".join(shlex.quote(o) for o in follow))
        return ""


FLAG_SETS = {TOOL_RSYNC: RsyncFlags, TOOL_RCLONE: RcloneFlags}


# ----------------------------------------------------------------------
# Filesystem backends (local / remote over ssh)
# ----------------------------------------------------------------------

def check_entry_name(name):
    """Error text for a file/folder name the user typed, or "" if it's
    usable. Names starting with '.' are fine; '.' and '..' themselves
    refer to existing folders, not new names."""
    if not name:
        return "The name can't be empty."
    if name in (".", ".."):
        return "'.' and '..' can't be used as names."
    if "/" in name or "\0" in name:
        return "Name cannot contain '/'."
    return ""


class FsError(Exception):
    pass


class ConnectionLost(FsError):
    pass


class Fs:
    """What panes, runners and conflict resolution need from a side of a
    copy. Paths are always absolute in that side's own namespace.

    exists/isdir/list_dir/size/... raise FsError (ConnectionLost when the
    remote can't be reached at all); rename and mkdir never replace an
    existing file or folder."""

    is_remote = False
    kind = "local"      # "local" | "ssh" | "rclone"
    spec = None         # ConnSpec / RcloneSpec, None for local
    pathmod = posixpath

    def label(self):
        raise NotImplementedError

    def display_path(self, path):
        raise NotImplementedError

    def resolve(self, path):
        raise NotImplementedError

    def resolve_folder(self, path):
        """Like resolve(), but raises FsError unless path is a folder."""
        return self.resolve(path)

    def nearest_folder(self, path):
        """(resolved path, True) if path is a folder, else (the nearest
        folder above it, False). Gives up at the starting folder that
        resolve("") is."""
        candidate = path
        while True:
            try:
                return self.resolve_folder(candidate), candidate == path
            except ConnectionLost:
                raise
            except FsError:
                if candidate == "":
                    raise
            stripped = candidate.rstrip("/")
            candidate = self.pathmod.dirname(stripped) if stripped else ""

    def list_dir(self, path):
        """Sorted list of (name, is_dir), dirs first."""
        raise NotImplementedError

    def exists(self, path):
        raise NotImplementedError

    def isdir(self, path):
        raise NotImplementedError

    def rename(self, old, new):
        raise NotImplementedError

    def mkdir(self, path):
        raise NotImplementedError

    def makedirs(self, path):
        raise NotImplementedError

    def size(self, path, is_dir, cancel_event):
        """(bytes, regular files), or None if cancelled or unknown."""
        raise NotImplementedError


class LocalFs(Fs):
    pathmod = os.path

    def label(self):
        return "local"

    def display_path(self, path):
        return path

    def rsync_arg(self, path):
        return path

    def rclone_arg(self, path):
        # always absolute, so never read as an option or 'remote:path'
        return path

    def resolve(self, path):
        return os.path.abspath(os.path.expanduser(path))

    def resolve_folder(self, path):
        resolved = self.resolve(path)
        if not os.path.isdir(resolved):
            raise FsError("folder not found: {}".format(resolved))
        return resolved

    def list_dir(self, path):
        """Return sorted list of (name, is_dir) for a directory, dirs first."""
        try:
            names = os.listdir(path)
        except OSError as e:
            raise FsError(str(e))
        dirs = []
        files = []
        for n in names:
            full = os.path.join(path, n)
            try:
                if os.path.isdir(full):
                    dirs.append(n)
                else:
                    files.append(n)
            except OSError:
                continue
        dirs.sort(key=str.lower)
        files.sort(key=str.lower)
        entries = [(d, True) for d in dirs]
        entries += [(f, False) for f in files]
        return entries

    def exists(self, path):
        return os.path.lexists(path)

    def isdir(self, path):
        return os.path.isdir(path)

    def rename(self, old, new):
        """Never replaces an existing new. os.rename() silently would, so
        non-directories are moved via link+unlink, which fails atomically
        if new exists. Filesystems without hard links (FAT/exFAT) fall back
        to check-then-rename."""
        if os.path.lexists(new):
            raise FsError("'{}' already exists".format(os.path.basename(new)))
        if not os.path.isdir(old) or os.path.islink(old):
            try:
                os.link(old, new, follow_symlinks=False)
            except FileExistsError:
                raise FsError("'{}' already exists".format(os.path.basename(new)))
            except OSError:
                pass
            else:
                try:
                    os.unlink(old)
                except OSError as e:
                    raise FsError("copied to the new name but could not remove the old "
                                  "name: {}".format(e))
                return
        try:
            os.rename(old, new)
        except OSError as e:
            raise FsError(str(e))

    def mkdir(self, path):
        try:
            os.mkdir(path)
        except OSError as e:
            raise FsError(str(e))

    def makedirs(self, path):
        try:
            os.makedirs(path, exist_ok=True)
        except OSError as e:
            raise FsError(str(e))

    def size(self, path, is_dir, cancel_event):
        """Total (bytes, regular files) of a file or directory tree - the
        file data rsync -a copies, so symlinks count as links, not as the
        size of what they point to. Checks cancel_event periodically (every
        200 files) so a background scan of a huge directory can be aborted
        promptly if the queue item is deleted. Returns None if cancelled or
        if the size can't be determined at all."""
        if is_dir:
            if not os.path.isdir(path):
                return None
            total = 0
            files_n = 0
            count = 0
            for root, dirs, files in os.walk(path):
                if cancel_event.is_set():
                    return None
                for fname in files:
                    count += 1
                    if count % 200 == 0 and cancel_event.is_set():
                        return None
                    try:
                        st = os.lstat(os.path.join(root, fname))
                    except OSError:
                        continue
                    if stat.S_ISREG(st.st_mode):
                        total += st.st_size
                        files_n += 1
            if cancel_event.is_set():
                return None
            return total, files_n
        else:
            try:
                st = os.lstat(path)
            except OSError:
                return None
            return (st.st_size, 1) if stat.S_ISREG(st.st_mode) else (0, 0)


LOCAL_FS = LocalFs()


class SshConnection:
    """One ssh ControlMaster connection. The master is opened in the plain
    terminal (curses suspended) so ssh can ask for host-key confirmation or
    a password itself. Every other ssh/rsync call multiplexes over its
    socket with BatchMode, so it fails instead of prompting while curses
    owns the screen."""

    def __init__(self, spec, sock_path, log_path):
        self.spec = spec
        self.sock_path = sock_path
        self.log_path = log_path

    def dest(self):
        return "{}@{}".format(self.spec.user, self.spec.host)

    # Remote commands normally finish in well under a second; this only
    # stops a hung host from freezing the UI forever.
    RUN_TIMEOUT = 120

    def _common_opts(self):
        # ssh expands %-tokens in ControlPath (e.g. a TMPDIR containing %)
        opts = ["-p", str(self.spec.port),
                "-o", "ControlPath=" + self.sock_path.replace("%", "%%"),
                "-o", "ServerAliveInterval=30",
                "-o", "ServerAliveCountMax=4"]
        if self.spec.keyfile:
            opts += ["-i", os.path.expanduser(self.spec.keyfile)]
        return opts

    def client_opts(self):
        return self._common_opts() + ["-o", "ControlMaster=no",
                                      "-o", "BatchMode=yes",
                                      "-o", "ConnectTimeout=15"]

    def rsync_e(self):
        """Value for rsync's -e option."""
        return "ssh " + " ".join(shlex.quote(o) for o in self.client_opts())

    def connect(self):
        """Open the master interactively. Returns (ok, error_text)."""
        if self.check():
            return True, ""
        cmd = ["ssh"] + self._common_opts() + [
            "-o", "ControlMaster=yes", "-o", "ControlPersist=yes",
            "-f", "-N", "--", self.dest()]
        # A socket left by a master that died uncleanly would make ssh skip
        # becoming the master ("ControlSocket already exists").
        if os.path.lexists(self.sock_path) and not self.check():
            try:
                os.unlink(self.sock_path)
            except OSError:
                pass
        # ssh prompts on /dev/tty, so stderr can go to a log. That keeps the
        # backgrounded master from ever writing over the curses screen.
        try:
            with open(self.log_path, "w") as log:
                rc = subprocess.call(cmd, stderr=log)
        except KeyboardInterrupt:
            return False, "cancelled"
        except OSError as e:
            return False, "could not start ssh: {}".format(e)
        if rc == 0 and self.check():
            return True, ""
        try:
            with open(self.log_path, "r") as log:
                err = log.read().strip()
        except (IOError, OSError):
            err = ""
        return False, err or "ssh exited with code {}".format(rc)

    def check(self):
        cmd = ["ssh"] + self._common_opts() + ["-O", "check", "--", self.dest()]
        try:
            return subprocess.call(cmd, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, timeout=10) == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def close(self):
        cmd = ["ssh"] + self._common_opts() + ["-O", "exit", "--", self.dest()]
        try:
            subprocess.call(cmd, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            pass

    def run(self, script, args):
        """Run a POSIX sh script remotely with args as "$1"...
        Returns (rc, stdout_bytes, stderr_text).

        The remote login shell (bash, csh, fish, ...) only ever parses the
        fixed command 'sh -s'; script and args travel on stdin, each arg
        byte-for-byte as printf octal escapes. So no path can be re-read
        differently by an unusual login shell."""
        prologue = []
        for i, a in enumerate(args, 1):
            octal = "".join("\\{:03o}".format(b) for b in os.fsencode(a))
            # the trailing x protects trailing newlines from $(...)
            prologue.append("a{0}=$(printf '{1}'; printf x); a{0}=${{a{0}%x}}".format(i, octal))
        prologue.append("set -- " + " ".join('"$a{}"'.format(i) for i in range(1, len(args) + 1)))
        stdin_data = ("\n".join(prologue) + "\n" + script + "\n").encode("ascii")
        cmd = ["ssh"] + self.client_opts() + ["--", self.dest(), "sh -s"]
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE)
        except OSError as e:
            return 255, b"", "could not start ssh: {}".format(e)
        try:
            out, err = proc.communicate(stdin_data, timeout=SshConnection.RUN_TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            return 124, b"", "remote command timed out after {}s".format(
                SshConnection.RUN_TIMEOUT)
        return proc.returncode, out, os.fsdecode(err)


class RemoteFs(Fs):
    """Filesystem operations on a remote host, using only POSIX sh, so it
    works on GNU/Linux as well as busybox (Synology) hosts."""

    is_remote = True
    kind = "ssh"

    # Output that is parsed starts after MARKER, so text printed by the
    # remote user's shell startup files can't be mistaken for data.
    MARKER = b"\0MCRSYNC\0"

    # One "<d|f><name>/" record per entry - '/' can't occur in a name.
    LIST_SCRIPT = (
        'cd -- "$1" || exit 2\n'
        '[ -r . ] || { echo "Permission denied" >&2; exit 2; }\n'
        'printf "\\000MCRSYNC\\000"\n'
        'for f in * .[!.]* ..?*; do\n'
        '  [ -e "$f" ] || [ -L "$f" ] || continue\n'
        '  if [ -d "$f" ]; then t=d; else t=f; fi\n'
        '  printf "%s%s/" "$t" "$f"\n'
        'done\n')
    # $PWD rather than $(pwd): command substitution drops trailing newlines
    RESOLVE_SCRIPT = (
        'if [ -n "$1" ]; then cd -- "$1" || exit 2; fi\n'
        'printf "\\000MCRSYNC\\000%s" "$PWD"\n')
    EXISTS_SCRIPT = '[ -e "$1" ] || [ -L "$1" ]'
    ISDIR_SCRIPT = '[ -d "$1" ]'
    # The check is repeated by mv -n (busybox and GNU), which refuses to
    # replace a file that appears in between; the last test catches mv -n
    # silently doing nothing.
    RENAME_SCRIPT = (
        'if [ -e "$2" ] || [ -L "$2" ]; then\n'
        '  echo "${2##*/} already exists" >&2; exit 3\n'
        'fi\n'
        'mv -n -- "$1" "$2" || exit 1\n'
        'if [ -e "$1" ] || [ -L "$1" ]; then\n'
        '  echo "${2##*/} already exists - nothing was renamed" >&2; exit 3\n'
        'fi\n')
    MKDIR_SCRIPT = 'mkdir -- "$1"'
    MAKEDIRS_SCRIPT = 'mkdir -p -- "$1"'

    def __init__(self, conn):
        self.conn = conn
        self.spec = conn.spec

    def label(self):
        return "ssh " + self.spec.key()

    def display_path(self, path):
        return self.spec.display_path(path)

    def rsync_arg(self, path):
        return "{}@{}:{}".format(self.spec.user, self.spec.host, path)

    def _run(self, script, *args):
        rc, out, err = self.conn.run(script, args)
        if rc == 255 and not self.conn.check():
            raise ConnectionLost("connection to {} is closed".format(self.spec.key()))
        return rc, out, err

    def _run_ok(self, script, *args):
        rc, out, err = self._run(script, *args)
        if rc != 0:
            raise FsError(err.strip() or "remote command failed (exit {})".format(rc))
        return out

    def _run_data(self, script, *args):
        out = self._run_ok(script, *args)
        pos = out.find(RemoteFs.MARKER)
        if pos < 0:
            raise FsError("unexpected output from the remote shell")
        return out[pos + len(RemoteFs.MARKER):]

    def resolve(self, path):
        """Absolute remote path. Blank or '~' is the home directory; since
        ssh starts there, '~/x' is just the relative path 'x'."""
        if path == "~":
            path = ""
        elif path.startswith("~/"):
            path = path[2:]
        resolved = os.fsdecode(self._run_data(RemoteFs.RESOLVE_SCRIPT, path))
        if not resolved.startswith("/"):
            raise FsError("remote shell reported no absolute path for {!r}".format(path))
        return resolved

    def list_dir(self, path):
        out = self._run_data(RemoteFs.LIST_SCRIPT, path)
        dirs = []
        files = []
        seen = set()
        for rec in out.split(b"/"):
            name = os.fsdecode(rec[1:])
            # A remote sh with bash's dotglob would also match dot files
            # with '*', listing them twice.
            if not rec or name in seen or name in (".", ".."):
                continue
            seen.add(name)
            if rec[:1] == b"d":
                dirs.append(name)
            else:
                files.append(name)
        dirs.sort(key=str.lower)
        files.sort(key=str.lower)
        return [(d, True) for d in dirs] + [(f, False) for f in files]

    def _test(self, script, path):
        rc, out, err = self._run(script, path)
        if rc not in (0, 1):
            raise FsError(err.strip() or "remote command failed (exit {})".format(rc))
        return rc == 0

    def exists(self, path):
        return self._test(RemoteFs.EXISTS_SCRIPT, path)

    def isdir(self, path):
        return self._test(RemoteFs.ISDIR_SCRIPT, path)

    def rename(self, old, new):
        self._run_ok(RemoteFs.RENAME_SCRIPT, old, new)

    def mkdir(self, path):
        self._run_ok(RemoteFs.MKDIR_SCRIPT, path)

    def makedirs(self, path):
        self._run_ok(RemoteFs.MAKEDIRS_SCRIPT, path)

    def size(self, path, is_dir, cancel_event):
        """(bytes, regular files) via an rsync dry run into an empty local
        dir - works on any host that has rsync. Returns None if cancelled
        or on error."""
        tmp = tempfile.mkdtemp(prefix="mcrsync-size-")
        try:
            src = path.rstrip("/") + "/" if is_dir else path
            cmd = ["rsync", "-r", "-l", "-n", "--stats", "-s", "-e", self.conn.rsync_e(),
                   "--", self.rsync_arg(src), tmp + "/"]
            # Output goes to a file, not a pipe: we only poll the process, so
            # a pipe could fill up (e.g. warnings per file) and hang rsync.
            with tempfile.TemporaryFile() as out:
                try:
                    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                            stdout=out, stderr=subprocess.DEVNULL)
                except OSError:
                    return None
                while proc.poll() is None:
                    if cancel_event.wait(0.2):
                        proc.kill()
                        proc.wait()
                        return None
                out.seek(0)
                text = os.fsdecode(out.read())
            m = STATS_RE["total_size"].search(text)
            n = STATS_RE["file_count"].search(text)
            if proc.returncode != 0 or not m or not n:
                return None
            # "Number of files: 8 (reg: 5, dir: 2, link: 1)" - reg is absent if 0
            reg = re.search(r"reg: ([\d,]+)", n.group(1) or "")
            return (int(m.group(1).replace(",", "")),
                    int(reg.group(1).replace(",", "")) if reg else 0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class RcloneUnavailable(ConnectionLost):
    """rclone gave up with an exit code that is worth retrying later
    (network trouble, an expired login, a provider's daily limit)."""


class RcloneCli:
    """Runs the rclone binary. Never prompts: stdin is closed and
    --ask-password=false makes an encrypted config fail instead of waiting
    for a password (it works if RCLONE_CONFIG_PASS or
    RCLONE_PASSWORD_COMMAND is set in mcrsync's environment)."""

    BINARY = "rclone"
    MIN_VERSION = (1, 60)
    RUN_TIMEOUT = 120
    VERSION_RE = re.compile(r"^rclone v(\d+)\.(\d+)")
    # plain (non-JSON) log lines: "2026/11/01 15:44:32 ERROR : x: msg"; fatal
    # errors ("Failed to copy: ...") have no level
    LOG_PREFIX_RE = re.compile(r"^\d{4}/\d\d/\d\d \d\d:\d\d:\d\d (?:([A-Z]+)\s*: )?")

    @staticmethod
    def plain_line(line):
        """(is_error, text) of a plain log line."""
        m = RcloneCli.LOG_PREFIX_RE.match(line)
        level = m.group(1) if m else None
        text = RcloneCli.LOG_PREFIX_RE.sub("", line)
        return level in (None, "ERROR", "CRITICAL"), text
    # Exit codes that pause a run instead of failing the item.
    PAUSE_CODES = {
        5: "temporary error (network or provider trouble)",
        7: "fatal error (e.g. login expired or account problem) - fix it, e.g. with "
           "'rclone config reconnect {}:', then press P",
        8: "transfer limit reached (e.g. a provider's daily upload limit)",
    }
    # 100 years: what rclone reports for "can't store modification times"
    NO_MODTIME_PRECISION = 3153600000000000000
    # rclone exits with the generic code 1 for many network failures (e.g.
    # connection refused), so those are recognised by their text.
    NETWORK_RE = re.compile(r"connection refused|no such host|i/o timeout|deadline exceeded|"
                            r"network is unreachable|no route to host|connection reset|"
                            r"name resolution|dial tcp|TLS handshake timeout", re.I)

    @staticmethod
    def is_network_error(text):
        return bool(RcloneCli.NETWORK_RE.search(text))

    @staticmethod
    def cmd(command, args):
        return [RcloneCli.BINARY, command, "--ask-password=false"] + list(args)

    @staticmethod
    def spawn(command, args, stdout, stderr):
        try:
            return subprocess.Popen(RcloneCli.cmd(command, args), stdin=subprocess.DEVNULL,
                                    stdout=stdout, stderr=stderr)
        except OSError as e:
            raise FsError("could not start rclone ({}) - is it installed and in PATH?".format(e))

    @staticmethod
    def run(command, args, timeout=RUN_TIMEOUT):
        """Returns (rc, stdout_bytes, stderr_text)."""
        proc = RcloneCli.spawn(command, args, subprocess.PIPE, subprocess.PIPE)
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            return 124, b"", "rclone {} timed out after {}s".format(command, timeout)
        return proc.returncode, out, os.fsdecode(err)

    @staticmethod
    def error_text(err, rc):
        """The most useful line of rclone's stderr."""
        lines = []
        for line in err.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict) and entry.get("level") in ("error", "critical"):
                    lines.append(str(entry.get("msg", "")).strip())
            elif line:
                is_error, text = RcloneCli.plain_line(line)
                if is_error:
                    lines.append(text)
        lines = [l for l in lines if l]
        return lines[-1] if lines else "rclone exited with code {}".format(rc)

    @staticmethod
    def version():
        """(major, minor), or raises FsError."""
        rc, out, err = RcloneCli.run("version", [])
        m = RcloneCli.VERSION_RE.match(os.fsdecode(out))
        if rc != 0 or not m:
            raise FsError("could not run 'rclone version': " + RcloneCli.error_text(err, rc))
        return int(m.group(1)), int(m.group(2))

    @staticmethod
    def list_remotes():
        """Remote names from the user's rclone config, or raises FsError."""
        rc, out, err = RcloneCli.run("listremotes", [])
        if rc != 0:
            text = RcloneCli.error_text(err, rc)
            if "decrypt" in text or "password" in text.lower():
                text = ("your rclone config is encrypted. Set RCLONE_PASSWORD_COMMAND (or "
                        "RCLONE_CONFIG_PASS) before starting mcrsync - mcrsync never asks for "
                        "that password itself. rclone said: " + text)
            raise FsError(text)
        # one "name:" per line; names may contain spaces
        return [l[:-1] for l in os.fsdecode(out).splitlines() if l.endswith(":")]

    @staticmethod
    def parse_json(out, what):
        try:
            return json.loads(os.fsdecode(out))
        except ValueError:
            raise FsError("unexpected output from rclone " + what)


class RcloneRemote:
    """Session state of one rclone remote: whether it was checked and is
    usable ("ready"), what its backend can do, and folders mcrsync created
    that bucket storage can't show yet (they only exist once a file is put
    in them)."""

    def __init__(self, spec):
        self.spec = spec
        self.ready = False
        self.problem = ""
        self.modtime_ok = True
        self.bucket_based = False
        self.is_local = False
        self.backend = ""   # backend name rclone resolves the remote to
        self.root = ""      # its root path (e.g. what an alias points to)
        self.made_dirs = set()

    def verify(self):
        """Check that the remote exists and read its features. Returns an
        error text, or "" when ready."""
        self.ready = False
        try:
            names = RcloneCli.list_remotes()
        except FsError as e:
            return self._failed(str(e))
        if self.spec.remote not in names:
            return self._failed("there is no remote '{}' in your rclone config (see "
                                "'rclone listremotes')".format(self.spec.remote))
        rc, out, err = RcloneCli.run("backend", ["features", "--", self.spec.arg("/")])
        if rc != 0:
            return self._failed(RcloneCli.error_text(err, rc))
        try:
            info = RcloneCli.parse_json(out, "backend features")
            features = info.get("Features") or {}
            self.modtime_ok = int(info.get("Precision", 0)) < RcloneCli.NO_MODTIME_PRECISION
            self.bucket_based = bool(features.get("BucketBased"))
            self.is_local = bool(features.get("IsLocal"))
            self.backend = str(info.get("Name", ""))
            self.root = str(info.get("Root", ""))
        except (FsError, AttributeError, TypeError, ValueError) as e:
            return self._failed("could not read the remote's features: {}".format(e))
        self.ready = True
        self.problem = ""
        return ""

    def _failed(self, text):
        self.problem = text
        return text

    def mark_unavailable(self, text):
        self.ready = False
        self.problem = text

    def storage_path(self, path):
        """(storage id, absolute path) of path, to tell whether two paths
        on different remotes can be the same place: aliases resolve to the
        remote (or local folder) they point to. A local backend maps to
        the local filesystem."""
        full = posixpath.normpath("/" + "/".join(
            p for p in (self.root.strip("/"), path.strip("/")) if p))
        if self.is_local:
            return "local", os.path.realpath(full)
        return "rclone:" + self.backend, full


class RcloneFs(Fs):
    is_remote = True
    kind = "rclone"

    def __init__(self, remote):
        self.remote = remote
        self.spec = remote.spec

    def label(self):
        return "rclone " + self.spec.remote

    def display_path(self, path):
        return self.spec.display_path(path)

    def rclone_arg(self, path):
        return self.spec.arg(path)

    def _run(self, command, args, timeout=RcloneCli.RUN_TIMEOUT):
        rc, out, err = RcloneCli.run(command, args, timeout)
        if rc == 0:
            return rc, out, err
        text = RcloneCli.error_text(err, rc)
        if rc in RcloneCli.PAUSE_CODES:
            text = "{}: {} - rclone said: {}".format(
                self.spec.key(), RcloneCli.PAUSE_CODES[rc].format(self.spec.remote), text)
        elif RcloneCli.is_network_error(text):
            text = "{} can't be reached: {}".format(self.spec.key(), text)
        else:
            return rc, out, err
        self.remote.mark_unavailable(text)
        raise RcloneUnavailable(text)

    def _run_ok(self, command, args, timeout=RcloneCli.RUN_TIMEOUT):
        rc, out, err = self._run(command, args, timeout)
        if rc != 0:
            raise FsError(RcloneCli.error_text(err, rc))
        return out

    @staticmethod
    def normalize(path):
        path = (path or "").strip()
        if path in ("", "~"):
            return "/"
        # leading "//" survives posixpath.normpath
        return posixpath.normpath("/" + path.lstrip("/"))

    def resolve(self, path):
        path = RcloneFs.normalize(path)
        if not self.isdir(path):
            raise FsError("folder not found: {}".format(self.display_path(path)))
        return path

    def list_dir(self, path):
        out = self._run_ok("lsjson", ["--no-mimetype", "--no-modtime", "--",
                                      self.rclone_arg(path)])
        entries = RcloneCli.parse_json(out, "lsjson")
        if not isinstance(entries, list):
            raise FsError("unexpected output from rclone lsjson")
        dirs = set()
        files = set()
        for e in entries:
            name = e.get("Name") if isinstance(e, dict) else None
            if not isinstance(name, str) or not name or "/" in name or name in (".", ".."):
                continue
            (dirs if e.get("IsDir") else files).add(name)
        for made in self.remote.made_dirs:
            parent, name = posixpath.split(made)
            if parent == path and name not in files:
                dirs.add(name)
        return ([(d, True) for d in sorted(dirs, key=str.lower)] +
                [(f, False) for f in sorted(files, key=str.lower)])

    def stat(self, path):
        """"dir", "file" or None (missing)."""
        if path == "/" or path in self.remote.made_dirs:
            return "dir"
        rc, out, err = self._run("lsjson", ["--stat", "--no-mimetype", "--no-modtime", "--",
                                            self.rclone_arg(path)])
        if rc != 0:
            text = RcloneCli.error_text(err, rc)
            if rc in (3, 4) or "not found" in text.lower():
                return None
            raise FsError(text)
        info = RcloneCli.parse_json(out, "lsjson --stat")
        if not isinstance(info, dict):
            raise FsError("unexpected output from rclone lsjson --stat")
        if not info.get("IsDir"):
            return "file"
        # Bucket storage reports any missing path as a folder - the
        # parent's listing is what tells.
        parent, name = posixpath.split(path)
        return "dir" if (name, True) in self.list_dir(parent) else None

    def exists(self, path):
        return self.stat(path) is not None

    def isdir(self, path):
        return self.stat(path) == "dir"

    def rename(self, old, new):
        """--ignore-existing makes rclone itself skip the move if new
        appears after our check (object storage has no atomic no-clobber
        rename); the checks afterwards catch a move that didn't happen."""
        if self.exists(new):
            raise FsError("'{}' already exists".format(posixpath.basename(new)))
        kind = self.stat(old)
        if kind is None:
            raise FsError("'{}' no longer exists".format(posixpath.basename(old)))
        # A folder's contents go into new (which doesn't exist); on object
        # storage that's file by file - so no time limit.
        if kind == "dir":
            command, args = "move", ["--ignore-existing", "--delete-empty-src-dirs"]
        else:
            command, args = "moveto", ["--ignore-existing"]
        self._run_ok(command, args + ["--", self.rclone_arg(old), self.rclone_arg(new)],
                     timeout=None)
        if kind == "dir":
            self.remote.made_dirs = set(
                new + d[len(old):] if d == old or d.startswith(old + "/") else d
                for d in self.remote.made_dirs)
            # rmdir only ever removes an empty folder
            self._run("rmdir", ["--", self.rclone_arg(old)])
        if not self.exists(new):
            raise FsError("'{}' already exists - nothing was renamed".format(
                posixpath.basename(new)))
        if self.exists(old):
            raise FsError("only partly renamed - some files are still under '{}'".format(
                posixpath.basename(old)))

    def mkdir(self, path):
        if self.exists(path):
            raise FsError("'{}' already exists".format(posixpath.basename(path)))
        self.makedirs(path)

    def makedirs(self, path):
        self._run_ok("mkdir", ["--", self.rclone_arg(path)])
        self.remote.made_dirs.add(path)

    def size(self, path, is_dir, cancel_event):
        with tempfile.TemporaryFile() as out:
            proc = RcloneCli.spawn("size", ["--json", "--", self.rclone_arg(path)],
                                   out, subprocess.DEVNULL)
            while proc.poll() is None:
                if cancel_event.wait(0.2):
                    proc.kill()
                    proc.wait()
                    return None
            if proc.returncode != 0:
                return None
            out.seek(0)
            try:
                info = json.loads(os.fsdecode(out.read()))
                return int(info["bytes"]), int(info["count"])
            except (ValueError, KeyError, TypeError):
                return None


class ConnectionRegistry:
    """All remote connections of this session: SSH connections keyed by
    user@host:port, rclone remotes by rclone:name. Queue items may refer
    to ones that aren't open (e.g. after loading a queue); those are
    opened on demand via open_interactive()."""

    def __init__(self):
        self.conns = {}
        self.rclone = {}
        self.rclone_version_ok = False
        self.busy = NOT_BUSY   # the UI's BusyIndicator, for the rclone checks
        self.sock_dir = None
        self.lock = threading.Lock()
        # Never reuse a socket name: a mux client talks to whatever master
        # owns the socket, regardless of the host it was asked for.
        self.next_id = 0

    def get(self, spec):
        with self.lock:
            conn = self.conns.get(spec.key())
            if conn is None:
                if self.sock_dir is None:
                    # Short path: unix socket paths are limited to ~100 chars.
                    self.sock_dir = tempfile.mkdtemp(prefix="mcrsync-")
                n = self.next_id
                self.next_id += 1
                conn = SshConnection(spec,
                                     os.path.join(self.sock_dir, "{}.sock".format(n)),
                                     os.path.join(self.sock_dir, "{}.log".format(n)))
                self.conns[spec.key()] = conn
            elif spec.keyfile != conn.spec.keyfile:
                conn.spec = spec
            return conn

    def rclone_remote(self, spec):
        with self.lock:
            remote = self.rclone.get(spec.key())
            if remote is None:
                remote = RcloneRemote(spec)
                self.rclone[spec.key()] = remote
            return remote

    def fs_for(self, spec):
        if spec is None:
            return LOCAL_FS
        if spec.kind == "rclone":
            return RcloneFs(self.rclone_remote(spec))
        return RemoteFs(self.get(spec))

    def is_open(self, spec):
        if spec is None:
            return True
        if spec.kind == "rclone":
            return self.rclone_remote(spec).ready
        conn = self.conns.get(spec.key())
        return conn is not None and conn.check()

    def problem(self, spec):
        """Why spec isn't usable, as far as known."""
        if spec.kind == "rclone":
            return self.rclone_remote(spec).problem
        return ""

    def describe(self, specs):
        """'key (reason), key' for messages about specs that failed."""
        parts = []
        for spec in specs:
            why = self.problem(spec)
            parts.append("{} ({})".format(spec.key(), why) if why else spec.key())
        return "; ".join(parts)

    def open_rclone(self, spec):
        """Check rclone itself (once) and the remote. Returns error text or
        ""; needs no terminal - rclone never prompts here."""
        remote = self.rclone_remote(spec)
        if not self.rclone_version_ok:
            try:
                version = RcloneCli.version()
            except FsError as e:
                remote.mark_unavailable(str(e))
                return str(e)
            if version < RcloneCli.MIN_VERSION:
                text = "rclone {}.{} is too old - mcrsync needs {}.{} or newer".format(
                    version[0], version[1], *RcloneCli.MIN_VERSION)
                remote.mark_unavailable(text)
                return text
            self.rclone_version_ok = True
        return remote.verify()

    def dead_specs(self, specs):
        seen = set()
        dead = []
        for spec in specs:
            if spec is None or spec.key() in seen:
                continue
            seen.add(spec.key())
            if not self.is_open(spec):
                dead.append(spec)
        return dead

    def open_interactive(self, stdscr, specs):
        """Open each connection: rclone remotes are just checked, SSH ones
        are opened in the plain terminal (curses suspended) so ssh can
        prompt. Returns the specs that failed; problem() says why for
        rclone ones."""
        with self.busy:
            failed = [s for s in specs if s.kind == "rclone" and self.open_rclone(s)]
        specs = [s for s in specs if s.kind != "rclone"]
        if not specs:
            return failed
        curses.endwin()
        try:
            # The normal screen still shows output from earlier logins.
            clear = curses.tigetstr("clear")
            if clear:
                sys.stdout.write(clear.decode("ascii", "replace"))
            print("mcrsync - SSH login (the file manager comes back afterwards)")
            for spec in specs:
                conn = self.get(spec)
                print("")
                print("mcrsync: connecting to {}{} ...".format(
                    spec.key(), " with key " + spec.keyfile if spec.keyfile else ""))
                print("(ssh asks below for host-key confirmation and/or the password "
                      "if needed. If it asks for the password again, the password was "
                      "wrong. Ctrl-C cancels.)")
                sys.stdout.flush()
                ok, err = conn.connect()
                if ok:
                    print("Connected.")
                else:
                    print("")
                    print("Connection FAILED: {}".format(err))
                    failed.append(spec)
            if failed:
                print("")
                try:
                    input("Press Enter to return to mcrsync...")
                except (EOFError, KeyboardInterrupt):
                    pass
        finally:
            stdscr.clear()
            stdscr.refresh()
        return failed

    def close(self, spec):
        if spec.kind == "rclone":
            # nothing to log out of; keep the remote's state for queue items
            return
        conn = self.conns.pop(spec.key(), None)
        if conn is not None:
            conn.close()

    def close_all(self):
        for key in list(self.conns.keys()):
            self.conns.pop(key).close()
        if self.sock_dir:
            shutil.rmtree(self.sock_dir, ignore_errors=True)
            self.sock_dir = None


# ----------------------------------------------------------------------
# rsync execution
# ----------------------------------------------------------------------

STATS_RE = {
    "files_transferred": re.compile(r"Number of (?:regular )?files transferred:\s*([\d,]+)"),
    "total_size": re.compile(r"Total file size:\s*([\d,]+)"),
    "file_count": re.compile(r"Number of files:\s*[\d,]+(?: \(([^)]*)\))?"),
}


class Progress:
    """Live state of the item being copied. Written only by the run thread
    and read by the UI (single attribute updates, so no lock is needed).

    'Reviewed' counts every regular file rsync has looked at, copied or
    already up to date - rsync prints one line per file with its size
    (--out-format + --info=name2). Against the item's size estimate that
    gives a percentage that really ends at 100%; rsync's own progress
    percentage only counts transferred data. Files being transferred count
    with the bytes actually sent so far (from progress2), not with their
    full size the moment they start - else one big file would jump to
    100% at once. A dry run sends nothing, so it counts the listed sizes."""

    PREPARING = "preparing"
    SCANNING = "scanning"
    COPYING = "copying"
    DELETING = "deleting"
    FINISHING = "finishing"
    DONE = "done"

    # --info=progress2, e.g. "  1,100,000  73%  402.09kB/s  0:00:02 (xfr#3, to-chk=0/6)";
    # ir-chk instead of to-chk while incremental recursion is still scanning
    LINE_RE = re.compile(r"^\s*([\d,]+)\s+\d+%\s+([\d.]+)([kMGT]?B)/s\s+[\d:]+"
                         r"(?:\s+\(xfr#(\d+), (ir|to)-chk=(\d+)/(\d+)\))?\s*$")
    UNITS = {"B": 1, "kB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4}

    def __init__(self, dry_run=False, total_bytes=None, total_files=None):
        self.dry_run = dry_run
        self.phase = Progress.PREPARING
        self.started = time.time()
        # from the item's size estimate; None if not known (yet)
        self.total_bytes = total_bytes
        self.total_files = total_files
        self.unchanged_bytes = 0    # files already up to date (or hard-linked)
        self.unchanged_files = 0
        self.listed_bytes = 0       # files rsync listed for transfer
        self.listed_files = 0
        self.files = 0              # files transferred (progress2)
        self.bytes = 0              # file data transferred (progress2)
        self.speed = None           # bytes/s
        self.current = ""
        self.deleted = 0
        self.conflicts = 0
        self.conflict_bytes = 0     # rclone only - rsync doesn't report it

    @property
    def copied_files(self):
        return self.listed_files if self.dry_run else self.files

    @property
    def copied_bytes(self):
        return self.listed_bytes if self.dry_run else self.bytes

    @property
    def reviewed_files(self):
        return self.unchanged_files + self.copied_files + self.conflicts

    @property
    def reviewed_bytes(self):
        return self.unchanged_bytes + self.copied_bytes + self.conflict_bytes

    def _seen_file(self):
        if self.phase in (Progress.SCANNING, Progress.FINISHING):
            self.phase = Progress.COPYING

    def feed_conflict(self, size=0):
        """A file skipped because the target is newer. rsync doesn't say
        how big it is."""
        self.conflicts += 1
        self.conflict_bytes += size
        self._seen_file()

    def feed_unchanged(self, size):
        self.unchanged_files += 1
        self.unchanged_bytes += size
        self._seen_file()

    def feed_listed(self, size, name):
        """A file a dry run would copy."""
        self.listed_files += 1
        self.listed_bytes += size
        self.current = name
        self._seen_file()

    def feed_deleted(self, name):
        """A file or folder deleted from the target (or that a dry run
        would delete)."""
        self.phase = Progress.DELETING
        self.deleted += 1
        self.current = name

    def feed_rclone_stats(self, stats):
        """Apply the stats object of rclone's JSON log (every second)."""
        try:
            self.bytes = int(stats.get("bytes") or 0)
            self.files = int(stats.get("transfers") or 0)
            speed = stats.get("speed")
            self.speed = float(speed) if speed is not None else None
        except (TypeError, ValueError):
            return
        transferring = stats.get("transferring")
        if isinstance(transferring, list) and transferring:
            first = transferring[0]
            if isinstance(first, dict) and isinstance(first.get("name"), str):
                self.current = first["name"]
                self._seen_file()

    def feed_progress(self, line):
        """Apply a progress2 line. Returns False if line isn't one."""
        m = Progress.LINE_RE.match(line)
        if not m:
            return False
        self.bytes = int(m.group(1).replace(",", ""))
        self.speed = float(m.group(2)) * Progress.UNITS[m.group(3)]
        if m.group(4):
            self.files = int(m.group(4))
            all_checked = m.group(5) == "to" and m.group(6) == "0"
            if all_checked and self.phase != Progress.DELETING:
                self.phase = Progress.FINISHING
        if self.phase == Progress.SCANNING:
            self.phase = Progress.COPYING
        return True

    def feed_item(self, kind, ftype, size, name):
        """One rsync output line: kind is the first itemize character
        ('<', '>', 'c', 'h', '.' = unchanged, '*' = deleting), ftype the
        second ('f' = regular file, 'd', 'L', ...)."""
        if kind == "*":
            self.feed_deleted(name)
            return
        if ftype == "f":
            if kind in "<>":
                self.listed_files += 1
                self.listed_bytes += size
            elif kind in ".h":
                self.unchanged_files += 1
                self.unchanged_bytes += size
        if kind in "<>ch":
            self.current = name
        self._seen_file()

    @staticmethod
    def short_size(n):
        return format_size_si(n).replace(" ", "")

    def describe(self):
        """One-line summary for the UI."""
        parts = [("DRY RUN " if self.dry_run else "") + self.phase]
        if self.phase == Progress.PREPARING:
            parts.append("checking the target")
        elif self.phase == Progress.SCANNING and not self.reviewed_files and not self.bytes:
            todo = "building the file list - big folders can take a while"
            if self.total_bytes is not None and self.total_files is not None:
                todo += " ({}, {:,} files to review)".format(
                    Progress.short_size(self.total_bytes), self.total_files)
            parts.append(todo)
        else:
            if self.total_bytes is not None:
                # the estimate may be a bit stale - never show more than 100%
                total = max(self.total_bytes, self.reviewed_bytes)
                pct = 100.0 * self.reviewed_bytes / total if total else 100.0
                parts.append("reviewed {} of {} ({:.0f}%)".format(
                    Progress.short_size(self.reviewed_bytes), Progress.short_size(total), pct))
            else:
                parts.append("reviewed {}".format(Progress.short_size(self.reviewed_bytes)))
            if self.total_files is not None:
                parts.append("{:,} of {:,} files".format(
                    self.reviewed_files, max(self.total_files, self.reviewed_files)))
            else:
                parts.append("{:,} files".format(self.reviewed_files))
            parts.append("{} {:,} file{}, {}".format(
                "would copy" if self.dry_run else "copied", self.copied_files,
                "" if self.copied_files == 1 else "s", Progress.short_size(self.copied_bytes)))
            if self.speed is not None and not self.dry_run:
                parts.append(Progress.short_size(self.speed) + "/s")
            if self.conflicts:
                parts.append("{:,} conflict{}".format(
                    self.conflicts, "" if self.conflicts == 1 else "s"))
            if self.deleted:
                parts.append("{:,} {}".format(
                    self.deleted, "would be deleted" if self.dry_run else "DELETED"))
        elapsed = int(time.time() - self.started)
        if elapsed < 60:
            parts.append("{}s".format(elapsed))
        elif elapsed < 3600:
            parts.append("{}m {:02d}s".format(elapsed // 60, elapsed % 60))
        else:
            parts.append(format_duration(elapsed))
        if self.current and self.phase in (Progress.COPYING, Progress.DELETING):
            parts.append("now: " + self.current)
        return " | ".join(parts)


class Runner:
    """What rsync and rclone items share: resetting an item for a run,
    refusing surprising item shapes, final statuses and conflict
    resolution. Subclasses run the tool (run_item, item_cmd) and provide
    still_conflicts() and copy_new_file() for resolutions."""

    TOOL = ""

    def __init__(self, registry):
        self.registry = registry

    def flags(self):
        return FLAG_SETS[self.TOOL]

    def _start(self, item, opts, log_cb):
        """Reset item for a run. Returns dry_run."""
        dry_run = self.flags().is_dry_run(opts)
        item.status = STATUS_RUNNING
        item.conflicts = []
        item.copied_list = []
        item.errors_list = []
        item.error_text = ""
        item.copied_files = 0
        item.progress = Progress(dry_run, item.size_bytes, item.size_files)
        # Our "don't clobber newer target files" safety net relies on -u.
        if not self.flags().has_update(opts):
            log_cb("WARNING: -u/--update not in {} options - newer target files may be "
                   "overwritten for {}".format(self.TOOL, item.src_display()))
        return dry_run

    @staticmethod
    def safe_relname(name):
        """A name from the tool's output that stays inside the item's
        folder when joined to it (a hostile server or remote could report
        '../x' or '/x')."""
        return (bool(name) and not name.startswith("/") and "\0" not in name and
                all(p not in ("", ".", "..") for p in name.split("/")))

    @staticmethod
    def local_path(fs, path):
        """The local filesystem path a side writes to, or None if it isn't
        on this machine."""
        if fs.kind == "local":
            return path
        if fs.kind == "rclone" and fs.remote.is_local:
            return fs.remote.storage_path(path)[1]
        return None

    @staticmethod
    def storage_path(fs, path):
        """(storage id, path) to compare two sides by, or None if unknown
        (SSH hosts can't be compared reliably)."""
        if fs.kind == "local":
            return "local", os.path.realpath(path)
        if fs.kind == "rclone":
            return fs.remote.storage_path(path)
        return None

    def check_target(self, item, src_fs, dst_fs):
        """Refuse item shapes where the tool would do something other than
        the obvious copy. Returns an error text or ""."""
        s = Runner.storage_path(src_fs, item.src)
        d = Runner.storage_path(dst_fs, item.dst)
        if s is not None and d is not None and s[0] == d[0]:
            if d[1] == s[1] or d[1].startswith(s[1].rstrip("/") + "/"):
                return "the target is the source itself or inside it"
        if item.is_dir:
            if dst_fs.exists(item.dst) and not dst_fs.isdir(item.dst):
                return "a file with that name already exists in the target"
        elif dst_fs.isdir(item.dst):
            # rsync would copy the file INTO that folder instead
            return "a folder with that name already exists in the target"
        return ""

    def _fail(self, item, text):
        item.status = STATUS_FAIL
        item.error_text = text
        item.errors_list.append((item.src_display(), item.dst_display(), text))

    def _add_results(self, item, src_fs, dst_fs, copied, newer, dry_run):
        """Fill the Verify lists from relative names of copied files and of
        files skipped because the target is newer."""
        copied_remark = "would copy (dry run)" if dry_run else "OK"
        if item.is_dir:
            unsafe = [n for n in copied + newer if not Runner.safe_relname(n)]
            for name in unsafe:
                item.errors_list.append((item.src_display(), item.dst_display(),
                                         "ignored an unexpected file name in {}'s output: "
                                         "{!r}".format(self.TOOL, name)))
            if unsafe:
                unsafe = set(unsafe)
                copied = [n for n in copied if n not in unsafe]
                newer = [n for n in newer if n not in unsafe]
        for relname in copied:
            if item.is_dir:
                item.copied_list.append((
                    item.src_display(src_fs.pathmod.join(item.src, relname)),
                    item.dst_display(dst_fs.pathmod.join(item.dst, relname)),
                    copied_remark))
            else:
                item.copied_list.append((item.src_display(), item.dst_display(), copied_remark))
        for relname in newer:
            if item.is_dir:
                item.conflicts.append(Conflict(relname,
                                               src_fs.pathmod.join(item.src, relname),
                                               dst_fs.pathmod.join(item.dst, relname),
                                               dry_run))
            else:
                item.conflicts.append(Conflict(src_fs.pathmod.basename(item.src),
                                               item.src, item.dst, dry_run))

    def _set_final_status(self, item, dry_run, seconds):
        if item.conflicts:
            item.status = STATUS_DRY_INCOMPLETE if dry_run else STATUS_INCOMPLETE
        elif dry_run:
            item.status = STATUS_DRY_OK
        else:
            item.status = STATUS_OK
            item.transfer_seconds = seconds

    def still_conflicts(self, src_fs, src, dst_fs, dst):
        raise NotImplementedError

    def copy_new_file(self, src_fs, src, dst_fs, dst):
        raise NotImplementedError

    def resolve_conflict(self, item, conflict, action):
        """Apply a user's chosen resolution to a single conflicting file.
        Nothing is ever overwritten: the existing target is either renamed
        away first or left alone."""
        src_fs = self.registry.fs_for(item.src_conn)
        dst_fs = self.registry.fs_for(item.dst_conn)
        base, ext = dst_fs.pathmod.splitext(conflict.dst_full)
        try:
            if action != "skip" and not self.still_conflicts(
                    src_fs, conflict.src_full, dst_fs, conflict.dst_full):
                return False, ("the files changed since the run (or the source is gone) - "
                               "nothing was changed. Run the item again.")
            if action == "skip":
                conflict.remark = "user skipped"
            elif action == "rename":
                # rename the EXISTING (target) file out of the way, then copy in
                newname = "{}.conflict-{}{}".format(base, int(time.time()), ext)
                dst_fs.rename(conflict.dst_full, newname)
                try:
                    self.copy_new_file(src_fs, conflict.src_full, dst_fs, conflict.dst_full)
                except FsError as e:
                    try:
                        dst_fs.rename(newname, conflict.dst_full)
                        where = "the existing file was put back"
                    except FsError:
                        where = "the existing file is now named {}".format(
                            dst_fs.pathmod.basename(newname))
                    raise FsError("copy failed ({}); {}".format(e, where))
                conflict.remark = "existing renamed to {}".format(dst_fs.pathmod.basename(newname))
            elif action == "autorename":
                # copy the INCOMING file in under a new auto-generated name,
                # leaving the existing target file untouched
                newname = "{}.new-{}{}".format(base, int(time.time()), ext)
                self.copy_new_file(src_fs, conflict.src_full, dst_fs, newname)
                conflict.remark = "auto-renamed incoming to {}".format(dst_fs.pathmod.basename(newname))
            conflict.resolution = action
            return True, ""
        except FsError as e:
            return False, str(e)


class RsyncRunner(Runner):
    """Builds and runs rsync for queue items between any combination of a
    local and (at most one) remote side.

    Conflicts (target newer than source) are taken from rsync itself: with
    -u and --info=skip1 it prints '<relpath> is newer' for every file it
    skipped for that reason - so there's no separate pre-scan."""

    TOOL = TOOL_RSYNC

    # Output format forced on every run (it overrides a user's -i): the
    # itemize code YXcstpoguax, the file size, the name. With
    # --info=name2 rsync prints such a line for every file it checks,
    # unchanged ones too (".f          1000 name"); deletions come out as
    # "*deleting   0 name".
    OUT_FORMAT = "--out-format=%i %l %n"
    ITEM_RE = re.compile(r"^([<>ch.*])([fdLDS])(.{9}) (\d+) (.+)$")
    NEWER_SUFFIX = " is newer"
    # rsync prints non-printable bytes in names as \#ooo (and a backslash
    # as \#134, so this is unambiguous)
    ESCAPE_RE = re.compile(br"\\#([0-7]{3})")

    @staticmethod
    def unescape(name):
        raw = os.fsencode(name)
        return os.fsdecode(RsyncRunner.ESCAPE_RE.sub(
            lambda m: bytes([int(m.group(1), 8)]), raw))

    @staticmethod
    def segments(read):
        """Yield rsync's output split on '\\n' or '\\r' (progress updates end
        in '\\r' so they can overwrite each other on a terminal). read()
        returns bytes, b"" at the end."""
        buf = b""
        while True:
            chunk = read()
            if not chunk:
                break
            buf += chunk
            parts = re.split(b"[\r\n]", buf)
            buf = parts.pop()
            for part in parts:
                yield part
        if buf:
            yield buf

    @staticmethod
    def parse_item(line):
        """(kind, type, size, name) of an OUT_FORMAT line, or None.
        kind '*' means the file is being deleted."""
        m = RsyncRunner.ITEM_RE.match(line)
        if not m:
            return None
        return (m.group(1), m.group(2), int(m.group(4)), RsyncRunner.unescape(m.group(5)))

    @staticmethod
    def describe_line(line, dry_run):
        """Readable run-log text for an rsync output line, or None."""
        would = "would be " if dry_run else ""
        item = RsyncRunner.parse_item(line)
        if item:
            kind, ftype, size, name = item
            if kind == "*":
                return "{}DELETED: {}".format(would, name)
            if kind in "<>":
                return "{}copied: {} ({})".format(would, name, Progress.short_size(size))
            if kind == "c":
                return "{}created: {}".format(would, name)
            return None
        if line.endswith(RsyncRunner.NEWER_SUFFIX):
            return "CONFLICT (target newer, not copied): {}".format(
                RsyncRunner.unescape(line[:-len(RsyncRunner.NEWER_SUFFIX)]))
        if line.startswith(("rsync:", "rsync error:", "rsync warning:")):
            return line
        return None

    @staticmethod
    def parse_output(lines):
        """Returns (copied_relnames, newer_relnames, rsync_error_lines)."""
        copied = []
        newer = []
        errors = []
        for line in lines:
            item = RsyncRunner.parse_item(line)
            if item:
                if item[0] in "<>ch":
                    copied.append(item[3])
            elif line.endswith(RsyncRunner.NEWER_SUFFIX):
                newer.append(RsyncRunner.unescape(line[:-len(RsyncRunner.NEWER_SUFFIX)]))
            elif line.startswith(("rsync:", "rsync error:")):
                errors.append(line)
        return copied, newer, errors

    def build_cmd(self, opts, src_fs, src, dst_fs, dst, is_dir):
        if src_fs.is_remote and dst_fs.is_remote:
            raise FsError("rsync can't copy between two remote hosts")
        cmd = ["rsync"] + list(opts)
        remote = src_fs if src_fs.is_remote else (dst_fs if dst_fs.is_remote else None)
        if remote is not None:
            # -s: remote paths are passed verbatim, not re-split by the remote shell
            cmd += ["-s", "-e", remote.conn.rsync_e()]
        if is_dir:
            src = src.rstrip("/") + "/"
            dst = dst.rstrip("/") + "/"
        # '--': a path can never be taken as an option
        return cmd + ["--", src_fs.rsync_arg(src), dst_fs.rsync_arg(dst)]

    def item_cmd(self, item, rsync_opts, src_fs, dst_fs):
        # added last so they win over e.g. a user's --progress
        return self.build_cmd(list(rsync_opts) + ["--info=skip1,progress2,name2",
                                                  RsyncRunner.OUT_FORMAT],
                              src_fs, item.src, dst_fs, item.dst, item.is_dir)

    @staticmethod
    def _spawn(cmd):
        # Bytes + fsdecode: odd bytes in rsync output can't crash the decode.
        return subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT)

    def _run_capture(self, cmd):
        """Run rsync to completion. Returns (rc, output_lines)."""
        try:
            proc = RsyncRunner._spawn(cmd)
            out, _ = proc.communicate()
        except OSError as e:
            raise FsError("could not start rsync: {}".format(e))
        return proc.returncode, [os.fsdecode(l) for l in out.split(b"\n")]

    def run_item(self, item, log_cb, rsync_opts=None):
        """Run one queue item's rsync copy. Mutates item in place.
        log_cb(str) is called for progress/log lines (thread-safe queue push).
        rsync_opts is the user-editable (expert mode) list of rsync flags;
        defaults to DEFAULT_RSYNC_OPTS if not supplied.
        Returns False if the ssh connection dropped - the item is then reset
        to NOT SYNCED so it can be retried after reconnecting."""
        if rsync_opts is None:
            rsync_opts = DEFAULT_RSYNC_OPTS
        dry_run = self._start(item, rsync_opts, log_cb)
        problem = RsyncFlags.problems(rsync_opts)
        if problem:
            self._fail(item, problem)
            return True

        src_fs = self.registry.fs_for(item.src_conn)
        dst_fs = self.registry.fs_for(item.dst_conn)
        remote = src_fs if src_fs.is_remote else (dst_fs if dst_fs.is_remote else None)
        try:
            problem = self.check_target(item, src_fs, dst_fs)
            if problem:
                self._fail(item, "not copied: " + problem)
                return True
            if not dry_run:
                # Single file: destination is the exact target FILE path, so
                # only its parent directory is created; rsync copies the
                # source onto that exact path/name.
                parent = item.dst if item.is_dir else dst_fs.pathmod.dirname(item.dst)
                if parent:
                    dst_fs.makedirs(parent)
            cmd = self.item_cmd(item, rsync_opts, src_fs, dst_fs)
        except ConnectionLost as e:
            item.status = STATUS_PENDING
            log_cb("CONNECTION LOST: {}".format(e))
            return False
        except FsError as e:
            self._fail(item, "could not prepare target: {}".format(e))
            return True

        transfer_start = time.time()
        try:
            proc = RsyncRunner._spawn(cmd)
        except OSError as e:
            self._fail(item, "could not start rsync: {}".format(e))
            return True

        progress = item.progress
        progress.phase = Progress.SCANNING
        out_lines = []
        fd = proc.stdout.fileno()
        for raw in RsyncRunner.segments(lambda: os.read(fd, 65536)):
            line = os.fsdecode(raw)
            if not line.strip() or progress.feed_progress(line):
                continue
            out_lines.append(line)
            text = RsyncRunner.describe_line(line, dry_run)
            if text:
                log_cb(text)
            parsed = RsyncRunner.parse_item(line)
            if parsed:
                progress.feed_item(*parsed)
            elif line.endswith(RsyncRunner.NEWER_SUFFIX):
                progress.feed_conflict()
        proc.stdout.close()
        proc.wait()
        progress.phase = Progress.DONE
        transfer_end = time.time()
        rc = proc.returncode

        if rc != 0 and remote is not None and not remote.conn.check():
            item.status = STATUS_PENDING
            log_cb("CONNECTION LOST to {} during {}".format(remote.spec.key(), item.src_display()))
            return False

        copied, newer, errors = RsyncRunner.parse_output(out_lines)
        self._add_results(item, src_fs, dst_fs, copied, newer, dry_run)

        # parse stats block
        text = "\n".join(out_lines)
        m = STATS_RE["files_transferred"].search(text)
        if m:
            item.copied_files = int(m.group(1).replace(",", ""))

        if rc != 0:
            detail = errors[0] if errors else ""
            self._fail(item, "rsync exited with code {}{}".format(
                rc, (" - " + detail) if detail else ""))
        else:
            self._set_final_status(item, dry_run, transfer_end - transfer_start)
        return True

    def still_conflicts(self, src_fs, src, dst_fs, dst):
        """Dry-run probe: is dst still newer than src? Guards resolutions
        against acting on a stale or misread conflict."""
        cmd = self.build_cmd(["-u", "-n", "--info=skip1"], src_fs, src, dst_fs, dst, False)
        rc, lines = self._run_capture(cmd)
        copied, newer, errors = RsyncRunner.parse_output(lines)
        return rc == 0 and bool(newer)

    def copy_new_file(self, src_fs, src, dst_fs, dst):
        """Copy src to dst, which must not exist yet. Deliberately ignores
        the user's rsync flags (they might include --remove-source-files
        etc.); --ignore-existing makes rsync itself refuse to replace
        anything, and the itemized output proves the copy happened."""
        cmd = self.build_cmd(["-t", "--ignore-existing", RsyncRunner.OUT_FORMAT],
                             src_fs, src, dst_fs, dst, False)
        rc, lines = self._run_capture(cmd)
        copied, newer, errors = RsyncRunner.parse_output(lines)
        if rc != 0:
            raise FsError(errors[0] if errors else "rsync exited with code {}".format(rc))
        if not copied:
            raise FsError("'{}' already exists - nothing was overwritten".format(
                dst_fs.pathmod.basename(dst)))


class RcloneRunner(Runner):
    """Runs 'rclone copy' (folders) or 'copyto' (files) for items with an
    rclone remote on at least one side and a local folder or another
    rclone remote on the other. Neither command deletes from the target;
    with RcloneFlags.SYNC_FLAG folders run 'rclone sync', which does.

    Everything is read from rclone's JSON log at DEBUG level: an entry for
    every file checked (copied, skipped because the target is newer, or
    unchanged) and a stats entry every second. Unchanged files are logged
    without their size, so the source is listed first for the reviewed
    progress."""

    TOOL = TOOL_RCLONE
    FORCED_OPTS = ["--use-json-log", "--log-level", "DEBUG", "--stats", "1s"]
    NEWER_MSG = "Destination is newer than source, skipping"
    COPIED_PREFIX = "Copied ("
    DRY_RUN_PREFIX = "Skipped copy as --dry-run is set"
    DELETED_MSG = "Deleted"
    DRY_DELETE_PREFIX = "Skipped delete as --dry-run is set"
    RMDIR_MSG = "Removing directory"
    DRY_RMDIR_PREFIX = "Skipped remove directory as --dry-run is set"
    SYMLINK_PREFIX = "Can't follow symlink"

    @staticmethod
    def parse_line(line):
        """(kind, name, value) for one line of rclone's output, or None.
        kind: "stats" (value = stats dict), "copied" (value = True if it
        replaced a file), "would_copy" (value = size), "newer",
        "unchanged", "deleted" (deleted or, in a dry run, would be; value
        = True for a folder), "symlink", "error" / "notice" (value =
        text)."""
        line = line.strip()
        if not line:
            return None
        if not line.startswith("{"):
            # e.g. the final "Failed to copy: ..." line, or notices printed
            # before the JSON log is set up
            is_error, text = RcloneCli.plain_line(line)
            return ("error" if is_error else "notice", "", text)
        try:
            entry = json.loads(line)
        except ValueError:
            return ("error", "", line)
        if not isinstance(entry, dict):
            return None
        if isinstance(entry.get("stats"), dict):
            return ("stats", "", entry["stats"])
        msg = str(entry.get("msg", "")).strip()
        level = entry.get("level")
        obj = entry.get("object")
        name = obj if isinstance(obj, str) else ""
        if name and msg.startswith(RcloneRunner.SYMLINK_PREFIX):
            return ("symlink", name, msg)
        if name and (msg == RcloneRunner.RMDIR_MSG or
                     msg.startswith(RcloneRunner.DRY_RMDIR_PREFIX)):
            return ("deleted", name, True)
        if name and str(entry.get("objectType", "")).endswith("Object"):
            if msg == RcloneRunner.NEWER_MSG:
                return ("newer", name, None)
            if msg.startswith(RcloneRunner.COPIED_PREFIX):
                return ("copied", name, "replaced" in msg)
            if msg.startswith(RcloneRunner.DRY_RUN_PREFIX):
                size = entry.get("size")
                return ("would_copy", name, size if isinstance(size, int) else 0)
            if msg == RcloneRunner.DELETED_MSG or msg.startswith(RcloneRunner.DRY_DELETE_PREFIX):
                return ("deleted", name, False)
            if level == "debug" and msg.endswith("skipping"):
                return ("unchanged", name, None)
        if level in ("error", "critical", "warning", "notice"):
            kind = "error" if level in ("error", "critical") else "notice"
            return (kind, name, "{}: {}".format(name, msg) if name else msg)
        return None

    @staticmethod
    def describe_event(event, dry_run, size):
        """Readable run-log text for an event, or None."""
        kind, name, value = event
        if kind == "copied":
            return "{}: {} ({})".format("replaced" if value else "copied", name,
                                           Progress.short_size(size))
        if kind == "would_copy":
            return "would be copied: {} ({})".format(name, Progress.short_size(value))
        if kind == "deleted":
            return "{}DELETED: {}{}".format("would be " if dry_run else "", name,
                                               "/" if value else "")
        if kind == "newer":
            return "CONFLICT (target newer, not copied): {}".format(name)
        if kind == "symlink":
            return "rclone error: symlink skipped (tick -l, --links to keep it): {}".format(name)
        if kind in ("error", "notice"):
            return "rclone {}: {}".format(kind, value)
        return None

    @staticmethod
    def side_arg(fs, path):
        if fs.kind == "ssh":
            raise FsError("rclone can't copy to or from an SSH pane")
        return fs.rclone_arg(path)

    @staticmethod
    def effective_opts(opts, dst_fs):
        """The flags rclone gets: without SYNC_FLAG (see command()), and
        without --links for local targets: it turns .rclonelink files back
        into symlinks there, and rclone then writes later files THROUGH
        such a symlink - so a remote could place files anywhere."""
        opts = RcloneFlags.without(opts, None, RcloneFlags.SYNC_FLAG)
        if Runner.local_path(dst_fs, "/") is not None:
            return RcloneFlags.without(opts, "l", "--links")
        return opts

    def item_args(self, item, opts, src_fs, dst_fs):
        # '--': a path can never be taken as an option
        return RcloneRunner.effective_opts(opts, dst_fs) + RcloneRunner.FORCED_OPTS + [
            "--", RcloneRunner.side_arg(src_fs, item.src), RcloneRunner.side_arg(dst_fs, item.dst)]

    @staticmethod
    def command(item, opts):
        # copy: the folder's contents into item.dst; sync: the same, then
        # deletes what isn't in the source; copyto: the file AS item.dst
        if not item.is_dir:
            return "copyto"
        return "sync" if RcloneFlags.is_sync(opts) else "copy"

    def item_cmd(self, item, opts, src_fs, dst_fs):
        return RcloneCli.cmd(RcloneRunner.command(item, opts),
                             self.item_args(item, opts, src_fs, dst_fs))

    def list_source(self, item, src_fs, opts):
        """((relpath -> size of each file, set of folder relpaths), "") of a
        folder item's source, or (None, error text)."""
        args = ["-R", "--no-mimetype", "--no-modtime"]
        # the same names the copy will log, e.g. 'x.rclonelink' for a symlink
        for short, long_name in (("l", "--links"), ("L", "--copy-links")):
            if RcloneFlags.has(opts, short, long_name):
                args.append(long_name)
        sizes = {}
        dirs = set()
        # stderr to a file: only stdout is read while rclone runs
        with tempfile.TemporaryFile() as err:
            try:
                proc = RcloneCli.spawn("lsjson", args + ["--", src_fs.rclone_arg(item.src)],
                                       subprocess.PIPE, err)
            except FsError as e:
                return None, str(e)
            # one entry per line: {"Path":"a/b.txt",...},
            for raw in proc.stdout:
                line = raw.strip().rstrip(b",")
                if not line.startswith(b"{"):
                    continue
                try:
                    entry = json.loads(os.fsdecode(line))
                    path = entry["Path"]
                    if not isinstance(path, str):
                        continue
                    if entry["IsDir"]:
                        dirs.add(path)
                    else:
                        sizes[path] = int(entry["Size"])
                except (ValueError, KeyError, TypeError):
                    continue
            proc.stdout.close()
            proc.wait()
            if proc.returncode != 0:
                err.seek(0)
                return None, RcloneCli.error_text(os.fsdecode(err.read()), proc.returncode)
        return (sizes, dirs), ""

    @staticmethod
    def symlink_in_target(item, dst_fs, listing):
        """A symlink in a local target folder where the source has a
        folder: rclone would write that folder's files through it, i.e.
        outside the target. Returns its relative path, or ""."""
        root = Runner.local_path(dst_fs, item.dst)
        if root is None or not item.is_dir:
            return ""
        sizes, dirs = listing
        prefixes = set(dirs)
        for rel in sizes:
            parts = rel.split("/")[:-1]
            for i in range(1, len(parts) + 1):
                prefixes.add("/".join(parts[:i]))
        # shortest first: a link higher up hides everything below it
        for rel in sorted(prefixes, key=lambda p: (p.count("/"), p)):
            if Runner.safe_relname(rel) and os.path.islink(os.path.join(root, rel)):
                return rel
        return ""

    @staticmethod
    def linked_folder_in_target(item, dst_fs, opts):
        """For sync into local storage: a symlink to a folder anywhere in
        the target. If rclone follows it (-L, or copy_links set in its
        environment or config, which no flag reliably overrides), sync
        deletes what it points to. Returns its relative path, or ""."""
        root = Runner.local_path(dst_fs, item.dst)
        if root is None or RcloneRunner.command(item, opts) != "sync":
            return ""
        # links to folders show up in dirnames, and aren't followed
        for dirpath, dirnames, filenames in os.walk(root):
            for name in sorted(dirnames):
                full = os.path.join(dirpath, name)
                if os.path.islink(full):
                    return os.path.relpath(full, root)
        return ""

    def _pause(self, item, rc, errors, log_cb):
        """For exit codes worth retrying later: the item goes back to NOT
        SYNCED and its rclone remotes count as not connected, which pauses
        the run until the user presses P."""
        item.status = STATUS_PENDING
        for spec in item.conn_specs():
            if spec.kind == "rclone":
                text = "{}: {}".format(spec.key(), RcloneCli.PAUSE_CODES[rc].format(spec.remote))
                if errors:
                    text += " - rclone said: " + errors[0]
                self.registry.rclone_remote(spec).mark_unavailable(text)
        log_cb("CONNECTION LOST: rclone exited with code {} during {}".format(
            rc, item.src_display()))

    def unreachable_side(self, item, src_fs, dst_fs):
        """After a failed copy: the text of a remote that can't be reached
        (it is then marked as not connected), or ""."""
        for fs, path in ((src_fs, item.src), (dst_fs, dst_fs.pathmod.dirname(item.dst))):
            if fs.kind != "rclone":
                continue
            try:
                fs.exists(path)
            except RcloneUnavailable as e:
                return str(e)
            except FsError:
                pass
        return ""

    def run_item(self, item, log_cb, opts=None):
        """Like RsyncRunner.run_item. Returns False if the run should pause
        (see _pause)."""
        if opts is None:
            opts = DEFAULT_RCLONE_OPTS
        dry_run = self._start(item, opts, log_cb)
        src_fs = self.registry.fs_for(item.src_conn)
        dst_fs = self.registry.fs_for(item.dst_conn)
        problem = RcloneFlags.problems(opts)
        if problem:
            self._fail(item, problem)
            return True
        try:
            problem = self.check_target(item, src_fs, dst_fs)
            if problem:
                self._fail(item, "not copied: " + problem)
                return True
            args = self.item_args(item, opts, src_fs, dst_fs)
        except ConnectionLost as e:
            item.status = STATUS_PENDING
            log_cb("CONNECTION LOST: {}".format(e))
            return False
        except FsError as e:
            self._fail(item, "could not check the target: {}".format(e))
            return True
        link = RcloneRunner.linked_folder_in_target(item, dst_fs, opts)
        if link:
            self._fail(item, "not synced: the target has a symlink to a folder, '{}' - if rclone "
                             "followed it, --delete would delete files outside the target. Use "
                             "it without --delete, or remove the link".format(link))
            return True

        progress = item.progress
        progress.phase = Progress.SCANNING
        sizes = {}
        if item.is_dir:
            listing, list_error = self.list_source(
                item, src_fs, RcloneRunner.effective_opts(opts, dst_fs))
            if listing is None and Runner.local_path(dst_fs, item.dst) is not None:
                # without the listing the symlink check below can't be done
                lost = self.unreachable_side(item, src_fs, dst_fs)
                if lost:
                    item.status = STATUS_PENDING
                    log_cb("CONNECTION LOST: {} (during {})".format(lost, item.src_display()))
                    return False
                self._fail(item, "could not list the source: " + list_error)
                return True
            if listing is not None:
                link = RcloneRunner.symlink_in_target(item, dst_fs, listing)
                if link:
                    self._fail(item, "not copied: the target has a symlink '{}' where the "
                                     "source has a folder - rclone would write through it, "
                                     "outside the target".format(link))
                    return True
                sizes = listing[0]

        def size_of(name):
            return sizes.get(name, 0) if item.is_dir else (item.size_bytes or 0)

        transfer_start = time.time()
        try:
            proc = RcloneCli.spawn(RcloneRunner.command(item, opts), args, subprocess.PIPE,
                                   subprocess.STDOUT)
        except FsError as e:
            self._fail(item, str(e))
            return True
        copied = []
        newer = []
        errors = []
        stats = {}
        fd = proc.stdout.fileno()
        for raw in RsyncRunner.segments(lambda: os.read(fd, 65536)):
            event = RcloneRunner.parse_line(os.fsdecode(raw))
            if event is None:
                continue
            kind, name, value = event
            if kind == "stats":
                stats = value
                progress.feed_rclone_stats(value)
                continue
            if kind == "copied":
                copied.append(name)
            elif kind == "would_copy":
                copied.append(name)
                progress.feed_listed(value, name)
            elif kind == "newer":
                newer.append(name)
                progress.feed_conflict(size_of(name))
            elif kind == "unchanged":
                progress.feed_unchanged(size_of(name))
            elif kind == "deleted":
                progress.feed_deleted(name + ("/" if value else ""))
            elif kind == "symlink":
                full = src_fs.pathmod.join(item.src, name) if item.is_dir else item.src
                item.errors_list.append((item.src_display(full), item.dst_display(),
                                         "symlink skipped - tick -l, --links to keep it"))
            elif kind == "error":
                errors.append(value)
            text = RcloneRunner.describe_event(event, dry_run, size_of(name))
            if text:
                log_cb(text)
        proc.stdout.close()
        proc.wait()
        progress.phase = Progress.DONE
        rc = proc.returncode

        if rc in RcloneCli.PAUSE_CODES:
            self._pause(item, rc, errors, log_cb)
            return False
        if rc != 0:
            lost = self.unreachable_side(item, src_fs, dst_fs)
            if lost:
                item.status = STATUS_PENDING
                log_cb("CONNECTION LOST: {} (during {})".format(lost, item.src_display()))
                return False
        self._add_results(item, src_fs, dst_fs, copied, newer, dry_run)
        item.copied_files = progress.copied_files
        if rc != 0:
            self._fail(item, "rclone exited with code {}{}".format(
                rc, (" - " + errors[0]) if errors else ""))
        else:
            self._set_final_status(item, dry_run, time.time() - transfer_start)
        return True

    def _run_events(self, command, args):
        """Run a short rclone command with a JSON log. Returns (rc, events,
        stderr). Exit codes worth retrying become FsError right away."""
        rc, out, err = RcloneCli.run(command, ["--use-json-log"] + args, timeout=None)
        if rc in RcloneCli.PAUSE_CODES:
            raise FsError("{} (rclone exit code {}) - try again later".format(
                RcloneCli.error_text(err, rc), rc))
        events = [e for e in (RcloneRunner.parse_line(l) for l in err.splitlines()) if e]
        return rc, events, err

    def still_conflicts(self, src_fs, src, dst_fs, dst):
        rc, events, err = self._run_events("copyto", [
            "--update", "--dry-run", "--log-level", "DEBUG", "--",
            RcloneRunner.side_arg(src_fs, src), RcloneRunner.side_arg(dst_fs, dst)])
        return rc == 0 and any(e[0] == "newer" for e in events)

    def copy_new_file(self, src_fs, src, dst_fs, dst):
        """Copy src to dst, which must not exist yet. Ignores the user's
        rclone flags; --ignore-existing makes rclone itself refuse to
        replace anything, and its log proves the copy happened."""
        rc, events, err = self._run_events("copyto", [
            "--ignore-existing", "--log-level", "INFO", "--",
            RcloneRunner.side_arg(src_fs, src), RcloneRunner.side_arg(dst_fs, dst)])
        if rc != 0:
            raise FsError(RcloneCli.error_text(err, rc))
        if not any(e[0] == "copied" for e in events):
            raise FsError("'{}' already exists - nothing was overwritten".format(
                dst_fs.pathmod.basename(dst)))


class Transfers:
    """Picks each queue item's runner: rclone if either side is an rclone
    remote, else rsync."""

    def __init__(self, registry):
        self.registry = registry
        self.runners = {TOOL_RSYNC: RsyncRunner(registry), TOOL_RCLONE: RcloneRunner(registry)}

    def for_item(self, item):
        return self.runners[item.tool()]

    def run_item(self, item, log_cb, opts):
        return self.for_item(item).run_item(item, log_cb, opts)

    def item_cmd(self, item, opts):
        return self.for_item(item).item_cmd(item, opts,
                                            self.registry.fs_for(item.src_conn),
                                            self.registry.fs_for(item.dst_conn))

    def resolve_conflict(self, item, conflict, action):
        return self.for_item(item).resolve_conflict(item, conflict, action)


# ----------------------------------------------------------------------
# Curses UI helpers
# ----------------------------------------------------------------------

UNPRINTABLE_RE = re.compile("[\x00-\x1f\x7f\ud800-\udfff]")


def printable(text):
    """File names can contain control characters (break the layout) and,
    when not valid UTF-8, lone surrogates (make addstr raise)."""
    return UNPRINTABLE_RE.sub("?", text)


def safe_addstr(win, y, x, text, attr=0):
    h, w = win.getmaxyx()
    if y < 0 or y >= h or x >= w:
        return
    maxlen = w - x - 1
    if maxlen <= 0:
        return
    try:
        win.addstr(y, x, printable(text)[:maxlen], attr)
    except (curses.error, UnicodeEncodeError):
        pass


class Keys:
    """Key codes curses has no constant for, or that come as several codes."""
    TAB = 9
    ESC = 27
    CTRL_U = 21
    ENTER = (10, 13, curses.KEY_ENTER)
    BACKSPACE = (curses.KEY_BACKSPACE, 127, 8)


class Color:
    """Color pair numbers, set up in App.__init__."""
    ACTIVE_HEADER = 1
    YELLOW = 2
    GREEN = 3
    RED = 4
    MAGENTA = 5
    STATUSBAR = 6
    CYAN = 7
    ACTIVE_FRAME = 8
    GRAY = 9
    BRIGHT_RED = 10


def edit_text(text, c):
    """Apply key c (from read_key) to the char list text. Returns True if
    it was an editing key. Without a cursor, Left/Right are ignored rather
    than inserted as escape-sequence garbage."""
    if c in Keys.BACKSPACE:
        if text:
            text.pop()
    elif c == Keys.CTRL_U:
        del text[:]
    elif isinstance(c, str):
        text.append(c)
    else:
        return False
    return True


def prompt_string(stdscr, prompt, default=""):
    """One-line input at the bottom of the screen, pre-filled with default.
    Enter accepts, Esc cancels (returns None), Ctrl-U clears the line."""
    h, w = stdscr.getmaxyx()
    win = curses.newwin(3, w, h - 3, 0)
    # keypad: arrow keys arrive as KEY_* instead of an Esc + bytes sequence
    win.keypad(True)
    text = list(default)
    field_w = max(4, w - 4)
    hint = " Enter: OK   Esc: cancel   Ctrl-U: clear "
    curses.curs_set(1)
    try:
        while True:
            win.erase()
            win.border()
            safe_addstr(win, 0, 2, " " + prompt + " ", curses.A_BOLD)
            safe_addstr(win, 2, max(2, w - len(hint) - 2), hint, curses.A_DIM)
            shown = "".join(text)[-(field_w - 1):]
            safe_addstr(win, 1, 2, shown)
            try:
                win.move(1, 2 + len(shown))
            except curses.error:
                pass
            win.noutrefresh()
            curses.doupdate()
            c = read_key(win)
            if c == Keys.ESC:
                return None
            if c in Keys.ENTER:
                return "".join(text)
            edit_text(text, c)
    finally:
        curses.curs_set(0)


def confirm(stdscr, prompt, attr=0):
    """attr styles the prompt text; the Y and N keys are always bold."""
    h, w = stdscr.getmaxyx()
    suffix = "(Y/N)"
    lines = textwrap.wrap(prompt + " " + suffix, max(10, w - 4)) or [""]
    lines = lines[:max(1, h - 3)]
    win = curses.newwin(len(lines) + 2, w, h - len(lines) - 2, 0)
    win.border()
    for i, line in enumerate(lines):
        # textwrap never splits the suffix, so it ends the last line
        if i == len(lines) - 1 and line.endswith(suffix):
            text = line[:-len(suffix)]
            safe_addstr(win, 1 + i, 2, text, attr)
            x = 2 + len(text)
            for ch in suffix:
                safe_addstr(win, 1 + i, x, ch, curses.A_BOLD if ch in "YN" else 0)
                x += 1
        else:
            safe_addstr(win, 1 + i, 2, line, attr)
    win.noutrefresh()
    curses.doupdate()
    while True:
        c = win.getch()
        if c in (ord('y'), ord('Y')):
            return True
        if c in (ord('n'), ord('N'), Keys.ESC):
            return False


def modal_input_dialog(stdscr, title, label, default=""):
    """A small modal dialog: title bar, one-line text field, OK/Cancel
    buttons underneath. Tab/Down cycles focus (field -> OK -> Cancel ->
    field), Shift-Tab/Up goes back, Left/Right switch between the buttons.
    Enter activates whichever is focused (field counts as OK). Esc always
    cancels. Ctrl-U clears the field. Returns (confirmed: bool, text: str)."""
    h, w = stdscr.getmaxyx()
    box_w = min(max(50, len(label) + 20), w - 4)
    box_h = 7
    y0 = max(0, (h - box_h) // 2)
    x0 = max(0, (w - box_w) // 2)
    win = curses.newwin(box_h, box_w, y0, x0)
    # Without keypad, an arrow key arrives as Esc + bytes and cancelled the
    # dialog.
    win.keypad(True)

    text = list(default)
    focus = "field"   # "field" | "ok" | "cancel"
    curses.curs_set(1)

    while True:
        win.erase()
        win.border()
        safe_addstr(win, 0, 2, " " + title + " ", curses.A_BOLD)
        safe_addstr(win, 1, 2, label)
        field_str = "".join(text)
        field_w = box_w - 4
        safe_addstr(win, 2, 2, " " * field_w)
        safe_addstr(win, 2, 2, field_str[-(field_w - 1):],
                    curses.A_UNDERLINE | (curses.A_REVERSE if focus == "field" else 0))

        ok_label, cancel_label = "[ OK ]", "[ Cancel ]"
        ok_x = box_w // 2 - len(ok_label) - 2
        cancel_x = box_w // 2 + 2
        safe_addstr(win, 4, ok_x, ok_label,
                    curses.A_REVERSE if focus == "ok" else curses.A_BOLD)
        safe_addstr(win, 4, cancel_x, cancel_label,
                    curses.A_REVERSE if focus == "cancel" else curses.A_BOLD)
        safe_addstr(win, 5, 2, "Enter: OK  Tab: buttons  Esc: cancel  Ctrl-U: clear",
                    curses.A_DIM)

        if focus == "field":
            curses.curs_set(1)
            try:
                win.move(2, min(field_w - 1, len(field_str) + 2))
            except curses.error:
                pass
        else:
            curses.curs_set(0)
        win.noutrefresh()
        curses.doupdate()

        c = read_key(win)
        if c == Keys.ESC:
            curses.curs_set(0)
            return False, ""
        elif c in (Keys.TAB, curses.KEY_DOWN):
            focus = {"field": "ok", "ok": "cancel", "cancel": "field"}[focus]
        elif c in (curses.KEY_BTAB, curses.KEY_UP):
            focus = {"field": "cancel", "ok": "field", "cancel": "ok"}[focus]
        elif c in (curses.KEY_LEFT, curses.KEY_RIGHT) and focus != "field":
            focus = "cancel" if focus == "ok" else "ok"
        elif c in Keys.ENTER:
            curses.curs_set(0)
            if focus == "cancel":
                return False, ""
            return True, "".join(text)
        elif focus == "field":
            edit_text(text, c)


def read_key(win):
    """Like getch(), but typed characters come back as str (so non-ASCII
    input works); special keys stay ints."""
    try:
        k = win.get_wch()
    except AttributeError:
        k = win.getch()
        return chr(k) if 32 <= k < 256 else k
    except curses.error:
        return -1
    if isinstance(k, str) and (ord(k) < 32 or k == "\x7f"):
        # control keys (Enter, Tab, Esc, Backspace, Ctrl-U, ...) as ints
        return ord(k)
    return k


def form_dialog(stdscr, title, fields, error=""):
    """Modal multi-field form with [ OK ] / [ Cancel ].
    fields: list of (label, value, hint); the focused field's hint is shown
    under the fields, error (if any) below that.
    Tab/Down/Enter go to the next field (Enter on the last field submits),
    Shift-Tab/Up go back, Esc cancels. Returns the list of values, or None
    if cancelled."""
    h, w = stdscr.getmaxyx()
    n = len(fields)
    if h < n + 10 or w < 48:
        message(stdscr, "The terminal is too small for this form - make it at least "
                "48 x {}.".format(n + 10))
        return None
    labels = [f[0] for f in fields]
    values = [list(f[1]) for f in fields]
    hints = [f[2] for f in fields]
    label_w = max(len(l) for l in labels) + 2
    box_w = min(max(64, label_w + 40), w - 2)
    box_h = n + 9
    y0 = max(0, (h - box_h) // 2)
    x0 = max(0, (w - box_w) // 2)
    win = curses.newwin(box_h, box_w, y0, x0)
    win.keypad(True)
    field_x = 2 + label_w
    field_w = max(4, box_w - field_x - 2)
    focus = 0   # 0..n-1 = fields, n = OK, n + 1 = Cancel

    while True:
        win.erase()
        win.border()
        safe_addstr(win, 0, 2, " " + title + " ", curses.A_BOLD)
        for i in range(n):
            text = "".join(values[i])
            attr = curses.A_UNDERLINE | (curses.A_REVERSE if focus == i else 0)
            safe_addstr(win, 2 + i, 2, labels[i] + ":", curses.A_BOLD if focus == i else 0)
            safe_addstr(win, 2 + i, field_x, " " * field_w, curses.A_UNDERLINE)
            safe_addstr(win, 2 + i, field_x, text[-(field_w - 1):], attr)
        if focus < n:
            safe_addstr(win, n + 3, 2, hints[focus], curses.A_DIM)
        if error:
            safe_addstr(win, n + 4, 2, error, curses.color_pair(Color.RED) | curses.A_BOLD)
        ok_label, cancel_label = "[ OK ]", "[ Cancel ]"
        ok_x = box_w // 2 - len(ok_label) - 2
        cancel_x = box_w // 2 + 2
        safe_addstr(win, n + 5, ok_x, ok_label,
                    curses.A_REVERSE if focus == n else curses.A_BOLD)
        safe_addstr(win, n + 5, cancel_x, cancel_label,
                    curses.A_REVERSE if focus == n + 1 else curses.A_BOLD)
        safe_addstr(win, n + 7, 2, "Tab/Down: next  Up: back  Enter: next/OK  Esc: cancel",
                    curses.A_DIM)

        if focus < n:
            curses.curs_set(1)
            try:
                win.move(2 + focus, field_x + min(field_w - 1, len(values[focus])))
            except curses.error:
                pass
        else:
            curses.curs_set(0)
        win.noutrefresh()
        curses.doupdate()

        c = read_key(win)
        if c == Keys.ESC:
            curses.curs_set(0)
            return None
        elif c in (Keys.TAB, curses.KEY_DOWN):
            focus = (focus + 1) % (n + 2)
        elif c in (curses.KEY_BTAB, curses.KEY_UP):
            focus = (focus - 1) % (n + 2)
        elif c in (curses.KEY_LEFT, curses.KEY_RIGHT) and focus >= n:
            focus = n + 1 if focus == n else n
        elif c in Keys.ENTER:
            if focus < n - 1:
                focus += 1
                continue
            curses.curs_set(0)
            if focus == n + 1:
                return None
            return ["".join(v) for v in values]
        elif focus < n:
            edit_text(values[focus], c)


def pick_dialog(stdscr, title, label, options, index=0, notes=None):
    """Modal list to choose one of options (strings). notes is an optional
    (heading, lines) section below the list, behind a divider. Up/Down
    move, Enter picks, Esc cancels. Returns the chosen index or None."""
    h, w = stdscr.getmaxyx()
    if h < 9 or w < 30:
        message(stdscr, "The terminal is too small for this list.")
        return None
    note_lines = notes[1] if notes else ()
    box_w = min(max([40, len(label) + 6, max(len(o) for o in options) + 10] +
                    [len(n) + 6 for n in note_lines]), w - 2)
    # blank line + divider + lines - only if the list still fits
    notes_h = len(note_lines) + 2 if notes else 0
    if h < len(options) + 8 + notes_h:
        notes_h = 0
    list_h = max(1, min(len(options), h - 8 - notes_h))
    box_h = list_h + 6 + notes_h
    win = curses.newwin(box_h, box_w, max(0, (h - box_h) // 2), max(0, (w - box_w) // 2))
    win.keypad(True)
    index = max(0, min(index, len(options) - 1))
    while True:
        win.erase()
        win.border()
        safe_addstr(win, 0, 2, " " + title + " ", curses.A_BOLD)
        safe_addstr(win, 1, 2, label)
        scroll = max(0, index - list_h + 1)
        for i in range(list_h):
            n = scroll + i
            if n >= len(options):
                break
            safe_addstr(win, 3 + i, 4, options[n].ljust(box_w - 8),
                        curses.A_REVERSE if n == index else 0)
        if notes_h:
            y = 4 + list_h
            try:
                win.addch(y, 0, curses.ACS_LTEE)
                win.hline(y, 1, curses.ACS_HLINE, box_w - 2)
                win.addch(y, box_w - 1, curses.ACS_RTEE)
            except curses.error:
                pass
            safe_addstr(win, y, 2, " " + notes[0] + " ", curses.A_BOLD)
            for i, line in enumerate(note_lines):
                safe_addstr(win, y + 1 + i, 2, line)
        safe_addstr(win, box_h - 2, 2, "Up/Down: choose  Enter: OK  Esc: cancel", curses.A_DIM)
        win.noutrefresh()
        curses.doupdate()
        c = read_key(win)
        if c == Keys.ESC:
            return None
        if c in Keys.ENTER:
            return index
        if c == curses.KEY_UP:
            index = max(0, index - 1)
        elif c == curses.KEY_DOWN:
            index = min(len(options) - 1, index + 1)


class BookmarkDialog:
    """The bookmark list popup. run() returns what the user chose, as
    (action, bookmark): "go", "add" (bookmark None), "rename", "delete";
    or None for Esc. The caller does the action and opens it again if
    the list should stay."""

    HINT = "Enter/1-9: go  A: add  F2: rename  D: delete  Esc: close"
    EMPTY = "No bookmarks yet. Press A to bookmark this folder."

    def __init__(self, stdscr, bookmarks, index=0):
        self.stdscr = stdscr
        self.bookmarks = bookmarks
        self.index = max(0, min(index, len(bookmarks) - 1))
        self.scroll = 0

    def rows(self):
        """(text, attr) per bookmark."""
        name_w = min(24, max([len(b.name) for b in self.bookmarks if not b.error] + [4]))
        rows = []
        for bm in self.bookmarks:
            label = "[{}]".format(bm.key).ljust(5)
            if bm.error:
                rows.append(("{} <invalid: {}>".format(label, bm.error),
                             curses.color_pair(Color.RED)))
            else:
                name = bm.name if len(bm.name) <= name_w else bm.name[:name_w - 1] + "~"
                rows.append(("{} {}  {}".format(label, name.ljust(name_w),
                                                bm.display_location()), 0))
        return rows

    def run(self):
        h, w = self.stdscr.getmaxyx()
        rows = self.rows()
        longest = max([len(r[0]) for r in rows] + [len(BookmarkDialog.EMPTY),
                                                   len(BookmarkDialog.HINT)])
        box_w = min(longest + 6, w - 2)
        list_h = max(1, min(len(rows) or 1, h - 6))
        box_h = list_h + 5
        win = curses.newwin(box_h, box_w, max(0, (h - box_h) // 2), max(0, (w - box_w) // 2))
        win.keypad(True)
        while True:
            if self.index < self.scroll:
                self.scroll = self.index
            if self.index >= self.scroll + list_h:
                self.scroll = self.index - list_h + 1
            win.erase()
            win.border()
            safe_addstr(win, 0, 2, " Bookmarks ", curses.A_BOLD)
            if not rows:
                safe_addstr(win, 2, 3, BookmarkDialog.EMPTY)
            for i in range(list_h):
                n = self.scroll + i
                if n >= len(rows):
                    break
                text, attr = rows[n]
                if n == self.index:
                    attr |= curses.A_REVERSE
                safe_addstr(win, 2 + i, 3, text[:box_w - 6].ljust(box_w - 6), attr)
            safe_addstr(win, box_h - 2, 2, BookmarkDialog.HINT, curses.A_DIM)
            win.noutrefresh()
            curses.doupdate()
            c = read_key(win)
            selected = self.bookmarks[self.index] if self.bookmarks else None
            if c == Keys.ESC:
                return None
            if c in Keys.ENTER and selected:
                return "go", selected
            if isinstance(c, str) and c in "123456789":
                picked = [b for b in self.bookmarks if b.number() == int(c)]
                if picked:
                    return "go", picked[0]
                curses.flash()
            if c in ("a", "A"):
                return "add", None
            if c == curses.KEY_F2 and selected:
                return "rename", selected
            if c in ("d", "D", curses.KEY_DC) and selected:
                return "delete", selected
            if c == curses.KEY_UP:
                self.index = max(0, self.index - 1)
            elif c == curses.KEY_DOWN:
                self.index = min(len(rows) - 1, self.index + 1) if rows else 0
            elif c == curses.KEY_PPAGE:
                self.index = max(0, self.index - list_h)
            elif c == curses.KEY_NPAGE:
                self.index = min(len(rows) - 1, self.index + list_h) if rows else 0
            elif c == curses.KEY_HOME:
                self.index = 0
            elif c == curses.KEY_END:
                self.index = max(0, len(rows) - 1)


def flash(win):
    curses.flash()


class NotBusy:
    """Stand-in for BusyIndicator where there is no screen (and in tests)."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


NOT_BUSY = NotBusy()


class BusyIndicator:
    """'with busy:' around something slow the main thread waits for
    (listing a remote folder, checking an rclone remote, ...): a
    highlighted spinner label covers the free right end of a frame's top
    border, ending at its corner, so a key press visibly did something.
    Nestable.

    curses isn't thread-safe, but inside the block the main thread makes no
    curses calls - so the spinner thread is the only one drawing, and it
    has stopped before the block returns. Never use it around anything
    that shows a dialog or leaves curses (ssh logins)."""

    FRAMES = "|/-\\"
    # widest that fits the free border; {} is the spinner frame
    LABELS = ("[ {} working ]", "[ {} ]", "{}")
    DELAY = 0.15        # quick operations don't flicker
    STEP = 0.12

    def __init__(self, cell):
        # cell() -> (y, x of the top-right corner, free border cells left of
        # it), or None
        self.cell = cell
        self.depth = 0
        self.stop = threading.Event()
        self.thread = None

    def __enter__(self):
        self.depth += 1
        if self.depth == 1:
            self.stop.clear()
            self.thread = threading.Thread(target=self._spin)
            self.thread.daemon = True
            self.thread.start()
        return self

    def __exit__(self, *exc):
        self.depth -= 1
        if self.depth == 0:
            self.stop.set()
            self.thread.join()
            self.thread = None
        return False

    def _spin(self):
        if self.stop.wait(BusyIndicator.DELAY):
            return
        cell = self.cell()
        if cell is None:
            return
        y, x, room = cell
        # the label's last cell is the corner itself
        label = next(l for l in BusyIndicator.LABELS if len(l.format("x")) - 1 <= room)
        width = len(label.format("x"))
        try:
            win = curses.newwin(1, width, y, x - width + 1)
        except curses.error:
            return
        frame = 0
        while True:
            try:
                # insstr: unlike addstr it doesn't fail on a window's last cell
                # reverse video: the terminal's own colours swapped, readable
                # in any colour theme
                win.insstr(0, 0, label.format(
                    BusyIndicator.FRAMES[frame % len(BusyIndicator.FRAMES)]),
                    curses.A_BOLD | curses.A_REVERSE)
                win.noutrefresh()
                curses.doupdate()
            except curses.error:
                pass
            frame += 1
            if self.stop.wait(BusyIndicator.STEP):
                break
        try:
            # the border it covered; the next full redraw repaints it anyway
            win.erase()
            win.hline(0, 0, curses.ACS_HLINE, width - 1)
            win.insch(0, width - 1, curses.ACS_URCORNER)
            win.noutrefresh()
            curses.doupdate()
        except curses.error:
            pass


def message(stdscr, text):
    h, w = stdscr.getmaxyx()
    lines = textwrap.wrap(text, max(10, w - 4)) or [""]
    lines = lines[:max(1, h - 3)]
    win = curses.newwin(len(lines) + 2, w, h - len(lines) - 2, 0)
    win.border()
    for i, line in enumerate(lines):
        safe_addstr(win, 1 + i, 2, line)
    safe_addstr(win, len(lines) + 1, w - 18, " press any key ", curses.A_DIM)
    win.noutrefresh()
    curses.doupdate()
    win.getch()


# ----------------------------------------------------------------------
# Pane (left/right file browser)
# ----------------------------------------------------------------------

class Pane:
    # first row in every folder except the root; Enter on it goes up
    PARENT = ("..", True)

    def __init__(self, path, fs, busy=NOT_BUSY):
        self.fs = fs
        self.busy = busy
        self.entries = []
        self.error = ""
        self.open_folder(fs.resolve(path))
        # where to go back to when a remote pane is disconnected
        self.local_path = self.path

    def open_folder(self, path):
        self.path = path
        self.index = 0
        self.scroll = 0
        self.reload()

    def reload(self):
        try:
            with self.busy:
                self.entries = self.fs.list_dir(self.path)
            if self.parent_path():
                self.entries.insert(0, Pane.PARENT)
            self.error = ""
        except ConnectionLost as e:
            self.entries = []
            self.error = "{} - press F5 to reconnect".format(e)
        except FsError as e:
            self.entries = []
            self.error = str(e)
        if self.index >= len(self.entries):
            self.index = max(0, len(self.entries) - 1)

    def switch_fs(self, fs, path):
        self.fs = fs
        self.open_folder(path)

    def on_parent_row(self):
        return bool(self.entries) and self.entries[self.index] is Pane.PARENT

    def selected(self):
        """The selected file/folder, or None (also on the '..' row)."""
        if not self.entries or self.on_parent_row():
            return None
        return self.entries[self.index]

    def selected_full(self):
        sel = self.selected()
        if sel is None:
            return None
        return self.fs.pathmod.join(self.path, sel[0])

    def parent_path(self):
        parent = self.fs.pathmod.dirname(self.path.rstrip("/"))
        if parent and parent != self.path:
            return parent
        return None

    def go_parent(self):
        parent = self.parent_path()
        if parent:
            old_base = self.fs.pathmod.basename(self.path)
            self.path = parent
            self.reload()
            for i, (name, isdir) in enumerate(self.entries):
                if name == old_base:
                    self.index = i
                    break

    def enter(self):
        if self.on_parent_row():
            self.go_parent()
            return
        sel = self.selected()
        if sel and sel[1]:
            self.open_folder(self.fs.pathmod.join(self.path, sel[0]))

    def move(self, delta, page_h):
        if not self.entries:
            return
        self.index = max(0, min(len(self.entries) - 1, self.index + delta))
        if self.index < self.scroll:
            self.scroll = self.index
        if self.index >= self.scroll + page_h:
            self.scroll = self.index - page_h + 1


# ----------------------------------------------------------------------
# Main application
# ----------------------------------------------------------------------

VIEW_NAV = "nav"
VIEW_QUEUE = "queue"
VIEW_VERIFY = "verify"
VIEW_OPTIONS = "options"
VIEW_HELP = "help"
VIEW_LOG = "log"

HELP_TEXT = [
    "mcrsync " + VERSION + " - Midnight-Commander-style rsync queue manager",
    "",
    "GLOBAL KEYS (work in every view)",
    "  ? or H  Show this help",
    "  T       Toggle between NAV view and QUEUE editor view",
    "  V       Open/close the VERIFY view",
    "  G       Open/close the RUN LOG: every copied/created/deleted",
    "          file, conflict, error and connection event of this",
    "          session, with times. It follows new lines; scroll",
    "          with Up/Down/PgUp/PgDn/Home, End follows again.",
    "          Keeps the last 5000 lines (not saved to disk).",
    "  S       Save the current queue (+ rsync options) to a file",
    "          (asks before overwriting an existing file)",
    "  L       Load a queue (+ rsync options) from a file. If the",
    "          file's rsync flags differ from yours, they are shown",
    "          in full and you choose whether to use them. Not",
    "          possible while a run is in progress.",
    "  F8      Open the rsync-options review screen, then run the queue",
    "  P       Pause / resume queue execution (takes effect between",
    "          items, not mid-transfer - rsync itself can't be paused",
    "          mid-file). If the run paused because an SSH",
    "          connection was lost, P logs in again first.",
    "  Q       Quit (asks for confirmation)",
    "",
    "TYPING TEXT (all popups and prompts)",
    "  Fields start with the current value (e.g. the rsync flags or",
    "  queue.json), so you can edit instead of retyping. Enter accepts,",
    "  Esc cancels without changing anything, Backspace deletes, Ctrl-U",
    "  clears the field.",
    "",
    "  The screen needs at least 48 x 15 characters; a smaller terminal",
    "  shows a notice instead (a running queue keeps going).",
    "",
    "  While mcrsync waits for something slow - listing a remote folder,",
    "  checking an rclone remote, a rename - '[ / working ]' shows at the",
    "  right end of the QUEUE box's top border (in other views: of the",
    "  screen's). Keys pressed meanwhile are handled afterwards.",
    "",
    "  When a newer release exists, 'vX.Y.Z available' shows left of that",
    "  spot. 'mcrsync selfupdate' (or 'python3 mcrsync.py selfupdate')",
    "  installs it.",
    "",
    "NAVIGATE VIEW (left = SOURCE pane, right = TARGET pane, bottom = last",
    "5 queued items)",
    "  Tab             Switch active pane between SOURCE (left) and",
    "                  TARGET (right)",
    "  Up / Down       Move selection within the active pane",
    "  Left / Bksp     Go to the parent directory of the active pane",
    "  Right / Enter   Enter the highlighted directory ('..' = parent)",
    "  PgUp / PgDn     Scroll a full page within the active pane",
    "  Home / End      Jump to the first / last item in the active pane",
    "  F5              Refresh both panes from disk (in case files changed",
    "                  outside mcrsync). Selection is kept if the same",
    "                  name still exists. This also happens automatically",
    "                  once a full queue run finishes. On a remote pane",
    "                  whose connection was lost, F5 offers to reconnect.",
    "  C               Queue a copy: selected item in the LEFT (source)",
    "                  pane -> current directory shown in the RIGHT",
    "                  (target) pane. Only works when the LEFT pane is",
    "                  active - if you press C while the RIGHT pane is",
    "                  active the screen will flash twice and nothing",
    "                  is queued.",
    "  F2              Rename the selected file/folder (in either pane).",
    "                  Opens a popup: type the new name, then Tab to move",
    "                  between the text field, [OK], and [Cancel]. Enter",
    "                  activates whichever is focused (so Enter in the",
    "                  text field = OK). Esc always cancels. If a file",
    "                  or folder with the new name already exists, the",
    "                  rename is refused rather than overwriting it.",
    "  Insert          Create a new folder in the active pane's current",
    "                  directory (in either pane). Same popup style as",
    "                  rename: type the name, OK/Cancel/Tab/Enter/Esc.",
    "                  Refuses to create it if something with that name",
    "                  already exists.",
    "  O               Connect the active pane to a remote host over SSH or",
    "                  to an rclone remote (you choose which), or - if it",
    "                  already is remote - disconnect it and go back to the",
    "                  local folder it showed before.",
    "  B               Bookmarks: saved folders, local or remote, shared",
    "                  by both panes. Enter or 1-9 opens one in the active",
    "                  pane, connecting it if needed. A bookmarks the",
    "                  current folder, F2 renames, D deletes. A missing",
    "                  folder opens the nearest one above it.",
    "  1-9             Open bookmark 1-9 in the active pane",
    "",
    "REMOTE (SSH) PANES",
    "  Either pane can show a remote host instead of the local disk, so you",
    "  can push (local SOURCE -> remote TARGET) or pull (remote SOURCE ->",
    "  local TARGET). The other pane must then be local, because rsync",
    "  can't copy directly between two remote hosts.",
    "",
    "  Pressing O and choosing SSH opens a form:",
    "    User          Login name on the remote host",
    "    Host          Host name or IPv4 address (IPv6 isn't supported)",
    "    Port          SSH port, normally 22",
    "    Base path     Folder the pane starts in; blank or ~ = home",
    "                  directory. You can still go above it with Left.",
    "    Key file      Private key file (e.g. ~/.ssh/id_ed25519). Leave it",
    "                  blank to log in with a password.",
    "  The values you enter are remembered (never the password) in",
    "  ~/.mcrsync.yaml and pre-filled next time.",
    "",
    "  After OK the screen switches to the plain terminal for a moment so",
    "  ssh itself can ask for the password (or to confirm an unknown host",
    "  key). mcrsync never sees or stores the password. The login is kept",
    "  open in the background and shared by everything that follows -",
    "  browsing, size estimates and every rsync transfer - so you are asked",
    "  only once per host.",
    "",
    "  A remote pane's title shows [ssh user@host:port]; the queue shows",
    "  remote paths as host:/path. rsync and a POSIX shell are all that",
    "  is needed on the remote host (rsync 3.1 or newer).",
    "",
    "  If the connection is lost:",
    "    - In a pane: it shows the error; press F5 to reconnect.",
    "    - During a run: the run pauses before the next item and the",
    "      status bar says so. Press P to log in again; the interrupted",
    "      item is then retried from the start (rsync skips what was",
    "      already copied).",
    "  Queued items remember their host. When you load a saved queue or",
    "  press F8, mcrsync asks you to log in to any host the queue needs",
    "  that isn't connected yet.",
    "",
    "RCLONE PANES (cloud and object storage)",
    "  A pane can also show a remote from your own rclone config (S3, B2,",
    "  Google Drive, OneDrive, ...). Items to or from it are copied with",
    "  rclone instead of rsync. The other pane can be local or another",
    "  rclone remote, but not an SSH host. Needs rclone 1.60 or newer.",
    "",
    "  Pressing O and choosing rclone lists your remotes (set them up with",
    "  'rclone config' first), then asks for the folder to start in (e.g.",
    "  bucket/photos; blank = top level). mcrsync never sees your cloud",
    "  credentials. An encrypted rclone config works only if",
    "  RCLONE_PASSWORD_COMMAND or RCLONE_CONFIG_PASS is set when mcrsync",
    "  starts - mcrsync never asks for that password.",
    "",
    "  Folders are copied with 'rclone copy', single files with 'rclone",
    "  copyto'; neither deletes anything on the target. Ticking --delete",
    "  on the F8 screen makes folders use 'rclone sync', which also",
    "  deletes target files that aren't in the source. With the default",
    "  --update, newer target files become conflicts, as with rsync.",
    "  Remotes that can't store modification times (some WebDAV/FTP",
    "  servers) compare with upload times instead - you are warned about",
    "  them when connecting and on the F8 screen.",
    "",
    "  Symlinks: with -l/--links (default) a symlink is uploaded as a small",
    "  name.rclonelink file. Without it, symlinks are skipped and listed in",
    "  Verify -> ERRORS. Copies INTO a local folder never use --links, so",
    "  .rclonelink files stay plain files there - a link planted on the",
    "  remote can't make later files land outside the target. For the same",
    "  reason an item is refused if its local target has a symlink where",
    "  the source has a folder. A sync (--delete) into a local folder is",
    "  refused if the target has any symlink to a folder: rclone could",
    "  follow it and delete files outside the target.",
    "  Permissions and owners are only kept with -M/--metadata, and only",
    "  where the backend supports it.",
    "",
    "  On bucket storage (S3, B2, ...) folders only exist once they contain",
    "  a file; folders you create with Insert stay visible for this",
    "  session. Renaming (F2 and conflict resolution) never replaces an",
    "  existing name, but isn't atomic there: a rename is a copy plus a",
    "  delete, so an interrupted one can leave both names, never none.",
    "",
    "  If rclone gives up with a temporary error (network or provider",
    "  trouble), a login/account problem or a provider's transfer limit,",
    "  the run pauses like a lost SSH connection: fix the cause, then",
    "  press P. Other errors make the item SYNC FAIL.",
    "",
    "  Copying a folder into itself is refused for the same remote, aliases",
    "  and local paths; two different remotes that point to the same",
    "  storage in other ways (e.g. a crypt remote) can't be detected.",
    "",
    "QUEUE EDITOR VIEW (single pane: size, files, #, source, dest, status)",
    "  Up / Down       Move selection",
    "  PgUp / PgDn     Scroll a full page",
    "  D  or  Delete   Remove the selected item from the queue (confirms).",
    "                  If a background size estimate was still being",
    "                  calculated for that item, it's cancelled right away.",
    "",
    "  SIZE and FILES columns show the item's total size (e.g. '400.0 M')",
    "  and number of files - everything rsync will review, whether it",
    "  already exists on the target or not (symlinks count as links, not",
    "  as what they point to). Computed in the background so it never",
    "  blocks the UI; the live progress line measures against it:",
    "    ...      Still being estimated - check back in a moment",
    "    nan      Size couldn't be determined (e.g. permission error)",
    "    offline  Remote source that isn't connected yet - estimated",
    "             once you connect (F8 or O)",
    "",
    "  The status bar's far right shows overall progress in this view, and",
    "  in every view while a run is going: '<n>/<total> items, <percent>%",
    "  done | ETA: <duration>'. Percent is the share of",
    "  queued items that are SYNCED OK. The ETA projects how long the",
    "  remaining items will take, based on the average transfer speed",
    "  (bytes/sec) of items that have already finished SYNCED OK in this",
    "  session - it shows 'nan' until at least one item has completed and",
    "  every remaining item's size estimate is known.",
    "",
    "  Status values you'll see in the queue and bottom-tail list:",
    "    NOT SYNCED    Queued, not yet attempted",
    "    IN PROGRESS   Currently being copied",
    "    SYNCED OK     Completed cleanly",
    "    SYNC FAIL     rsync or rclone returned a hard error",
    "    INCOMPLETE    Copy ran, but some files were skipped because the",
    "                  target copy was newer than the source (needs your",
    "                  attention in the Verify view)",
    "    DRY RUN OK    A dry run (-n) found nothing wrong. Nothing was",
    "                  copied, so a real run will still process it.",
    "    DRY RUN INCOMPLETE",
    "                  A dry run found files that are newer in the target",
    "                  (listed in Verify -> SKIPPED, but can't be resolved",
    "                  until a real run)",
    "",
    "RUN OPTIONS SCREEN (opened by F8, before anything actually runs)",
    "  rsync and rclone each have their own flags, and each set applies to",
    "  every queue item copied with that tool. Only the tools the queue",
    "  uses are shown.",
    "  Up / Down       Move through the flag checkboxes",
    "  Space           Switch the selected flag on/off. Flags without a",
    "                  checkbox (e.g. --exclude=...) are listed under",
    "                  'Other flags' and kept as they are.",
    "  E               Edit all flags of the selected tool as a free-form",
    "                  string (shell-style quoting is respected). Values",
    "                  must be attached with '=' (--exclude='*.tmp'): a",
    "                  separate word would be one more path to copy.",
    "  D               Restore the selected tool's built-in defaults",
    "                  (rsync: -a -u --stats --itemize-changes,",
    "                  rclone: --update --links)",
    "  Enter           Confirm and start the run. If -u/--update is not",
    "                  in the flags you'll get an extra confirmation,",
    "                  because that flag is what protects newer target",
    "                  files from being overwritten.",
    "  Esc             Cancel, go back without starting anything",
    "",
    "  Below the checkboxes is the exact command the first item of each",
    "  tool will get - the other items differ only in their paths.",
    "",
    "  Note: --stats fills in the Verify view's counts; removing it is fine",
    "  for rsync itself, but Verify will have less detail to show. The",
    "  per-file list in Verify works with or without --itemize-changes.",
    "  mcrsync always adds --info=skip1,progress2,name2 and its own",
    "  --out-format (files skipped because the target is newer, live",
    "  progress, and one line per checked file with its size) and, for",
    "  remote items, -s -e 'ssh ...'.",
    "  For rclone, mcrsync adds --use-json-log --log-level DEBUG --stats 1s",
    "  (its log is how mcrsync sees progress and conflicts), so rclone",
    "  flags that change the log or progress output (-v, -q, -P,",
    "  --log-file, --stats..., ...) are refused.",
    "",
    "LIVE PROGRESS (bottom of NAV, QUEUE and RUN LOG views while running)",
    "  > #3 photos | copying | reviewed 812.4M of 1.7G (47%) |",
    "    1,234 of 8,000 files | copied 4 files, 1.1M | 301.3K/s | 4m 05s |",
    "    now: 2019/img_0123.jpg",
    "  'reviewed' counts every file rsync has checked - copied, or already",
    "  up to date on the target - against the item's SIZE estimate, so the",
    "  percentage reaches 100% (a bit less if there are conflicts: rsync",
    "  doesn't report the size of files it skips because the target is",
    "  newer; they are counted as files and shown as 'N conflicts').",
    "  'copied' is what was actually transferred.",
    "  Phases of each item:",
    "    preparing   Checking the target, creating its folder",
    "    scanning    rsync builds the file list - on big folders over SSH",
    "                this can take minutes before anything is copied",
    "    copying     Files are being checked and transferred; 'now:' is",
    "                the file being copied",
    "    deleting    (red) only with --delete... flags: rsync or rclone",
    "                is removing files from the target",
    "    finishing   All files checked; rsync sets folder times and",
    "                writes its statistics",
    "  A dry run shows 'DRY RUN' and 'would copy'.",
    "",
    "  DRY RUN: add -n (or --dry-run) to see what WOULD happen without",
    "  changing anything. The screen then shows [DRY RUN], no folders are",
    "  created, items end up as DRY RUN OK / DRY RUN INCOMPLETE instead of",
    "  SYNCED OK, and Verify lists the files that would be copied. Remove",
    "  -n and press F8 again to do the real copy.",
    "",
    "VERIFY VIEW (reviewing what happened after a run)",
    "  Tab             Cycle through tabs: COPIED / SKIPPED / RENAMED /",
    "                  ERRORS",
    "  Up / Down       Move selection",
    "  PgUp / PgDn     Scroll a full page",
    "  Enter           On a row in the SKIPPED tab, resolve that",
    "                  conflicting file: choose Skip / Rename existing /",
    "                  Autorename incoming. Once every conflict on an",
    "                  item is resolved it flips back to SYNCED OK.",
    "  Esc  or  V      Back to the previous view",
    "",
    "  Tab meanings:",
    "    COPIED        Files rsync actually transferred",
    "    SKIPPED       Unresolved conflicts (target was newer than",
    "                  source) - these need you to pick a resolution",
    "    RENAMED       Conflicts you've already resolved with Rename or",
    "                  Autorename, and what they were renamed to",
    "    ERRORS        Hard failures (rsync or rclone errors, skipped",
    "                  symlinks, etc.)",
    "",
    "SAFETY NOTES",
    "  - The default rsync flags include -u (--update), so rsync itself",
    "    will never overwrite a target file that is newer than the",
    "    source.",
    "  - Every target file rsync skips because it is newer than the",
    "    source is reported as a conflict (INCOMPLETE status) so you can",
    "    decide what to do with it, instead of it just silently vanishing",
    "    from the run. If you remove -u, rsync overwrites such files and",
    "    no conflicts are reported.",
    "  - Loading a saved queue always resets any leftover 'IN PROGRESS'",
    "    and DRY RUN items back to 'NOT SYNCED', so resuming with F8 is",
    "    safe. Saved queues contain host names and key file paths, never",
    "    passwords. A queue file with invalid paths or host details is",
    "    refused as a whole.",
    "  - Items rsync would handle in a surprising way are refused with",
    "    SYNC FAIL instead of run: a file whose target name is an existing",
    "    folder (rsync would copy INTO that folder), a folder whose target",
    "    name is an existing file, and a folder copied into itself.",
    "  - Resolving a conflict first re-checks that the target is still",
    "    newer than the source; if anything changed since the run, nothing",
    "    is done. Rename and Autorename never overwrite an existing file,",
    "    and if the copy fails after renaming, the existing file is put",
    "    back. These copies use only -t, not your custom rsync flags.",
    "  - F2 rename and Insert never replace an existing file or folder,",
    "    locally or remotely.",
    "  - The F8 screen always shows all flags and marks (CAUTION) those",
    "    that can delete or overwrite data, write outside the target or",
    "    run commands (--delete..., --remove-source-files, --inplace,",
    "    --log-file, -K, -e, rclone's ...-command flags, ...). Flags",
    "    mcrsync doesn't know - e.g. rclone backend flags, which can change",
    "    where data goes - are marked CHECK. Loading a queue file shows",
    "    both before you decide whether to use its flags.",
    "  - File names in rsync's or rclone's output that would lead out of",
    "    the item's folder ('..', absolute paths) are ignored and listed in",
    "    Verify -> ERRORS.",
    "  - The item being copied can't be deleted from the queue; other",
    "    items can, and the run picks up the change.",
    "",
    "Press Esc, H, or ? to close this help.",
]

# HELP_TEXT key column: keys at column 2 (up to 14 chars), then 2+ spaces and
# the description
HELP_KEY_LINE_RE = re.compile(r"^  (\S(?:.{0,12}\S)?) {2,}\S")
# status bar hints: "key:label" separated by two spaces
HINT_KEY_RE = re.compile(r"(?:^|  )([^ :]+):")


def help_key_spans(line):
    """(start, end) of each key in a HELP_TEXT line's key column."""
    m = HELP_KEY_LINE_RE.match(line)
    if not m:
        return []
    return [k.span() for k in re.finditer(r"[^ ]+", line[:m.end(1)])
            if k.group() not in ("or", "/")]

VERIFY_TABS = ["COPIED", "SKIPPED", "RENAMED", "ERRORS"]


class App:
    def __init__(self, stdscr, start_path):
        self.stdscr = stdscr
        self.view = VIEW_NAV
        self.registry = ConnectionRegistry()
        self.runner = Transfers(self.registry)
        self.settings = Settings()
        self.settings.load()
        self.bookmarks = Bookmarks(self.settings)
        self.busy = BusyIndicator(self.busy_cell)
        self.registry.busy = self.busy
        self.update_notice = UpdateNotice()
        self.left = Pane(start_path, LOCAL_FS, self.busy)
        self.right = Pane(start_path, LOCAL_FS, self.busy)
        self.active = "left"
        self.queue = []                 # list[QueueItem]
        self.queue_index = 0
        self.log_lines = []             # running log, used for bottom pane / run log
        self.paused = False
        self.running = False
        self.run_thread = None
        # Threads: the run worker writes item results/status/progress, the
        # size worker only size_*, and the main thread only reads those while
        # a run is going (attribute writes are atomic under the GIL). Only
        # log_lines, appended from all threads, needs this lock.
        self.lock = threading.Lock()
        self.verify_tab = 0
        self.verify_index = 0
        self.flash_count = 0
        self.rsync_opts = list(DEFAULT_RSYNC_OPTS)
        self.rclone_opts = list(DEFAULT_RCLONE_OPTS)
        self.options_index = 0
        self.help_scroll = 0
        self.prev_view = VIEW_NAV
        self.pending_pane_refresh = False
        # Set by the run worker when it paused because these ConnSpecs are
        # down; the main thread reconnects them when the user presses p.
        self.reconnect_needed = []
        self.run_dry = False
        self.log_scroll = None    # None = follow the end of the log
        self.log_dropped = False

        # Background, lazy queue-item size estimation. A single worker
        # thread processes items sequentially so there's exactly one place
        # that ever mutates item.size_bytes/size_state, which keeps this
        # simple and race-free; cancellation of an in-flight/queued item is
        # handled via that item's own cancel_event (see schedule_size_calc
        # and QueueItem.cancel_event).
        self.size_calc_queue = pyqueue.Queue()
        self.size_calc_thread = threading.Thread(target=self._size_calc_worker)
        self.size_calc_thread.daemon = True
        self.size_calc_thread.start()

        curses.curs_set(0)
        curses.use_default_colors()
        self.inactive_frame_attr = curses.A_DIM
        # many 8-color terminals show bold red as bright red
        self.bright_red_attr = curses.color_pair(Color.RED) | curses.A_BOLD
        try:
            curses.start_color()
            # Bright variants when available; bold black would render as gray
            bright = curses.COLORS >= 16
            active_bg = curses.COLOR_WHITE + 8 if bright else curses.COLOR_WHITE
            curses.init_pair(Color.ACTIVE_HEADER, curses.COLOR_BLACK, active_bg)
            curses.init_pair(Color.YELLOW, curses.COLOR_YELLOW, -1)
            curses.init_pair(Color.GREEN, curses.COLOR_GREEN, -1)
            curses.init_pair(Color.RED, curses.COLOR_RED, -1)
            curses.init_pair(Color.MAGENTA, curses.COLOR_MAGENTA, -1)
            curses.init_pair(Color.STATUSBAR, curses.COLOR_WHITE, curses.COLOR_BLUE)
            curses.init_pair(Color.CYAN, curses.COLOR_CYAN, -1)
            curses.init_pair(Color.ACTIVE_FRAME, active_bg, -1)
            if bright:
                curses.init_pair(Color.GRAY, curses.COLOR_BLACK + 8, -1)
                self.inactive_frame_attr = curses.color_pair(Color.GRAY)
                curses.init_pair(Color.BRIGHT_RED, curses.COLOR_RED + 8, -1)
                self.bright_red_attr = curses.color_pair(Color.BRIGHT_RED)
        except curses.error:
            pass

    # ---------------- logging ----------------
    LOG_MAX = 5000

    def log(self, text):
        with self.lock:
            self.log_lines.append(time.strftime("%H:%M:%S ") + text)
            if len(self.log_lines) > App.LOG_MAX:
                self.log_lines = self.log_lines[-App.LOG_MAX:]
                self.log_dropped = True

    def running_item(self):
        for it in self.queue:
            if it.status == STATUS_RUNNING and it.progress is not None:
                return it
        return None

    def progress_text(self):
        """Live line for the NAV/QUEUE views, or "" when nothing runs."""
        if not self.running:
            return ""
        if self.paused and self.reconnect_needed:
            return "Waiting: connection lost - press P to log in again"
        it = self.running_item()
        if it is None:
            return "Paused between items - press P to resume" if self.paused else "Starting next item..."
        n = self.queue.index(it) + 1 if it in self.queue else 0
        return "#{} {} | {}".format(n, it.src_display(), it.progress.describe())

    def last_queue_tail(self, n=5):
        return self.queue[-n:]

    # ---------------- queue ops ----------------
    def add_to_queue(self, src, dst, is_dir=True, src_conn=None, dst_conn=None):
        item = QueueItem(src, dst, is_dir, src_conn, dst_conn)
        self.queue.append(item)
        self.schedule_size_calc(item)
        return item

    def schedule_size_calc(self, item):
        """Enqueue a background size estimate for item, unless one is
        already known or already in flight."""
        if item.has_size():
            item.size_state = "done"
            return
        if item.size_state in ("pending", "computing"):
            return
        item.size_state = "pending"
        self.size_calc_queue.put(item)

    def reschedule_offline_sizes(self):
        """Call after opening connections."""
        for item in self.queue:
            if item.size_state == "offline":
                self.schedule_size_calc(item)

    def _size_calc_worker(self):
        while True:
            item = self.size_calc_queue.get()
            if item.cancel_event.is_set():
                # Deleted before we even got to it - nothing to do.
                continue
            if not self.registry.is_open(item.src_conn):
                item.size_state = "offline"
                continue
            item.size_state = "computing"
            fs = self.registry.fs_for(item.src_conn)
            try:
                size = fs.size(item.src, item.is_dir, item.cancel_event)
            except Exception as e:
                # this thread must survive anything, or no size is ever
                # estimated again
                self.log("Size estimate failed for {}: {}".format(item.src_display(), e))
                size = None
            if item.cancel_event.is_set():
                # Deleted while we were scanning - discard the result.
                continue
            if size is None:
                item.size_state = "error"
            else:
                item.size_bytes, item.size_files = size
                item.size_state = "done"

    def compute_progress_and_eta(self):
        """Returns (percent_done, eta_seconds_or_None) for the queue, or
        (None, None) if the queue is empty. percent_done is item-count
        based (# SYNCED OK / total). eta_seconds is byte-rate based, using
        the average bytes/sec of previously-completed SYNCED OK items
        applied to the estimated remaining bytes; None (displayed as
        'nan') if that can't be computed yet."""
        total = len(self.queue)
        if total == 0:
            return (None, None)
        ok_count = sum(1 for it in self.queue if it.status == STATUS_OK)
        percent = 100.0 * ok_count / total

        rates = []
        for it in self.queue:
            if (it.status == STATUS_OK and it.size_bytes and
                    it.transfer_seconds and it.transfer_seconds > 0):
                rates.append(it.size_bytes / it.transfer_seconds)
        if not rates:
            return (percent, None)
        avg_rate = sum(rates) / len(rates)
        if avg_rate <= 0:
            return (percent, None)

        remaining = [it for it in self.queue if it.status != STATUS_OK]
        if not remaining:
            return (percent, 0.0)
        if any(it.size_bytes is None for it in remaining):
            return (percent, None)
        remaining_bytes = sum(it.size_bytes for it in remaining)
        # the running item's already reviewed part is done
        running = self.running_item() if self.running else None
        if running is not None:
            remaining_bytes -= min(running.size_bytes, running.progress.reviewed_bytes)
        return (percent, remaining_bytes / avg_rate)

    def opts_for(self, tool):
        return self.rclone_opts if tool == TOOL_RCLONE else self.rsync_opts

    def set_opts(self, tool, opts):
        if tool == TOOL_RCLONE:
            self.rclone_opts = opts
        else:
            self.rsync_opts = opts

    def save_queue(self, path):
        QueueFile.save(path, self.queue, self.rsync_opts, self.rclone_opts)

    def confirm_file_opts(self, flags, opts):
        """Ask whether to use a queue file's flags for flags.TOOL. Returns
        opts, or None to keep the current ones."""
        if opts is None or opts == self.opts_for(flags.TOOL):
            return None
        # Flags in a file are someone else's choice - show them in full,
        # quoted, so a token with spaces can't disguise itself.
        shown = " ".join(shlex.quote(o) for o in opts)
        question = "This queue file also sets the {} flags: {} . ".format(flags.TOOL, shown)
        risky = flags.risky(opts)
        if risky:
            question += ("WARNING - these can delete or overwrite data, write outside the "
                         "target or run commands: {} . ".format(
                             " ".join(shlex.quote(o) for o in risky)))
        unchecked = flags.unchecked(opts)
        if unchecked:
            question += ("CHECK - mcrsync doesn't know what these do (they could e.g. send "
                         "data elsewhere): {} . ".format(
                             " ".join(shlex.quote(o) for o in unchecked)))
        question += "Use these flags? (N = keep your current flags)"
        attr = self.bright_red_attr if risky or unchecked else 0
        return opts if confirm(self.stdscr, question, attr) else None

    def load_queue_interactive(self, path):
        try:
            items, rsync_opts, rclone_opts = QueueFile.read(path)
        except KeyError as e:
            message(self.stdscr, "Load failed: an item is missing the field {}. "
                    "Nothing was loaded.".format(e))
            return
        except (OSError, ValueError) as e:
            message(self.stdscr, "Load failed: {}. Nothing was loaded.".format(e))
            return
        use_rsync = self.confirm_file_opts(RsyncFlags, rsync_opts)
        use_rclone = self.confirm_file_opts(RcloneFlags, rclone_opts)
        self.load_queue(items, use_rsync, use_rclone)
        hosts = sorted(set(s.key() for it in self.queue for s in it.conn_specs()))
        extra = ""
        if hosts:
            extra = (". It uses remote(s) {} - you'll be asked to log in "
                     "when you press F8.".format(", ".join(hosts)))
        message(self.stdscr, "Loaded {} item(s) from {}{}".format(
            len(self.queue), path, extra))

    def load_queue(self, items, rsync_opts=None, rclone_opts=None):
        self.queue = items
        self.queue_index = 0
        if rsync_opts is not None:
            self.rsync_opts = rsync_opts
        if rclone_opts is not None:
            self.rclone_opts = rclone_opts
        for item in self.queue:
            self.schedule_size_calc(item)

    # ---------------- execution ----------------
    def refresh_panes(self):
        """Reload both nav panes from disk, keeping the current selection
        (by name) where it still exists."""
        for pane in (self.left, self.right):
            sel = pane.selected()
            sel_name = sel[0] if sel else None
            pane.reload()
            if sel_name is not None:
                for i, (name, _) in enumerate(pane.entries):
                    if name == sel_name:
                        pane.index = i
                        break

    def start_run(self):
        if self.running:
            return
        self.running = True
        self.paused = False
        self.reconnect_needed = []
        self.run_dry = any(FLAG_SETS[tool].is_dry_run(self.opts_for(tool))
                           for tool in self.queue_tools())
        self.run_thread = threading.Thread(target=self._run_worker)
        self.run_thread.daemon = True
        self.run_thread.start()

    def next_run_item(self, seen):
        """Next queue item not yet handled in this run. Looked up fresh each
        time (by identity, not index), so deleting items during a run can't
        make it skip one, and items added during the run are included."""
        # seen maps id -> item: holding the item keeps its id from being
        # reused by a newly added one
        for idx, item in enumerate(self.queue):
            if id(item) not in seen:
                seen[id(item)] = item
                return idx, item
        return None, None

    def _run_worker(self):
        seen = {}
        while True:
            idx, item = self.next_run_item(seen)
            if item is None:
                break
            if item.status in (STATUS_OK,):
                continue
            while True:
                while self.paused:
                    time.sleep(0.3)
                if item.cancel_event.is_set():
                    break   # deleted from the queue while we were paused
                # Reconnecting needs the terminal, which only the main thread
                # may touch - so pause and let the user trigger it with P.
                dead = self.registry.dead_specs(item.conn_specs())
                if dead:
                    self.reconnect_needed = dead
                    self.paused = True
                    self.log("CONNECTION LOST: {} - press P to log in again and resume".format(
                        self.registry.describe(dead)))
                    continue
                self.queue_index = idx
                self.log("Starting: {} -> {}".format(item.src_display(), item.dst_display()))
                if self.runner.run_item(item, self.log, self.opts_for(item.tool())):
                    break
            self.log("Finished [{}]: {}".format(item.status_label(), item.src_display()))
        self.running = False
        # Panes/curses calls must happen on the main thread - just flag it
        # and let the main loop pick it up and reload on its next tick.
        self.pending_pane_refresh = True

    def toggle_pause(self):
        if not self.running:
            return
        if self.paused and self.reconnect_needed:
            failed = self.registry.open_interactive(self.stdscr, self.reconnect_needed)
            if failed:
                message(self.stdscr, "Still not connected to {} - the run stays paused. "
                        "Press P to try again.".format(self.registry.describe(failed)))
                return
            self.reconnect_needed = []
            self.reschedule_offline_sizes()
        self.paused = not self.paused
        self.log("PAUSED" if self.paused else "RESUMED")

    def pending_items(self):
        return [it for it in self.queue if it.status != STATUS_OK]

    def queue_tools(self):
        """Tools the unfinished items use, rsync first."""
        tools = set(it.tool() for it in self.pending_items())
        return [t for t in (TOOL_RSYNC, TOOL_RCLONE) if t in tools]

    def ensure_queue_connections(self, kinds=("ssh", "rclone")):
        """Log in to every remote of these kinds that unfinished queue
        items need. Returns False (after telling the user) if any failed."""
        specs = []
        for it in self.pending_items():
            specs += [s for s in it.conn_specs() if s.kind in kinds]
        dead = self.registry.dead_specs(specs)
        if not dead:
            return True
        failed = self.registry.open_interactive(self.stdscr, dead)
        self.reschedule_offline_sizes()
        if failed:
            message(self.stdscr, "Could not connect to {} - the run was not started.".format(
                self.registry.describe(failed)))
            return False
        return True

    # ---------------- remote panes ----------------
    # shown under the SSH / rclone choice
    RCLONE_SETUP = ("rclone setup", (
        "Install:   sudo -v ; curl https://rclone.org/install.sh | sudo bash",
        "Configure: rclone config",
    ))
    def pane_name(self, pane):
        return "SOURCE" if pane is self.left else "TARGET"

    def toggle_remote(self, pane):
        if pane.fs.is_remote:
            self.disconnect_pane(pane)
        else:
            self.connect_pane(pane)

    def other_pane(self, pane):
        return self.right if pane is self.left else self.left

    def pane_rule_problem(self, pane, kind=None):
        """Why pane can't be connected (to kind "ssh"/"rclone", None = to
        anything), or "". Pane rules: an SSH pane needs a local pane
        opposite (rsync can't copy between two remote hosts, and rclone
        can't use mcrsync's SSH login); an rclone pane can face a local or
        another rclone pane."""
        other = self.other_pane(pane)
        if other.fs.kind == "ssh":
            return ("The {0} pane is connected to {1} over SSH, so this pane must "
                    "stay local: rsync can't copy between two remote hosts, and rclone can't "
                    "use mcrsync's SSH login. Press O in the {0} pane to disconnect it "
                    "first.".format(self.pane_name(other), other.fs.spec.key()))
        if kind == "ssh" and other.fs.kind == "rclone":
            return ("The {0} pane shows the rclone remote {1}, so this pane can't use SSH: "
                    "rclone can't use mcrsync's SSH login. Press O in the {0} pane to "
                    "disconnect it first.".format(self.pane_name(other), other.fs.spec.key()))
        return ""

    def connect_pane(self, pane):
        other = self.other_pane(pane)
        problem = self.pane_rule_problem(pane)
        if problem:
            message(self.stdscr, problem)
            return
        if self.settings.error:
            message(self.stdscr, "Could not read saved connection settings ({}). The form "
                    "starts empty and won't be saved until the file is fixed.".format(
                        self.settings.error))
        if other.fs.kind == "rclone":
            # SSH isn't possible opposite an rclone pane
            self.connect_rclone_pane(pane)
            return
        last_kind = str(self.settings.section("last_connect").get("kind", "ssh"))
        choice = pick_dialog(self.stdscr, "Connect {} pane".format(self.pane_name(pane)),
                             "Connect this pane to:",
                             ["SSH host (copies with rsync)",
                              "rclone remote (copies with rclone)"],
                             1 if last_kind == "rclone" else 0, App.RCLONE_SETUP)
        if choice is None:
            return
        kind = "rclone" if choice == 1 else "ssh"
        # saved with the connection's own entries once it works
        self.settings.set_section("last_connect", OrderedDict([("kind", kind)]))
        if kind == "rclone":
            self.connect_rclone_pane(pane)
        else:
            self.connect_ssh_pane(pane)

    def save_connect_settings(self, name, values):
        """Save the connect form's entries (section name) and the chosen
        connection kind. Remembered for this session even if the file
        can't be saved."""
        sections = [(name, values),
                    ("last_connect", self.settings.section("last_connect"))]

        def change(data):
            for section, entries in sections:
                if entries:
                    data[section] = entries

        if not self.settings.error:
            err = self.settings.update(change)
            if err:
                self.log(err)
        for section, entries in sections:
            if entries:
                self.settings.set_section(section, entries)

    def connect_rclone_pane(self, pane):
        try:
            with self.busy:
                names = RcloneCli.list_remotes()
        except FsError as e:
            message(self.stdscr, "Can't use rclone: {}".format(e))
            return
        usable = [n for n in names if RcloneSpec.NAME_RE.match(n)]
        if not usable:
            message(self.stdscr, "No usable rclone remotes{}. Set one up with 'rclone config' "
                    "first; mcrsync supports remote names made of letters, digits, _ and "
                    "-.".format(" (found: " + ", ".join(names) + ")" if names else ""))
            return
        last = self.settings.section("last_rclone")
        last_remote = str(last.get("remote", ""))
        label = "Remote from your rclone config:"
        if len(usable) < len(names):
            label = "Remote (names with other characters than letters, digits, _ - are not shown):"
        idx = pick_dialog(self.stdscr, "Connect {} pane to rclone".format(self.pane_name(pane)),
                          label, [n + ":" for n in usable],
                          usable.index(last_remote) if last_remote in usable else 0)
        if idx is None:
            return
        spec = RcloneSpec(usable[idx])
        if not self.open_connection(spec):
            return
        fs = self.registry.fs_for(spec)
        value = str(last.get("path", "")) if spec.remote == last_remote else ""
        error = ""
        while True:
            result = form_dialog(self.stdscr, "Connect {} pane to {}:".format(
                self.pane_name(pane), spec.remote), [
                    ("Start path", value, "Folder to start in, e.g. bucket/photos. "
                                          "Blank = top level."),
                ], error)
            if result is None:
                return
            value = result[0].strip()
            try:
                with self.busy:
                    path = fs.resolve(value)
                break
            except ConnectionLost as e:
                message(self.stdscr, str(e))
                return
            except FsError as e:
                error = str(e)[:1].upper() + str(e)[1:]
        self.save_connect_settings("last_rclone", OrderedDict([
            ("remote", spec.remote),
            ("path", value),
        ]))
        self.show_connected(pane, fs, path)

    def show_connected(self, pane, fs, path):
        """Switch pane to the newly connected fs."""
        if not pane.fs.is_remote:
            pane.local_path = pane.path
        pane.switch_fs(fs, path)
        self.reschedule_offline_sizes()
        self.log("Connected {} pane to {}".format(self.pane_name(pane), fs.spec.key()))
        if fs.kind == "rclone" and not fs.remote.modtime_ok:
            message(self.stdscr, "Note: {} can't store modification times. rclone's --update "
                    "then compares with upload times, so 'newer on target' (and the conflicts "
                    "it finds) isn't reliable for this remote. The F8 screen repeats this "
                    "warning.".format(fs.spec.key()))

    def connect_ssh_pane(self, pane):
        last = self.settings.section("last_remote")
        try:
            local_user = getpass.getuser()
        except Exception:
            # e.g. no passwd entry inside a chroot
            local_user = ""
        values = [str(last.get("user", local_user)),
                  str(last.get("host", "")),
                  str(last.get("port", 22)),
                  str(last.get("base_path", "")),
                  str(last.get("keyfile", ""))]
        error = ""
        while True:
            fields = [
                ("User", values[0], "Login name on the remote host."),
                ("Host", values[1], "Host name or IPv4 address."),
                ("Port", values[2], "SSH port - normally 22."),
                ("Base path", values[3], "Folder to start in. Blank or ~ = home directory."),
                ("Key file", values[4], "Private key file. Leave blank to log in with a password."),
            ]
            result = form_dialog(self.stdscr, "Connect {} pane via SSH".format(
                self.pane_name(pane)), fields, error)
            if result is None:
                return
            values = [v.strip() for v in result]
            spec, error = self.validate_remote_form(values)
            if not error:
                break

        self.save_connect_settings("last_remote", OrderedDict([
            ("user", spec.user),
            ("host", spec.host),
            ("port", spec.port),
            ("base_path", values[3]),
            ("keyfile", spec.keyfile),
        ]))

        if not self.open_connection(spec):
            return
        fs = self.registry.fs_for(spec)
        try:
            with self.busy:
                path = fs.resolve(values[3])
        except FsError as e:
            try:
                with self.busy:
                    path = fs.resolve("")
            except FsError as e2:
                message(self.stdscr, "Connected, but can't list the remote home directory: "
                        "{}".format(e2))
                return
            message(self.stdscr, "Base path '{}' can't be opened ({}) - showing the home "
                    "directory instead.".format(values[3], e))
        self.show_connected(pane, fs, path)

    def validate_remote_form(self, values):
        user, host, port, base, keyfile = values
        return ConnSpec.from_form(user, host, port, keyfile)

    def disconnect_pane(self, pane):
        question = "Disconnect the {} pane from {} and go back to {}?".format(
            self.pane_name(pane), pane.fs.spec.key(), pane.local_path)
        if not self.may_leave_connection(pane, question):
            return
        self.close_pane_connection(pane)
        pane.switch_fs(LOCAL_FS, pane.local_path)

    def may_leave_connection(self, pane, question=None):
        """Whether pane may leave its connection. Refused while the run uses
        it; question is asked first, if given or if unfinished queue items
        use it."""
        spec = pane.fs.spec
        users = [it for it in self.queue
                 if it.status != STATUS_OK and
                 spec.key() in [s.key() for s in it.conn_specs()]]
        if users and self.running:
            message(self.stdscr, "The running queue uses {} - wait for it to finish "
                    "before disconnecting.".format(spec.key()))
            return False
        if users:
            note = ("{} unfinished queue item(s) use this host; you'll be asked to log in "
                    "again when you press F8.".format(len(users)))
            question = "{} {}".format(
                question or "Leave {}?".format(spec.key()), note)
        if question is None:
            return True
        return confirm(self.stdscr, question, curses.color_pair(Color.YELLOW))

    def close_pane_connection(self, pane):
        """Close pane's connection, unless the other pane still uses it."""
        spec = pane.fs.spec
        other = self.other_pane(pane).fs.spec
        if other is None or other.key() != spec.key():
            self.registry.close(spec)
        self.log("Disconnected {} pane from {}".format(self.pane_name(pane), spec.key()))

    def open_connection(self, spec):
        """Log in to / check spec if needed. Returns False (after telling
        the user) if it failed."""
        if spec.kind == "rclone":
            with self.busy:
                err = self.registry.open_rclone(spec)
            if err:
                message(self.stdscr, "Can't use {}: {}".format(spec.key(), err))
                return False
            return True
        if self.registry.is_open(spec):
            return True
        # ssh tells why in the plain terminal
        return not self.registry.open_interactive(self.stdscr, [spec])

    # ---------------- bookmarks ----------------
    def open_bookmarks(self, pane):
        index = None
        while True:
            self.bookmarks.load()
            if self.bookmarks.error:
                message(self.stdscr, "Can't use bookmarks: {}".format(self.bookmarks.error))
                return
            items = self.bookmarks.items
            if index is None:
                current = self.bookmarks.find(pane.fs.spec, pane.path)
                index = items.index(current) if current else 0
            # repaint what earlier popups and messages covered
            self.draw()
            dialog = BookmarkDialog(self.stdscr, items, index)
            choice = dialog.run()
            if choice is None:
                return
            index = dialog.index
            action, bm = choice
            if action == "go":
                self.draw()
                self.jump_to_bookmark(pane, bm)
                return
            if action == "add":
                number = self.add_bookmark(pane)
                if number is not None:
                    added = self.bookmarks.get(number)
                    index = self.bookmarks.items.index(added) if added else index
            elif action == "rename":
                self.rename_bookmark(bm)
            elif action == "delete":
                self.delete_bookmark(bm)

    def quick_jump(self, pane, number):
        self.bookmarks.load()
        if self.bookmarks.error:
            message(self.stdscr, "Can't use bookmarks: {}".format(self.bookmarks.error))
            return
        bm = self.bookmarks.get(number)
        if bm is None:
            curses.flash()
            return
        self.jump_to_bookmark(pane, bm)

    def ask_bookmark_name(self, title, value, hint):
        error = ""
        while True:
            result = form_dialog(self.stdscr, title, [("Name", value, hint)], error)
            if result is None:
                return None
            value = result[0].strip()
            if value:
                return value
            error = "The name can't be empty."

    def add_bookmark(self, pane):
        """Returns the new bookmark's number, or None."""
        spec, path = pane.fs.spec, pane.path
        existing = self.bookmarks.find(spec, path)
        if existing:
            message(self.stdscr, "Already bookmarked as [{}] {}.".format(
                existing.key, existing.name))
            return None
        name = self.ask_bookmark_name(
            "Add bookmark", Bookmark.suggest_name(spec, path),
            "For {}".format(pane.fs.display_path(path)))
        if name is None:
            return None
        number, err = self.bookmarks.add(name, spec, path)
        if err:
            message(self.stdscr, err)
            return None
        note = ""
        if number > Bookmarks.QUICK_MAX:
            note = " (no quick-jump key - delete one of 1-{} to get one)".format(
                Bookmarks.QUICK_MAX)
        self.log("Bookmarked [{}] {}{}".format(number, name, note))
        return number

    def rename_bookmark(self, bm):
        if bm.error:
            message(self.stdscr, "Bookmark [{}] is invalid ({}) - it can only be deleted "
                    "here.".format(bm.key, bm.error))
            return
        name = self.ask_bookmark_name("Rename bookmark [{}]".format(bm.key), bm.name,
                                      "For {}".format(bm.display_location()))
        if name is None or name == bm.name:
            return
        err = self.bookmarks.rename(bm.key, name)
        if err:
            message(self.stdscr, err)
            return
        self.log("Renamed bookmark [{}] to {}".format(bm.key, name))

    def delete_bookmark(self, bm):
        label = bm.name if not bm.error else "(invalid)"
        if not confirm(self.stdscr, "Delete bookmark [{}] {}?".format(bm.key, label),
                       curses.color_pair(Color.YELLOW)):
            return
        err = self.bookmarks.delete(bm.key)
        if err:
            message(self.stdscr, err)
            return
        self.log("Deleted bookmark [{}] {}".format(bm.key, label))

    def jump_to_bookmark(self, pane, bm):
        """Show bm's folder in pane, connecting it first if needed. The
        pane's old connection is left only once the new one works."""
        if bm.error:
            message(self.stdscr, "Bookmark [{}] can't be used: {}.".format(bm.key, bm.error))
            return
        spec = bm.spec
        old = pane.fs.spec
        same = (old.key() if old else None) == (spec.key() if spec else None)
        if not same:
            problem = self.pane_rule_problem(pane, spec.kind) if spec else ""
            if problem:
                message(self.stdscr, problem)
                return
            if old is not None and not self.may_leave_connection(pane):
                return
            if spec is not None and not self.open_connection(spec):
                return
        fs = pane.fs if same else self.registry.fs_for(spec)
        try:
            with self.busy:
                path, exact = fs.nearest_folder(bm.path)
        except FsError as e:
            message(self.stdscr, "Can't open bookmark [{}] {}: {}".format(bm.key, bm.name, e))
            return
        if not same and old is not None:
            self.close_pane_connection(pane)
        if same or spec is None:
            pane.switch_fs(fs, path)
        else:
            self.show_connected(pane, fs, path)
        if not exact:
            message(self.stdscr, "Folder {} no longer exists - showing {}.".format(
                bm.display_location(), fs.display_path(path)))

    def reconnect_panes_if_needed(self):
        for pane in (self.left, self.right):
            spec = pane.fs.spec
            if spec is None or self.registry.is_open(spec):
                continue
            if spec.kind == "rclone":
                question = "{} isn't usable right now ({}). Try again?".format(
                    spec.key(), self.registry.problem(spec) or "not checked yet")
            else:
                question = "The connection to {} is closed. Log in again?".format(spec.key())
            if confirm(self.stdscr, question):
                failed = self.registry.open_interactive(self.stdscr, [spec])
                if failed and spec.kind == "rclone":
                    message(self.stdscr, "Still not usable: " + self.registry.describe(failed))
                self.reschedule_offline_sizes()

    # ---------------- verify data ----------------
    def verify_rows(self, tab_name):
        rows = []
        if tab_name == "COPIED":
            for it in self.queue:
                for (s, d, r) in it.copied_list:
                    rows.append((s, d, r))
                if it.copied_files and not it.copied_list:
                    rows.append((it.src_display(), it.dst_display(),
                                 "{} file(s) transferred".format(it.copied_files)))
        elif tab_name == "SKIPPED":
            for it in self.queue:
                for c in it.conflicts:
                    if c.resolution is None:
                        rows.append(("__conflict__", it, c))
        elif tab_name == "RENAMED":
            for it in self.queue:
                for c in it.conflicts:
                    if c.resolution in ("rename", "autorename"):
                        rows.append((it.src_display(c.src_full), it.dst_display(c.dst_full),
                                     c.remark))
        elif tab_name == "ERRORS":
            for it in self.queue:
                for (s, d, r) in it.errors_list:
                    rows.append((s, d, r))
        return rows

    # ---------------- drawing ----------------
    # Below this the views' sub-windows can't be created (curses.error).
    MIN_W, MIN_H = 48, 15
    NAV_QUEUE_H = 8     # the QUEUE box under the NAV panes

    def too_small(self):
        h, w = self.stdscr.getmaxyx()
        return h < App.MIN_H or w < App.MIN_W

    def busy_cell(self):
        """Where the busy spinner goes: the right end of the QUEUE box's
        top border in NAV, else of the view's frame. (y, x of the corner,
        free border cells between the title and the corner)."""
        if self.too_small():
            return None
        h, w = self.stdscr.getmaxyx()
        y = h - App.NAV_QUEUE_H - 1 if self.view == VIEW_NAV else 0
        # one border cell is kept free after the title
        return y, w - 1, max(0, w - 2 - getattr(self, "frame_title_end", 0))

    def draw_frame_title(self, win, text, attr=curses.A_BOLD):
        """Title on the top border the busy spinner shares."""
        safe_addstr(win, 0, 2, text, attr)
        self.frame_title_end = 2 + len(text)
        self.draw_update_notice(win)

    def draw_update_notice(self, win):
        """Right-aligned on the top border, but clear of the widest busy
        spinner label at its right end. Left out if it doesn't fit."""
        version = self.update_notice.newer_version
        if not version:
            return
        w = win.getmaxyx()[1]
        # exclusive end: one border cell is kept free before the spinner
        end = w - 1 - len(BusyIndicator.LABELS[0].format("x"))
        for text in (" v{} available - mcrsync selfupdate ".format(version),
                     " v{} available ".format(version)):
            x = end - len(text)
            # one border cell is kept free after the title
            if x > self.frame_title_end:
                safe_addstr(win, 0, x, text, curses.color_pair(Color.YELLOW) | curses.A_BOLD)
                return

    def draw_too_small(self, h, w):
        lines = ["Terminal too small",
                 "({} x {}, need {} x {})".format(w, h, App.MIN_W, App.MIN_H),
                 "Enlarge it to continue",
                 "(a run keeps going)" if self.running else "q: quit"]
        for i, line in enumerate(lines):
            if i < h:
                safe_addstr(self.stdscr, i, 0, line[:max(0, w - 1)])
        self.stdscr.noutrefresh()
        curses.doupdate()

    def draw(self):
        self.stdscr.erase()
        h, w = self.stdscr.getmaxyx()
        if self.too_small():
            self.draw_too_small(h, w)
            return
        # Draw+register the background/statusbar on stdscr FIRST. stdscr spans
        # the whole screen, so if it were noutrefresh()'d after the content
        # sub-windows, its blank erased cells would win the compositing and
        # blank out everything except the statusbar. Content windows are
        # sized to leave the last row free, so they can't clobber it.
        self.draw_statusbar(h, w)
        self.stdscr.noutrefresh()

        if self.view == VIEW_NAV:
            self.draw_nav(h, w)
        elif self.view == VIEW_QUEUE:
            self.draw_queue(h, w)
        elif self.view == VIEW_VERIFY:
            self.draw_verify(h, w)
        elif self.view == VIEW_OPTIONS:
            self.draw_options(h, w)
        elif self.view == VIEW_HELP:
            self.draw_help(h, w)
        elif self.view == VIEW_LOG:
            self.draw_log(h, w)
        curses.doupdate()

    def draw_statusbar(self, h, w):
        if self.view == VIEW_NAV:
            pane = self.left if self.active == "left" else self.right
            remote_key = "O:disconnect" if pane.fs.is_remote else "O:connect"
            help_text = ("?/H:help  Tab:switch  <-/->:nav  " + remote_key + "  C:to queue"
                          "  F2:rename  Ins:mkdir  F5:refresh  T:view queue  "
                          "V:verify  G:log  S:save  L:load  F8:run  P:pause  Q:quit")
        elif self.view == VIEW_QUEUE:
            help_text = ("?/H:help  T:nav-view  V:verify  G:log  S:save  L:load  F8:run  "
                          "P:pause  PgUp/PgDn:scroll  Q:quit")
        elif self.view == VIEW_VERIFY:
            help_text = "?/H:help  Tab:cycle-tab  Enter:resolve  Esc/V:back  Q:quit"
        elif self.view == VIEW_HELP:
            help_text = "Esc/H/?:close  Up/Down/PgUp/PgDn:scroll  Q:quit"
        elif self.view == VIEW_LOG:
            help_text = "Esc/G:close  Up/Down/PgUp/PgDn:scroll  End:follow new lines  Q:quit"
        else:
            help_text = ("?/H:help  Up/Down:move  Space:toggle  E:edit as text  D:defaults  "
                         "Enter:confirm&run  Esc:cancel  Q:quit")
        # Run state goes first so a narrow terminal can't cut it off.
        run_state = ""
        if self.running:
            if self.paused and self.reconnect_needed:
                state = "PAUSED - connection lost, press P to log in again"
            elif self.paused:
                state = "PAUSED"
            else:
                state = "RUNNING"
            run_state = "[{}{}] ".format("DRY RUN " if self.run_dry else "", state)
        bar = (run_state + help_text)

        # Whole-queue progress: in every view while running. It takes
        # precedence over the key hints, which get cut instead.
        right_text = ""
        if self.queue and (self.running or self.view == VIEW_QUEUE):
            percent, eta = self.compute_progress_and_eta()
            if percent is not None:
                ok_count = sum(1 for it in self.queue if it.status == STATUS_OK)
                eta_str = format_duration(eta) if eta is not None else "nan"
                right_text = " {}/{} items, {:.0f}% done | ETA: {}".format(
                    ok_count, len(self.queue), percent, eta_str)

        avail = max(0, w - 1)
        hints_end = avail
        if right_text:
            room = avail - len(right_text)
            if room >= len(run_state):
                bar = bar[:room].ljust(room) + right_text
                hints_end = room
        safe_addstr(self.stdscr, h - 1, 0, bar.ljust(avail), curses.color_pair(Color.STATUSBAR))
        for m in HINT_KEY_RE.finditer(help_text):
            start = len(run_state) + m.start(1)
            end = min(len(run_state) + m.end(1), hints_end)
            if start < end:
                safe_addstr(self.stdscr, h - 1, start, bar[start:end],
                            curses.color_pair(Color.STATUSBAR) | curses.A_BOLD)

    def draw_pane(self, win, pane, title, active):
        win.erase()
        frame_attr = curses.color_pair(Color.ACTIVE_FRAME) | curses.A_BOLD if active else self.inactive_frame_attr
        win.attron(frame_attr)
        win.border()
        win.attroff(frame_attr)
        h, w = win.getmaxyx()
        hdr_attr = curses.color_pair(Color.ACTIVE_HEADER) if active else curses.A_BOLD
        safe_addstr(win, 0, 2, " {} [{}] ".format(title, pane.fs.label()), hdr_attr)
        safe_addstr(win, 1, 2, pane.fs.display_path(pane.path)[:w - 4], curses.A_DIM)
        page_h = h - 3
        for i in range(page_h):
            idx = pane.scroll + i
            if idx >= len(pane.entries):
                break
            name, isdir = pane.entries[idx]
            label = ("/" + name) if isdir else (" " + name)
            attr = 0
            if isdir:
                attr = curses.color_pair(Color.YELLOW)
            if idx == pane.index:
                attr |= curses.A_REVERSE
            safe_addstr(win, 2 + i, 2, label, attr)
        if pane.error:
            for i, line in enumerate(textwrap.wrap("Can't list folder: " + pane.error,
                                                   max(10, w - 4))[:page_h]):
                safe_addstr(win, 2 + i, 2, line, curses.color_pair(Color.RED))
        elif not pane.entries or pane.entries == [Pane.PARENT]:
            safe_addstr(win, 2 + len(pane.entries), 2, "(empty)", curses.A_DIM)

    def draw_nav(self, h, w):
        bottom_h = App.NAV_QUEUE_H
        pane_h = h - bottom_h - 1
        half = w // 2
        lwin = curses.newwin(pane_h, half, 0, 0)
        rwin = curses.newwin(pane_h, w - half, 0, half)
        self.draw_pane(lwin, self.left, "SOURCE", self.active == "left")
        self.draw_pane(rwin, self.right, "TARGET", self.active == "right")
        lwin.noutrefresh()
        rwin.noutrefresh()

        bwin = curses.newwin(bottom_h, w, pane_h, 0)
        bwin.border()
        self.draw_frame_title(bwin, " QUEUE (last 5) - {} item(s) total ".format(
            len(self.queue)))
        tail = self.last_queue_tail(5)
        for i, it in enumerate(tail):
            n = len(self.queue) - len(tail) + i + 1
            line = "#{:<4d} {}  ->  {}   [{}]".format(
                n, it.src_display(), it.dst_display(), it.status_label())
            attr = self.status_attr(it.status)
            safe_addstr(bwin, 1 + i, 2, line, attr)
        self.draw_progress_line(bwin, bottom_h - 2)
        bwin.noutrefresh()

    def draw_progress_line(self, win, y):
        text = self.progress_text()
        if not text:
            return
        it = self.running_item()
        attr = curses.A_BOLD | curses.color_pair(Color.CYAN)
        if (it is not None and it.progress.phase == Progress.DELETING) or self.reconnect_needed:
            attr = curses.A_BOLD | curses.color_pair(Color.RED)
        safe_addstr(win, y, 2, "> " + text, attr)

    def status_attr(self, status):
        if status == STATUS_OK:
            return curses.color_pair(Color.GREEN)
        if status == STATUS_FAIL:
            return curses.color_pair(Color.RED)
        if status in (STATUS_INCOMPLETE, STATUS_DRY_INCOMPLETE):
            return curses.color_pair(Color.MAGENTA)
        if status == STATUS_DRY_OK:
            return curses.color_pair(Color.CYAN)
        return 0

    def item_size_display(self, it):
        if it.size_bytes is not None:
            return format_size_si(it.size_bytes)
        if it.size_state in ("pending", "computing"):
            return "..."
        if it.size_state == "offline":
            return "offline"
        return "nan"

    def draw_queue(self, h, w):
        win = curses.newwin(h - 1, w, 0, 0)
        win.border()
        self.draw_frame_title(win, " QUEUE EDITOR ")

        # Fixed-width columns: SIZE, #, then SOURCE/DEST split the rest
        # evenly, then STATUS. SIZE is right-justified (so "0.0 B" and
        # "1023.9 T" both line up with their unit letter on the right);
        # the rest are left-justified, each truncated to (width - 1) chars
        # before being padded to its full width, which guarantees at
        # least one trailing separator space per field - that's what keeps
        # a long SOURCE/DEST value from bleeding into the next column.
        size_w, files_w, num_w, status_reserve = 11, 10, 6, 19
        content_w = max(20, w - 4)
        remaining = max(8, content_w - size_w - files_w - num_w - status_reserve)
        col_src = remaining // 2
        col_dst = remaining - col_src

        x_size = 2
        x_num = x_size + size_w + files_w
        x_src = x_num + num_w
        x_dst = x_src + col_src
        x_status = x_dst + col_dst

        header = ("SIZE"[:size_w - 1].rjust(size_w - 1) + " " +
                  "FILES".rjust(files_w - 2) + "  " +
                  "#"[:num_w - 1].ljust(num_w) +
                  "SOURCE"[:col_src - 1].ljust(col_src) +
                  "DEST"[:col_dst - 1].ljust(col_dst))
        safe_addstr(win, 1, 2, header, curses.A_UNDERLINE)
        safe_addstr(win, 1, x_status, "STATUS".ljust(200), curses.A_UNDERLINE)

        page_h = h - 5
        if not hasattr(self, "_qscroll"):
            self._qscroll = 0
        if self.queue_index < self._qscroll:
            self._qscroll = self.queue_index
        if self.queue_index >= self._qscroll + page_h:
            self._qscroll = self.queue_index - page_h + 1
        for i in range(page_h):
            idx = self._qscroll + i
            if idx >= len(self.queue):
                break
            it = self.queue[idx]
            attr = self.status_attr(it.status)
            if idx == self.queue_index:
                attr |= curses.A_REVERSE
            size_str = self.item_size_display(it)
            # Right-justify within (size_w - 1) so values of differing
            # length (e.g. "0.0 B" vs "1023.9 T") still line up on their
            # right edge (units column-aligned), then add one explicit
            # separator space so it can never touch the "#" column.
            files_str = "{:,}".format(it.size_files) if it.size_files is not None else ""
            line = (size_str[:size_w - 1].rjust(size_w - 1) + " " +
                    files_str[:files_w - 2].rjust(files_w - 2) + "  " +
                    str(idx + 1)[:num_w - 1].ljust(num_w) +
                    it.src_display()[:col_src - 1].ljust(col_src) +
                    it.dst_display()[:col_dst - 1].ljust(col_dst))
            safe_addstr(win, 2 + i, 2, line, attr)
            # ljust(200) is a cheap way to guarantee the status field fills
            # with spaces all the way to the window edge (safe_addstr clips
            # to the actual visible width on its own), per request: no
            # leftover characters should show past the status text.
            safe_addstr(win, 2 + i, x_status, it.status_label().ljust(200), attr)
        self.draw_progress_line(win, h - 3)
        win.noutrefresh()

    def draw_log(self, h, w):
        win = curses.newwin(h - 1, w, 0, 0)
        win.border()
        with self.lock:
            lines = list(self.log_lines)
        page_h = h - 5
        max_scroll = max(0, len(lines) - page_h)
        if self.log_scroll is None or self.log_scroll >= max_scroll:
            scroll = max_scroll
            self.log_scroll = None
        else:
            scroll = max(0, self.log_scroll)
        title = " RUN LOG - {} line(s){}{} ".format(
            len(lines), ", oldest dropped" if self.log_dropped else "",
            "" if self.log_scroll is None else " - scrolled, End: follow new lines")
        self.draw_frame_title(win, title)
        for i in range(page_h):
            if scroll + i >= len(lines):
                break
            line = lines[scroll + i]
            attr = 0
            if ("DELETED" in line or "CONNECTION LOST" in line or
                    line[9:].startswith(("rsync", "rclone error"))):
                attr = curses.color_pair(Color.RED)
            elif "CONFLICT" in line:
                attr = curses.color_pair(Color.MAGENTA)
            safe_addstr(win, 1 + i, 2, line, attr)
        if not lines:
            safe_addstr(win, 2, 2, "(nothing logged yet - start a run with F8)", curses.A_DIM)
        self.draw_progress_line(win, h - 3)
        win.noutrefresh()

    def handle_log_key(self, c):
        h, w = self.stdscr.getmaxyx()
        page_h = h - 5
        with self.lock:
            max_scroll = max(0, len(self.log_lines) - page_h)
        current = max_scroll if self.log_scroll is None else self.log_scroll
        if c == curses.KEY_UP:
            self.log_scroll = max(0, current - 1)
        elif c == curses.KEY_DOWN:
            self.log_scroll = current + 1
        elif c == curses.KEY_PPAGE:
            self.log_scroll = max(0, current - page_h)
        elif c == curses.KEY_NPAGE:
            self.log_scroll = current + page_h
        elif c == curses.KEY_HOME:
            self.log_scroll = 0
        elif c == curses.KEY_END:
            self.log_scroll = None
        elif c in (Keys.ESC, ord('g')):
            self.view = self.prev_view

    def draw_verify(self, h, w):
        win = curses.newwin(h - 1, w, 0, 0)
        win.border()
        tabs_line = "  ".join(
            ("[" + t + "]") if i == self.verify_tab else (" " + t + " ")
            for i, t in enumerate(VERIFY_TABS))
        self.draw_frame_title(win, " VERIFY: " + tabs_line + " ")
        rows = self.verify_rows(VERIFY_TABS[self.verify_tab])
        safe_addstr(win, 1, 2, "SOURCE".ljust((w - 6) // 3) +
                    "TARGET".ljust((w - 6) // 3) + "REMARK", curses.A_UNDERLINE)
        page_h = h - 5
        if self.verify_index >= len(rows):
            self.verify_index = max(0, len(rows) - 1)
        scroll = max(0, self.verify_index - page_h + 1)
        col = (w - 6) // 3
        for i in range(page_h):
            idx = scroll + i
            if idx >= len(rows):
                break
            row = rows[idx]
            if row[0] == "__conflict__":
                it, c = row[1], row[2]
                s, d, remark = c.relpath, it.dst_display(), c.remark
            else:
                s, d, remark = row
            attr = curses.A_REVERSE if idx == self.verify_index else 0
            line = (str(s)[:col - 1].ljust(col) + str(d)[:col - 1].ljust(col) + str(remark))
            safe_addstr(win, 2 + i, 2, line, attr)
        if not rows:
            safe_addstr(win, 3, 2, "(nothing in this category)", curses.A_DIM)
        win.noutrefresh()

    def options_tools(self):
        """The tools whose flags the F8 screen shows."""
        return self.queue_tools() or [TOOL_RSYNC]

    def options_rows(self):
        """(tool, choice) for every checkbox on the F8 screen."""
        return [(tool, choice) for tool in self.options_tools()
                for choice in FLAG_SETS[tool].CHOICES]

    def no_modtime_remotes(self):
        """Keys of rclone remotes the queue uses that can't store
        modification times (known once they were checked)."""
        keys = []
        for it in self.pending_items():
            for spec in it.conn_specs():
                if spec.kind == "rclone" and spec.key() not in keys:
                    remote = self.registry.rclone_remote(spec)
                    if remote.ready and not remote.modtime_ok:
                        keys.append(spec.key())
        return keys

    def options_tail(self, text_w):
        """(indent, text, attr) lines below the checkboxes: other flags,
        the exact command and every warning, per tool."""
        red = curses.color_pair(Color.RED) | curses.A_BOLD
        tail = []
        for tool in self.options_tools():
            flags = FLAG_SETS[tool]
            opts = self.opts_for(tool)
            tail.append((2, "", 0))
            others = " ".join(shlex.quote(o) for o in flags.others(opts)) or "(none)"
            for i, line in enumerate(textwrap.wrap("{} - Other flags: {}".format(tool, others),
                                                   text_w, break_on_hyphens=False)):
                tail.append((2 if i == 0 else 4, line, 0))
            idx, cmd = self.options_preview_cmd(tool)
            if idx is not None:
                tail.append((2, "Command for item #{} of {} (other {} items get their own "
                                "paths):".format(idx + 1, len(self.queue), tool), curses.A_BOLD))
                for line in textwrap.wrap(cmd, text_w, break_on_hyphens=False):
                    tail.append((4, line, curses.A_REVERSE))
            warnings = []
            problem = flags.problems(opts)
            if problem:
                warnings.append(("CAN'T RUN: " + problem[:1].upper() + problem[1:], red))
            risky = flags.risky(opts)
            if risky:
                warnings.append(("CAUTION: these {} flags can delete or overwrite data, write "
                                 "outside the target or run commands: {}".format(
                                     tool, " ".join(risky)), red))
            unchecked = flags.unchecked(opts)
            if unchecked:
                warnings.append(("CHECK: mcrsync doesn't know what these {} flags do (they could "
                                 "e.g. send data elsewhere): {}".format(
                                     tool, " ".join(unchecked)),
                                 curses.color_pair(Color.MAGENTA) | curses.A_BOLD))
            if tool == TOOL_RCLONE and flags.has(opts, "l", "--links"):
                warnings.append(("", 0))
                warnings.append(("Note: -l/--links is left out for copies INTO a local folder - "
                                 "symlinks from a remote stay .rclonelink files there, so they "
                                 "can't point outside the target.", 0))
            if tool == TOOL_RCLONE and flags.is_sync(opts):
                warnings.append(("", 0))
                warnings.append(("Note: --delete makes folder items run 'rclone sync' instead of "
                                 "'rclone copy'; single files are still copied with "
                                 "'rclone copyto'.", 0))
            if flags.is_dry_run(opts):
                warnings.append(("DRY RUN (-n): nothing will be copied or created by {}. Items "
                                 "end up as DRY RUN OK / DRY RUN INCOMPLETE.".format(tool),
                                 curses.color_pair(Color.CYAN) | curses.A_BOLD))
            if not flags.has_update(opts):
                warnings.append(("WARNING: -u/--update is NOT set for {} - newer files in the "
                                 "target could be overwritten!".format(tool), red))
            if tool == TOOL_RCLONE:
                for key in self.no_modtime_remotes():
                    warnings.append(("WARNING: {} can't store modification times - --update "
                                     "compares with upload times there, so newer target files "
                                     "may be overwritten or reported as conflicts "
                                     "wrongly.".format(key), red))
            for text, attr in warnings:
                if not text:
                    tail.append((2, "", 0))
                for i, line in enumerate(textwrap.wrap(text, text_w, break_on_hyphens=False)):
                    tail.append((2 if i == 0 else 4, line, attr))
        return tail

    def draw_options(self, h, w):
        win = curses.newwin(h - 1, w, 0, 0)
        win.border()
        self.draw_frame_title(win, " RUN OPTIONS - apply to ALL queued items of each tool ")
        red = curses.color_pair(Color.RED) | curses.A_BOLD

        # Command and warnings get their rows before the checkboxes: flags may
        # come from a loaded queue file, and a hidden one could be dangerous.
        tail = self.options_tail(max(10, w - 8))

        # Checkboxes (with a heading per tool) get the rows left over,
        # scrolled to keep the cursor visible.
        rows = self.options_rows()
        self.options_index = max(0, min(self.options_index, len(rows) - 1))
        lines = []      # (text, attr, row index or None)
        tool = None
        for n, (row_tool, choice) in enumerate(rows):
            if row_tool != tool:
                tool = row_tool
                count = sum(1 for it in self.pending_items() if it.tool() == tool)
                lines.append(("{} flags ({} item{}):".format(
                    tool, count, "" if count == 1 else "s"), curses.A_UNDERLINE, None))
            flags = FLAG_SETS[row_tool]
            short, long_name, desc, is_risky = choice
            mark = "[x]" if flags.has(self.opts_for(row_tool), short, long_name) else "[ ]"
            name = "-{}, {}".format(short, long_name) if short else long_name
            text = "{} {} {:<20} {}".format(">" if n == self.options_index else " ",
                                                mark, name, desc)
            attr = red if is_risky else 0
            if n == self.options_index:
                attr |= curses.A_REVERSE
            lines.append((text, attr, n))
        list_h = max(1, min(len(lines), h - 6 - len(tail)))
        pos = next(i for i, l in enumerate(lines) if l[2] == self.options_index)
        scroll = max(0, pos - list_h + 1)
        for i in range(list_h):
            if scroll + i >= len(lines):
                break
            text, attr, _ = lines[scroll + i]
            safe_addstr(win, 2 + i, 2, text, attr)

        y = 2 + list_h
        for indent, line, attr in tail:
            if y >= h - 3:
                break
            safe_addstr(win, y, indent, line, attr)
            y += 1

        safe_addstr(win, h - 3, 2,
                    "[Space] Toggle   [E] Edit as text   [D] Defaults   "
                    "[Enter] Confirm & run   [Esc] Cancel", curses.A_BOLD)
        win.noutrefresh()

    def options_preview_cmd(self, tool):
        """(queue index, shell-quoted command) of the first item a run
        would copy with tool, or (None, "")."""
        idx = next((i for i, it in enumerate(self.queue)
                    if it.status != STATUS_OK and it.tool() == tool), None)
        if idx is None:
            return None, ""
        item = self.queue[idx]
        try:
            cmd = self.runner.item_cmd(item, self.opts_for(tool))
        except FsError as e:
            return idx, "(no command: {})".format(e)
        return idx, " ".join(shlex.quote(o) for o in cmd)

    def handle_options_key(self, c):
        rows = self.options_rows()
        tool, choice = rows[max(0, min(self.options_index, len(rows) - 1))]
        flags = FLAG_SETS[tool]
        if c == curses.KEY_UP:
            self.options_index = max(0, self.options_index - 1)
        elif c == curses.KEY_DOWN:
            self.options_index = min(len(rows) - 1, self.options_index + 1)
        elif c == ord(' '):
            opts = flags.toggle(self.opts_for(tool), choice)
            if opts:
                self.set_opts(tool, opts)
            else:
                flash(self.stdscr)
        elif c == ord('e'):
            new_line = prompt_string(self.stdscr, "{} flags:".format(tool),
                                      " ".join(shlex.quote(o) for o in self.opts_for(tool)))
            if new_line is None:
                return
            try:
                parsed = shlex.split(new_line)
            except ValueError as e:
                message(self.stdscr, "Could not parse flags: {}".format(e))
                return
            if not parsed:
                message(self.stdscr, "At least one flag is required - keeping previous options.")
                return
            problem = flags.problems(parsed)
            if problem:
                message(self.stdscr, problem[:1].upper() + problem[1:] +
                        " - keeping previous options.")
                return
            self.set_opts(tool, parsed)
        elif c == ord('d'):
            if confirm(self.stdscr, "Restore default {} flags ({})?".format(
                    tool, " ".join(flags.DEFAULTS)), self.bright_red_attr):
                self.set_opts(tool, list(flags.DEFAULTS))
        elif c in Keys.ENTER:
            self.confirm_and_run()
        elif c == Keys.ESC:
            self.view = VIEW_NAV

    def confirm_and_run(self):
        for tool in self.queue_tools():
            flags = FLAG_SETS[tool]
            problem = flags.problems(self.opts_for(tool))
            if problem:
                message(self.stdscr, "Can't run: " + problem)
                return
            if not flags.has_update(self.opts_for(tool)) and not confirm(
                    self.stdscr, "-u/--update is not set for {} - newer target files may be "
                    "overwritten. Continue anyway?".format(tool), self.bright_red_attr):
                return
        if self.ensure_queue_connections():
            self.view = VIEW_NAV
            self.start_run()

    def draw_help(self, h, w):
        win = curses.newwin(h - 1, w, 0, 0)
        win.border()
        self.draw_frame_title(win, " HELP (Esc/H/? to close) ")
        page_h = h - 4
        max_scroll = max(0, len(HELP_TEXT) - page_h)
        self.help_scroll = max(0, min(self.help_scroll, max_scroll))
        for i in range(page_h):
            idx = self.help_scroll + i
            if idx >= len(HELP_TEXT):
                break
            line = HELP_TEXT[idx]
            safe_addstr(win, 2 + i, 2, line)
            for start, end in help_key_spans(line):
                safe_addstr(win, 2 + i, 2 + start, line[start:end], curses.A_BOLD)
        if len(HELP_TEXT) > page_h:
            pct = 100 if max_scroll == 0 else int(100 * self.help_scroll / max_scroll)
            safe_addstr(win, h - 3, 2,
                        "-- line {}/{} ({}%) - Up/Down/PgUp/PgDn to scroll --".format(
                            self.help_scroll + 1, len(HELP_TEXT), pct), curses.A_DIM)
        win.noutrefresh()

    def handle_help_key(self, c):
        h, w = self.stdscr.getmaxyx()
        page_h = h - 5
        if c == curses.KEY_UP:
            self.help_scroll = max(0, self.help_scroll - 1)
        elif c == curses.KEY_DOWN:
            self.help_scroll += 1
        elif c == curses.KEY_PPAGE:
            self.help_scroll = max(0, self.help_scroll - page_h)
        elif c == curses.KEY_NPAGE:
            self.help_scroll += page_h
        elif c in (Keys.ESC, ord('h'), ord('?')):
            self.view = self.prev_view

    # ---------------- input handling ----------------
    def run(self):
        self.stdscr.nodelay(True)
        self.stdscr.timeout(200)
        while True:
            if self.pending_pane_refresh:
                self.pending_pane_refresh = False
                self.refresh_panes()
                self.log("Queue run finished - nav panes refreshed.")
            self.draw()
            c = self.stdscr.getch()
            if c in (-1, curses.KEY_RESIZE):
                continue
            # keys are shown upper case, but either case works
            if ord('A') <= c <= ord('Z'):
                c += 32
            if self.too_small():
                # nothing is visible, so no key may act on hidden state
                if c == ord('q') and not self.running:
                    break
                continue
            if not self.handle_key(c):
                break

    def handle_key(self, c):
        if c == ord('q'):
            if confirm(self.stdscr, "Quit mcrsync?", self.bright_red_attr):
                return False
            return True

        if self.view == VIEW_HELP:
            self.handle_help_key(c)
            return True
        if self.view == VIEW_LOG:
            self.handle_log_key(c)
            return True

        if c in (ord('?'), ord('h')):
            self.prev_view = self.view
            self.view = VIEW_HELP
            self.help_scroll = 0
            return True

        if self.view == VIEW_NAV:
            self.handle_nav_key(c)
        elif self.view == VIEW_QUEUE:
            self.handle_queue_key(c)
        elif self.view == VIEW_VERIFY:
            self.handle_verify_key(c)
        elif self.view == VIEW_OPTIONS:
            self.handle_options_key(c)
            return True  # options screen owns all keys except q (handled above)

        # global keys available everywhere
        if c == ord('g'):
            self.prev_view = self.view
            self.view = VIEW_LOG
            self.log_scroll = None
        elif c == ord('t'):
            self.view = VIEW_QUEUE if self.view == VIEW_NAV else VIEW_NAV
        elif c == ord('v'):
            if self.view != VIEW_VERIFY:
                self.view = VIEW_VERIFY
            else:
                self.view = VIEW_NAV
        elif c == ord('s'):
            name = prompt_string(self.stdscr, "Save queue as (path):", "queue.json")
            if name:
                if os.path.isdir(name):
                    message(self.stdscr, "'{}' is a folder - type a file name.".format(name))
                    return True
                if os.path.lexists(name) and not confirm(
                        self.stdscr, "'{}' already exists. Overwrite it?".format(name),
                        self.bright_red_attr):
                    return True
                try:
                    self.save_queue(name)
                    message(self.stdscr, "Saved {} item(s) to {}".format(len(self.queue), name))
                except OSError as e:
                    message(self.stdscr, "Save failed: {}".format(e))
        elif c == ord('l'):
            if self.running:
                message(self.stdscr, "A run is in progress - load a queue after it has finished.")
                return True
            name = prompt_string(self.stdscr, "Load queue from (path):", "queue.json")
            if name:
                self.load_queue_interactive(name)
        elif c == curses.KEY_F8:
            if not self.queue:
                message(self.stdscr, "Queue is empty.")
            elif self.running:
                message(self.stdscr, "A run is already in progress.")
            elif self.ensure_queue_connections(kinds=("rclone",)):
                # rclone remotes are checked first (no login needed), so the
                # screen can warn about remotes without modification times
                self.view = VIEW_OPTIONS
                self.options_index = 0
        elif c == ord('p'):
            self.toggle_pause()
        return True

    def handle_nav_key(self, c):
        pane = self.left if self.active == "left" else self.right
        h, w = self.stdscr.getmaxyx()
        page_h = h - App.NAV_QUEUE_H - 4
        if c == Keys.TAB:
            self.active = "right" if self.active == "left" else "left"
        elif c == curses.KEY_F5:
            self.reconnect_panes_if_needed()
            self.refresh_panes()
        elif c == ord('o'):
            self.toggle_remote(pane)
        elif c == ord('b'):
            self.open_bookmarks(pane)
        elif ord('1') <= c <= ord('9'):
            self.quick_jump(pane, c - ord('0'))
        elif c == curses.KEY_UP:
            pane.move(-1, page_h)
        elif c == curses.KEY_DOWN:
            pane.move(1, page_h)
        elif c == curses.KEY_PPAGE:
            pane.move(-page_h, page_h)
        elif c == curses.KEY_NPAGE:
            pane.move(page_h, page_h)
        elif c == curses.KEY_HOME:
            pane.move(-len(pane.entries), page_h)
        elif c == curses.KEY_END:
            pane.move(len(pane.entries), page_h)
        elif c == curses.KEY_LEFT or c in Keys.BACKSPACE:
            pane.go_parent()
        elif c == curses.KEY_RIGHT or c in Keys.ENTER:
            pane.enter()
        elif c == ord('c'):
            if self.active != "left":
                curses.flash()
                curses.flash()
                return
            sel = self.left.selected()
            if sel is None:
                return
            name, is_dir = sel
            src_full = self.left.selected_full()
            dst_full = self.right.fs.pathmod.join(self.right.path, name)
            item = self.add_to_queue(src_full, dst_full, is_dir,
                                     self.left.fs.spec, self.right.fs.spec)
            self.log("Queued ({}): {} -> {}".format(
                "dir" if is_dir else "file", item.src_display(), item.dst_display()))
        elif c == curses.KEY_F2:
            self.rename_selected(pane)
        elif c == curses.KEY_IC:
            self.create_folder(pane)

    def rename_selected(self, pane):
        sel = pane.selected()
        if sel is None:
            message(self.stdscr, "Nothing selected to rename.")
            return
        old_name, is_dir = sel
        kind = "folder" if is_dir else "file"
        confirmed, new_name = modal_input_dialog(
            self.stdscr, "Rename", "New name for {} '{}':".format(kind, old_name),
            default=old_name)
        if not confirmed:
            return
        new_name = new_name.strip()
        if not new_name or new_name == old_name:
            return
        problem = check_entry_name(new_name)
        if problem:
            message(self.stdscr, problem)
            return
        old_full = pane.fs.pathmod.join(pane.path, old_name)
        new_full = pane.fs.pathmod.join(pane.path, new_name)
        try:
            # no dialogs inside the busy block
            with self.busy:
                exists = pane.fs.exists(new_full)
                if not exists:
                    pane.fs.rename(old_full, new_full)
        except FsError as e:
            message(self.stdscr, "Rename failed: {}".format(e))
            return
        if exists:
            message(self.stdscr, "'{}' already exists - not overwriting.".format(new_name))
            return
        pane.reload()
        for i, (name, _) in enumerate(pane.entries):
            if name == new_name:
                pane.index = i
                break
        self.log("Renamed: {} -> {}".format(pane.fs.display_path(old_full), new_name))

    def create_folder(self, pane):
        confirmed, name = modal_input_dialog(
            self.stdscr, "Create Folder", "New folder name:", default="")
        if not confirmed:
            return
        name = name.strip()
        if not name:
            return
        problem = check_entry_name(name)
        if problem:
            message(self.stdscr, problem)
            return
        new_full = pane.fs.pathmod.join(pane.path, name)
        try:
            with self.busy:
                exists = pane.fs.exists(new_full)
                if not exists:
                    pane.fs.mkdir(new_full)
        except FsError as e:
            message(self.stdscr, "Create folder failed: {}".format(e))
            return
        if exists:
            message(self.stdscr, "'{}' already exists.".format(name))
            return
        pane.reload()
        for i, (nm, _) in enumerate(pane.entries):
            if nm == name:
                pane.index = i
                break
        self.log("Created folder: {}".format(pane.fs.display_path(new_full)))

    def handle_queue_key(self, c):
        h, w = self.stdscr.getmaxyx()
        page_h = h - 5
        if c == curses.KEY_UP:
            self.queue_index = max(0, self.queue_index - 1)
        elif c == curses.KEY_DOWN:
            self.queue_index = min(max(0, len(self.queue) - 1), self.queue_index + 1)
        elif c == curses.KEY_PPAGE:
            self.queue_index = max(0, self.queue_index - page_h)
        elif c == curses.KEY_NPAGE:
            self.queue_index = min(max(0, len(self.queue) - 1), self.queue_index + page_h)
        elif c in (curses.KEY_DC, ord('d')):
            if self.queue and self.queue[self.queue_index].status == STATUS_RUNNING:
                message(self.stdscr, "This item is being copied right now and can't be deleted. "
                        "Press P to pause - the run stops after this item.")
            elif self.queue and confirm(self.stdscr, "Delete selected queue item?",
                                        self.bright_red_attr):
                doomed = self.queue[self.queue_index]
                doomed.cancel_event.set()
                del self.queue[self.queue_index]
                self.queue_index = max(0, self.queue_index - 1)

    def handle_verify_key(self, c):
        h, w = self.stdscr.getmaxyx()
        page_h = h - 5
        if c == Keys.TAB:
            self.verify_tab = (self.verify_tab + 1) % len(VERIFY_TABS)
            self.verify_index = 0
        elif c == curses.KEY_UP:
            self.verify_index = max(0, self.verify_index - 1)
        elif c == curses.KEY_DOWN:
            self.verify_index += 1
        elif c == curses.KEY_PPAGE:
            self.verify_index = max(0, self.verify_index - page_h)
        elif c == curses.KEY_NPAGE:
            self.verify_index += page_h
        elif c == Keys.ESC:
            self.view = VIEW_NAV
        elif c in Keys.ENTER:
            self.resolve_selected_conflict()

    def resolve_selected_conflict(self):
        if VERIFY_TABS[self.verify_tab] != "SKIPPED":
            return
        rows = self.verify_rows("SKIPPED")
        if self.verify_index >= len(rows):
            return
        row = rows[self.verify_index]
        if row[0] != "__conflict__":
            return
        it, c = row[1], row[2]
        if c.dry_run:
            message(self.stdscr, "This conflict was found by a dry run, so nothing was copied "
                    "yet. Remove -n in the F8 options and run for real, then resolve it here.")
            return
        if self.running:
            # the run may be redoing this very item (it retries INCOMPLETE ones)
            message(self.stdscr, "A run is in progress - resolve conflicts after it has finished.")
            return
        if it.status != STATUS_INCOMPLETE:
            # a newer run replaced this item's results while we were looking
            return
        choice = prompt_string(
            self.stdscr,
            "{}: (S)kip / (R)ename existing / (A)utorename incoming:".format(c.relpath), "")
        choice = (choice or "").strip().lower()
        action = {"s": "skip", "r": "rename", "a": "autorename"}.get(choice[:1] if choice else "")
        if not action:
            return
        dead = self.registry.dead_specs(it.conn_specs())
        if dead and self.registry.open_interactive(self.stdscr, dead):
            message(self.stdscr, "Not connected - nothing was changed.")
            return
        with self.busy:
            ok, err = self.runner.resolve_conflict(it, c, action)
        if ok:
            message(self.stdscr, "Resolved {}: {}".format(c.relpath, c.remark))
            # if any conflicts remain unresolved, item stays INCOMPLETE
            if all(cc.resolution is not None for cc in it.conflicts):
                it.status = STATUS_OK
        else:
            message(self.stdscr, "Resolution failed: {}".format(err))


# ----------------------------------------------------------------------
# Self-update
# ----------------------------------------------------------------------

# The mcrsync.py asset of the latest GitHub release
UPDATE_URL = "https://github.com/winternet-studio/mcrsync/releases/latest/download/mcrsync.py"

VERSION_RE = re.compile(br'^VERSION = "(\d+)\.(\d+)\.(\d+)"$', re.M)

# Returns {"version": "x.y.z"} of the latest release. Plain http: old Pythons
# (e.g. on a NAS) often lack current CA certificates, and the answer can't do
# harm - it only decides whether a notice shows.
LATEST_VERSION_URL = "http://mcrsync.winternet.no/latest-version"


class UpdateNotice:
    """Checks in the background whether a newer release exists. Any
    failure (no network, odd answer, slow server) just means no notice.
    MCRSYNC_LATEST_VERSION_URL overrides the URL; empty turns it off."""

    TIMEOUT = 5
    VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")

    def __init__(self, url=None):
        if url is None:
            url = os.environ.get("MCRSYNC_LATEST_VERSION_URL", LATEST_VERSION_URL)
        self.url = url
        # set by the thread, read by the UI (atomic under the GIL)
        self.newer_version = None

    def start(self):
        if not self.url:
            return
        # daemon: a hanging request never delays quitting
        t = threading.Thread(target=self._check)
        t.daemon = True
        t.start()

    def _check(self):
        try:
            self.newer_version = self.fetch()
        except Exception:
            # nothing may reach stderr: it would garble the curses screen
            pass

    @staticmethod
    def user_agent():
        """E.g. "mcrsync/2.1.1 (Linux; Python 3.4.10)"."""
        import platform
        return "mcrsync/{} ({}; Python {})".format(
            VERSION, platform.system() or "unknown", platform.python_version())

    def fetch(self):
        """The latest version if newer than this one, else None. Raises on
        anything unexpected."""
        import urllib.request
        req =urllib.request.Request(self.url, headers={"User-Agent": UpdateNotice.user_agent()})
        resp = urllib.request.urlopen(req, timeout=UpdateNotice.TIMEOUT)
        try:
            data = json.loads(resp.read(1000).decode("utf-8"))
        finally:
            resp.close()
        m = UpdateNotice.VERSION_RE.match(data["version"])
        if not m:
            raise ValueError("not a version number")
        latest = tuple(int(n) for n in m.groups())
        current = tuple(int(n) for n in VERSION.split("."))
        return data["version"] if latest > current else None


class SelfUpdater:
    """`mcrsync selfupdate`: replaces the running script file (symlinks
    followed) with the latest release if that is newer, keeping its
    permissions."""

    def __init__(self, url=None, path=None):
        self.url = url or os.environ.get("MCRSYNC_UPDATE_URL") or UPDATE_URL
        self.path = os.path.realpath(path or __file__)

    def download(self):
        try:
            import urllib.request
            resp = urllib.request.urlopen(self.url, timeout=60)
            try:
                return resp.read()
            finally:
                resp.close()
        except Exception:
            # Old Pythons (e.g. on a NAS) often lack current CA certificates.
            if not shutil.which("curl"):
                raise
        p = subprocess.Popen(["curl", "-fsSL", self.url],  # -L: release URLs redirect
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = p.communicate()
        if p.returncode != 0:
            raise RuntimeError(err.decode("utf-8", "replace").strip()
                               or "curl exited with {}".format(p.returncode))
        return out

    def check(self, data):
        """Version tuple of a downloaded script; raises if it isn't one."""
        m = VERSION_RE.search(data)
        if not data.startswith(b"#!") or b"mcrsync" not in data[:1000] or not m:
            raise RuntimeError("the download is not an mcrsync script")
        try:
            compile(data, self.url, "exec")
        except SyntaxError as e:
            raise RuntimeError("the download is not valid Python: {}".format(e))
        return tuple(int(n) for n in m.groups())

    def replace(self, data):
        fd, tmp = tempfile.mkstemp(prefix=".mcrsync-update-",
                                   dir=os.path.dirname(self.path))
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            shutil.copymode(self.path, tmp)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def run(self):
        try:
            print("Downloading {}".format(self.url))
            data = self.download()
            latest = self.check(data)
            current = tuple(int(n) for n in VERSION.split("."))
            if latest <= current:
                print("Already up to date ({}).".format(VERSION))
                return 0
            latest = ".".join(str(n) for n in latest)
            print("Updating {} -> {}".format(VERSION, latest))
            self.replace(data)
        except PermissionError:
            print("No permission to replace {}. Try again with sudo.".format(self.path))
            return 1
        except Exception as e:
            print("Update failed, nothing was changed: {}".format(e))
            return 1
        print("Updated {}".format(self.path))
        return 0


# ----------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------

def exit_on_signal(signum, frame):
    raise SystemExit(128 + signum)


def main(stdscr):
    # Closing the terminal (SIGHUP) or a kill (SIGTERM) must still run the
    # finally below - otherwise the logged-in ssh masters stay alive.
    signal.signal(signal.SIGHUP, exit_on_signal)
    signal.signal(signal.SIGTERM, exit_on_signal)
    start_path = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
    app = App(stdscr, start_path)
    app.update_notice.start()
    try:
        app.run()
    finally:
        app.registry.close_all()


if __name__ == "__main__":
    if sys.argv[1:] == ["selfupdate"]:
        sys.exit(SelfUpdater().run())
    if sys.argv[1:] in (["--version"], ["-V"]):
        print("mcrsync " + VERSION)
        sys.exit(0)
    # With keypad mode, curses waits ESCDELAY ms (default 1000) to tell a
    # lone Esc from an arrow key's escape sequence.
    os.environ.setdefault("ESCDELAY", "25")
    curses.wrapper(main)
