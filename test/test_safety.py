# -*- coding: utf-8 -*-
"""Safety tests: input validation, and that nothing on either filesystem is
overwritten, moved or created unless that is exactly what was asked for.
Remote tests need the Docker rig (see test/README.md)."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import mcrsync as m
from test_mcrsync import TEST_HOSTS, KEYFILE, PORT, RemoteBase

OPTS = m.DEFAULT_RSYNC_OPTS


def write(path, text, mtime=None):
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d)
    with open(path, "w") as f:
        f.write(text)
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def read(path):
    with open(path) as f:
        return f.read()


def snapshot(root):
    """{relpath: content-or-'<dir>'/'-> target'} of a whole local tree."""
    out = {}
    for base, dirs, files in os.walk(root):
        for d in dirs:
            p = os.path.join(base, d)
            out[os.path.relpath(p, root)] = ("-> " + os.readlink(p)) if os.path.islink(p) else "<dir>"
        for f in files:
            p = os.path.join(base, f)
            out[os.path.relpath(p, root)] = ("-> " + os.readlink(p)) if os.path.islink(p) else read(p)
    return out


FUTURE = time.time() + 86400


# ----------------------------------------------------------------------
# Input validation (queue files, connect form)
# ----------------------------------------------------------------------

class ConnSpecValidationTest(unittest.TestCase):
    def test_rejects_option_injection(self):
        for user, host in (("-oProxyCommand=touch /tmp/x", "h"), ("u", "-oProxyCommand=x"),
                           ("u u", "h"), ("u", "h h"), ("", "h"), ("u", ""), ("u;rm", "h"),
                           ("u", "h\n"), (None, "h"), ("u", 5)):
            with self.assertRaises(ValueError, msg=repr((user, host))):
                m.ConnSpec(user, host)

    def test_port_and_keyfile(self):
        for port in (0, 65536, "abc", None, -1):
            with self.assertRaises(ValueError, msg=repr(port)):
                m.ConnSpec("u", "h", port)
        for keyfile in ("/a\nb", "/k%h/id"):
            with self.assertRaises(ValueError):
                m.ConnSpec("u", "h", 22, keyfile)
        self.assertEqual(m.ConnSpec("u.x-y_z", "nas-1.lan", "2222").port, 2222)

    def test_from_form_messages(self):
        self.assertEqual(m.ConnSpec.from_form("", "h", "22", "")[1], "User is required.")
        self.assertEqual(m.ConnSpec.from_form("u", "", "22", "")[1], "Host is required.")
        self.assertIn("IPv6", m.ConnSpec.from_form("u", "::1", "22", "")[1])
        self.assertIn("Key file not found", m.ConnSpec.from_form("u", "h", "22", "/nope")[1])
        self.assertIn("Port", m.ConnSpec.from_form("u", "h", "99999", "")[1])
        self.assertIn("Invalid user", m.ConnSpec.from_form("-x", "h", "22", "")[1])
        spec, err = m.ConnSpec.from_form("u", "h", "", "")
        self.assertEqual((spec.port, err), (22, ""))


class QueueFileValidationTest(unittest.TestCase):
    GOOD = {"src": "/a", "dst": "/b", "is_dir": True}

    def item(self, **kw):
        d = dict(self.GOOD)
        d.update(kw)
        return m.QueueItem.from_dict(d)

    def test_rejects_paths_that_could_be_options_or_hosts(self):
        for bad in ("--delete", "-e sh", "relative/x", "host:/x", "", None, 5, "/a\0b"):
            for field in ("src", "dst"):
                with self.assertRaises(ValueError, msg=repr((field, bad))):
                    self.item(**{field: bad})

    def test_rejects_bad_conn_and_types(self):
        with self.assertRaises(ValueError):
            self.item(dst_conn={"user": "-oProxyCommand=x", "host": "h"})
        with self.assertRaises(ValueError):
            self.item(src_conn={"user": "u", "host": "h", "port": "x"})
        with self.assertRaises(ValueError):
            self.item(is_dir="false")
        with self.assertRaises(KeyError):
            self.item(dst_conn={"host": "h"})

    def test_json_round_trip(self):
        it = m.QueueItem("/a b/it's", "/x\ny", False, m.ConnSpec("u", "h"), None)
        back = m.QueueItem.from_dict(json.loads(json.dumps(it.to_dict())))
        self.assertEqual((back.src, back.dst, back.is_dir), ("/a b/it's", "/x\ny", False))


class BuildCmdTest(unittest.TestCase):
    def test_paths_after_double_dash(self):
        r = m.RsyncRunner(m.ConnectionRegistry())
        cmd = r.build_cmd(["-a"], m.LOCAL_FS, "/s", m.LOCAL_FS, "/d", True)
        self.assertEqual(cmd[-3:], ["--", "/s/", "/d/"])
        cmd = r.build_cmd(["-a"], m.LOCAL_FS, "/s", m.LOCAL_FS, "/d", False)
        self.assertEqual(cmd[-3:], ["--", "/s", "/d"])

    def test_two_remotes_refused(self):
        reg = m.ConnectionRegistry()
        r = m.RsyncRunner(reg)
        a = reg.fs_for(m.ConnSpec("u", "a"))
        b = reg.fs_for(m.ConnSpec("u", "b"))
        with self.assertRaises(m.FsError):
            r.build_cmd([], a, "/x", b, "/y", True)
        reg.close_all()

    def test_control_path_escaped(self):
        conn = m.SshConnection(m.ConnSpec("u", "h", 22, "/k ey/id"), "/tmp/s%C.sock", "/dev/null")
        opts = conn.client_opts()
        self.assertIn("ControlPath=/tmp/s%%C.sock", opts)
        self.assertIn("/k ey/id", opts)


class RegistryTest(unittest.TestCase):
    def test_socket_never_reused(self):
        # Regression: names came from len(conns), so after a close a new
        # host got the socket of a still-open other host.
        reg = m.ConnectionRegistry()
        a = reg.get(m.ConnSpec("u", "a"))
        b = reg.get(m.ConnSpec("u", "b"))
        reg.close(a.spec)
        c = reg.get(m.ConnSpec("u", "c"))
        a2 = reg.get(m.ConnSpec("u", "a"))
        paths = [a.sock_path, b.sock_path, c.sock_path, a2.sock_path]
        self.assertEqual(len(set(paths)), 4, paths)
        reg.close_all()
        self.assertIsNone(reg.sock_dir)


class UnescapeTest(unittest.TestCase):
    def test_rsync_escapes(self):
        u = m.RsyncRunner.unescape
        self.assertEqual(u("new\\#012line"), "new\nline")
        self.assertEqual(u("back\\#134#012slash"), "back\\#012slash")
        self.assertEqual(u("\\#303\\#274n\\#303\\#257"), "ünï")
        self.assertEqual(u("plain name"), "plain name")


class SettingsSafetyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "s.yaml")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_not_utf8_is_left_alone(self):
        with open(self.path, "wb") as f:
            f.write(b"user: \xff\xfe\n")
        s = m.Settings(self.path)
        s.load()
        self.assertTrue(s.error)
        s.save()
        with open(self.path, "rb") as f:
            self.assertEqual(f.read(), b"user: \xff\xfe\n")

    def test_directory_in_the_way(self):
        os.mkdir(self.path)
        s = m.Settings(self.path)
        s.load()
        self.assertTrue(s.error)
        self.assertNotEqual(s.save(), "")
        self.assertTrue(os.path.isdir(self.path))

    def test_save_keeps_symlink_and_leaves_no_temp(self):
        real = os.path.join(self.tmp, "real.yaml")
        write(real, "a: 1\n")
        os.symlink(real, self.path)
        s = m.Settings(self.path)
        s.load()
        s.data["a"] = 2
        self.assertEqual(s.save(), "")
        self.assertTrue(os.path.islink(self.path))
        self.assertIn("a: 2", read(real))
        self.assertEqual(sorted(os.listdir(self.tmp)), ["real.yaml", "s.yaml"])


# ----------------------------------------------------------------------
# Local filesystem: never overwrite
# ----------------------------------------------------------------------

class LocalRenameTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def p(self, *parts):
        return os.path.join(self.tmp, *parts)

    def test_never_replaces(self):
        write(self.p("a"), "A")
        write(self.p("b"), "B")
        os.mkdir(self.p("emptydir"))
        os.mkdir(self.p("dir"))
        before = snapshot(self.tmp)
        for old, new in (("a", "b"), ("a", "emptydir"), ("dir", "emptydir"), ("dir", "b")):
            with self.assertRaises(m.FsError, msg=(old, new)):
                m.LOCAL_FS.rename(self.p(old), self.p(new))
        self.assertEqual(snapshot(self.tmp), before)

    def test_moves_files_dirs_and_symlinks(self):
        write(self.p("a"), "A")
        os.mkdir(self.p("dir"))
        os.symlink("a", self.p("link"))
        m.LOCAL_FS.rename(self.p("a"), self.p("a2"))
        m.LOCAL_FS.rename(self.p("dir"), self.p("dir2"))
        m.LOCAL_FS.rename(self.p("link"), self.p("link2"))
        self.assertEqual(snapshot(self.tmp), {"a2": "A", "dir2": "<dir>", "link2": "-> a"})


class LocalItemSafetyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.runner = m.RsyncRunner(m.ConnectionRegistry())

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def p(self, *parts):
        return os.path.join(self.tmp, *parts)

    def run_item(self, it, opts=OPTS):
        self.runner.run_item(it, lambda s: None, opts)
        return it

    def test_file_onto_existing_folder_refused(self):
        write(self.p("src", "photo.jpg"), "new")
        write(self.p("dst", "photo.jpg", "inside.txt"), "keep")
        before = snapshot(self.tmp)
        it = self.run_item(m.QueueItem(self.p("src", "photo.jpg"), self.p("dst", "photo.jpg"), False))
        self.assertEqual(it.status, m.STATUS_FAIL)
        self.assertIn("folder with that name", it.error_text)
        self.assertEqual(snapshot(self.tmp), before)

    def test_folder_onto_existing_file_refused(self):
        write(self.p("src", "docs", "a"), "a")
        write(self.p("dst", "docs"), "i am a file")
        before = snapshot(self.tmp)
        it = self.run_item(m.QueueItem(self.p("src", "docs"), self.p("dst", "docs"), True))
        self.assertEqual(it.status, m.STATUS_FAIL)
        self.assertEqual(snapshot(self.tmp), before)

    def test_into_itself_refused(self):
        write(self.p("src", "a"), "a")
        before = snapshot(self.tmp)
        for dst in (self.p("src"), self.p("src", "sub"), self.p("src", "sub", "deeper")):
            it = self.run_item(m.QueueItem(self.p("src"), dst, True))
            self.assertEqual(it.status, m.STATUS_FAIL, dst)
            self.assertIn("inside", it.error_text)
        self.assertEqual(snapshot(self.tmp), before)
        # a sibling whose name merely starts the same is fine
        it = self.run_item(m.QueueItem(self.p("src"), self.p("src2"), True))
        self.assertEqual(it.status, m.STATUS_OK, it.error_text)

    def test_dry_run_changes_nothing(self):
        write(self.p("src", "docs", "a"), "new")
        write(self.p("src", "docs", "b"), "new")
        write(self.p("dst", "docs", "a"), "newer", FUTURE)
        before = snapshot(self.tmp)
        for opts in (OPTS + ["-n"], ["-anu", "--itemize-changes"], OPTS + ["--dry-run"]):
            it = self.run_item(m.QueueItem(self.p("src", "docs"), self.p("dst", "docs"), True), opts)
            self.assertEqual(it.status, m.STATUS_DRY_INCOMPLETE, opts)
            it = self.run_item(m.QueueItem(self.p("src", "docs"), self.p("new", "deep", "docs"), True), opts)
            self.assertEqual(it.status, m.STATUS_DRY_OK, opts)
        self.assertEqual(snapshot(self.tmp), before)

    def test_newer_target_never_overwritten(self):
        write(self.p("src", "a"), "old")
        write(self.p("dst", "a"), "newer", FUTURE)
        for opts in (OPTS, ["-avu"], ["-a", "--update", "--itemize-changes"]):
            it = self.run_item(m.QueueItem(self.p("src"), self.p("dst"), True), opts)
            self.assertEqual(it.status, m.STATUS_INCOMPLETE, opts)
            self.assertEqual(read(self.p("dst", "a")), "newer")


class LocalResolutionTest(unittest.TestCase):
    NAMES = ["a.txt", "new\nline.txt", "ünï.txt", "back\\#012slash", "-dash", "sp ace"]

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.src = os.path.join(self.tmp, "src")
        self.dst = os.path.join(self.tmp, "dst")
        for n in self.NAMES:
            write(os.path.join(self.src, n), "incoming " + n)
            write(os.path.join(self.dst, n), "existing " + n, FUTURE)
        self.runner = m.RsyncRunner(m.ConnectionRegistry())
        self.item = m.QueueItem(self.src, self.dst, True)
        self.runner.run_item(self.item, lambda s: None, OPTS)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def conflict(self, name):
        return [c for c in self.item.conflicts if c.relpath == name][0]

    def test_odd_names_parsed(self):
        self.assertEqual(self.item.status, m.STATUS_INCOMPLETE)
        self.assertEqual(sorted(c.relpath for c in self.item.conflicts), sorted(self.NAMES))

    def test_rename_and_autorename_every_name(self):
        for i, n in enumerate(self.NAMES):
            action = "rename" if i % 2 == 0 else "autorename"
            ok, err = self.runner.resolve_conflict(self.item, self.conflict(n), action)
            self.assertTrue(ok, (n, err))
        after = snapshot(self.dst)
        for i, n in enumerate(self.NAMES):
            kept = [k for k, v in after.items() if v == "existing " + n]
            incoming = [k for k, v in after.items() if v == "incoming " + n]
            self.assertEqual(len(kept), 1, (n, after))
            self.assertEqual(len(incoming), 1, (n, after))
            if i % 2 == 0:
                self.assertEqual(incoming[0], n)
            else:
                self.assertEqual(kept[0], n)
        # source untouched
        self.assertEqual(sorted(os.listdir(self.src)), sorted(self.NAMES))

    def test_stale_conflict_refused(self):
        write(os.path.join(self.src, "a.txt"), "even newer source", FUTURE + 100)
        before = snapshot(self.tmp)
        ok, err = self.runner.resolve_conflict(self.item, self.conflict("a.txt"), "rename")
        self.assertFalse(ok)
        self.assertIn("changed since the run", err)
        self.assertEqual(snapshot(self.tmp), before)

    def test_source_gone_refused(self):
        os.unlink(os.path.join(self.src, "a.txt"))
        before = snapshot(self.tmp)
        ok, err = self.runner.resolve_conflict(self.item, self.conflict("a.txt"), "autorename")
        self.assertFalse(ok)
        self.assertEqual(snapshot(self.tmp), before)

    def test_failed_copy_puts_existing_back(self):
        def boom(*args):
            raise m.FsError("disk full")
        self.runner.copy_new_file = boom
        before = snapshot(self.tmp)
        ok, err = self.runner.resolve_conflict(self.item, self.conflict("a.txt"), "rename")
        self.assertFalse(ok)
        self.assertIn("put back", err)
        self.assertEqual(snapshot(self.tmp), before)

    def test_copy_new_file_never_overwrites(self):
        target = os.path.join(self.dst, "a.txt")
        with self.assertRaises(m.FsError):
            self.runner.copy_new_file(m.LOCAL_FS, os.path.join(self.src, "a.txt"), m.LOCAL_FS, target)
        self.assertEqual(read(target), "existing a.txt")

    def test_skip_changes_nothing(self):
        before = snapshot(self.tmp)
        ok, err = self.runner.resolve_conflict(self.item, self.conflict("a.txt"), "skip")
        self.assertTrue(ok)
        self.assertEqual(snapshot(self.tmp), before)


# ----------------------------------------------------------------------
# Remote filesystem
# ----------------------------------------------------------------------

@unittest.skipUnless(TEST_HOSTS, "MCRSYNC_TEST_HOSTS not set (run inside the Docker rig)")
class RemoteSafetyTest(RemoteBase):
    """Runs against every rig host."""

    def remote_snapshot(self, fs, root):
        script = 'cd -- "$1" && find . | LC_ALL=C sort && find . -type f -exec cat {} \\; -exec echo \\;'
        return self.remote_sh(fs, script, root)

    def identity(self, fs):
        return self.remote_sh(fs, 'cat /etc/os-release | grep "^ID="').strip()

    def test_operations_always_reach_the_right_host(self):
        if len(TEST_HOSTS) < 2:
            self.skipTest("needs two hosts")
        a_spec, a_fs, _ = self.connect(TEST_HOSTS[0])
        b_spec, b_fs, _ = self.connect(TEST_HOSTS[1])
        a_id, b_id = self.identity(a_fs), self.identity(b_fs)
        self.assertNotEqual(a_id, b_id)
        self.registry.close(a_spec)
        a2_spec, a2_fs, _ = self.connect(TEST_HOSTS[0])
        self.assertEqual(self.identity(a2_fs), a_id)
        self.assertEqual(self.identity(b_fs), b_id)
        self.assertNotEqual(a2_fs.conn.sock_path, b_fs.conn.sock_path)

    def test_stale_socket_file_is_replaced(self):
        spec = m.ConnSpec("keyuser", TEST_HOSTS[0], PORT, KEYFILE)
        conn = self.registry.get(spec)
        os.makedirs(os.path.dirname(conn.sock_path), exist_ok=True)
        write(conn.sock_path, "stale")
        ok, err = conn.connect()
        self.assertTrue(ok, err)

    def test_keyfile_with_odd_path(self):
        odd = os.path.join(self.tmp, "my key's \"dir\"", "id")
        os.makedirs(os.path.dirname(odd))
        shutil.copy(KEYFILE, odd)
        os.chmod(odd, 0o600)
        for host in TEST_HOSTS:
            spec = m.ConnSpec("keyuser", host, PORT, odd)
            ok, err = self.registry.get(spec).connect()
            self.assertTrue(ok, err)
            fs = self.registry.fs_for(spec)
            work = fs.resolve("") + "/t"
            self.remote_sh(fs, 'rm -rf -- "$1"; mkdir -p -- "$1"', work)
            write(os.path.join(self.tmp, host, "f"), "x")
            it = m.QueueItem(os.path.join(self.tmp, host), work + "/d", True, None, spec)
            self.runner.run_item(it, lambda s: None, OPTS)
            self.assertEqual(it.status, m.STATUS_OK, it.error_text)
            self.registry.close(spec)

    def test_socket_dir_with_percent(self):
        d = tempfile.mkdtemp(prefix="sock%h%C-")
        try:
            self.registry.sock_dir = d
            spec, fs, work = self.connect(TEST_HOSTS[0])
            self.assertTrue(os.path.exists(fs.conn.sock_path))
            self.assertEqual(fs.resolve(""), "/home/keyuser")
        finally:
            self.registry.close_all()
            shutil.rmtree(d, ignore_errors=True)

    def test_shell_banner_ignored(self):
        host = TEST_HOSTS[-1]   # debian: bash reads .bashrc for ssh commands
        spec, fs, work = self.connect(host)
        self.remote_sh(fs, 'cp .bashrc .bashrc.mcrsync-test 2>/dev/null; echo "echo BANNER/JUNK" > .bashrc')
        try:
            self.remote_sh(fs, ': > "$1/file"', work)
            self.assertEqual(fs.list_dir(work), [("file", False)])
            self.assertEqual(fs.resolve(""), "/home/keyuser")
        finally:
            self.remote_sh(fs, 'if [ -f .bashrc.mcrsync-test ]; then mv .bashrc.mcrsync-test .bashrc; '
                               'else rm -f .bashrc; fi')

    def test_unreadable_dir_is_an_error_not_empty(self):
        for host in TEST_HOSTS:
            spec, fs, work = self.connect(host)
            self.remote_sh(fs, 'mkdir "$1/locked" && : > "$1/locked/f" && chmod 300 "$1/locked"', work)
            try:
                with self.assertRaises(m.FsError):
                    fs.list_dir(work + "/locked")
            finally:
                self.remote_sh(fs, 'chmod 700 "$1/locked"', work)

    def test_remote_rename_never_replaces(self):
        for host in TEST_HOSTS:
            spec, fs, work = self.connect(host)
            self.remote_sh(fs, 'cd "$1" && echo A > a && echo B > b && mkdir d e && echo X > d/x', work)
            before = self.remote_snapshot(fs, work)
            for old, new in (("a", "b"), ("a", "d"), ("e", "d"), ("d", "b")):
                with self.assertRaises(m.FsError, msg=(host, old, new)):
                    fs.rename(work + "/" + old, work + "/" + new)
            self.assertEqual(self.remote_snapshot(fs, work), before, host)

    def test_remote_target_shapes_refused(self):
        for host in TEST_HOSTS:
            spec, fs, work = self.connect(host)
            self.remote_sh(fs, 'mkdir -p "$1/photo.jpg" && echo keep > "$1/photo.jpg/in" && '
                               'echo file > "$1/docs"', work)
            before = self.remote_snapshot(fs, work)
            write(os.path.join(self.tmp, host, "photo.jpg"), "new")
            write(os.path.join(self.tmp, host, "docs", "a"), "a")
            it = m.QueueItem(os.path.join(self.tmp, host, "photo.jpg"), work + "/photo.jpg",
                             False, None, spec)
            self.runner.run_item(it, lambda s: None, OPTS)
            self.assertEqual(it.status, m.STATUS_FAIL, host)
            it = m.QueueItem(os.path.join(self.tmp, host, "docs"), work + "/docs", True, None, spec)
            self.runner.run_item(it, lambda s: None, OPTS)
            self.assertEqual(it.status, m.STATUS_FAIL, host)
            self.assertEqual(self.remote_snapshot(fs, work), before, host)

    def test_remote_dry_run_changes_nothing(self):
        for host in TEST_HOSTS:
            spec, fs, work = self.connect(host)
            self.remote_sh(fs, 'mkdir -p "$1/dst" && echo newer > "$1/dst/a" && '
                               'touch -d "2030-01-01 00:00:00" "$1/dst/a"', work)
            before = self.remote_snapshot(fs, work)
            write(os.path.join(self.tmp, host, "a"), "old")
            write(os.path.join(self.tmp, host, "b"), "new")
            for dst in (work + "/dst", work + "/fresh/deep"):
                it = m.QueueItem(os.path.join(self.tmp, host), dst, True, None, spec)
                self.runner.run_item(it, lambda s: None, ["-anu", "--itemize-changes"])
                self.assertIn(it.status, (m.STATUS_DRY_OK, m.STATUS_DRY_INCOMPLETE), it.error_text)
            self.assertEqual(self.remote_snapshot(fs, work), before, host)

    def test_remote_resolution_odd_names(self):
        names = ["a.txt", "new\nline", "ünï", "sp ace", "it's"]
        for host in TEST_HOSTS:
            spec, fs, work = self.connect(host)
            src = os.path.join(self.tmp, host)
            for n in names:
                write(os.path.join(src, n), "incoming")
                self.remote_sh(fs, 'mkdir -p "$1/dst"; echo existing > "$1/dst/$2"; '
                                   'touch -d "2030-01-01 00:00:00" "$1/dst/$2"', work, n)
            it = m.QueueItem(src, work + "/dst", True, None, spec)
            self.runner.run_item(it, lambda s: None, OPTS)
            self.assertEqual(sorted(c.relpath for c in it.conflicts), sorted(names), host)
            for c in it.conflicts:
                ok, err = self.runner.resolve_conflict(it, c, "rename")
                self.assertTrue(ok, (host, c.relpath, err))
            for n in names:
                self.assertEqual(self.remote_sh(fs, 'cat "$1/dst/$2"', work, n), b"incoming", (host, n))
            count = self.remote_sh(fs, 'ls "$1/dst" | grep -c conflict-', work).strip()
            self.assertEqual(count, str(len(names)).encode(), host)

    def test_size_cancel(self):
        spec, fs, work = self.connect(TEST_HOSTS[0])
        ev = threading.Event()
        ev.set()
        t = time.time()
        self.assertIsNone(fs.size(work, True, ev))
        self.assertLess(time.time() - t, 5)


@unittest.skipUnless(TEST_HOSTS, "MCRSYNC_TEST_HOSTS not set (run inside the Docker rig)")
class RemoteShellIndependenceTest(RemoteBase):
    def test_odd_args_round_trip(self):
        names = ["back\\slash", "two\\\\back", "%s %d %%", "trail\n", "\nlead", "-n", "'\"$`!*?[",
                 "tab\there", "ünï", "a" * 200]
        for host in TEST_HOSTS:
            spec, fs, work = self.connect(host)
            for n in names:
                fs.mkdir(work + "/" + n)
            got = sorted(name for name, is_dir in fs.list_dir(work) if is_dir)
            self.assertEqual(got, sorted(names), host)
            self.assertEqual(fs.resolve(work + "/trail\n"), work + "/trail\n", host)
            for n in names:
                fs.rename(work + "/" + n, work + "/" + n + ".x")
                self.assertTrue(fs.exists(work + "/" + n + ".x"), (host, n))
                self.assertFalse(fs.exists(work + "/" + n), (host, n))


class RemoteCommandLineTest(unittest.TestCase):
    def test_login_shell_only_sees_fixed_command(self):
        captured = []

        class FakeProc(object):
            returncode = 0

            def __init__(self, cmd, **kw):
                captured.append(cmd)

            def communicate(self, data=None, timeout=None):
                captured.append(data)
                return b"", b""

        real = m.subprocess.Popen
        m.subprocess.Popen = FakeProc
        try:
            conn = m.SshConnection(m.ConnSpec("u", "h"), "/tmp/x.sock", "/dev/null")
            conn.run('echo "$1"', ["/path with 'quotes' and \\ and $HOME"])
        finally:
            m.subprocess.Popen = real
        cmd, data = captured
        self.assertEqual(cmd[-3:], ["--", "u@h", "sh -s"])
        self.assertNotIn(b"quotes", data)   # args travel only octal-encoded
        self.assertIn(b"\\047", data)


class DisplayTest(unittest.TestCase):
    def test_printable(self):
        bad = os.fsdecode(b"bad\xff\x1b[31mname\n")
        self.assertEqual(m.printable(bad), "bad??[31mname?")
        self.assertEqual(m.printable("ünï ok"), "ünï ok")


class RiskyFlagsTest(unittest.TestCase):
    def test_risky(self):
        opts = ["-a", "-u", "--delete-after", "--del", "--remove-source-files", "--rsync-path=x",
                "-e", "-ave", "--rsh=x", "--inplace", "--append-verify", "--stats", "-E", "--exclude=x"]
        self.assertEqual(m.RsyncFlags.risky(opts),
                         ["--delete-after", "--del", "--remove-source-files", "--rsync-path=x",
                          "-e", "-ave", "--rsh=x", "--inplace", "--append-verify"])
        self.assertEqual(m.RsyncFlags.risky(m.DEFAULT_RSYNC_OPTS), [])


class QueueFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "q.json")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def write_json(self, data):
        with open(self.path, "w") as f:
            json.dump(data, f)

    def test_bad_opts_rejected(self):
        for opts in ("-a", [], ["-a", 5], ["-a\0"], None):
            self.write_json({"rsync_opts": opts, "items": []})
            with self.assertRaises(ValueError, msg=repr(opts)):
                m.App.read_queue_file(self.path)

    def test_formats(self):
        self.write_json([{"src": "/a", "dst": "/b"}])
        items, opts = m.App.read_queue_file(self.path)
        self.assertEqual((len(items), opts), (1, None))
        self.write_json({"rsync_opts": ["-a", "--delete"], "items": [{"src": "/a", "dst": "/b"}]})
        items, opts = m.App.read_queue_file(self.path)
        self.assertEqual(opts, ["-a", "--delete"])
        for bad in ("[1, 2", "42", '"x"'):
            with open(self.path, "w") as f:
                f.write(bad)
            with self.assertRaises((ValueError, KeyError, TypeError, AttributeError), msg=bad):
                m.App.read_queue_file(self.path)

    def test_one_bad_item_rejects_whole_file(self):
        self.write_json({"items": [{"src": "/a", "dst": "/b"}, {"src": "rel", "dst": "/b"}]})
        with self.assertRaises(ValueError):
            m.App.read_queue_file(self.path)

    def test_planted_symlink_at_temp_name_not_followed(self):
        # Another user pre-creates the old predictable temp names as
        # symlinks to a victim file in a shared folder.
        victim = os.path.join(self.tmp, "victim")
        write(victim, "precious")
        for name in ("q.json.tmp", "s.yaml.tmp"):
            os.symlink(victim, os.path.join(self.tmp, name))
        app = m.App.__new__(m.App)
        app.rsync_opts = ["-a"]
        app.queue = [m.QueueItem("/a", "/b")]
        app.save_queue(self.path)
        s = m.Settings(os.path.join(self.tmp, "s.yaml"))
        s.data["a"] = 1
        self.assertEqual(s.save(), "")
        self.assertEqual(read(victim), "precious")
        self.assertEqual(m.App.read_queue_file(self.path)[0][0].src, "/a")
        self.assertEqual(sorted(os.listdir(self.tmp)),
                         ["q.json", "q.json.tmp", "s.yaml", "s.yaml.tmp", "victim"])

    def test_save_through_symlinked_folder(self):
        # e.g. Synology: /var/services/homes -> /volume1/homes
        real = os.path.join(self.tmp, "real")
        os.mkdir(real)
        link = os.path.join(self.tmp, "homes")
        os.symlink(real, link)
        app = m.App.__new__(m.App)
        app.rsync_opts = ["-a"]
        app.queue = []
        app.save_queue(os.path.join(link, "q.json"))
        s = m.Settings(os.path.join(link, "s.yaml"))
        self.assertEqual(s.save(), "")
        self.assertEqual(sorted(os.listdir(real)), ["q.json", "s.yaml"])

    def test_save_is_atomic(self):
        app = m.App.__new__(m.App)
        app.rsync_opts = ["-a"]
        app.queue = [m.QueueItem("/a", "/b")]
        app.save_queue(self.path)
        self.assertEqual(os.listdir(self.tmp), ["q.json"])
        items, opts = m.App.read_queue_file(self.path)
        self.assertEqual(items[0].src, "/a")


class RunOrderTest(unittest.TestCase):
    """_run_worker's item selection while the queue changes underneath."""

    def make_app(self, n):
        app = m.App.__new__(m.App)
        app.queue = [m.QueueItem("/s{0}".format(i), "/d{0}".format(i)) for i in range(n)]
        app.paused = False
        app.rsync_opts = m.DEFAULT_RSYNC_OPTS
        app.registry = m.ConnectionRegistry()
        app.log = lambda s: None
        app.running = True
        return app

    def test_delete_and_append_during_run(self):
        app = self.make_app(4)
        ran = []
        q = app.queue
        doomed = q[2]
        added = m.QueueItem("/new", "/newd")

        class FakeRunner(object):
            def run_item(self, item, log_cb, opts):
                ran.append(item.src)
                if item.src == "/s0":
                    # user deletes an item before the current one + a later
                    # one, and appends a new one, while /s0 is running
                    doomed.cancel_event.set()
                    q.remove(doomed)
                    q.insert(0, q.pop(q.index(item)))
                    q.append(added)
                item.status = m.STATUS_OK
                return True

        app.runner = FakeRunner()
        app._run_worker()
        self.assertEqual(ran, ["/s0", "/s1", "/s3", "/new"])

    def test_ok_items_skipped(self):
        app = self.make_app(3)
        app.queue[1].status = m.STATUS_OK
        ran = []

        class FakeRunner(object):
            def run_item(self, item, log_cb, opts):
                ran.append(item.src)
                return True

        app.runner = FakeRunner()
        app._run_worker()
        self.assertEqual(ran, ["/s0", "/s2"])


DOT_NAMES = [".a", ".bashrc", "..double", "...", ".hidden dir", ". space", ".-dash"]


class LocalDotFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.src = os.path.join(self.tmp, "src")
        for n in DOT_NAMES:
            write(os.path.join(self.src, n), n)
        write(os.path.join(self.src, ".config", "app", ".rc"), "nested")
        self.runner = m.RsyncRunner(m.ConnectionRegistry())

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_listed(self):
        names = [n for n, d in m.LOCAL_FS.list_dir(self.src)]
        self.assertEqual(sorted(names), sorted(DOT_NAMES + [".config"]))
        self.assertNotIn(".", names)
        self.assertNotIn("..", names)

    def test_copied_and_sized(self):
        dst = os.path.join(self.tmp, "dst")
        it = m.QueueItem(self.src, dst, True)
        self.runner.run_item(it, lambda s: None, OPTS)
        self.assertEqual(it.status, m.STATUS_OK, it.error_text)
        self.assertEqual(snapshot(dst), snapshot(self.src))
        expected = sum(len(n) for n in DOT_NAMES) + len("nested")
        self.assertEqual(m.LOCAL_FS.size(self.src, True, threading.Event()),
                         (expected, len(DOT_NAMES) + 1))

    def test_dot_file_as_single_item(self):
        dst = os.path.join(self.tmp, "dst")
        os.mkdir(dst)
        it = m.QueueItem(os.path.join(self.src, ".bashrc"), os.path.join(dst, ".bashrc"), False)
        self.runner.run_item(it, lambda s: None, OPTS)
        self.assertEqual(it.status, m.STATUS_OK, it.error_text)
        self.assertEqual(os.listdir(dst), [".bashrc"])

    def test_conflict_names(self):
        dst = os.path.join(self.tmp, "dst")
        for n in DOT_NAMES:
            write(os.path.join(dst, n), "existing", FUTURE)
        it = m.QueueItem(self.src, dst, True)
        self.runner.run_item(it, lambda s: None, OPTS)
        self.assertEqual(sorted(c.relpath for c in it.conflicts), sorted(DOT_NAMES))
        for c in it.conflicts:
            ok, err = self.runner.resolve_conflict(it, c, "rename")
            self.assertTrue(ok, (c.relpath, err))
        after = sorted(os.listdir(dst))
        for n in DOT_NAMES:
            self.assertEqual(read(os.path.join(dst, n)), n)
            kept = [a for a in after if a.startswith(n + ".conflict-")]
            self.assertEqual(len(kept), 1, (n, after))

    def test_dot_names_refused_for_rename_and_mkdir(self):
        write(os.path.join(self.tmp, "f"), "x")
        before = snapshot(self.tmp)
        for bad in (".", ".."):
            for op in (lambda: m.LOCAL_FS.rename(os.path.join(self.tmp, "f"),
                                                 os.path.join(self.tmp, bad)),
                       lambda: m.LOCAL_FS.mkdir(os.path.join(self.tmp, bad))):
                with self.assertRaises(m.FsError):
                    op()
        self.assertEqual(snapshot(self.tmp), before)
        for bad in (".", "..", "", "a/b", "a\0b"):
            self.assertTrue(m.check_entry_name(bad), repr(bad))
        for good in (".a", "..a", "...", "a.", ". x"):
            self.assertEqual(m.check_entry_name(good), "", repr(good))


@unittest.skipUnless(TEST_HOSTS, "MCRSYNC_TEST_HOSTS not set (run inside the Docker rig)")
class RemoteDotFileTest(RemoteBase):
    def test_listed_copied_sized_both_ways(self):
        for host in TEST_HOSTS:
            spec, fs, work = self.connect(host)
            for n in DOT_NAMES:
                self.remote_sh(fs, 'printf "%s" "$2" > "$1/$2"', work, n)
            self.remote_sh(fs, 'mkdir -p "$1/.config/app" && printf nested > "$1/.config/app/.rc"', work)
            names = [n for n, d in fs.list_dir(work)]
            self.assertEqual(sorted(names), sorted(DOT_NAMES + [".config"]), host)
            expected = sum(len(n) for n in DOT_NAMES) + len("nested")
            self.assertEqual(fs.size(work, True, threading.Event()),
                             (expected, len(DOT_NAMES) + 1), host)
            # pull
            local = os.path.join(self.tmp, host)
            it = m.QueueItem(work, local, True, spec, None)
            self.runner.run_item(it, lambda s: None, OPTS)
            self.assertEqual(it.status, m.STATUS_OK, it.error_text)
            self.assertEqual(sorted(os.listdir(local)), sorted(DOT_NAMES + [".config"]))
            self.assertEqual(read(os.path.join(local, ".config", "app", ".rc")), "nested")
            # push back to a new folder
            it = m.QueueItem(local, work + "/.back", True, None, spec)
            self.runner.run_item(it, lambda s: None, OPTS)
            self.assertEqual(it.status, m.STATUS_OK, it.error_text)
            self.assertEqual(sorted(n for n, d in fs.list_dir(work + "/.back")),
                             sorted(DOT_NAMES + [".config"]))

    def test_only_dot_entries_and_glob_like_names(self):
        for host in TEST_HOSTS:
            spec, fs, work = self.connect(host)
            self.assertEqual(fs.list_dir(work), [])
            self.remote_sh(fs, ': > "$1/.only"', work)
            self.assertEqual(fs.list_dir(work), [(".only", False)])
            for n in ("*", ".[!.]*", "..?*", "[a]"):
                self.remote_sh(fs, ': > "$1/$2"', work, n)
            names = [n for n, d in fs.list_dir(work)]
            self.assertEqual(sorted(names), sorted([".only", "*", ".[!.]*", "..?*", "[a]"]), host)

    def test_listing_with_dotglob_shell(self):
        # bash as sh with dotglob: '*' also matches dot files
        host = TEST_HOSTS[-1]   # debian has bash
        spec, fs, work = self.connect(host)
        self.remote_sh(fs, ': > "$1/.x"; : > "$1/y"; mkdir "$1/.d"', work)
        script = "shopt -s dotglob 2>/dev/null\n" + m.RemoteFs.LIST_SCRIPT
        rc, out, err = fs.conn.run("exec bash -c " + m.shlex.quote(script) + ' bash "$1"', [work])
        self.assertEqual(rc, 0, err)
        raw = out[out.find(m.RemoteFs.MARKER) + len(m.RemoteFs.MARKER):]
        self.assertEqual(raw.count(b".x/"), 2)   # the shell really lists it twice
        real = fs._run_data
        fs._run_data = lambda script, *args: raw
        try:
            self.assertEqual(fs.list_dir(work), [(".d", True), (".x", False), ("y", False)])
        finally:
            fs._run_data = real

    def test_dot_names_refused_remotely(self):
        for host in TEST_HOSTS:
            spec, fs, work = self.connect(host)
            self.remote_sh(fs, ': > "$1/f"', work)
            for bad in (".", ".."):
                with self.assertRaises(m.FsError):
                    fs.rename(work + "/f", work + "/" + bad)
                with self.assertRaises(m.FsError):
                    fs.mkdir(work + "/" + bad)
            self.assertEqual(fs.list_dir(work), [("f", False)])


if __name__ == "__main__":
    unittest.main()
