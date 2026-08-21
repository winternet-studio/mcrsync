#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mcrsync.py - A Midnight-Commander-style rsync queue manager.

Targets Python 3.4 (Synology chroot). Avoids f-strings, subprocess.run,
os.scandir and other 3.5+-only features on purpose.

Views:
  NAV    - left (source) / right (target) file panes + last-5 queue tail
  QUEUE  - full-screen queue editor (#, source, dest, status)
  VERIFY - post-run report: COPIED / SKIPPED / RENAMED / ERRORS tabs

Keys (NAV + QUEUE unless noted):
  Tab        toggle left/right pane        (NAV only)
  Left       go to parent dir              (NAV only)
  Right/Ent  enter directory                (NAV only)
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
import sys
import json
import time
import shutil
import threading
import subprocess
import re
import shlex
import queue as pyqueue

DEFAULT_RSYNC_OPTS = ["-a", "-u", "--stats", "--itemize-changes"]

# ----------------------------------------------------------------------
# Data model
# ----------------------------------------------------------------------

STATUS_PENDING = "NOT SYNCED"
STATUS_RUNNING = "IN PROGRESS"
STATUS_OK = "SYNCED OK"
STATUS_FAIL = "SYNC FAIL"
STATUS_INCOMPLETE = "INCOMPLETE"


class Conflict(object):
    """A file that exists in both source and dest, dest is newer."""
    def __init__(self, relpath, src_full, dst_full, src_mtime, dst_mtime):
        self.relpath = relpath
        self.src_full = src_full
        self.dst_full = dst_full
        self.src_mtime = src_mtime
        self.dst_mtime = dst_mtime
        self.resolution = None      # None / "skip" / "rename" / "autorename"
        self.remark = "target newer - needs attention"


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


class QueueItem(object):
    def __init__(self, src, dst, is_dir=True):
        self.src = src
        self.dst = dst
        self.is_dir = is_dir
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

        # Size estimate (bytes), computed lazily in the background.
        self.size_bytes = None
        # "unknown" | "pending" | "computing" | "done" | "error"
        self.size_state = "unknown"
        # Not persisted to JSON - used to abort an in-flight/queued
        # background size calculation if this item gets deleted.
        self.cancel_event = threading.Event()
        # Wall-clock seconds the rsync transfer took, recorded ONLY for
        # items that finish with SYNCED OK (used for ETA rate estimates).
        self.transfer_seconds = None

    def to_dict(self):
        return {
            "src": self.src,
            "dst": self.dst,
            "is_dir": self.is_dir,
            "status": self.status,
            "size_bytes": self.size_bytes,
            "transfer_seconds": self.transfer_seconds,
        }

    @staticmethod
    def from_dict(d):
        it = QueueItem(d["src"], d["dst"], d.get("is_dir", True))
        it.status = d.get("status", STATUS_PENDING)
        # never resume as "in progress"
        if it.status == STATUS_RUNNING:
            it.status = STATUS_PENDING
        it.size_bytes = d.get("size_bytes")
        it.transfer_seconds = d.get("transfer_seconds")
        it.size_state = "done" if it.size_bytes is not None else "unknown"
        return it


# ----------------------------------------------------------------------
# Filesystem helpers
# ----------------------------------------------------------------------

def list_dir_entries(path):
    """Return sorted list of (name, is_dir) for a directory, dirs first."""
    try:
        names = os.listdir(path)
    except OSError as e:
        return []
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


def compute_item_size(path, is_dir, cancel_event):
    """Sum up the total size in bytes of a file or directory tree. Checks
    cancel_event periodically (every 200 files) so a background scan of a
    huge directory can be aborted promptly if the queue item is deleted.
    Returns None if cancelled or if the size can't be determined at all."""
    if is_dir:
        total = 0
        seen_any = False
        count = 0
        for root, dirs, files in os.walk(path):
            if cancel_event.is_set():
                return None
            for fname in files:
                count += 1
                if count % 200 == 0 and cancel_event.is_set():
                    return None
                fp = os.path.join(root, fname)
                try:
                    total += os.path.getsize(fp)
                    seen_any = True
                except OSError:
                    continue
        if cancel_event.is_set():
            return None
        return total if (seen_any or os.path.isdir(path)) else None
    else:
        try:
            return os.path.getsize(path)
        except OSError:
            return None


def find_conflicts(src_dir, dst_dir):
    """Walk src_dir; for every file also present in dst_dir with a newer
    mtime on the dst side, record a Conflict. Local paths only."""
    conflicts = []
    if not os.path.isdir(dst_dir):
        return conflicts
    for root, dirs, files in os.walk(src_dir):
        rel_root = os.path.relpath(root, src_dir)
        for fname in files:
            src_full = os.path.join(root, fname)
            rel = fname if rel_root == "." else os.path.join(rel_root, fname)
            dst_full = os.path.join(dst_dir, rel)
            if os.path.isfile(dst_full):
                try:
                    smtime = os.path.getmtime(src_full)
                    dmtime = os.path.getmtime(dst_full)
                except OSError:
                    continue
                if dmtime > smtime:
                    conflicts.append(Conflict(rel, src_full, dst_full, smtime, dmtime))
    return conflicts


def find_file_conflict(src_file, dst_file):
    """Single-file counterpart to find_conflicts(): returns a one-item (or
    empty) list so callers can treat files and dirs uniformly."""
    if not os.path.isfile(dst_file):
        return []
    try:
        smtime = os.path.getmtime(src_file)
        dmtime = os.path.getmtime(dst_file)
    except OSError:
        return []
    if dmtime > smtime:
        return [Conflict(os.path.basename(src_file), src_file, dst_file, smtime, dmtime)]
    return []


# ----------------------------------------------------------------------
# rsync execution
# ----------------------------------------------------------------------

STATS_RE = {
    "files_transferred": re.compile(r"Number of (?:regular )?files transferred:\s*([\d,]+)"),
    "created_files": re.compile(r"Number of created files:\s*([\d,]+)"),
    "bytes": re.compile(r"Total transferred file size:\s*([\d,]+)"),
}


def run_rsync_item(item, log_cb, rsync_opts=None):
    """Run one queue item's rsync copy. Mutates item in place.
    log_cb(str) is called for progress/log lines (thread-safe queue push).
    rsync_opts is the user-editable (expert mode) list of rsync flags;
    defaults to DEFAULT_RSYNC_OPTS if not supplied."""
    if rsync_opts is None:
        rsync_opts = DEFAULT_RSYNC_OPTS
    item.status = STATUS_RUNNING

    # Our "don't clobber newer target files" safety net relies on -u.
    # If the user's expert-mode options dropped it, still detect conflicts
    # so they show up in Verify, but warn loudly in the log.
    if "-u" not in rsync_opts and "--update" not in rsync_opts:
        log_cb("WARNING: -u/--update not in rsync options - newer target "
               "files may be overwritten for {0}".format(item.src))

    if item.is_dir:
        item.conflicts = find_conflicts(item.src, item.dst)
    else:
        item.conflicts = find_file_conflict(item.src, item.dst)
    if item.conflicts:
        for c in item.conflicts:
            log_cb("CONFLICT: {0} (dst newer)".format(c.relpath))

    if item.is_dir:
        os.makedirs(item.dst, exist_ok=True)
        cmd = ["rsync"] + list(rsync_opts) + \
            [item.src.rstrip("/") + "/", item.dst.rstrip("/") + "/"]
    else:
        # Single file: destination must be the exact target FILE path, not
        # a directory. Only its parent directory should be created; passing
        # item.dst straight through (no trailing slash) tells rsync to copy
        # the source onto that exact path/name rather than into a same-named
        # directory.
        parent = os.path.dirname(item.dst)
        if parent:
            os.makedirs(parent, exist_ok=True)
        cmd = ["rsync"] + list(rsync_opts) + [item.src, item.dst]
    transfer_start = time.time()
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT,
                                 universal_newlines=True)
    except OSError as e:
        item.status = STATUS_FAIL
        item.error_text = "could not start rsync: {0}".format(e)
        item.errors_list.append((item.src, item.dst, item.error_text))
        return

    out_lines = []
    for line in proc.stdout:
        line = line.rstrip("\n")
        out_lines.append(line)
        if line.startswith((">", "<", "c", "*")):
            log_cb(line)
    proc.wait()
    transfer_end = time.time()
    rc = proc.returncode

    # parse itemized lines for a copied-files list
    for line in out_lines:
        if len(line) > 12 and line[0] in ">c<*" and " " in line:
            parts = line.split(" ", 1)
            if len(parts) == 2:
                relname = parts[1]
                if item.is_dir:
                    item.copied_list.append((os.path.join(item.src, relname),
                                              os.path.join(item.dst, relname),
                                              "OK"))
                else:
                    item.copied_list.append((item.src, item.dst, "OK"))

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
        item.status = STATUS_FAIL
        item.error_text = "rsync exited with code {0}".format(rc)
        item.errors_list.append((item.src, item.dst, item.error_text))
    elif item.conflicts:
        item.status = STATUS_INCOMPLETE
    else:
        item.status = STATUS_OK
        item.transfer_seconds = transfer_end - transfer_start


def resolve_conflict(conflict, action):
    """Apply a user's chosen resolution to a single conflicting file."""
    try:
        if action == "skip":
            conflict.remark = "user skipped"
        elif action == "rename":
            # rename the EXISTING (target) file out of the way, then copy in
            base, ext = os.path.splitext(conflict.dst_full)
            newname = "{0}.conflict-{1}{2}".format(base, int(time.time()), ext)
            shutil.move(conflict.dst_full, newname)
            shutil.copy2(conflict.src_full, conflict.dst_full)
            conflict.remark = "existing renamed to {0}".format(os.path.basename(newname))
        elif action == "autorename":
            # copy the INCOMING file in under a new auto-generated name,
            # leaving the existing target file untouched
            base, ext = os.path.splitext(conflict.dst_full)
            newname = "{0}.new-{1}{2}".format(base, int(time.time()), ext)
            shutil.copy2(conflict.src_full, newname)
            conflict.remark = "auto-renamed incoming to {0}".format(os.path.basename(newname))
        conflict.resolution = action
        return True, ""
    except (OSError, shutil.Error) as e:
        return False, str(e)


# ----------------------------------------------------------------------
# Curses UI helpers
# ----------------------------------------------------------------------

def safe_addstr(win, y, x, text, attr=0):
    h, w = win.getmaxyx()
    if y < 0 or y >= h or x >= w:
        return
    maxlen = w - x - 1
    if maxlen <= 0:
        return
    try:
        win.addstr(y, x, text[:maxlen], attr)
    except curses.error:
        pass


def prompt_string(stdscr, prompt, default=""):
    h, w = stdscr.getmaxyx()
    win = curses.newwin(3, w, h - 3, 0)
    win.border()
    safe_addstr(win, 0, 2, " " + prompt + " ")
    curses.echo()
    curses.curs_set(1)
    win.move(1, 2)
    win.noutrefresh()
    curses.doupdate()
    try:
        s = win.getstr(1, 2, w - 6).decode("utf-8")
    except Exception:
        s = ""
    curses.noecho()
    curses.curs_set(0)
    if not s:
        s = default
    return s


def confirm(stdscr, prompt):
    h, w = stdscr.getmaxyx()
    win = curses.newwin(3, w, h - 3, 0)
    win.border()
    safe_addstr(win, 1, 2, prompt + " (y/n)")
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
    buttons underneath. Tab cycles focus (field -> OK -> Cancel -> field).
    Enter activates whichever is focused (field counts as OK). Esc always
    cancels. Returns (confirmed: bool, text: str)."""
    h, w = stdscr.getmaxyx()
    box_w = min(max(50, len(label) + 20), w - 4)
    box_h = 7
    y0 = max(0, (h - box_h) // 2)
    x0 = max(0, (w - box_w) // 2)
    win = curses.newwin(box_h, box_w, y0, x0)

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

        c = win.getch()
        if c == 27:  # Esc
            curses.curs_set(0)
            return False, ""
        elif c == 9:  # Tab
            focus = {"field": "ok", "ok": "cancel", "cancel": "field"}[focus]
        elif c in (curses.KEY_BTAB,):
            focus = {"field": "cancel", "ok": "field", "cancel": "ok"}[focus]
        elif c in (10, 13):  # Enter
            curses.curs_set(0)
            if focus == "cancel":
                return False, ""
            return True, "".join(text)
        elif focus == "field":
            if c in (curses.KEY_BACKSPACE, 127, 8):
                if text:
                    text.pop()
            elif 32 <= c < 256:
                try:
                    text.append(chr(c))
                except ValueError:
                    pass


def flash(win):
    curses.flash()


def message(stdscr, text):
    h, w = stdscr.getmaxyx()
    win = curses.newwin(3, w, h - 3, 0)
    win.border()
    safe_addstr(win, 1, 2, text)
    win.noutrefresh()
    curses.doupdate()
    win.getch()


# ----------------------------------------------------------------------
# Pane (left/right file browser)
# ----------------------------------------------------------------------

class Pane(object):
    def __init__(self, path):
        self.path = os.path.abspath(path)
        self.entries = []
        self.index = 0
        self.scroll = 0
        self.reload()

    def reload(self):
        self.entries = list_dir_entries(self.path)
        if self.index >= len(self.entries):
            self.index = max(0, len(self.entries) - 1)

    def selected(self):
        if not self.entries:
            return None
        return self.entries[self.index]

    def selected_full(self):
        sel = self.selected()
        if sel is None:
            return None
        return os.path.join(self.path, sel[0])

    def go_parent(self):
        parent = os.path.dirname(self.path.rstrip("/"))
        if parent and parent != self.path:
            old_base = os.path.basename(self.path)
            self.path = parent
            self.reload()
            for i, (name, isdir) in enumerate(self.entries):
                if name == old_base:
                    self.index = i
                    break

    def enter(self):
        sel = self.selected()
        if sel and sel[1]:
            self.path = os.path.join(self.path, sel[0])
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

HELP_TEXT = [
    "mcrsync - Midnight-Commander-style rsync queue manager",
    "",
    "GLOBAL KEYS (work in every view)",
    "  ?  or  h        show this help (press again / Esc to close)",
    "  t               toggle between NAV view and QUEUE editor view",
    "  v               open/close the VERIFY view",
    "  s               save the current queue (+ rsync options) to a file",
    "  l               load a queue (+ rsync options) from a file",
    "  F8              open the rsync-options review screen, then run the queue",
    "  p               pause / resume queue execution (takes effect between",
    "                  items, not mid-transfer - rsync itself can't be paused",
    "                  mid-file)",
    "  q               quit (asks for confirmation)",
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
    "                  once a full queue run finishes.",
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
    "",
    "QUEUE EDITOR VIEW (single pane: size, #, source, dest, status)",
    "  Up / Down       move selection",
    "  PgUp / PgDn     scroll a full page",
    "  d  or  Delete   remove the selected item from the queue (confirms).",
    "                  If a background size estimate was still being",
    "                  calculated for that item, it's cancelled right away.",
    "",
    "  SIZE column shows an estimated total size (e.g. '400.0 M'),",
    "  computed lazily in the background so it never blocks the UI:",
    "    ...   still being estimated - check back in a moment",
    "    nan   size couldn't be determined (e.g. permission error)",
    "",
    "  The status bar's far right shows overall progress while in this",
    "  view: '<percent>% done | ETA: <duration>'. Percent is the share of",
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
    "  Note: --stats and --itemize-changes are what populate the Verify",
    "  view's copied-file list and counts. Removing them in expert mode",
    "  is fine for rsync itself, but Verify will have less detail to show.",
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
    "  - Before each item runs, mcrsync also does its own scan for any",
    "    target file newer than its source counterpart and reports it",
    "    as a conflict (INCOMPLETE status) so you can decide what to do",
    "    with it, instead of it just silently vanishing from the run.",
    "  - Loading a saved queue always resets any leftover 'IN PROGRESS'",
    "    items back to 'NOT SYNCED', so resuming with F8 is safe.",
    "",
    "Press Esc, h, or ? to close this help.",
]

VERIFY_TABS = ["COPIED", "SKIPPED", "RENAMED", "ERRORS"]


class App(object):
    def __init__(self, stdscr, start_path):
        self.stdscr = stdscr
        self.view = VIEW_NAV
        self.left = Pane(start_path)
        self.right = Pane(start_path)
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
        except curses.error:
            pass

    # ---------------- logging ----------------
    def log(self, text):
        with self.lock:
            self.log_lines.append(text)
            if len(self.log_lines) > 500:
                self.log_lines = self.log_lines[-500:]

    def last_queue_tail(self, n=5):
        return self.queue[-n:]

    # ---------------- queue ops ----------------
    def add_to_queue(self, src, dst, is_dir=True):
        item = QueueItem(src, dst, is_dir)
        self.queue.append(item)
        self.schedule_size_calc(item)
        return item

    def schedule_size_calc(self, item):
        """Enqueue a background size estimate for item, unless one is
        already known or already in flight."""
        if item.size_bytes is not None:
            item.size_state = "done"
            return
        if item.size_state in ("pending", "computing"):
            return
        item.size_state = "pending"
        self.size_calc_queue.put(item)

    def _size_calc_worker(self):
        while True:
            item = self.size_calc_queue.get()
            if item.cancel_event.is_set():
                # Deleted before we even got to it - nothing to do.
                continue
            item.size_state = "computing"
            size = compute_item_size(item.src, item.is_dir, item.cancel_event)
            if item.cancel_event.is_set():
                # Deleted while we were scanning - discard the result.
                continue
            item.size_bytes = size
            item.size_state = "done" if size is not None else "error"

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
        return (percent, remaining_bytes / avg_rate)

    def save_queue(self, path):
        data = {
            "rsync_opts": self.rsync_opts,
            "items": [it.to_dict() for it in self.queue],
        }
        with open(path, "w") as f:
            json.dump(data, f, indent=2)

    def load_queue(self, path):
        with open(path, "r") as f:
            data = json.load(f)
        if isinstance(data, list):
            # backward compatibility with the old (opts-less) format
            self.queue = [QueueItem.from_dict(d) for d in data]
        else:
            self.rsync_opts = data.get("rsync_opts", list(DEFAULT_RSYNC_OPTS))
            self.queue = [QueueItem.from_dict(d) for d in data.get("items", [])]
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
        self.run_thread = threading.Thread(target=self._run_worker)
        self.run_thread.daemon = True
        self.run_thread.start()

    def _run_worker(self):
        for idx, item in enumerate(self.queue):
            while self.paused:
                time.sleep(0.3)
            if item.status in (STATUS_OK,):
                continue
            self.queue_index = idx
            self.log("Starting: {0} -> {1}".format(item.src, item.dst))
            run_rsync_item(item, self.log, self.rsync_opts)
            self.log("Finished [{0}]: {1}".format(item.status, item.src))
        self.running = False
        # Panes/curses calls must happen on the main thread - just flag it
        # and let the main loop pick it up and reload on its next tick.
        self.pending_pane_refresh = True

    def toggle_pause(self):
        if not self.running:
            return
        self.paused = not self.paused
        self.log("PAUSED" if self.paused else "RESUMED")

    # ---------------- verify data ----------------
    def verify_rows(self, tab_name):
        rows = []
        if tab_name == "COPIED":
            for it in self.queue:
                for (s, d, r) in it.copied_list:
                    rows.append((s, d, r))
                if it.copied_files and not it.copied_list:
                    rows.append((it.src, it.dst,
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
                        rows.append((c.src_full, c.dst_full, c.remark))
        elif tab_name == "ERRORS":
            for it in self.queue:
                for (s, d, r) in it.errors_list:
                    rows.append((s, d, r))
        return rows

    # ---------------- drawing ----------------
    def draw(self):
        self.stdscr.erase()
        h, w = self.stdscr.getmaxyx()
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
        curses.doupdate()

    def draw_statusbar(self, h, w):
        if self.view == VIEW_NAV:
            help_text = ("?/h:help  Tab:switch  <-/->:nav  c:queue-copy  F2:rename  "
                          "Ins:mkdir  F5:refresh  t:queue-view  "
                          "v:verify  s:save  l:load  F8:run  p:pause  q:quit")
        elif self.view == VIEW_QUEUE:
            help_text = ("?/h:help  t:nav-view  v:verify  s:save  l:load  F8:run  "
                          "p:pause  PgUp/PgDn:scroll  q:quit")
        elif self.view == VIEW_VERIFY:
            help_text = "?/h:help  Tab:cycle-tab  Enter:resolve  Esc/v:back  q:quit"
        elif self.view == VIEW_HELP:
            help_text = "Esc/h/?:close  Up/Down/PgUp/PgDn:scroll  q:quit"
        else:
            help_text = "?/h:help  e:edit flags  d:defaults  Enter:confirm&run  Esc:cancel  q:quit"
        run_state = ""
        if self.running:
            run_state = " [RUNNING{0}]".format(" - PAUSED" if self.paused else "")
        bar = (help_text + run_state)

        right_text = ""
        if self.view == VIEW_QUEUE:
            percent, eta = self.compute_progress_and_eta()
            if percent is not None:
                eta_str = format_duration(eta) if eta is not None else "nan"
                right_text = "{0:.0f}% done | ETA: {1}".format(percent, eta_str)

        avail = max(0, w - 1)
        if right_text and len(bar) + 1 + len(right_text) <= avail:
            pad = avail - len(bar) - len(right_text)
            bar = bar + (" " * pad) + right_text
        safe_addstr(self.stdscr, h - 1, 0, bar.ljust(avail), curses.color_pair(6))

    def draw_pane(self, win, pane, title, active):
        win.erase()
        win.border()
        h, w = win.getmaxyx()
        hdr_attr = curses.color_pair(1) if active else 0
        safe_addstr(win, 0, 2, " " + title + " ", hdr_attr | curses.A_BOLD)
        safe_addstr(win, 1, 2, pane.path[:w - 4], curses.A_DIM)
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
        if not pane.entries:
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
            line = "#{0:<4d} {1}  ->  {2}   [{3}]".format(n, it.src, it.dst, it.status)
            attr = self.status_attr(it.status)
            safe_addstr(bwin, 1 + i, 2, line, attr)
        bwin.noutrefresh()

    def status_attr(self, status):
        if status == STATUS_OK:
            return curses.color_pair(3)
        if status == STATUS_FAIL:
            return curses.color_pair(4)
        if status == STATUS_INCOMPLETE:
            return curses.color_pair(5)
        return 0

    def item_size_display(self, it):
        if it.size_bytes is not None:
            return format_size_si(it.size_bytes)
        if it.size_state in ("pending", "computing"):
            return "..."
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
        size_w, num_w, status_reserve = 11, 6, 14
        content_w = max(20, w - 4)
        remaining = max(8, content_w - size_w - num_w - status_reserve)
        col_src = remaining // 2
        col_dst = remaining - col_src

        x_size = 2
        x_num = x_size + size_w
        x_src = x_num + num_w
        x_dst = x_src + col_src
        x_status = x_dst + col_dst

        header = ("SIZE"[:size_w - 1].rjust(size_w - 1) + " " +
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
            line = (size_str[:size_w - 1].rjust(size_w - 1) + " " +
                    str(idx + 1)[:num_w - 1].ljust(num_w) +
                    it.src[:col_src - 1].ljust(col_src) +
                    it.dst[:col_dst - 1].ljust(col_dst))
            safe_addstr(win, 2 + i, 2, line, attr)
            # ljust(200) is a cheap way to guarantee the status field fills
            # with spaces all the way to the window edge (safe_addstr clips
            # to the actual visible width on its own), per request: no
            # leftover characters should show past the status text.
            safe_addstr(win, 2 + i, x_status, it.status.ljust(200), attr)
        win.noutrefresh()

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
                s, d, remark = c.relpath, it.dst, c.remark
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
        cur = " ".join(self.rsync_opts)
        safe_addstr(win, 2, 2, "Current command line flags:")
        safe_addstr(win, 3, 4, cur[:w - 8], curses.A_REVERSE)
        safe_addstr(win, 5, 2,
                    "Full rsync command that will run per item:")
        example = "rsync " + cur + " <source>/ <dest>/"
        safe_addstr(win, 6, 4, example[:w - 8], curses.A_DIM)

        warn_y = 8
        if "-u" not in self.rsync_opts and "--update" not in self.rsync_opts:
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
                                      " ".join(self.rsync_opts))
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
            if "-u" not in self.rsync_opts and "--update" not in self.rsync_opts:
                proceed = confirm(
                    self.stdscr,
                    "-u/--update is not set - newer target files may be "
                    "overwritten. Continue anyway?")
            if proceed:
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
            if c == -1:
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
        if c == ord('t'):
            self.view = VIEW_QUEUE if self.view == VIEW_NAV else VIEW_NAV
        elif c == ord('v'):
            if self.view != VIEW_VERIFY:
                self.view = VIEW_VERIFY
            else:
                self.view = VIEW_NAV
        elif c == ord('s'):
            name = prompt_string(self.stdscr, "Save queue as (path):", "queue.json")
            if name:
                try:
                    self.save_queue(name)
                    message(self.stdscr, "Saved {0} item(s) to {1}".format(len(self.queue), name))
                except OSError as e:
                    message(self.stdscr, "Save failed: {0}".format(e))
        elif c == ord('l'):
            name = prompt_string(self.stdscr, "Load queue from (path):", "queue.json")
            if name:
                try:
                    self.load_queue(name)
                    message(self.stdscr, "Loaded {0} item(s) from {1}".format(len(self.queue), name))
                except (OSError, ValueError) as e:
                    message(self.stdscr, "Load failed: {0}".format(e))
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
            self.refresh_panes()
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
            dst_full = os.path.join(self.right.path, name)
            self.add_to_queue(src_full, dst_full, is_dir)
            self.log("Queued ({0}): {1} -> {2}".format(
                "dir" if is_dir else "file", src_full, dst_full))
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
        if "/" in new_name:
            message(self.stdscr, "Name cannot contain '/'.")
            return
        old_full = os.path.join(pane.path, old_name)
        new_full = os.path.join(pane.path, new_name)
        if os.path.exists(new_full):
            message(self.stdscr, "'{0}' already exists - not overwriting.".format(new_name))
            return
        try:
            os.rename(old_full, new_full)
        except OSError as e:
            message(self.stdscr, "Rename failed: {0}".format(e))
            return
        pane.reload()
        for i, (name, _) in enumerate(pane.entries):
            if name == new_name:
                pane.index = i
                break
        self.log("Renamed: {0} -> {1}".format(old_full, new_full))

    def create_folder(self, pane):
        confirmed, name = modal_input_dialog(
            self.stdscr, "Create Folder", "New folder name:", default="")
        if not confirmed:
            return
        name = name.strip()
        if not name:
            return
        if "/" in name:
            message(self.stdscr, "Name cannot contain '/'.")
            return
        new_full = os.path.join(pane.path, name)
        if os.path.exists(new_full):
            message(self.stdscr, "'{0}' already exists.".format(name))
            return
        try:
            os.mkdir(new_full)
        except OSError as e:
            message(self.stdscr, "Create folder failed: {0}".format(e))
            return
        pane.reload()
        for i, (nm, _) in enumerate(pane.entries):
            if nm == name:
                pane.index = i
                break
        self.log("Created folder: {0}".format(new_full))

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
            if self.queue and confirm(self.stdscr, "Delete selected queue item?"):
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
        rows = self.verify_rows("SKIPPED")
        if self.verify_index >= len(rows):
            return
        row = rows[self.verify_index]
        if row[0] != "__conflict__":
            return
        it, c = row[1], row[2]
        choice = prompt_string(
            self.stdscr,
            "{0}: (S)kip / (R)ename existing / (A)utorename incoming:".format(c.relpath), "")
        choice = choice.strip().lower()
        action = {"s": "skip", "r": "rename", "a": "autorename"}.get(choice[:1] if choice else "")
        if not action:
            return
        ok, err = resolve_conflict(c, action)
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

def main(stdscr):
    start_path = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
    app = App(stdscr, start_path)
    app.run()


if __name__ == "__main__":
    curses.wrapper(main)
