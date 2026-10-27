#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mcrsync.py - A Midnight-Commander-style rsync queue manager.

Targets Python 3.4 (Synology chroot). Avoids f-strings, subprocess.run,
os.scandir and other 3.5+-only features on purpose.

Either pane can be switched to a remote SSH host (key 'o'); rsync then runs
over a shared ssh ControlMaster connection. See HELP_TEXT for details.

Views:
  NAV    - left (source) / right (target) file panes + last-5 queue tail
  QUEUE  - full-screen queue editor (#, source, dest, status)
  VERIFY - post-run report: COPIED / SKIPPED / RENAMED / ERRORS tabs

Keys (NAV + QUEUE unless noted):
  Tab        toggle left/right pane        (NAV only)
  Left       go to parent dir              (NAV only)
  Right/Ent  enter directory                (NAV only)
  o          connect active pane to an SSH host / disconnect it (NAV only)
  c          queue copy of selected item in LEFT pane -> current RIGHT dir
  t          toggle NAV <-> QUEUE view
  v          open VERIFY view (after a run, or to inspect last run)
  s          save queue to a .json file
  l          load queue from a .json file
  F8         execute the queue
  p          pause / resume execution
  PgUp/PgDn  scroll active pane / list
  q          quit (confirm)

In VERIFY view:
  Tab        cycle COPIED / SKIPPED / RENAMED / ERRORS
  Enter      (on a SKIPPED "conflict" row) resolve: Skip / Rename / Autorename
  Esc / v    back to previous view
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

DEFAULT_RSYNC_OPTS = ["-a", "-u", "--stats", "--itemize-changes"]

# ----------------------------------------------------------------------
# Data model
# ----------------------------------------------------------------------

STATUS_PENDING = "NOT SYNCED"
STATUS_RUNNING = "IN PROGRESS"
STATUS_OK = "SYNCED OK"
STATUS_FAIL = "SYNC FAIL"
STATUS_INCOMPLETE = "INCOMPLETE"
# Dry-run results must not look like SYNCED OK, or a later real run would
# skip those items.
STATUS_DRY_OK = "DRY RUN OK"
STATUS_DRY_INCOMPLETE = "DRY RUN INCOMPLETE"


class Conflict(object):
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


class ConnSpec(object):
    """Identifies a remote SSH endpoint. Never holds a password - ssh asks
    for that itself when the connection is opened.

    Specs also come from queue files, so the constructor validates: a user
    or host starting with '-' would otherwise be taken as an ssh option
    (e.g. -oProxyCommand=... runs a local command)."""

    # \Z, not $: $ also matches before a trailing newline
    NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*\Z")

    def __init__(self, user, host, port=22, keyfile=""):
        if not isinstance(user, str) or not ConnSpec.NAME_RE.match(user):
            raise ValueError("invalid user name {0!r} (letters, digits, . _ - only)".format(user))
        if not isinstance(host, str) or not ConnSpec.NAME_RE.match(host):
            raise ValueError("invalid host {0!r} (host name or IPv4 address)".format(host))
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
            return None, "Key file not found: {0}".format(keyfile)
        try:
            return ConnSpec(user, host, port or "22", keyfile), ""
        except ValueError as e:
            return None, str(e)[0].upper() + str(e)[1:] + "."

    def key(self):
        return "{0}@{1}:{2}".format(self.user, self.host, self.port)

    def display_path(self, path):
        return "{0}:{1}".format(self.host, path)

    def to_dict(self):
        return {
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
            return "%3.1f %s" % (n / factor, suffix)
    return "%3.1f %s" % (n, "B")


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
        parts.append("%dd" % days)
    if days or hours:
        parts.append("%dh" % hours)
    parts.append("%dm" % mins)
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


class QueueItem(object):
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
        self.copied_dirs = 0
        self.skipped_files = 0
        self.skipped_dirs = 0
        self.bytes_transferred = 0
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

    def src_display(self, path=None):
        path = self.src if path is None else path
        return self.src_conn.display_path(path) if self.src_conn else path

    def dst_display(self, path=None):
        path = self.dst if path is None else path
        return self.dst_conn.display_path(path) if self.dst_conn else path

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
            raise ValueError("invalid path in queue: {0!r} (must be absolute)".format(path))
        return path

    @staticmethod
    def from_dict(d):
        QueueItem.check_path(d["src"])
        QueueItem.check_path(d["dst"])
        if not isinstance(d.get("is_dir", True), bool):
            raise ValueError("invalid is_dir in queue: {0!r}".format(d.get("is_dir")))
        it = QueueItem(d["src"], d["dst"], d.get("is_dir", True),
                       ConnSpec.from_dict(d.get("src_conn")),
                       ConnSpec.from_dict(d.get("dst_conn")))
        it.status = d.get("status", STATUS_PENDING)
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


# ----------------------------------------------------------------------
# Settings (~/.mcrsync.yaml)
# ----------------------------------------------------------------------

class SettingsError(Exception):
    pass


class Settings(object):
    """User settings stored as a small YAML subset (PyYAML usually isn't
    available on NAS Python installs): '#' comments, 'key: value' scalars
    and one level of nested mappings. Values are integers or strings
    (plain, 'single' or "double" quoted). Anything else is rejected with a
    line number instead of being silently misread."""

    DEFAULT_PATH = os.path.join(os.path.expanduser("~"), ".mcrsync.yaml")
    KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):(?:\s+(.*))?$")
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
            self.error = "{0}: {1}".format(self.path, e)
            return
        try:
            self.data = Settings.parse(text)
        except SettingsError as e:
            self.error = "{0}: {1}".format(self.path, e)

    def save(self):
        """Returns an error string, or "" on success. A file that failed to
        parse is never overwritten, so hand edits aren't lost."""
        if self.error:
            return "not saving settings - fix this first: " + self.error
        # realpath keeps a symlinked settings file a symlink
        try:
            atomic_write(os.path.realpath(self.path), Settings.dump(self.data))
        except (IOError, OSError) as e:
            return "could not save settings: {0}".format(e)
        return ""

    def section(self, name):
        value = self.data.get(name)
        return value if isinstance(value, dict) else OrderedDict()

    def set_section(self, name, values):
        self.data[name] = values

    @staticmethod
    def parse(text):
        data = OrderedDict()
        current = None   # nested mapping being filled, None at top level
        for n, raw in enumerate(text.splitlines(), 1):
            line = Settings._strip_comment(raw, n).rstrip()
            if not line.strip():
                continue
            stripped = line.lstrip(" ")
            if stripped.startswith("\t"):
                raise SettingsError("line {0}: tabs are not allowed for indentation".format(n))
            indent = len(line) - len(stripped)
            m = Settings.KEY_RE.match(stripped)
            if not m:
                raise SettingsError("line {0}: unsupported syntax, expected 'key: value'".format(n))
            key, value = m.group(1), m.group(2)
            if indent == 0:
                if value is None:
                    current = OrderedDict()
                    data[key] = current
                else:
                    data[key] = Settings._parse_scalar(value, n)
                    current = None
            else:
                if current is None:
                    raise SettingsError("line {0}: unexpected indentation".format(n))
                if value is None:
                    raise SettingsError("line {0}: only one level of nesting is supported".format(n))
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
                raise SettingsError("line {0}: unterminated \"string\"".format(n))
            out = []
            body = value[1:-1]
            i = 0
            while i < len(body):
                ch = body[i]
                if ch == "\\":
                    if i + 1 >= len(body) or body[i + 1] not in '\\"':
                        raise SettingsError("line {0}: only \\\\ and \\\" escapes are supported".format(n))
                    out.append(body[i + 1])
                    i += 2
                    continue
                if ch == '"':
                    raise SettingsError("line {0}: unescaped \" inside string".format(n))
                out.append(ch)
                i += 1
            return "".join(out)
        if value.startswith("'"):
            if len(value) < 2 or not value.endswith("'"):
                raise SettingsError("line {0}: unterminated 'string'".format(n))
            return value[1:-1].replace("''", "'")
        if Settings.INT_RE.match(value):
            return int(value)
        if value.lower() in Settings.AMBIGUOUS_PLAIN or value[0] in "[]{}&*!|>%@`-?:,":
            raise SettingsError("line {0}: put quotes around {1}".format(n, value))
        return value

    @staticmethod
    def _dump_scalar(value):
        if isinstance(value, int):
            return str(value)
        return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'

    @staticmethod
    def dump(data):
        lines = ["# mcrsync settings - written by mcrsync, safe to edit by hand.",
                 "# Supported: comments, 'key: value' and one level of nesting."]
        for key, value in data.items():
            if isinstance(value, dict):
                lines.append("{0}:".format(key))
                for k, v in value.items():
                    lines.append("  {0}: {1}".format(k, Settings._dump_scalar(v)))
            else:
                lines.append("{0}: {1}".format(key, Settings._dump_scalar(value)))
        return "\n".join(lines) + "\n"


# ----------------------------------------------------------------------
# rsync option helpers
# ----------------------------------------------------------------------

class RsyncFlags(object):
    """Flag checks on the user-editable rsync option list. Short flags are
    also found inside combined clusters like '-avu'."""

    SHORT_CLUSTER_RE = re.compile(r"^-[A-Za-z]+$")

    @staticmethod
    def has(opts, short, long_name):
        for o in opts:
            if o == long_name:
                return True
            if RsyncFlags.SHORT_CLUSTER_RE.match(o) and short in o[1:]:
                return True
        return False

    @staticmethod
    def without(opts, short, long_name):
        out = []
        for o in opts:
            if o == long_name:
                continue
            if RsyncFlags.SHORT_CLUSTER_RE.match(o) and short in o[1:]:
                o = "-" + o[1:].replace(short, "")
                if o == "-":
                    continue
            out.append(o)
        return out

    @staticmethod
    def risky(opts):
        """Flags that delete or overwrite data, or run other commands."""
        out = []
        for o in opts:
            name = o.split("=", 1)[0]
            if (name.startswith("--delete") or name.startswith("--append") or
                    name in ("--del", "--remove-source-files", "--force", "--inplace",
                             "--rsync-path", "--rsh") or
                    (RsyncFlags.SHORT_CLUSTER_RE.match(o) and "e" in o[1:])):
                out.append(o)
        return out

    @staticmethod
    def is_dry_run(opts):
        return RsyncFlags.has(opts, "n", "--dry-run")

    @staticmethod
    def has_update(opts):
        return RsyncFlags.has(opts, "u", "--update")


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


class LocalFs(object):
    is_remote = False
    spec = None
    pathmod = os.path

    def label(self):
        return "local"

    def display_path(self, path):
        return path

    def rsync_arg(self, path):
        return path

    def resolve(self, path):
        return os.path.abspath(os.path.expanduser(path))

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
            raise FsError("'{0}' already exists".format(os.path.basename(new)))
        if not os.path.isdir(old) or os.path.islink(old):
            try:
                os.link(old, new, follow_symlinks=False)
            except FileExistsError:
                raise FsError("'{0}' already exists".format(os.path.basename(new)))
            except OSError:
                pass
            else:
                try:
                    os.unlink(old)
                except OSError as e:
                    raise FsError("copied to the new name but could not remove the old "
                                  "name: {0}".format(e))
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


class SshConnection(object):
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
        return "{0}@{1}".format(self.spec.user, self.spec.host)

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
            return False, "could not start ssh: {0}".format(e)
        if rc == 0 and self.check():
            return True, ""
        try:
            with open(self.log_path, "r") as log:
                err = log.read().strip()
        except (IOError, OSError):
            err = ""
        return False, err or "ssh exited with code {0}".format(rc)

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
            octal = "".join("\\{0:03o}".format(b) for b in os.fsencode(a))
            # the trailing x protects trailing newlines from $(...)
            prologue.append("a{0}=$(printf '{1}'; printf x); a{0}=${{a{0}%x}}".format(i, octal))
        prologue.append("set -- " + " ".join('"$a{0}"'.format(i) for i in range(1, len(args) + 1)))
        stdin_data = ("\n".join(prologue) + "\n" + script + "\n").encode("ascii")
        cmd = ["ssh"] + self.client_opts() + ["--", self.dest(), "sh -s"]
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE)
        except OSError as e:
            return 255, b"", "could not start ssh: {0}".format(e)
        try:
            out, err = proc.communicate(stdin_data, timeout=SshConnection.RUN_TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            return 124, b"", "remote command timed out after {0}s".format(
                SshConnection.RUN_TIMEOUT)
        return proc.returncode, out, os.fsdecode(err)


class RemoteFs(object):
    """Filesystem operations on a remote host, using only POSIX sh, so it
    works on GNU/Linux as well as busybox (Synology) hosts."""

    is_remote = True
    pathmod = posixpath

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
        return "{0}@{1}:{2}".format(self.spec.user, self.spec.host, path)

    def _run(self, script, *args):
        rc, out, err = self.conn.run(script, args)
        if rc == 255 and not self.conn.check():
            raise ConnectionLost("connection to {0} is closed".format(self.spec.key()))
        return rc, out, err

    def _run_ok(self, script, *args):
        rc, out, err = self._run(script, *args)
        if rc != 0:
            raise FsError(err.strip() or "remote command failed (exit {0})".format(rc))
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
            raise FsError("remote shell reported no absolute path for {0!r}".format(path))
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
            raise FsError(err.strip() or "remote command failed (exit {0})".format(rc))
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


class ConnectionRegistry(object):
    """All SSH connections of this session, keyed by user@host:port. Queue
    items may refer to connections that aren't open (e.g. after loading a
    queue); those are opened on demand via open_interactive()."""

    def __init__(self):
        self.conns = {}
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
                                     os.path.join(self.sock_dir, "{0}.sock".format(n)),
                                     os.path.join(self.sock_dir, "{0}.log".format(n)))
                self.conns[spec.key()] = conn
            elif spec.keyfile != conn.spec.keyfile:
                conn.spec = spec
            return conn

    def fs_for(self, spec):
        if spec is None:
            return LOCAL_FS
        return RemoteFs(self.get(spec))

    def is_open(self, spec):
        if spec is None:
            return True
        conn = self.conns.get(spec.key())
        return conn is not None and conn.check()

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
        """Suspend curses, open each connection in the plain terminal so ssh
        can prompt, then restore the UI. Returns the specs that failed."""
        failed = []
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
                print("mcrsync: connecting to {0}{1} ...".format(
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
                    print("Connection FAILED: {0}".format(err))
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
    "created_files": re.compile(r"Number of created files:\s*([\d,]+)"),
    "bytes": re.compile(r"Total transferred file size:\s*([\d,]+)"),
    "total_size": re.compile(r"Total file size:\s*([\d,]+)"),
    "file_count": re.compile(r"Number of files:\s*[\d,]+(?: \(([^)]*)\))?"),
}


class Progress(object):
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
        return self.unchanged_bytes + self.copied_bytes

    def feed_conflict(self):
        """A file skipped because the target is newer: checked, but rsync
        doesn't say how big it is."""
        self.conflicts += 1

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
            self.phase = Progress.DELETING
            self.deleted += 1
            self.current = name
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
        if self.phase in (Progress.SCANNING, Progress.FINISHING):
            self.phase = Progress.COPYING

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
                todo += " ({0}, {1:,} files to review)".format(
                    Progress.short_size(self.total_bytes), self.total_files)
            parts.append(todo)
        else:
            if self.total_bytes is not None:
                # the estimate may be a bit stale - never show more than 100%
                total = max(self.total_bytes, self.reviewed_bytes)
                pct = 100.0 * self.reviewed_bytes / total if total else 100.0
                parts.append("reviewed {0} of {1} ({2:.0f}%)".format(
                    Progress.short_size(self.reviewed_bytes), Progress.short_size(total), pct))
            else:
                parts.append("reviewed {0}".format(Progress.short_size(self.reviewed_bytes)))
            if self.total_files is not None:
                parts.append("{0:,} of {1:,} files".format(
                    self.reviewed_files, max(self.total_files, self.reviewed_files)))
            else:
                parts.append("{0:,} files".format(self.reviewed_files))
            parts.append("{0} {1:,} file{2}, {3}".format(
                "would copy" if self.dry_run else "copied", self.copied_files,
                "" if self.copied_files == 1 else "s", Progress.short_size(self.copied_bytes)))
            if self.speed is not None and not self.dry_run:
                parts.append(Progress.short_size(self.speed) + "/s")
            if self.conflicts:
                parts.append("{0:,} conflict{1}".format(
                    self.conflicts, "" if self.conflicts == 1 else "s"))
            if self.deleted:
                parts.append("{0:,} {1}".format(
                    self.deleted, "would be deleted" if self.dry_run else "DELETED"))
        elapsed = int(time.time() - self.started)
        if elapsed < 60:
            parts.append("{0}s".format(elapsed))
        elif elapsed < 3600:
            parts.append("{0}m {1:02d}s".format(elapsed // 60, elapsed % 60))
        else:
            parts.append(format_duration(elapsed))
        if self.current and self.phase in (Progress.COPYING, Progress.DELETING):
            parts.append("now: " + self.current)
        return " | ".join(parts)


class RsyncRunner(object):
    """Builds and runs rsync for queue items between any combination of a
    local and (at most one) remote side, and applies conflict resolutions.

    Conflicts (target newer than source) are taken from rsync itself: with
    -u and --info=skip1 it prints '<relpath> is newer' for every file it
    skipped for that reason - so there's no separate pre-scan."""

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

    def __init__(self, registry):
        self.registry = registry

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
                return "{0}DELETED: {1}".format(would, name)
            if kind in "<>":
                return "{0}copied: {1} ({2})".format(would, name, Progress.short_size(size))
            if kind == "c":
                return "{0}created: {1}".format(would, name)
            return None
        if line.endswith(RsyncRunner.NEWER_SUFFIX):
            return "CONFLICT (target newer, not copied): {0}".format(
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
            raise FsError("could not start rsync: {0}".format(e))
        return proc.returncode, [os.fsdecode(l) for l in out.split(b"\n")]

    def check_target(self, item, src_fs, dst_fs):
        """Refuse item shapes where rsync would do something other than the
        obvious copy. Returns an error text or ""."""
        if not src_fs.is_remote and not dst_fs.is_remote:
            s = os.path.realpath(item.src)
            d = os.path.realpath(item.dst)
            if d == s or d.startswith(s.rstrip("/") + "/"):
                return "the target is the source itself or inside it"
        if item.is_dir:
            if dst_fs.exists(item.dst) and not dst_fs.isdir(item.dst):
                return "a file with that name already exists in the target"
        elif dst_fs.isdir(item.dst):
            # rsync would copy the file INTO that folder instead
            return "a folder with that name already exists in the target"
        return ""

    def run_item(self, item, log_cb, rsync_opts=None):
        """Run one queue item's rsync copy. Mutates item in place.
        log_cb(str) is called for progress/log lines (thread-safe queue push).
        rsync_opts is the user-editable (expert mode) list of rsync flags;
        defaults to DEFAULT_RSYNC_OPTS if not supplied.
        Returns False if the ssh connection dropped - the item is then reset
        to NOT SYNCED so it can be retried after reconnecting."""
        if rsync_opts is None:
            rsync_opts = DEFAULT_RSYNC_OPTS
        dry_run = RsyncFlags.is_dry_run(rsync_opts)
        item.status = STATUS_RUNNING
        item.conflicts = []
        item.copied_list = []
        item.errors_list = []
        item.error_text = ""
        item.copied_files = 0
        item.bytes_transferred = 0
        item.progress = Progress(dry_run, item.size_bytes, item.size_files)

        # Our "don't clobber newer target files" safety net relies on -u.
        if not RsyncFlags.has_update(rsync_opts):
            log_cb("WARNING: -u/--update not in rsync options - newer target "
                   "files may be overwritten for {0}".format(item.src_display()))

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
            # added last so they win over e.g. a user's --progress
            cmd = self.build_cmd(list(rsync_opts) + ["--info=skip1,progress2,name2",
                                                     RsyncRunner.OUT_FORMAT],
                                 src_fs, item.src, dst_fs, item.dst, item.is_dir)
        except ConnectionLost as e:
            item.status = STATUS_PENDING
            log_cb("CONNECTION LOST: {0}".format(e))
            return False
        except FsError as e:
            self._fail(item, "could not prepare target: {0}".format(e))
            return True

        transfer_start = time.time()
        try:
            proc = RsyncRunner._spawn(cmd)
        except OSError as e:
            self._fail(item, "could not start rsync: {0}".format(e))
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
            log_cb("CONNECTION LOST to {0} during {1}".format(remote.spec.key(), item.src_display()))
            return False

        copied, newer, errors = RsyncRunner.parse_output(out_lines)
        copied_remark = "would copy (dry run)" if dry_run else "OK"
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

        # parse stats block
        text = "\n".join(out_lines)
        m = STATS_RE["files_transferred"].search(text)
        if m:
            item.copied_files = int(m.group(1).replace(",", ""))
        m = STATS_RE["bytes"].search(text)
        if m:
            item.bytes_transferred = int(m.group(1).replace(",", ""))

        item.skipped_files = len(item.conflicts)

        if rc != 0:
            detail = errors[0] if errors else ""
            self._fail(item, "rsync exited with code {0}{1}".format(
                rc, (" - " + detail) if detail else ""))
        elif item.conflicts:
            item.status = STATUS_DRY_INCOMPLETE if dry_run else STATUS_INCOMPLETE
        elif dry_run:
            item.status = STATUS_DRY_OK
        else:
            item.status = STATUS_OK
            item.transfer_seconds = transfer_end - transfer_start
        return True

    def _fail(self, item, text):
        item.status = STATUS_FAIL
        item.error_text = text
        item.errors_list.append((item.src_display(), item.dst_display(), text))

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
            raise FsError(errors[0] if errors else "rsync exited with code {0}".format(rc))
        if not copied:
            raise FsError("'{0}' already exists - nothing was overwritten".format(
                dst_fs.pathmod.basename(dst)))

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
                newname = "{0}.conflict-{1}{2}".format(base, int(time.time()), ext)
                dst_fs.rename(conflict.dst_full, newname)
                try:
                    self.copy_new_file(src_fs, conflict.src_full, dst_fs, conflict.dst_full)
                except FsError as e:
                    try:
                        dst_fs.rename(newname, conflict.dst_full)
                        where = "the existing file was put back"
                    except FsError:
                        where = "the existing file is now named {0}".format(
                            dst_fs.pathmod.basename(newname))
                    raise FsError("copy failed ({0}); {1}".format(e, where))
                conflict.remark = "existing renamed to {0}".format(dst_fs.pathmod.basename(newname))
            elif action == "autorename":
                # copy the INCOMING file in under a new auto-generated name,
                # leaving the existing target file untouched
                newname = "{0}.new-{1}{2}".format(base, int(time.time()), ext)
                self.copy_new_file(src_fs, conflict.src_full, dst_fs, newname)
                conflict.remark = "auto-renamed incoming to {0}".format(dst_fs.pathmod.basename(newname))
            conflict.resolution = action
            return True, ""
        except FsError as e:
            return False, str(e)


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


CTRL_U = 21


def edit_text(text, c):
    """Apply key c (from read_key) to the char list text. Returns True if
    it was an editing key. Without a cursor, Left/Right are ignored rather
    than inserted as escape-sequence garbage."""
    if c in (curses.KEY_BACKSPACE, 127, 8):
        if text:
            text.pop()
    elif c == CTRL_U:
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
            if c == 27:
                return None
            if c in (10, 13, curses.KEY_ENTER):
                return "".join(text)
            edit_text(text, c)
    finally:
        curses.curs_set(0)


def confirm(stdscr, prompt):
    h, w = stdscr.getmaxyx()
    lines = textwrap.wrap(prompt + " (y/n)", max(10, w - 4)) or [""]
    lines = lines[:max(1, h - 3)]
    win = curses.newwin(len(lines) + 2, w, h - len(lines) - 2, 0)
    win.border()
    for i, line in enumerate(lines):
        safe_addstr(win, 1 + i, 2, line)
    win.noutrefresh()
    curses.doupdate()
    while True:
        c = win.getch()
        if c in (ord('y'), ord('Y')):
            return True
        if c in (ord('n'), ord('N'), 27):
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
        if c == 27:  # Esc
            curses.curs_set(0)
            return False, ""
        elif c in (9, curses.KEY_DOWN):  # Tab
            focus = {"field": "ok", "ok": "cancel", "cancel": "field"}[focus]
        elif c in (curses.KEY_BTAB, curses.KEY_UP):
            focus = {"field": "cancel", "ok": "field", "cancel": "ok"}[focus]
        elif c in (curses.KEY_LEFT, curses.KEY_RIGHT) and focus != "field":
            focus = "cancel" if focus == "ok" else "ok"
        elif c in (10, 13, curses.KEY_ENTER):  # Enter
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
                "48 x {0}.".format(n + 10))
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
            safe_addstr(win, n + 4, 2, error, curses.color_pair(4) | curses.A_BOLD)
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
        if c == 27:  # Esc
            curses.curs_set(0)
            return None
        elif c in (9, curses.KEY_DOWN):
            focus = (focus + 1) % (n + 2)
        elif c in (curses.KEY_BTAB, curses.KEY_UP):
            focus = (focus - 1) % (n + 2)
        elif c in (curses.KEY_LEFT, curses.KEY_RIGHT) and focus >= n:
            focus = n + 1 if focus == n else n
        elif c in (10, 13, curses.KEY_ENTER):
            if focus < n - 1:
                focus += 1
                continue
            curses.curs_set(0)
            if focus == n + 1:
                return None
            return ["".join(v) for v in values]
        elif focus < n:
            edit_text(values[focus], c)


def flash(win):
    curses.flash()


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

class Pane(object):
    def __init__(self, path, fs):
        self.fs = fs
        self.path = fs.resolve(path)
        # where to go back to when a remote pane is disconnected
        self.local_path = self.path
        self.entries = []
        self.error = ""
        self.index = 0
        self.scroll = 0
        self.reload()

    def reload(self):
        try:
            self.entries = self.fs.list_dir(self.path)
            self.error = ""
        except ConnectionLost as e:
            self.entries = []
            self.error = "{0} - press F5 to reconnect".format(e)
        except FsError as e:
            self.entries = []
            self.error = str(e)
        if self.index >= len(self.entries):
            self.index = max(0, len(self.entries) - 1)

    def switch_fs(self, fs, path):
        self.fs = fs
        self.path = path
        self.index = 0
        self.scroll = 0
        self.reload()

    def selected(self):
        if not self.entries:
            return None
        return self.entries[self.index]

    def selected_full(self):
        sel = self.selected()
        if sel is None:
            return None
        return self.fs.pathmod.join(self.path, sel[0])

    def go_parent(self):
        parent = self.fs.pathmod.dirname(self.path.rstrip("/"))
        if parent and parent != self.path:
            old_base = self.fs.pathmod.basename(self.path)
            self.path = parent
            self.reload()
            for i, (name, isdir) in enumerate(self.entries):
                if name == old_base:
                    self.index = i
                    break

    def enter(self):
        sel = self.selected()
        if sel and sel[1]:
            self.path = self.fs.pathmod.join(self.path, sel[0])
            self.index = 0
            self.scroll = 0
            self.reload()

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
    "mcrsync - Midnight-Commander-style rsync queue manager",
    "",
    "GLOBAL KEYS (work in every view)",
    "  ?  or  h        show this help (press again / Esc to close)",
    "  t               toggle between NAV view and QUEUE editor view",
    "  v               open/close the VERIFY view",
    "  g               open/close the RUN LOG: every copied/created/deleted",
    "                  file, conflict, error and connection event of this",
    "                  session, with times. It follows new lines; scroll",
    "                  with Up/Down/PgUp/PgDn/Home, End follows again.",
    "                  Keeps the last 5000 lines (not saved to disk).",
    "  s               save the current queue (+ rsync options) to a file",
    "                  (asks before overwriting an existing file)",
    "  l               load a queue (+ rsync options) from a file. If the",
    "                  file's rsync flags differ from yours, they are shown",
    "                  in full and you choose whether to use them. Not",
    "                  possible while a run is in progress.",
    "  F8              open the rsync-options review screen, then run the queue",
    "  p               pause / resume queue execution (takes effect between",
    "                  items, not mid-transfer - rsync itself can't be paused",
    "                  mid-file). If the run paused because an SSH",
    "                  connection was lost, p logs in again first.",
    "  q               quit (asks for confirmation)",
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
    "NAVIGATE VIEW (left = SOURCE pane, right = TARGET pane, bottom = last",
    "5 queued items)",
    "  Tab             switch active pane between SOURCE (left) and",
    "                  TARGET (right)",
    "  Up / Down       move selection within the active pane",
    "  Left            go to the parent directory of the active pane",
    "  Right / Enter   enter the highlighted directory",
    "  PgUp / PgDn     scroll a full page within the active pane",
    "  F5              refresh both panes from disk (in case files changed",
    "                  outside mcrsync). Selection is kept if the same",
    "                  name still exists. This also happens automatically",
    "                  once a full queue run finishes. On a remote pane",
    "                  whose connection was lost, F5 offers to reconnect.",
    "  c               queue a copy: selected item in the LEFT (source)",
    "                  pane -> current directory shown in the RIGHT",
    "                  (target) pane. Only works when the LEFT pane is",
    "                  active - if you press c while the RIGHT pane is",
    "                  active the screen will flash twice and nothing",
    "                  is queued.",
    "  F2              rename the selected file/folder (in either pane).",
    "                  Opens a popup: type the new name, then Tab to move",
    "                  between the text field, [OK], and [Cancel]. Enter",
    "                  activates whichever is focused (so Enter in the",
    "                  text field = OK). Esc always cancels. If a file",
    "                  or folder with the new name already exists, the",
    "                  rename is refused rather than overwriting it.",
    "  Insert          create a new folder in the active pane's current",
    "                  directory (in either pane). Same popup style as",
    "                  rename: type the name, OK/Cancel/Tab/Enter/Esc.",
    "                  Refuses to create it if something with that name",
    "                  already exists.",
    "  o               connect the active pane to a remote host over SSH,",
    "                  or - if it already is remote - disconnect it and go",
    "                  back to the local folder it showed before.",
    "",
    "REMOTE (SSH) PANES",
    "  Either pane can show a remote host instead of the local disk, so you",
    "  can push (local SOURCE -> remote TARGET) or pull (remote SOURCE ->",
    "  local TARGET). Only one pane can be remote at a time, because rsync",
    "  can't copy directly between two remote hosts.",
    "",
    "  Pressing o opens a form:",
    "    User          login name on the remote host",
    "    Host          host name or IPv4 address (IPv6 isn't supported)",
    "    Port          SSH port, normally 22",
    "    Base path     folder the pane starts in; blank or ~ = home",
    "                  directory. You can still go above it with Left.",
    "    Key file      private key file (e.g. ~/.ssh/id_ed25519). Leave it",
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
    "    - in a pane: it shows the error; press F5 to reconnect.",
    "    - during a run: the run pauses before the next item and the",
    "      status bar says so. Press p to log in again; the interrupted",
    "      item is then retried from the start (rsync skips what was",
    "      already copied).",
    "  Queued items remember their host. When you load a saved queue or",
    "  press F8, mcrsync asks you to log in to any host the queue needs",
    "  that isn't connected yet.",
    "",
    "QUEUE EDITOR VIEW (single pane: size, files, #, source, dest, status)",
    "  Up / Down       move selection",
    "  PgUp / PgDn     scroll a full page",
    "  d  or  Delete   remove the selected item from the queue (confirms).",
    "                  If a background size estimate was still being",
    "                  calculated for that item, it's cancelled right away.",
    "",
    "  SIZE and FILES columns show the item's total size (e.g. '400.0 M')",
    "  and number of files - everything rsync will review, whether it",
    "  already exists on the target or not (symlinks count as links, not",
    "  as what they point to). Computed in the background so it never",
    "  blocks the UI; the live progress line measures against it:",
    "    ...      still being estimated - check back in a moment",
    "    nan      size couldn't be determined (e.g. permission error)",
    "    offline  remote source that isn't connected yet - estimated",
    "             once you connect (F8 or o)",
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
    "    NOT SYNCED    queued, not yet attempted",
    "    IN PROGRESS   currently being copied",
    "    SYNCED OK     completed cleanly",
    "    SYNC FAIL     rsync returned a hard error",
    "    INCOMPLETE    copy ran, but some files were skipped because the",
    "                  target copy was newer than the source (needs your",
    "                  attention in the Verify view)",
    "    DRY RUN OK    a dry run (-n) found nothing wrong. Nothing was",
    "                  copied, so a real run will still process it.",
    "    DRY RUN INCOMPLETE",
    "                  a dry run found files that are newer in the target",
    "                  (listed in Verify -> SKIPPED, but can't be resolved",
    "                  until a real run)",
    "",
    "RSYNC OPTIONS SCREEN (opened by F8, before anything actually runs)",
    "  These flags are GLOBAL - they apply to every item in the queue.",
    "  e               edit the flags as a free-form string (shell-style",
    "                  quoting is respected)",
    "  d               restore the built-in defaults",
    "                  (-a -u --stats --itemize-changes)",
    "  Enter           confirm and start the run. If -u/--update is not",
    "                  in the flags you'll get an extra confirmation,",
    "                  because that flag is what protects newer target",
    "                  files from being overwritten.",
    "  Esc             cancel, go back without starting anything",
    "",
    "  Note: --stats fills in the Verify view's counts; removing it is fine",
    "  for rsync itself, but Verify will have less detail to show. The",
    "  per-file list in Verify works with or without --itemize-changes.",
    "  mcrsync always adds --info=skip1,progress2,name2 and its own",
    "  --out-format (files skipped because the target is newer, live",
    "  progress, and one line per checked file with its size) and, for",
    "  remote items, -s -e 'ssh ...'.",
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
    "    preparing   checking the target, creating its folder",
    "    scanning    rsync builds the file list - on big folders over SSH",
    "                this can take minutes before anything is copied",
    "    copying     files are being checked and transferred; 'now:' is",
    "                the file being copied",
    "    deleting    (red) only with --delete... flags: rsync is removing",
    "                files from the target",
    "    finishing   all files checked; rsync sets folder times and",
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
    "  Tab             cycle through tabs: COPIED / SKIPPED / RENAMED /",
    "                  ERRORS",
    "  Up / Down       move selection",
    "  PgUp / PgDn     scroll a full page",
    "  Enter           on a row in the SKIPPED tab, resolve that",
    "                  conflicting file: choose Skip / Rename existing /",
    "                  Autorename incoming. Once every conflict on an",
    "                  item is resolved it flips back to SYNCED OK.",
    "  Esc  or  v      back to the previous view",
    "",
    "  Tab meanings:",
    "    COPIED        files rsync actually transferred",
    "    SKIPPED       unresolved conflicts (target was newer than",
    "                  source) - these need you to pick a resolution",
    "    RENAMED       conflicts you've already resolved with Rename or",
    "                  Autorename, and what they were renamed to",
    "    ERRORS        hard failures (non-zero rsync exit code, etc.)",
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
    "  - The F8 screen always shows all rsync flags and marks those that",
    "    can delete or overwrite data or run commands (--delete...,",
    "    --remove-source-files, --inplace, --append, --rsync-path, -e).",
    "  - The item being copied can't be deleted from the queue; other",
    "    items can, and the run picks up the change.",
    "",
    "Press Esc, h, or ? to close this help.",
]

VERIFY_TABS = ["COPIED", "SKIPPED", "RENAMED", "ERRORS"]


class App(object):
    def __init__(self, stdscr, start_path):
        self.stdscr = stdscr
        self.view = VIEW_NAV
        self.registry = ConnectionRegistry()
        self.runner = RsyncRunner(self.registry)
        self.settings = Settings()
        self.settings.load()
        self.left = Pane(start_path, LOCAL_FS)
        self.right = Pane(start_path, LOCAL_FS)
        self.active = "left"
        self.queue = []                 # list[QueueItem]
        self.queue_index = 0
        self.log_lines = []             # running log, used for bottom pane / run log
        self.paused = False
        self.running = False
        self.run_thread = None
        self.lock = threading.Lock()
        self.verify_tab = 0
        self.verify_index = 0
        self.flash_count = 0
        self.rsync_opts = list(DEFAULT_RSYNC_OPTS)
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
        try:
            curses.start_color()
            curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_CYAN)   # active pane hdr
            curses.init_pair(2, curses.COLOR_YELLOW, -1)                  # dirs
            curses.init_pair(3, curses.COLOR_GREEN, -1)                   # OK
            curses.init_pair(4, curses.COLOR_RED, -1)                     # FAIL
            curses.init_pair(5, curses.COLOR_MAGENTA, -1)                 # INCOMPLETE
            curses.init_pair(6, curses.COLOR_WHITE, curses.COLOR_BLUE)    # header bar
            curses.init_pair(7, curses.COLOR_CYAN, -1)                    # DRY RUN OK
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
            return "Waiting: connection lost - press p to log in again"
        it = self.running_item()
        if it is None:
            return "Paused between items - press p to resume" if self.paused else "Starting next item..."
        n = self.queue.index(it) + 1 if it in self.queue else 0
        return "#{0} {1} | {2}".format(n, it.src_display(), it.progress.describe())

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
                self.log("Size estimate failed for {0}: {1}".format(item.src_display(), e))
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

    def save_queue(self, path):
        data = {
            "rsync_opts": self.rsync_opts,
            "items": [it.to_dict() for it in self.queue],
        }
        atomic_write(path, json.dumps(data, indent=2))

    @staticmethod
    def read_queue_file(path):
        """Parse and validate a queue file. Returns (items, rsync_opts or
        None for the old opts-less format); raises ValueError/KeyError/
        OSError. Nothing is applied, so a bad file changes nothing."""
        with open(path, "r") as f:
            data = json.load(f)
        if isinstance(data, list):
            # backward compatibility with the old (opts-less) format
            return [QueueItem.from_dict(d) for d in data], None
        if not isinstance(data, dict):
            raise ValueError("not a queue file")
        opts = data.get("rsync_opts", list(DEFAULT_RSYNC_OPTS))
        if (not isinstance(opts, list) or not opts or
                not all(isinstance(o, str) and "\0" not in o for o in opts)):
            raise ValueError("invalid rsync_opts in queue file")
        return [QueueItem.from_dict(d) for d in data.get("items", [])], opts

    def load_queue_interactive(self, path):
        try:
            items, opts = App.read_queue_file(path)
        except KeyError as e:
            message(self.stdscr, "Load failed: an item is missing the field {0}. "
                    "Nothing was loaded.".format(e))
            return
        except (OSError, ValueError) as e:
            message(self.stdscr, "Load failed: {0}. Nothing was loaded.".format(e))
            return
        use_opts = None
        if opts is not None and opts != self.rsync_opts:
            # Flags in a file are someone else's choice - show them in full,
            # quoted, so a token with spaces can't disguise itself.
            shown = " ".join(shlex.quote(o) for o in opts)
            question = "This queue file also sets the rsync flags: {0} . ".format(shown)
            risky = RsyncFlags.risky(opts)
            if risky:
                question += ("WARNING - these can delete or overwrite data or run commands: "
                             "{0} . ".format(" ".join(shlex.quote(o) for o in risky)))
            question += "Use these flags? (n = keep your current flags)"
            if confirm(self.stdscr, question):
                use_opts = opts
        self.load_queue(items, use_opts)
        hosts = sorted(set(s.key() for it in self.queue for s in it.conn_specs()))
        extra = ""
        if hosts:
            extra = (". It uses remote host(s) {0} - you'll be asked to log in "
                     "when you press F8.".format(", ".join(hosts)))
        message(self.stdscr, "Loaded {0} item(s) from {1}{2}".format(
            len(self.queue), path, extra))

    def load_queue(self, items, rsync_opts=None):
        self.queue = items
        self.queue_index = 0
        if rsync_opts is not None:
            self.rsync_opts = rsync_opts
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
        self.run_dry = RsyncFlags.is_dry_run(self.rsync_opts)
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
                # may touch - so pause and let the user trigger it with p.
                dead = self.registry.dead_specs(item.conn_specs())
                if dead:
                    self.reconnect_needed = dead
                    self.paused = True
                    self.log("CONNECTION LOST: {0} - press p to log in again and resume".format(
                        ", ".join(s.key() for s in dead)))
                    continue
                self.queue_index = idx
                self.log("Starting: {0} -> {1}".format(item.src_display(), item.dst_display()))
                if self.runner.run_item(item, self.log, self.rsync_opts):
                    break
            self.log("Finished [{0}]: {1}".format(item.status, item.src_display()))
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
                message(self.stdscr, "Still not connected to {0} - the run stays paused. "
                        "Press p to try again.".format(", ".join(s.key() for s in failed)))
                return
            self.reconnect_needed = []
            self.reschedule_offline_sizes()
        self.paused = not self.paused
        self.log("PAUSED" if self.paused else "RESUMED")

    def ensure_queue_connections(self):
        """Log in to every host that unfinished queue items need. Returns
        False (after telling the user) if any login failed."""
        specs = []
        for it in self.queue:
            if it.status != STATUS_OK:
                specs += it.conn_specs()
        dead = self.registry.dead_specs(specs)
        if not dead:
            return True
        failed = self.registry.open_interactive(self.stdscr, dead)
        self.reschedule_offline_sizes()
        if failed:
            message(self.stdscr, "Could not connect to {0} - the run was not started.".format(
                ", ".join(s.key() for s in failed)))
            return False
        return True

    # ---------------- remote panes ----------------
    def pane_name(self, pane):
        return "SOURCE" if pane is self.left else "TARGET"

    def toggle_remote(self, pane):
        if pane.fs.is_remote:
            self.disconnect_pane(pane)
        else:
            self.connect_pane(pane)

    def connect_pane(self, pane):
        other = self.right if pane is self.left else self.left
        if other.fs.is_remote:
            message(self.stdscr, "The {0} pane is already connected to {1}. rsync can't copy "
                    "between two remote hosts, so only one pane can be remote - press o in "
                    "the {0} pane to disconnect it first.".format(
                        self.pane_name(other), other.fs.spec.key()))
            return
        if self.settings.error:
            message(self.stdscr, "Could not read saved connection settings ({0}). The form "
                    "starts empty and won't be saved until the file is fixed.".format(
                        self.settings.error))
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
            result = form_dialog(self.stdscr, "Connect {0} pane via SSH".format(
                self.pane_name(pane)), fields, error)
            if result is None:
                return
            values = [v.strip() for v in result]
            spec, error = self.validate_remote_form(values)
            if not error:
                break

        self.settings.set_section("last_remote", OrderedDict([
            ("user", spec.user),
            ("host", spec.host),
            ("port", spec.port),
            ("base_path", values[3]),
            ("keyfile", spec.keyfile),
        ]))
        if not self.settings.error:
            err = self.settings.save()
            if err:
                self.log(err)

        if not self.registry.is_open(spec):
            if self.registry.open_interactive(self.stdscr, [spec]):
                return
        fs = self.registry.fs_for(spec)
        try:
            path = fs.resolve(values[3])
        except FsError as e:
            try:
                path = fs.resolve("")
            except FsError as e2:
                message(self.stdscr, "Connected, but can't list the remote home directory: "
                        "{0}".format(e2))
                return
            message(self.stdscr, "Base path '{0}' can't be opened ({1}) - showing the home "
                    "directory instead.".format(values[3], e))
        pane.local_path = pane.path
        pane.switch_fs(fs, path)
        self.reschedule_offline_sizes()
        self.log("Connected {0} pane to {1}".format(self.pane_name(pane), spec.key()))

    def validate_remote_form(self, values):
        user, host, port, base, keyfile = values
        return ConnSpec.from_form(user, host, port, keyfile)

    def disconnect_pane(self, pane):
        spec = pane.fs.spec
        users = [it for it in self.queue
                 if it.status != STATUS_OK and
                 spec.key() in [s.key() for s in it.conn_specs()]]
        if users and self.running:
            message(self.stdscr, "The running queue uses {0} - wait for it to finish "
                    "before disconnecting.".format(spec.key()))
            return
        question = "Disconnect the {0} pane from {1} and go back to {2}?".format(
            self.pane_name(pane), spec.key(), pane.local_path)
        if users:
            question += (" {0} unfinished queue item(s) use this host; you'll be asked "
                         "to log in again when you press F8.".format(len(users)))
        if not confirm(self.stdscr, question):
            return
        self.registry.close(spec)
        pane.switch_fs(LOCAL_FS, pane.local_path)
        self.log("Disconnected {0} pane from {1}".format(self.pane_name(pane), spec.key()))

    def reconnect_panes_if_needed(self):
        for pane in (self.left, self.right):
            if pane.fs.is_remote and not self.registry.is_open(pane.fs.spec):
                if confirm(self.stdscr, "The connection to {0} is closed. Log in again?".format(
                        pane.fs.spec.key())):
                    self.registry.open_interactive(self.stdscr, [pane.fs.spec])
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
                                 "{0} file(s) transferred".format(it.copied_files)))
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

    def too_small(self):
        h, w = self.stdscr.getmaxyx()
        return h < App.MIN_H or w < App.MIN_W

    def draw_too_small(self, h, w):
        lines = ["Terminal too small",
                 "({0} x {1}, need {2} x {3})".format(w, h, App.MIN_W, App.MIN_H),
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
            ssh_key = "o:ssh-disconnect" if pane.fs.is_remote else "o:ssh-connect"
            help_text = ("?/h:help  Tab:switch  <-/->:nav  c:queue-copy  " + ssh_key +
                          "  F2:rename  Ins:mkdir  F5:refresh  t:queue-view  "
                          "v:verify  g:log  s:save  l:load  F8:run  p:pause  q:quit")
        elif self.view == VIEW_QUEUE:
            help_text = ("?/h:help  t:nav-view  v:verify  g:log  s:save  l:load  F8:run  "
                          "p:pause  PgUp/PgDn:scroll  q:quit")
        elif self.view == VIEW_VERIFY:
            help_text = "?/h:help  Tab:cycle-tab  Enter:resolve  Esc/v:back  q:quit"
        elif self.view == VIEW_HELP:
            help_text = "Esc/h/?:close  Up/Down/PgUp/PgDn:scroll  q:quit"
        elif self.view == VIEW_LOG:
            help_text = "Esc/g:close  Up/Down/PgUp/PgDn:scroll  End:follow new lines  q:quit"
        else:
            help_text = "?/h:help  e:edit flags  d:defaults  Enter:confirm&run  Esc:cancel  q:quit"
        # Run state goes first so a narrow terminal can't cut it off.
        run_state = ""
        if self.running:
            if self.paused and self.reconnect_needed:
                state = "PAUSED - connection lost, press p to log in again"
            elif self.paused:
                state = "PAUSED"
            else:
                state = "RUNNING"
            run_state = "[{0}{1}] ".format("DRY RUN " if self.run_dry else "", state)
        bar = (run_state + help_text)

        # Whole-queue progress: in every view while running. It takes
        # precedence over the key hints, which get cut instead.
        right_text = ""
        if self.queue and (self.running or self.view == VIEW_QUEUE):
            percent, eta = self.compute_progress_and_eta()
            if percent is not None:
                ok_count = sum(1 for it in self.queue if it.status == STATUS_OK)
                eta_str = format_duration(eta) if eta is not None else "nan"
                right_text = " {0}/{1} items, {2:.0f}% done | ETA: {3}".format(
                    ok_count, len(self.queue), percent, eta_str)

        avail = max(0, w - 1)
        if right_text:
            room = avail - len(right_text)
            if room >= len(run_state):
                bar = bar[:room].ljust(room) + right_text
        safe_addstr(self.stdscr, h - 1, 0, bar.ljust(avail), curses.color_pair(6))

    def draw_pane(self, win, pane, title, active):
        win.erase()
        win.border()
        h, w = win.getmaxyx()
        hdr_attr = curses.color_pair(1) if active else 0
        safe_addstr(win, 0, 2, " {0} [{1}] ".format(title, pane.fs.label()),
                    hdr_attr | curses.A_BOLD)
        safe_addstr(win, 1, 2, pane.fs.display_path(pane.path)[:w - 4], curses.A_DIM)
        page_h = h - 3
        for i in range(page_h):
            idx = pane.scroll + i
            if idx >= len(pane.entries):
                break
            name, isdir = pane.entries[idx]
            label = ("/" + name) if isdir else ("  " + name)
            attr = 0
            if isdir:
                attr = curses.color_pair(2)
            if idx == pane.index:
                attr |= curses.A_REVERSE
            safe_addstr(win, 2 + i, 2, label, attr)
        if pane.error:
            for i, line in enumerate(textwrap.wrap("Can't list folder: " + pane.error,
                                                   max(10, w - 4))[:page_h]):
                safe_addstr(win, 2 + i, 2, line, curses.color_pair(4))
        elif not pane.entries:
            safe_addstr(win, 2, 2, "(empty)", curses.A_DIM)

    def draw_nav(self, h, w):
        bottom_h = 8
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
        safe_addstr(bwin, 0, 2, " QUEUE (last 5) - {0} item(s) total ".format(len(self.queue)),
                    curses.A_BOLD)
        tail = self.last_queue_tail(5)
        for i, it in enumerate(tail):
            n = len(self.queue) - len(tail) + i + 1
            line = "#{0:<4d} {1}  ->  {2}   [{3}]".format(
                n, it.src_display(), it.dst_display(), it.status)
            attr = self.status_attr(it.status)
            safe_addstr(bwin, 1 + i, 2, line, attr)
        self.draw_progress_line(bwin, bottom_h - 2)
        bwin.noutrefresh()

    def draw_progress_line(self, win, y):
        text = self.progress_text()
        if not text:
            return
        it = self.running_item()
        attr = curses.A_BOLD | curses.color_pair(7)
        if (it is not None and it.progress.phase == Progress.DELETING) or self.reconnect_needed:
            attr = curses.A_BOLD | curses.color_pair(4)
        safe_addstr(win, y, 2, "> " + text, attr)

    def status_attr(self, status):
        if status == STATUS_OK:
            return curses.color_pair(3)
        if status == STATUS_FAIL:
            return curses.color_pair(4)
        if status in (STATUS_INCOMPLETE, STATUS_DRY_INCOMPLETE):
            return curses.color_pair(5)
        if status == STATUS_DRY_OK:
            return curses.color_pair(7)
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
        safe_addstr(win, 0, 2, " QUEUE EDITOR ", curses.A_BOLD)

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
            files_str = "{0:,}".format(it.size_files) if it.size_files is not None else ""
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
            safe_addstr(win, 2 + i, x_status, it.status.ljust(200), attr)
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
        title = " RUN LOG - {0} line(s){1}{2} ".format(
            len(lines), ", oldest dropped" if self.log_dropped else "",
            "" if self.log_scroll is None else " - scrolled, End: follow new lines")
        safe_addstr(win, 0, 2, title, curses.A_BOLD)
        for i in range(page_h):
            if scroll + i >= len(lines):
                break
            line = lines[scroll + i]
            attr = 0
            if "DELETED" in line or "CONNECTION LOST" in line or line[9:].startswith("rsync"):
                attr = curses.color_pair(4)
            elif "CONFLICT" in line:
                attr = curses.color_pair(5)
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
        elif c in (27, ord('g')):
            self.view = self.prev_view

    def draw_verify(self, h, w):
        win = curses.newwin(h - 1, w, 0, 0)
        win.border()
        tabs_line = "  ".join(
            ("[" + t + "]") if i == self.verify_tab else (" " + t + " ")
            for i, t in enumerate(VERIFY_TABS))
        safe_addstr(win, 0, 2, " VERIFY: " + tabs_line + " ", curses.A_BOLD)
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

    def draw_options(self, h, w):
        win = curses.newwin(h - 1, w, 0, 0)
        win.border()
        title = " RSYNC OPTIONS (expert mode) - applies to ALL queued items "
        safe_addstr(win, 0, 2, title, curses.A_BOLD)
        # Shown in full (wrapped, quoted): flags may come from a loaded queue
        # file, and a cut-off line could hide a dangerous one.
        cur = " ".join(shlex.quote(o) for o in self.rsync_opts)
        safe_addstr(win, 2, 2, "Current command line flags:")
        flag_lines = textwrap.wrap(cur, max(10, w - 8), break_on_hyphens=False) or [""]
        y = 3
        for line in flag_lines[:max(1, h - 16)]:
            safe_addstr(win, y, 4, line, curses.A_REVERSE)
            y += 1
        safe_addstr(win, y + 1, 2, "Full rsync command that will run per item:")
        safe_addstr(win, y + 2, 4, "rsync <flags above> --info=skip1,progress2 <source>/ <dest>/",
                    curses.A_DIM)
        hosts = sorted(set(s.key() for it in self.queue for s in it.conn_specs()))
        warn_y = y + 4
        if hosts:
            safe_addstr(win, y + 3, 4, ("Remote items also get: -s -e 'ssh ...'   Hosts: " +
                                        ", ".join(hosts))[:w - 8], curses.A_DIM)
            warn_y = y + 5

        risky = RsyncFlags.risky(self.rsync_opts)
        if risky:
            safe_addstr(win, warn_y, 2,
                        "CAUTION: these flags can delete or overwrite data or run commands: " +
                        " ".join(risky), curses.color_pair(4) | curses.A_BOLD)
            warn_y += 2
        if RsyncFlags.is_dry_run(self.rsync_opts):
            safe_addstr(win, warn_y, 2,
                        "DRY RUN (-n): nothing will be copied or created. Items end up as "
                        "DRY RUN OK / DRY RUN INCOMPLETE.", curses.color_pair(7) | curses.A_BOLD)
            warn_y += 2
        if not RsyncFlags.has_update(self.rsync_opts):
            safe_addstr(win, warn_y, 2,
                        "WARNING: -u/--update is NOT set - newer files in "
                        "the target could be overwritten!", curses.color_pair(4) | curses.A_BOLD)
            warn_y += 2

        common = [
            ("-a", "archive mode (recurse, perms, times, links, etc.)"),
            ("-u / --update", "skip files newer on the receiver (recommended safety net)"),
            ("-n / --dry-run", "show what would happen, transfer nothing"),
            ("-v", "verbose output"),
            ("-z", "compress data during transfer"),
            ("--delete", "delete files in dest that no longer exist in source (DANGEROUS)"),
            ("--stats", "print transfer statistics (used for the Verify view)"),
            ("--itemize-changes", "per-file change list (used for the Verify view)"),
        ]
        safe_addstr(win, warn_y + 1, 2, "Reference - common flags:", curses.A_UNDERLINE)
        for i, (flag, desc) in enumerate(common):
            y = warn_y + 2 + i
            if y >= h - 6:
                break
            safe_addstr(win, y, 4, "{0:<16} {1}".format(flag, desc), curses.A_DIM)

        safe_addstr(win, h - 4, 2,
                    "[e] edit flags   [d] restore defaults   [Enter] confirm & run   [Esc] cancel",
                    curses.A_BOLD)
        win.noutrefresh()

    def handle_options_key(self, c):
        if c == ord('e'):
            new_line = prompt_string(self.stdscr, "rsync flags:",
                                      " ".join(shlex.quote(o) for o in self.rsync_opts))
            if new_line is None:
                return
            try:
                parsed = shlex.split(new_line)
            except ValueError as e:
                message(self.stdscr, "Could not parse flags: {0}".format(e))
                return
            if not parsed:
                message(self.stdscr, "At least one flag is required - keeping previous options.")
                return
            self.rsync_opts = parsed
        elif c == ord('d'):
            if confirm(self.stdscr, "Restore default rsync flags?"):
                self.rsync_opts = list(DEFAULT_RSYNC_OPTS)
        elif c in (10, 13):
            proceed = True
            if not RsyncFlags.has_update(self.rsync_opts):
                proceed = confirm(
                    self.stdscr,
                    "-u/--update is not set - newer target files may be "
                    "overwritten. Continue anyway?")
            if proceed and self.ensure_queue_connections():
                self.view = VIEW_NAV
                self.start_run()
        elif c == 27:
            self.view = VIEW_NAV

    def draw_help(self, h, w):
        win = curses.newwin(h - 1, w, 0, 0)
        win.border()
        safe_addstr(win, 0, 2, " HELP (Esc/h/? to close) ", curses.A_BOLD)
        page_h = h - 4
        max_scroll = max(0, len(HELP_TEXT) - page_h)
        self.help_scroll = max(0, min(self.help_scroll, max_scroll))
        for i in range(page_h):
            idx = self.help_scroll + i
            if idx >= len(HELP_TEXT):
                break
            safe_addstr(win, 2 + i, 2, HELP_TEXT[idx])
        if len(HELP_TEXT) > page_h:
            pct = 100 if max_scroll == 0 else int(100 * self.help_scroll / max_scroll)
            safe_addstr(win, h - 3, 2,
                        "-- line {0}/{1} ({2}%) - Up/Down/PgUp/PgDn to scroll --".format(
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
        elif c in (27, ord('h'), ord('?')):
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
            if self.too_small():
                # nothing is visible, so no key may act on hidden state
                if c == ord('q') and not self.running:
                    break
                continue
            if not self.handle_key(c):
                break

    def handle_key(self, c):
        if c == ord('q'):
            if confirm(self.stdscr, "Quit mcrsync?"):
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
                    message(self.stdscr, "'{0}' is a folder - type a file name.".format(name))
                    return True
                if os.path.lexists(name) and not confirm(
                        self.stdscr, "'{0}' already exists. Overwrite it?".format(name)):
                    return True
                try:
                    self.save_queue(name)
                    message(self.stdscr, "Saved {0} item(s) to {1}".format(len(self.queue), name))
                except OSError as e:
                    message(self.stdscr, "Save failed: {0}".format(e))
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
            else:
                self.view = VIEW_OPTIONS
        elif c == ord('p'):
            self.toggle_pause()
        return True

    def handle_nav_key(self, c):
        pane = self.left if self.active == "left" else self.right
        h, w = self.stdscr.getmaxyx()
        page_h = h - 8 - 4
        if c == 9:  # Tab
            self.active = "right" if self.active == "left" else "left"
        elif c == curses.KEY_F5:
            self.reconnect_panes_if_needed()
            self.refresh_panes()
        elif c == ord('o'):
            self.toggle_remote(pane)
        elif c == curses.KEY_UP:
            pane.move(-1, page_h)
        elif c == curses.KEY_DOWN:
            pane.move(1, page_h)
        elif c == curses.KEY_PPAGE:
            pane.move(-page_h, page_h)
        elif c == curses.KEY_NPAGE:
            pane.move(page_h, page_h)
        elif c == curses.KEY_LEFT:
            pane.go_parent()
        elif c == curses.KEY_RIGHT or c in (10, 13):
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
            self.log("Queued ({0}): {1} -> {2}".format(
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
            self.stdscr, "Rename", "New name for {0} '{1}':".format(kind, old_name),
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
            if pane.fs.exists(new_full):
                message(self.stdscr, "'{0}' already exists - not overwriting.".format(new_name))
                return
            pane.fs.rename(old_full, new_full)
        except FsError as e:
            message(self.stdscr, "Rename failed: {0}".format(e))
            return
        pane.reload()
        for i, (name, _) in enumerate(pane.entries):
            if name == new_name:
                pane.index = i
                break
        self.log("Renamed: {0} -> {1}".format(pane.fs.display_path(old_full), new_name))

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
            if pane.fs.exists(new_full):
                message(self.stdscr, "'{0}' already exists.".format(name))
                return
            pane.fs.mkdir(new_full)
        except FsError as e:
            message(self.stdscr, "Create folder failed: {0}".format(e))
            return
        pane.reload()
        for i, (nm, _) in enumerate(pane.entries):
            if nm == name:
                pane.index = i
                break
        self.log("Created folder: {0}".format(pane.fs.display_path(new_full)))

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
                        "Press p to pause - the run stops after this item.")
            elif self.queue and confirm(self.stdscr, "Delete selected queue item?"):
                doomed = self.queue[self.queue_index]
                doomed.cancel_event.set()
                del self.queue[self.queue_index]
                self.queue_index = max(0, self.queue_index - 1)

    def handle_verify_key(self, c):
        h, w = self.stdscr.getmaxyx()
        page_h = h - 5
        if c == 9:
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
        elif c == 27:
            self.view = VIEW_NAV
        elif c in (10, 13):
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
        if it.status != STATUS_INCOMPLETE:
            # a newer run replaced this item's results while we were looking
            return
        choice = prompt_string(
            self.stdscr,
            "{0}: (S)kip / (R)ename existing / (A)utorename incoming:".format(c.relpath), "")
        choice = (choice or "").strip().lower()
        action = {"s": "skip", "r": "rename", "a": "autorename"}.get(choice[:1] if choice else "")
        if not action:
            return
        dead = self.registry.dead_specs(it.conn_specs())
        if dead and self.registry.open_interactive(self.stdscr, dead):
            message(self.stdscr, "Not connected - nothing was changed.")
            return
        ok, err = self.runner.resolve_conflict(it, c, action)
        if ok:
            message(self.stdscr, "Resolved {0}: {1}".format(c.relpath, c.remark))
            # if any conflicts remain unresolved, item stays INCOMPLETE
            if all(cc.resolution is not None for cc in it.conflicts):
                it.status = STATUS_OK
        else:
            message(self.stdscr, "Resolution failed: {0}".format(err))


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
    try:
        app.run()
    finally:
        app.registry.close_all()


if __name__ == "__main__":
    # With keypad mode, curses waits ESCDELAY ms (default 1000) to tell a
    # lone Esc from an arrow key's escape sequence.
    os.environ.setdefault("ESCDELAY", "25")
    curses.wrapper(main)
