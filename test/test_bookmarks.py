# -*- coding: utf-8 -*-
"""Bookmarks: the list in the settings (numbers, invalid entries, several
instances), finding the nearest existing folder, jump rules, and the real
UI through tmux (local, SSH and rclone bookmarks)."""

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
import mcrsync as m
from test_mcrsync import TEST_HOSTS, KEYFILE, PORT

APP = os.path.join(HERE, "..", "mcrsync.py")
SETTINGS = os.path.expanduser("~/.mcrsync.yaml")
HOST = TEST_HOSTS[0] if TEST_HOSTS else ""


def read(path):
    with open(path) as f:
        return f.read()


def write(path, text):
    with open(path, "w") as f:
        f.write(text)


class BookmarksTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "s.yaml")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def bookmarks(self):
        bms = m.Bookmarks(m.Settings(self.path))
        bms.load()
        return bms

    def test_add_round_trip_and_numbers(self):
        bms = self.bookmarks()
        ssh = m.ConnSpec("admin", "nas", 2222, "~/.ssh/id_nas")
        self.assertEqual(bms.add("home", None, "/home/x"), (1, ""))
        self.assertEqual(bms.add("NAS \"photos\" #1", ssh, "/volume1/photos"), (2, ""))
        self.assertEqual(bms.add("s3", m.RcloneSpec("s3"), "/bucket"), (3, ""))
        bms = self.bookmarks()
        self.assertEqual([b.key for b in bms.items], ["1", "2", "3"])
        nas = bms.get(2)
        self.assertEqual(nas.name, "NAS \"photos\" #1")
        self.assertEqual(nas.spec.key(), ssh.key())
        self.assertEqual(nas.spec.keyfile, "~/.ssh/id_nas")
        self.assertEqual(nas.display_location(), "nas:/volume1/photos")
        self.assertEqual(bms.get(3).spec.key(), "rclone:s3")
        # a deleted number is reused; the others keep theirs
        self.assertEqual(bms.delete("1"), "")
        self.assertEqual([b.key for b in bms.items], ["2", "3"])
        self.assertEqual(bms.add("other", None, "/other"), (1, ""))
        self.assertEqual(bms.add("fourth", None, "/fourth"), (4, ""))
        self.assertEqual(bms.rename("4", "renamed"), "")
        self.assertEqual(self.bookmarks().get(4).name, "renamed")
        self.assertNotIn("password", read(self.path))

    def test_duplicates_refused(self):
        bms = self.bookmarks()
        spec = m.ConnSpec("admin", "nas", 22)
        bms.add("a", spec, "/x")
        number, err = bms.add("b", m.ConnSpec("admin", "nas", 22, "~/.ssh/other"), "/x")
        self.assertIsNone(number)
        self.assertIn("Already bookmarked as [1] a", err)
        # same path, other connection: not a duplicate
        self.assertEqual(bms.add("c", None, "/x"), (2, ""))
        self.assertEqual(bms.find(spec, "/x").key, "1")
        self.assertEqual(bms.find(None, "/x").key, "2")
        self.assertIsNone(bms.find(None, "/y"))

    def test_invalid_entries_are_kept(self):
        original = ("bookmarks:\n"
                    "  1:\n"
                    "    name: \"ok\"\n"
                    "    type: \"local\"\n"
                    "    path: \"/data\"\n"
                    "  3:\n"
                    "    name: \"bad port\"\n"
                    "    type: \"ssh\"\n"
                    "    user: \"u\"\n"
                    "    host: \"h\"\n"
                    "    port: 99999\n"
                    "    path: \"/x\"\n"
                    "  4: \"not a mapping\"\n"
                    "  5:\n"
                    "    name: \"ftp\"\n"
                    "    type: \"ftp\"\n"
                    "    path: \"/x\"\n"
                    "  6:\n"
                    "    name: \"no host\"\n"
                    "    type: \"ssh\"\n"
                    "    user: \"u\"\n"
                    "    path: \"/x\"\n"
                    "  7:\n"
                    "    name: \"evil\"\n"
                    "    type: \"ssh\"\n"
                    "    user: \"-oProxyCommand=x\"\n"
                    "    host: \"h\"\n"
                    "    path: \"/x\"\n"
                    "  0:\n"
                    "    name: \"zero\"\n"
                    "    path: \"/x\"\n"
                    "  x:\n"
                    "    name: \"letter\"\n"
                    "    path: \"/x\"\n")
        write(self.path, original)
        bms = self.bookmarks()
        self.assertEqual(bms.error, "")
        by_key = dict((b.key, b) for b in bms.items)
        self.assertEqual(by_key["1"].error, "")
        self.assertIn("port", by_key["3"].error)
        self.assertIn("mapping", by_key["4"].error)
        self.assertIn("unknown type", by_key["5"].error)
        self.assertIn("host", by_key["6"].error)
        self.assertIn("user name", by_key["7"].error)
        self.assertIn("not a bookmark number", by_key["0"].error)
        self.assertIn("not a bookmark number", by_key["x"].error)
        # numbered ones in order, broken keys last
        self.assertEqual([b.key for b in bms.items], ["1", "3", "4", "5", "6", "7", "0", "x"])
        self.assertEqual(bms.add("new", None, "/new"), (2, ""))
        saved = m.Settings.parse(read(self.path))["bookmarks"]
        before = m.Settings.parse(original)["bookmarks"]
        for key in ("3", "4", "5", "6", "7", "0", "x"):
            self.assertEqual(saved[key], before[key])
        self.assertEqual(saved["2"]["path"], "/new")
        # an invalid one can still be deleted
        self.assertEqual(bms.delete("3"), "")
        self.assertNotIn("3", m.Settings.parse(read(self.path))["bookmarks"])

    def test_broken_file_blocks_and_is_left_alone(self):
        write(self.path, "bookmarks: [1]\n")
        bms = self.bookmarks()
        self.assertIn("line 1", bms.error)
        number, err = bms.add("a", None, "/a")
        self.assertIsNone(number)
        self.assertIn("fix this first", err)
        self.assertEqual(read(self.path), "bookmarks: [1]\n")
        write(self.path, "bookmarks: \"text\"\n")
        bms = self.bookmarks()
        self.assertIn("numbered entries", bms.error)
        self.assertIsNone(bms.add("a", None, "/a")[0])
        self.assertEqual(read(self.path), "bookmarks: \"text\"\n")

    def test_two_instances_keep_each_others_changes(self):
        one = self.bookmarks()
        two = self.bookmarks()
        self.assertEqual(one.add("a", None, "/a"), (1, ""))
        # two's list is stale, but adding reads the file again
        self.assertEqual(two.add("b", None, "/b"), (2, ""))
        self.assertEqual(one.add("b again", None, "/b")[0], None)
        self.assertEqual([b.key for b in one.items], ["1", "2"])
        # other settings saved from a stale copy don't drop bookmarks either
        app = m.App.__new__(m.App)
        app.settings = m.Settings(self.path)
        app.settings.load()
        one.add("c", None, "/c")
        app.log = lambda text: self.fail(text)
        app.settings.set_section("last_connect", m.OrderedDict([("kind", "ssh")]))
        app.save_connect_settings("last_remote", m.OrderedDict([("user", "u")]))
        data = m.Settings.parse(read(self.path))
        self.assertEqual(sorted(data["bookmarks"].keys()), ["1", "2", "3"])
        self.assertEqual(data["last_connect"]["kind"], "ssh")

    def test_suggest_name(self):
        ssh = m.ConnSpec("u", "nas", 22)
        self.assertEqual(m.Bookmark.suggest_name(None, "/home/x/photos"), "photos")
        self.assertEqual(m.Bookmark.suggest_name(None, "/"), "/")
        self.assertEqual(m.Bookmark.suggest_name(ssh, "/volume1/photos"), "photos (nas)")
        self.assertEqual(m.Bookmark.suggest_name(ssh, "/"), "nas")
        self.assertEqual(m.Bookmark.suggest_name(m.RcloneSpec("s3"), "/bkt"), "bkt (s3)")
        self.assertEqual(m.Bookmark.suggest_name(m.RcloneSpec("s3"), "/"), "s3")


class NearestFolderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.tmp, "a", "b"))
        write(os.path.join(self.tmp, "a", "file.txt"), "x")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_local(self):
        fs = m.LOCAL_FS
        a = os.path.join(self.tmp, "a")
        self.assertEqual(fs.nearest_folder(os.path.join(a, "b")), (os.path.join(a, "b"), True))
        self.assertEqual(fs.nearest_folder(os.path.join(a, "gone", "deeper")), (a, False))
        self.assertEqual(fs.nearest_folder(os.path.join(a, "file.txt")), (a, False))
        self.assertEqual(fs.nearest_folder(a + "/gone/"), (a, False))

    @unittest.skipUnless(shutil.which("rclone"), "rclone is not installed")
    def test_rclone(self):
        conf = os.path.join(self.tmp, "rclone.conf")
        write(conf, "[loc]\ntype = local\n")
        old = os.environ.get("RCLONE_CONFIG")
        os.environ["RCLONE_CONFIG"] = conf
        try:
            registry = m.ConnectionRegistry()
            spec = m.RcloneSpec("loc")
            self.assertEqual(registry.open_rclone(spec), "")
            fs = registry.fs_for(spec)
            a = os.path.join(self.tmp, "a")
            self.assertEqual(fs.nearest_folder(a + "/b"), (a + "/b", True))
            self.assertEqual(fs.nearest_folder(a + "/gone/x"), (a, False))
        finally:
            if old is None:
                os.environ.pop("RCLONE_CONFIG")
            else:
                os.environ["RCLONE_CONFIG"] = old

    @unittest.skipUnless(TEST_HOSTS, "needs the Docker rig")
    def test_ssh(self):
        registry = m.ConnectionRegistry()
        spec = m.ConnSpec("keyuser", HOST, PORT, KEYFILE)
        conn = registry.get(spec)
        ok, err = conn.connect()
        self.assertTrue(ok, err)
        try:
            fs = registry.fs_for(spec)
            self.assertEqual(fs.nearest_folder("/etc/gone/x"), ("/etc", False))
            self.assertEqual(fs.nearest_folder("/etc/passwd"), ("/etc", False))
            self.assertEqual(fs.nearest_folder("~/gone"), (fs.resolve("~"), False))
        finally:
            registry.close_all()


class FakeFs:
    is_remote = True
    kind = "ssh"

    def __init__(self, spec):
        self.spec = spec


class FakePane:
    def __init__(self, fs, path):
        self.fs = fs
        self.path = path
        self.local_path = "/"


class JumpRulesTest(unittest.TestCase):
    """Jumps that must leave the pane alone (no curses: messages recorded)."""

    def setUp(self):
        self.messages = []
        self.orig_message = m.message
        m.message = lambda stdscr, text: self.messages.append(text)
        self.app = m.App.__new__(m.App)
        self.app.stdscr = None
        self.app.queue = []
        self.app.running = False
        self.nas = m.ConnSpec("u", "nas", 22)
        self.app.left = FakePane(m.LOCAL_FS, "/home")
        self.app.right = FakePane(FakeFs(self.nas), "/volume1")

    def tearDown(self):
        m.message = self.orig_message

    def test_running_queue_keeps_the_connection(self):
        self.app.queue = [m.QueueItem("/a", "/volume1/a", True, None, self.nas)]
        self.app.running = True
        bm = m.Bookmark.from_entry("1", {"name": "home", "type": "local", "path": "/tmp"})
        self.app.jump_to_bookmark(self.app.right, bm)
        self.assertIn("running queue uses", self.messages[0])
        self.assertEqual(self.app.right.path, "/volume1")

    def test_pane_rules(self):
        other = m.Bookmark.from_entry("2", {"name": "other", "type": "ssh", "user": "u",
                                            "host": "other", "path": "/x"})
        cloud = m.Bookmark.from_entry("3", {"name": "s3", "type": "rclone", "remote": "s3",
                                            "path": "/x"})
        for bm in (other, cloud):
            self.app.jump_to_bookmark(self.app.left, bm)
            self.assertIn("must stay local", self.messages.pop())
            self.assertEqual(self.app.left.path, "/home")

    def test_invalid_bookmark(self):
        bm = m.Bookmark.from_entry("4", {"name": "x", "type": "ftp", "path": "/x"})
        self.app.jump_to_bookmark(self.app.left, bm)
        self.assertIn("can't be used", self.messages[0])
        self.assertEqual(self.app.left.path, "/home")


# ----------------------------------------------------------------------
# The real UI through tmux
# ----------------------------------------------------------------------

@unittest.skipUnless(shutil.which("tmux"), "needs tmux")
class BookmarkUiTest(unittest.TestCase):
    SESSION = "mcrsync-bookmarks"

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="bm-ui-")
        for rel in ("a", "b/deep", "target"):
            os.makedirs(os.path.join(self.root, rel))
        if os.path.exists(SETTINGS):
            os.unlink(SETTINGS)
        self.rclone_conf = os.path.join(self.root, "rclone.conf")
        write(self.rclone_conf, "[loc]\ntype = local\n")

    def tearDown(self):
        subprocess.call(["tmux", "kill-session", "-t", self.SESSION], stderr=subprocess.DEVNULL)
        time.sleep(0.5)
        shutil.rmtree(self.root, ignore_errors=True)
        if os.path.exists(SETTINGS):
            os.unlink(SETTINGS)

    def start(self):
        subprocess.check_call(["tmux", "new-session", "-d", "-s", self.SESSION, "-x", "150",
                               "-y", "40", "-c", self.root,
                               "MCRSYNC_LATEST_VERSION_URL= RCLONE_CONFIG={0} python3 {1}; "
                               "sleep 600".format(self.rclone_conf, APP)])
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

    def in_log(self, text):
        self.keys("g")
        self.wait_for(text)
        self.keys("g")

    def pane_path(self, side):
        """The path line of the left or right pane."""
        line = self.screen().split("\n")[1]
        half = line[:len(line) // 2] if side == "left" else line[len(line) // 2:]
        return half.strip(" │|")

    def wait_path(self, side, path, timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            if self.pane_path(side) == path:
                return
            time.sleep(0.3)
        self.fail("{0} pane shows {1!r}, not {2!r}; screen:\n{3}".format(
            side, self.pane_path(side), path, self.screen()))

    def add_bookmark(self, expected_name):
        self.keys("b")
        self.wait_for(" Bookmarks ")
        self.keys("a")
        s = self.wait_for("Add bookmark")
        self.assertIn(expected_name, s)
        self.keys("Enter")
        self.wait_for(" Bookmarks ")
        self.keys("Escape")

    def test_local_add_jump_rename_delete(self):
        self.start()
        self.keys("b")
        self.wait_for("No bookmarks yet")
        self.keys("Escape")
        self.keys("Down")           # a
        self.keys("Enter")
        self.wait_path("left", self.root + "/a")
        self.add_bookmark("a")
        self.in_log("Bookmarked [1] a")
        self.keys("b")
        self.keys("a")
        self.wait_for("Already bookmarked as [1] a.")
        self.keys("Enter")
        self.keys("Escape")
        self.keys("Left")           # back on a
        self.keys("Down")           # b
        self.keys("Enter")
        self.keys("Down")           # deep
        self.keys("Enter")
        self.wait_path("left", self.root + "/b/deep")
        self.keys("b")
        self.keys("a")
        self.wait_for("Add bookmark")
        self.keys("C-u")
        self.keys("Enter")
        self.wait_for("The name can't be empty.")
        self.keys("Deep one", literal=True)
        self.keys("Enter")
        s = self.wait_for("[2]   Deep one")
        self.assertIn("[1]   a", s)
        self.keys("Escape")
        # quick-jump in either pane
        self.keys("1")
        self.wait_path("left", self.root + "/a")
        self.keys("Tab")
        self.keys("2")
        self.wait_path("right", self.root + "/b/deep")
        self.keys("9")              # unused: nothing happens
        self.wait_path("right", self.root + "/b/deep")
        # the list opens on the current folder's bookmark; F2 renames it
        self.keys("b")
        self.wait_for(" Bookmarks ")
        self.keys("F2")
        self.wait_for("Rename bookmark [2]")
        self.keys("C-u")
        self.keys("Deeper", literal=True)
        self.keys("Enter")
        self.wait_for("[2]   Deeper")
        # a bookmark added in another instance shows up straight away
        bms = m.Bookmarks(m.Settings(SETTINGS))
        bms.load()
        bms.add("from elsewhere", None, self.root + "/target")
        self.keys("Escape")
        self.keys("b")
        self.wait_for("[3]   from elsewhere")
        self.keys("3")
        self.wait_path("right", self.root + "/target")
        # a missing folder opens the nearest one above it
        os.rmdir(os.path.join(self.root, "b", "deep"))
        self.keys("2")
        self.wait_for("no longer exists - showing {0}/b.".format(self.root))
        self.keys("Enter")
        self.wait_path("right", self.root + "/b")
        # delete asks first
        self.keys("b")
        self.wait_for(" Bookmarks ")
        self.keys("Home")
        self.keys("d")
        self.wait_for("Delete bookmark [1] a?")
        self.keys("y")
        s = self.wait_for("[2]   Deeper")
        self.assertNotIn("[1]   a", s)
        self.keys("Escape")
        saved = m.Settings.parse(read(SETTINGS))["bookmarks"]
        self.assertEqual(sorted(saved.keys()), ["2", "3"])
        self.assertEqual(saved["2"]["name"], "Deeper")

    def test_invalid_entry_and_quick_key_note(self):
        lines = ["bookmarks:"]
        for n in range(1, 10):
            lines += ["  {0}:".format(n), "    name: \"n{0}\"".format(n),
                      "    path: \"{0}\"".format(self.root)]
        lines += ["  10:", "    name: \"bad\"", "    type: \"ftp\"", "    path: \"/x\""]
        write(SETTINGS, "\n".join(lines) + "\n")
        self.start()
        self.keys("b")
        s = self.wait_for("[10]  <invalid: unknown type 'ftp'>")
        self.assertIn("[9]   n9", s)
        self.keys("End")
        self.keys("Enter")
        self.wait_for("Bookmark [10] can't be used: unknown type 'ftp'.")
        self.keys("Enter")
        self.keys("Down")           # a
        self.keys("Enter")
        self.add_bookmark("a")
        self.in_log("Bookmarked [11] a (no quick-jump key - delete one of 1-9 to get one)")
        self.assertIn('type: "ftp"', read(SETTINGS))

    @unittest.skipUnless(shutil.which("rclone"), "rclone is not installed")
    def test_rclone_and_local_bookmarks(self):
        bms = m.Bookmarks(m.Settings(SETTINGS))
        bms.load()
        bms.add("cloud", m.RcloneSpec("loc"), self.root + "/b")
        bms.add("home", None, self.root + "/a")
        self.start()
        self.keys("Tab")
        self.keys("1")
        self.wait_for("TARGET [rclone loc]")
        self.wait_path("right", "loc:" + self.root + "/b")
        self.in_log("Connected TARGET pane to rclone:loc")
        # queued items using the remote: asks before leaving it
        self.keys("Tab")
        self.keys("Down")
        self.keys("c")
        self.keys("Tab")
        self.keys("2")
        self.wait_for("1 unfinished queue item(s) use this host")
        self.keys("n")
        self.wait_for("TARGET [rclone loc]")
        self.keys("2")
        self.wait_for("1 unfinished queue item(s) use this host")
        self.keys("y")
        self.wait_for("TARGET [local]")
        self.wait_path("right", self.root + "/a")
        self.in_log("Disconnected TARGET pane from rclone:loc")
        # rclone opposite rclone is fine
        self.keys("1")
        self.wait_for("TARGET [rclone loc]")
        self.keys("Tab")
        self.keys("1")
        self.wait_for("SOURCE [rclone loc]")

    @unittest.skipUnless(TEST_HOSTS, "needs the Docker rig")
    def test_ssh_bookmarks(self):
        with open(os.path.expanduser("~/.ssh/known_hosts"), "w") as kh:
            kh.write(subprocess.check_output(
                "ssh-keyscan -p {0} {1} 2>/dev/null".format(PORT, HOST), shell=True).decode())
        bms = m.Bookmarks(m.Settings(SETTINGS))
        bms.load()
        bms.add("nas", m.ConnSpec("keyuser", HOST, PORT, KEYFILE), "/etc")
        bms.add("down", m.ConnSpec("keyuser", HOST, PORT + 1, KEYFILE), "/etc")
        bms.add("cloud", m.RcloneSpec("loc"), self.root)
        bms.add("tmp", m.ConnSpec("keyuser", HOST, PORT, KEYFILE), "/tmp")
        self.start()
        self.keys("Tab")
        self.keys("Down")           # a
        self.keys("Enter")
        self.keys("1")
        self.wait_for("TARGET [ssh keyuser@{0}:{1}]".format(HOST, PORT))
        self.wait_path("right", HOST + ":/etc")
        # same host: only the folder changes
        self.keys("4")
        self.wait_path("right", HOST + ":/tmp")
        # a failed login leaves the pane as it was
        self.keys("2")
        self.wait_for("Press Enter to return to mcrsync")
        self.keys("Enter")
        self.wait_for("TARGET [ssh keyuser@{0}:{1}]".format(HOST, PORT))
        self.wait_path("right", HOST + ":/tmp")
        # the other pane can't connect while this one is on SSH
        self.keys("Tab")
        self.keys("3")
        self.wait_for("must stay local")
        self.keys("Enter")
        self.wait_for("SOURCE [local]")
        # suggested name for a remote root folder
        self.keys("Tab")
        self.keys("Left")
        self.wait_path("right", HOST + ":/")
        self.keys("b")
        self.wait_for(" Bookmarks ")
        self.keys("a")
        s = self.wait_for("Add bookmark")
        self.assertIn("Name: " + HOST, s)
        self.keys("Escape")
        self.keys("Escape")
        # back to local: the disconnected pane goes where the bookmark says
        self.keys("Tab")
        self.keys("b")
        self.keys("a")
        self.wait_for("Add bookmark")
        self.keys("Enter")
        self.wait_for("[5]   bm-ui-")
        self.keys("Escape")
        self.keys("Tab")
        self.keys("5")
        self.wait_for("TARGET [local]")
        self.wait_path("right", self.root)


if __name__ == "__main__":
    unittest.main()
