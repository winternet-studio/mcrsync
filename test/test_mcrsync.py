# -*- coding: utf-8 -*-
"""Tests for mcrsync's non-UI logic. Run inside the Docker rig (see
test/README.md) - the SSH tests need the remote containers and are skipped
when MCRSYNC_TEST_HOSTS isn't set."""

import os
import sys
import shutil
import subprocess
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import mcrsync as m

TEST_HOSTS = os.environ.get("MCRSYNC_TEST_HOSTS", "").split()
KEYFILE = "/root/.ssh/id_test"
PORT = 2222
NASTY_NAMES = ["a b", "it's", "new\nline", "-dash", ".hidden", "ünïcode", "star*", "$HOME"]


class SettingsTest(unittest.TestCase):
    def test_round_trip(self):
        s = m.Settings("/nonexistent")
        s.set_section("last_remote", m.OrderedDict([
            ("user", "allan"), ("host", "nas.lan"), ("port", 2222),
            ("base_path", "/volume1/my \"photos\" #1"), ("keyfile", "~/.ssh/id_x"),
        ]))
        text = m.Settings.dump(s.data)
        data = m.Settings.parse(text)
        self.assertEqual(data["last_remote"]["port"], 2222)
        self.assertEqual(data["last_remote"]["base_path"], "/volume1/my \"photos\" #1")
        self.assertEqual(list(data["last_remote"].keys()),
                         ["user", "host", "port", "base_path", "keyfile"])

    def test_hand_written(self):
        data = m.Settings.parse(
            "# comment\n"
            "last_remote:\n"
            "  user: allan   # trailing comment\n"
            "  host: 'it''s.lan'\n"
            "  port: 22\n"
            "  keyfile: ~/.ssh/id_ed25519\n"
            "top: plain value\n")
        self.assertEqual(data["last_remote"]["user"], "allan")
        self.assertEqual(data["last_remote"]["host"], "it's.lan")
        self.assertEqual(data["last_remote"]["keyfile"], "~/.ssh/id_ed25519")
        self.assertEqual(data["top"], "plain value")

    def test_rejects_unsupported(self):
        for text in ("- item\n", "a:\n  b:\n    c: 1\n", "a: [1, 2]\n", "a: yes\n",
                     "a: ~\n", "a: \"open\n", "  a: 1\n", "a:b\n", "a:\n\tb: 1\n"):
            with self.assertRaises(m.SettingsError, msg=repr(text)):
                m.Settings.parse(text)

    def test_bad_file_is_not_overwritten(self):
        d = tempfile.mkdtemp()
        try:
            path = os.path.join(d, "s.yaml")
            with open(path, "w") as f:
                f.write("a: [1]\n")
            s = m.Settings(path)
            s.load()
            self.assertIn("line 1", s.error)
            self.assertNotEqual(s.save(), "")
            with open(path) as f:
                self.assertEqual(f.read(), "a: [1]\n")
        finally:
            shutil.rmtree(d)


class RsyncFlagsTest(unittest.TestCase):
    def test_has(self):
        self.assertTrue(m.RsyncFlags.has_update(["-a", "-u"]))
        self.assertTrue(m.RsyncFlags.has_update(["-avu"]))
        self.assertTrue(m.RsyncFlags.has_update(["--update"]))
        self.assertFalse(m.RsyncFlags.has_update(["-a", "--stats", "--no-perms"]))
        self.assertTrue(m.RsyncFlags.is_dry_run(["-avn"]))
        self.assertFalse(m.RsyncFlags.is_dry_run(["--info=name"]))

    def test_without(self):
        self.assertEqual(m.RsyncFlags.without(["-avu", "-u", "--update", "--stats"], "u", "--update"),
                         ["-av", "--stats"])


class ParseOutputTest(unittest.TestCase):
    def test_parse(self):
        lines = [
            "a.txt is newer",
            ">f+++++++++ 5 c.txt",
            "cd+++++++++ 4096 sub dir/",
            ".f          100 unchanged.txt",
            ".d..t...... 4096 ./",
            "sub/b.txt is newer",
            "rsync: [sender] link_stat \"/x\" failed: No such file or directory (2)",
            "Number of files: 5 (reg: 3, dir: 2)",
        ]
        copied, newer, errors = m.RsyncRunner.parse_output(lines)
        self.assertEqual(copied, ["c.txt", "sub dir/"])
        self.assertEqual(newer, ["a.txt", "sub/b.txt"])
        self.assertEqual(len(errors), 1)


class QueueItemTest(unittest.TestCase):
    def test_round_trip_with_conn(self):
        spec = m.ConnSpec("u", "h", 2222, "~/.ssh/k")
        it = m.QueueItem("/src", "/dst", True, None, spec)
        it.status = m.STATUS_DRY_OK
        back = m.QueueItem.from_dict(it.to_dict())
        self.assertIsNone(back.src_conn)
        self.assertEqual(back.dst_conn.key(), "u@h:2222")
        self.assertEqual(back.dst_conn.keyfile, "~/.ssh/k")
        self.assertEqual(back.status, m.STATUS_PENDING)
        self.assertEqual(back.dst_display(), "h:/dst")

    def test_old_format(self):
        back = m.QueueItem.from_dict({"src": "/a", "dst": "/b", "status": m.STATUS_OK})
        self.assertIsNone(back.src_conn)
        self.assertIsNone(back.dst_conn)
        self.assertEqual(back.status, m.STATUS_OK)


def make_tree(root):
    """src tree: a.txt, sub/b.txt, c.txt"""
    os.makedirs(os.path.join(root, "sub"))
    for rel in ("a.txt", "sub/b.txt", "c.txt"):
        with open(os.path.join(root, rel), "w") as f:
            f.write(rel)


class LocalRunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.src = os.path.join(self.tmp, "src")
        self.dst = os.path.join(self.tmp, "dst")
        make_tree(self.src)
        os.makedirs(os.path.join(self.dst, "sub"))
        for rel in ("a.txt", "sub/b.txt"):
            p = os.path.join(self.dst, rel)
            with open(p, "w") as f:
                f.write("newer")
            future = time.time() + 86400
            os.utime(p, (future, future))
        self.runner = m.RsyncRunner(m.ConnectionRegistry())

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_conflicts_and_resolution(self):
        it = m.QueueItem(self.src, self.dst, True)
        self.assertTrue(self.runner.run_item(it, lambda s: None, m.DEFAULT_RSYNC_OPTS))
        self.assertEqual(it.status, m.STATUS_INCOMPLETE)
        self.assertEqual(sorted(c.relpath for c in it.conflicts), ["a.txt", "sub/b.txt"])
        with open(os.path.join(self.dst, "a.txt")) as f:
            self.assertEqual(f.read(), "newer")
        a = [c for c in it.conflicts if c.relpath == "a.txt"][0]
        ok, err = self.runner.resolve_conflict(it, a, "rename")
        self.assertTrue(ok, err)
        with open(os.path.join(self.dst, "a.txt")) as f:
            self.assertEqual(f.read(), "a.txt")
        self.assertTrue(any(n.startswith("a.conflict-") for n in os.listdir(self.dst)))

    def test_dry_run(self):
        target = os.path.join(self.tmp, "fresh")
        it = m.QueueItem(self.src, target, True)
        self.runner.run_item(it, lambda s: None, m.DEFAULT_RSYNC_OPTS + ["-n"])
        self.assertEqual(it.status, m.STATUS_DRY_OK)
        self.assertFalse(os.path.exists(target))
        self.assertIsNone(it.transfer_seconds)
        self.assertTrue(any(r[2].startswith("would copy") for r in it.copied_list))

    def test_single_file(self):
        it = m.QueueItem(os.path.join(self.src, "a.txt"), os.path.join(self.dst, "a.txt"), False)
        self.runner.run_item(it, lambda s: None, m.DEFAULT_RSYNC_OPTS)
        self.assertEqual(it.status, m.STATUS_INCOMPLETE)
        self.assertEqual([c.relpath for c in it.conflicts], ["a.txt"])


class RemoteBase(unittest.TestCase):
    """Setup shared by the remote test classes (no tests of its own)."""

    @classmethod
    def setUpClass(cls):
        # Non-interactive run: pre-trust the test host keys.
        os.makedirs("/root/.ssh", exist_ok=True)
        with open("/root/.ssh/known_hosts", "w") as kh:
            for host in TEST_HOSTS:
                for _ in range(30):
                    out = subprocess.check_output(["ssh-keyscan", "-p", str(PORT), host],
                                                  stderr=subprocess.DEVNULL)
                    if out:
                        kh.write(out.decode())
                        break
                    time.sleep(1)

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.registry = m.ConnectionRegistry()
        self.runner = m.RsyncRunner(self.registry)

    def tearDown(self):
        self.registry.close_all()
        shutil.rmtree(self.tmp)

    def connect(self, host):
        spec = m.ConnSpec("keyuser", host, PORT, KEYFILE)
        ok, err = self.registry.get(spec).connect()
        self.assertTrue(ok, err)
        fs = self.registry.fs_for(spec)
        work = fs.resolve("")
        fs.conn.run('rm -rf -- "$1"; mkdir -p -- "$1"', [work + "/t"])
        return spec, fs, work + "/t"

    def remote_sh(self, fs, script, *args):
        rc, out, err = fs.conn.run(script, list(args))
        self.assertEqual(rc, 0, err)
        return out


@unittest.skipUnless(TEST_HOSTS, "MCRSYNC_TEST_HOSTS not set (run inside the Docker rig)")
class RemoteTest(RemoteBase):
    """Runs every test against each remote container (busybox + GNU)."""

    def test_connect_check_close(self):
        for host in TEST_HOSTS:
            spec, fs, work = self.connect(host)
            self.assertTrue(self.registry.is_open(spec))
            self.registry.close(spec)
            self.assertFalse(m.SshConnection(spec, fs.conn.sock_path, "/dev/null").check())

    def test_resolve(self):
        for host in TEST_HOSTS:
            spec, fs, work = self.connect(host)
            self.assertEqual(fs.resolve(""), "/home/keyuser")
            self.assertEqual(fs.resolve("~"), "/home/keyuser")
            self.assertEqual(fs.resolve("~/t"), work)
            self.assertEqual(fs.resolve(work + "/../t"), work)
            with self.assertRaises(m.FsError):
                fs.resolve("/does/not/exist")

    def test_list_nasty_names(self):
        for host in TEST_HOSTS:
            spec, fs, work = self.connect(host)
            for name in NASTY_NAMES:
                self.remote_sh(fs, ': > "$1/$2"', work, name)
            fs.mkdir(work + "/sub dir")
            entries = fs.list_dir(work)
            self.assertEqual(entries[0], ("sub dir", True))
            self.assertEqual(sorted(n for n, d in entries if not d), sorted(NASTY_NAMES), host)
            self.assertEqual(fs.list_dir(work + "/sub dir"), [])
            with self.assertRaises(m.FsError):
                fs.list_dir(work + "/missing")

    def test_rename_mkdir_exists(self):
        for host in TEST_HOSTS:
            spec, fs, work = self.connect(host)
            fs.makedirs(work + "/x/y z")
            self.assertTrue(fs.exists(work + "/x/y z"))
            self.remote_sh(fs, ': > "$1"', work + "/x/f")
            fs.rename(work + "/x/f", work + "/x/it's")
            self.assertTrue(fs.exists(work + "/x/it's"))
            self.assertFalse(fs.exists(work + "/x/f"))
            with self.assertRaises(m.FsError):
                fs.rename(work + "/x/it's", work + "/x/y z")
            with self.assertRaises(m.FsError):
                fs.mkdir(work + "/x")

    def test_size(self):
        for host in TEST_HOSTS:
            spec, fs, work = self.connect(host)
            self.remote_sh(fs, 'mkdir -p "$1/d"; printf 12345 > "$1/d/a"; printf 123 > "$1/b"', work)
            ev = threading.Event()
            self.assertEqual(fs.size(work, True, ev), (8, 2))
            self.assertEqual(fs.size(work + "/b", False, ev), (3, 1))
            self.assertIsNone(fs.size(work + "/missing", True, ev))

    def make_remote_dst_with_newer(self, fs, work):
        self.remote_sh(fs, 'mkdir -p "$1/dst/sub"; echo newer > "$1/dst/a.txt"; '
                           'echo newer > "$1/dst/sub/b.txt"; '
                           'touch -d "2030-01-01 00:00:00" "$1/dst/a.txt" "$1/dst/sub/b.txt"', work)

    def test_push_with_conflicts_and_resolution(self):
        for host in TEST_HOSTS:
            spec, fs, work = self.connect(host)
            src = os.path.join(self.tmp, host)
            make_tree(src)
            self.make_remote_dst_with_newer(fs, work)
            it = m.QueueItem(src, work + "/dst", True, None, spec)
            self.assertTrue(self.runner.run_item(it, lambda s: None, m.DEFAULT_RSYNC_OPTS))
            self.assertEqual(it.status, m.STATUS_INCOMPLETE, it.error_text)
            self.assertEqual(sorted(c.relpath for c in it.conflicts), ["a.txt", "sub/b.txt"], host)
            self.assertTrue(fs.exists(work + "/dst/c.txt"))
            a = [c for c in it.conflicts if c.relpath == "a.txt"][0]
            ok, err = self.runner.resolve_conflict(it, a, "autorename")
            self.assertTrue(ok, err)
            names = [n for n, d in fs.list_dir(work + "/dst")]
            self.assertTrue(any(n.startswith("a.new-") for n in names), names)
            b = [c for c in it.conflicts if c.relpath == "sub/b.txt"][0]
            ok, err = self.runner.resolve_conflict(it, b, "rename")
            self.assertTrue(ok, err)
            out = self.remote_sh(fs, 'cat "$1"', work + "/dst/sub/b.txt")
            self.assertEqual(out, b"sub/b.txt")

    def test_push_to_new_nested_dir(self):
        for host in TEST_HOSTS:
            spec, fs, work = self.connect(host)
            src = os.path.join(self.tmp, host)
            make_tree(src)
            it = m.QueueItem(src, work + "/new/deeper dir", True, None, spec)
            self.runner.run_item(it, lambda s: None, m.DEFAULT_RSYNC_OPTS)
            self.assertEqual(it.status, m.STATUS_OK, it.error_text)
            self.assertTrue(fs.exists(work + "/new/deeper dir/sub/b.txt"))

    def test_pull_with_conflicts(self):
        for host in TEST_HOSTS:
            spec, fs, work = self.connect(host)
            self.remote_sh(fs, 'mkdir -p "$1/src/sub"; echo a > "$1/src/a.txt"; '
                               'echo b > "$1/src/sub/b.txt"', work)
            dst = os.path.join(self.tmp, host)
            os.makedirs(dst)
            with open(os.path.join(dst, "a.txt"), "w") as f:
                f.write("newer")
            future = time.time() + 86400
            os.utime(os.path.join(dst, "a.txt"), (future, future))
            it = m.QueueItem(work + "/src", dst, True, spec, None)
            self.runner.run_item(it, lambda s: None, m.DEFAULT_RSYNC_OPTS)
            self.assertEqual(it.status, m.STATUS_INCOMPLETE, it.error_text)
            self.assertEqual([c.relpath for c in it.conflicts], ["a.txt"])
            self.assertTrue(os.path.isfile(os.path.join(dst, "sub", "b.txt")))

    def test_pull_single_file(self):
        for host in TEST_HOSTS:
            spec, fs, work = self.connect(host)
            self.remote_sh(fs, 'echo x > "$1/it'"'"'s a file"', work)
            dst = os.path.join(self.tmp, host, "copy")
            it = m.QueueItem(work + "/it's a file", dst, False, spec, None)
            self.runner.run_item(it, lambda s: None, m.DEFAULT_RSYNC_OPTS)
            self.assertEqual(it.status, m.STATUS_OK, it.error_text)
            self.assertTrue(os.path.isfile(dst))

    def test_dry_run_push(self):
        for host in TEST_HOSTS:
            spec, fs, work = self.connect(host)
            src = os.path.join(self.tmp, host)
            make_tree(src)
            self.make_remote_dst_with_newer(fs, work)
            it = m.QueueItem(src, work + "/dst", True, None, spec)
            self.runner.run_item(it, lambda s: None, m.DEFAULT_RSYNC_OPTS + ["--dry-run"])
            self.assertEqual(it.status, m.STATUS_DRY_INCOMPLETE, it.error_text)
            self.assertTrue(all(c.dry_run for c in it.conflicts))
            self.assertFalse(fs.exists(work + "/dst/c.txt"))
            it2 = m.QueueItem(src, work + "/fresh", True, None, spec)
            self.runner.run_item(it2, lambda s: None, m.DEFAULT_RSYNC_OPTS + ["-n"])
            self.assertEqual(it2.status, m.STATUS_DRY_OK, it2.error_text)
            self.assertFalse(fs.exists(work + "/fresh"))

    def test_connection_lost(self):
        for host in TEST_HOSTS:
            spec, fs, work = self.connect(host)
            src = os.path.join(self.tmp, host)
            make_tree(src)
            fs.conn.close()
            # Without the key, BatchMode can't log in on its own - this is
            # the password-login case after the master died.
            keyless = m.ConnSpec(spec.user, spec.host, spec.port, "")
            fs = self.registry.fs_for(keyless)
            it = m.QueueItem(src, work + "/dst", True, None, keyless)
            self.assertFalse(self.runner.run_item(it, lambda s: None, m.DEFAULT_RSYNC_OPTS))
            self.assertEqual(it.status, m.STATUS_PENDING)
            with self.assertRaises(m.ConnectionLost):
                fs.list_dir(work)


if __name__ == "__main__":
    unittest.main()
