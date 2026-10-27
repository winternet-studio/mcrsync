# -*- coding: utf-8 -*-
"""End-to-end UI tests: drive the real curses UI through tmux inside the
Docker rig, including ssh's own password prompts."""

import glob
import json
import os
import shutil
import subprocess
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_mcrsync import TEST_HOSTS, KEYFILE, PORT

APP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "mcrsync.py")
HOST = TEST_HOSTS[0] if TEST_HOSTS else ""
SETTINGS = os.path.expanduser("~/.mcrsync.yaml")


def have_tmux():
    return shutil.which("tmux") is not None


def sh(cmd):
    return subprocess.check_output(cmd, shell=True, stderr=subprocess.STDOUT).decode()


@unittest.skipUnless(TEST_HOSTS and have_tmux(), "needs the Docker rig with tmux")
class UiTest(unittest.TestCase):
    SESSION = "mcrsync-ui"

    @classmethod
    def setUpClass(cls):
        with open(os.path.expanduser("~/.ssh/known_hosts"), "w") as kh:
            kh.write(sh("ssh-keyscan -p {0} {1} 2>/dev/null".format(PORT, HOST)))

    def setUp(self):
        self.root = "/tmp/ui"
        shutil.rmtree(self.root, ignore_errors=True)
        os.makedirs(self.root)
        if os.path.exists(SETTINGS):
            os.unlink(SETTINGS)
        self.remote("rm -rf ui; mkdir ui", user="keyuser")
        self.remote("rm -rf ui; mkdir ui", user="pwuser", password=True)

    def tearDown(self):
        subprocess.call(["tmux", "kill-session", "-t", self.SESSION],
                        stderr=subprocess.DEVNULL)
        time.sleep(0.5)

    # ---- helpers ----
    def remote(self, cmd, user="keyuser", password=False):
        if password:
            # pwuser has no key; reach its home via root-less trick: keyuser
            # can't write there, so use the rig's known password with ssh's
            # askpass hook.
            env = dict(os.environ, SSH_ASKPASS="/tmp/askpass.sh", DISPLAY=":0")
            with open("/tmp/askpass.sh", "w") as f:
                f.write("#!/bin/sh\necho secret\n")
            os.chmod("/tmp/askpass.sh", 0o700)
            return subprocess.check_output(
                ["setsid", "ssh", "-p", str(PORT), "-o", "PreferredAuthentications=password",
                 "pwuser@" + HOST, cmd], env=env, stdin=subprocess.DEVNULL).decode()
        return sh("ssh -i {0} -p {1} keyuser@{2} '{3}'".format(KEYFILE, PORT, HOST, cmd))

    def start(self, args="", cwd=None):
        subprocess.check_call(["tmux", "new-session", "-d", "-s", self.SESSION, "-x", "150",
                               "-y", "40", "-c", cwd or self.root,
                               "python3 {0} {1}; sleep 600".format(APP, args)])
        self.wait_for("SOURCE [")

    def keys(self, *keys, literal=False, delay=0.6):
        cmd = ["tmux", "send-keys", "-t", self.SESSION]
        if literal:
            cmd += ["-l", "--"]
        subprocess.check_call(cmd + list(keys))
        time.sleep(delay)

    def type(self, text):
        self.keys(text, literal=True)

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

    def write_settings(self, user, keyfile=""):
        with open(SETTINGS, "w") as f:
            f.write('last_remote:\n  user: {0}\n  host: {1}\n  port: {2}\n'
                    '  base_path: "~/ui"\n  keyfile: "{3}"\n'.format(user, HOST, PORT, keyfile))

    def connect_with_password(self):
        self.keys("o")
        self.wait_for("Connect TARGET pane via SSH")
        for _ in range(5):
            self.keys("Enter", delay=0.3)
        self.wait_for("password:")
        self.type("secret")
        self.keys("Enter")
        self.wait_for("TARGET [ssh pwuser@")

    def app_pid(self):
        pid = self.find_app_pid()
        if pid is None:
            self.fail("mcrsync process not found")
        return pid

    def find_app_pid(self):
        for pid in os.listdir("/proc"):
            try:
                with open("/proc/{0}/cmdline".format(pid), "rb") as f:
                    cmdline = f.read().split(b"\0")
            except (IOError, OSError):
                continue
            if cmdline[0].startswith(b"python3") and any(a.endswith(b"mcrsync.py") for a in cmdline):
                return int(pid)
        return None

    # ---- tests ----
    def test_form_validation(self):
        self.start()
        self.keys("o")
        self.wait_for("Connect SOURCE pane via SSH")
        for _ in range(4):
            self.keys("BSpace", delay=0.1)
        self.type("-oProxyCommand=x")
        self.keys("Enter")
        self.type(HOST)
        for _ in range(4):
            self.keys("Enter", delay=0.3)
        s = self.wait_for("Invalid user name")
        self.assertIn("Connect SOURCE pane via SSH", s)
        self.keys("Escape")
        self.assertNotIn("[ssh", self.screen())
        self.assertFalse(os.path.exists(SETTINGS))

    def test_password_push_and_settings(self):
        os.makedirs(os.path.join(self.root, "photos", "sub"))
        with open(os.path.join(self.root, "photos", "sub", "b.jpg"), "w") as f:
            f.write("b")
        self.write_settings("pwuser")
        self.start()
        self.keys("Tab")
        self.connect_with_password()
        self.keys("Tab")
        self.keys("c")
        self.keys("F8")
        self.wait_for("RSYNC OPTIONS")
        self.keys("Enter")
        self.wait_for("[SYNCED OK]")
        self.assertIn("{0}:/home/pwuser/ui/photos".format(HOST), self.screen())
        self.assertEqual(self.remote("cat ui/photos/sub/b.jpg", user="pwuser", password=True), "b")
        with open(SETTINGS) as f:
            saved = f.read()
        self.assertIn('user: "pwuser"', saved)
        self.assertNotIn("secret", saved)

    def test_connection_lost_mid_run_pauses_and_resumes(self):
        os.makedirs(os.path.join(self.root, "big"))
        with open(os.path.join(self.root, "big", "blob"), "wb") as f:
            f.write(os.urandom(2000000))
        self.write_settings("pwuser")
        self.start()
        self.keys("Tab")
        self.connect_with_password()
        self.keys("Tab")
        self.keys("c")
        self.keys("F8")
        self.keys("e")
        self.keys("C-u")
        self.type("-a -u --stats --itemize-changes --bwlimit=200")
        self.keys("Enter")
        self.keys("Enter")
        self.wait_for("[IN PROGRESS]")
        sh("for s in /tmp/mcrsync-*/*.sock; do ssh -S $s -O exit -p {0} pwuser@{1}; done"
           .format(PORT, HOST))
        s = self.wait_for("connection lost, press p")
        self.assertIn("[NOT SYNCED]", s)
        self.keys("p")
        self.wait_for("password:")
        self.type("secret")
        self.keys("Enter")
        self.wait_for("[SYNCED OK]", timeout=60)

    def test_key_pull_dry_run_and_resolution(self):
        self.remote("mkdir -p ui/docs; echo one > ui/docs/one.txt; echo two > ui/docs/two.txt")
        os.makedirs(os.path.join(self.root, "docs"))
        mine = os.path.join(self.root, "docs", "two.txt")
        with open(mine, "w") as f:
            f.write("mine")
        os.utime(mine, (time.time() + 86400, time.time() + 86400))
        self.write_settings("keyuser", KEYFILE)
        self.start()
        self.keys("o")
        self.wait_for("Connect SOURCE pane via SSH")
        for _ in range(5):
            self.keys("Enter", delay=0.3)
        self.wait_for("SOURCE [ssh keyuser@")
        # second remote pane is refused
        self.keys("Tab")
        self.keys("o")
        self.wait_for("rsync can't copy between two remote hosts")
        self.keys("Enter")
        self.keys("Tab")
        self.keys("c")
        # dry run
        self.keys("F8")
        self.keys("e")
        self.keys("C-u")
        self.type("-a -u -n --stats --itemize-changes")
        self.keys("Enter")
        self.wait_for("DRY RUN (-n)")
        self.keys("Enter")
        self.wait_for("[DRY RUN INCOMPLETE]")
        self.assertEqual(sorted(os.listdir(os.path.join(self.root, "docs"))), ["two.txt"])
        self.keys("v")
        self.keys("Tab")
        self.wait_for("two.txt")
        self.keys("Enter")
        self.wait_for("found by a dry run")
        self.keys("Enter")
        self.keys("Escape")
        # real run
        self.keys("F8")
        self.keys("d")
        self.keys("y")
        self.keys("Enter")
        self.wait_for("[INCOMPLETE]")
        self.keys("v")
        self.wait_for("needs attention")
        self.keys("Enter")
        self.type("r")
        self.keys("Enter")
        self.wait_for("existing renamed to two.conflict-")
        self.keys("Enter")
        self.keys("Escape")
        self.wait_for("[SYNCED OK]")
        files = sorted(os.listdir(os.path.join(self.root, "docs")))
        self.assertEqual(files[0], "one.txt")
        self.assertTrue(files[1].startswith("two.conflict-"))
        with open(mine) as f:
            self.assertEqual(f.read(), "two\n")

    def test_malicious_queue_file_is_rejected(self):
        with open(os.path.join(self.root, "queue.json"), "w") as f:
            json.dump({"rsync_opts": ["-a"], "items": [
                {"src": "/tmp", "dst": "/home/x", "is_dir": True,
                 "dst_conn": {"user": "-oProxyCommand=touch /tmp/ui/pwned", "host": "h"}}]}, f)
        self.start()
        self.keys("l")
        self.keys("Enter")
        s = self.wait_for("Load failed")
        self.assertIn("invalid user name", s)
        self.keys("Enter")
        self.keys("F8")
        self.wait_for("Queue is empty")
        self.assertFalse(os.path.exists(os.path.join(self.root, "pwned")))

    def test_queue_file_flags_need_confirmation(self):
        with open(os.path.join(self.root, "queue.json"), "w") as f:
            json.dump({"rsync_opts": ["-a", "-u", "--remove-source-files", "--exclude=a b"],
                       "items": [{"src": "/tmp/ui", "dst": "/tmp/elsewhere", "is_dir": True}]}, f)
        self.start()
        # declined: items load, flags stay the defaults
        self.keys("l")
        self.keys("Enter")
        s = self.wait_for("Use these flags?")
        self.assertIn("WARNING", s)
        self.assertIn("--remove-source-files", s)
        self.assertIn("'--exclude=a b'", s)
        self.keys("n")
        self.wait_for("Loaded 1 item(s)")
        self.keys("Enter")
        self.keys("F8")
        s = self.wait_for("RSYNC OPTIONS")
        self.assertNotIn("--remove-source-files", s)
        self.assertNotIn("CAUTION", s)
        self.keys("Escape")
        # accepted: the options screen shows the full list plus a caution
        self.keys("l")
        self.keys("Enter")
        self.wait_for("Use these flags?")
        self.keys("y")
        self.wait_for("Loaded 1 item(s)")
        self.keys("Enter")
        self.keys("F8")
        s = self.wait_for("CAUTION")
        self.assertIn("--remove-source-files", s)
        self.keys("Escape")

    def test_save_asks_before_overwriting(self):
        target = os.path.join(self.root, "precious.txt")
        with open(target, "w") as f:
            f.write("precious")
        self.start()
        self.keys("s")
        self.keys("C-u")
        self.type("precious.txt")
        self.keys("Enter")
        self.wait_for("already exists. Overwrite it?")
        self.keys("n")
        with open(target) as f:
            self.assertEqual(f.read(), "precious")
        self.keys("s")
        self.keys("C-u")
        self.type("precious.txt")
        self.keys("Enter")
        self.wait_for("Overwrite it?")
        self.keys("y")
        self.wait_for("Saved 0 item(s)")
        with open(target) as f:
            self.assertIn('"items"', f.read())

    def test_running_item_cannot_be_deleted(self):
        os.makedirs(os.path.join(self.root, "src", "big"))
        with open(os.path.join(self.root, "src", "big", "blob"), "wb") as f:
            f.write(os.urandom(1500000))
        os.makedirs(os.path.join(self.root, "dst"))
        self.start(os.path.join(self.root, "src"))
        # TARGET: up to /tmp/ui (cursor lands on "src"), Up to "dst", enter it
        self.keys("Tab")
        self.keys("Left")
        self.wait_for("/dst")
        self.keys("Up")
        self.keys("Right")
        self.wait_for("/tmp/ui/dst")
        self.keys("Tab")
        self.keys("c")
        self.keys("F8")
        self.keys("e")
        self.keys("C-u")
        self.type("-a -u --stats --itemize-changes --bwlimit=150")
        self.keys("Enter")
        self.keys("Enter")
        self.wait_for("[IN PROGRESS]")
        self.keys("t")
        self.keys("d")
        self.wait_for("being copied right now")
        self.keys("Enter")
        self.keys("t")
        self.wait_for("[SYNCED OK]", timeout=60)

    def test_undisplayable_file_names(self):
        d = os.fsencode(self.root)
        for name in (b"bad\xff\xfename", b"esc\x1b[31mred", b"new\nline", b"tab\tx"):
            with open(os.path.join(d, name), "w") as f:
                f.write("x")
        self.start()
        s = self.wait_for("bad??name")
        self.assertIn("esc?[31mred", s)
        self.assertIn("new?line", s)
        self.keys("Down")
        self.keys("Down")
        self.keys("F2")
        self.wait_for("Rename")
        self.keys("Escape")
        self.assertIn("SOURCE [local]", self.screen())

    def test_arrow_keys_in_dialogs(self):
        with open(os.path.join(self.root, "old.txt"), "w") as f:
            f.write("x")
        self.start()
        # rename popup: arrows used to arrive as Esc and cancel it
        self.keys("F2")
        self.wait_for("New name for file 'old.txt'")
        self.keys("Left")
        self.keys("Right")
        self.keys("C-u")
        self.type("new.txt")
        self.keys("Left")
        s = self.screen()
        self.assertIn("new.txt", s)
        self.assertNotIn("[D", s)
        self.keys("Enter")
        self.wait_for("new.txt")
        self.assertTrue(os.path.exists(os.path.join(self.root, "new.txt")))
        # mkdir popup: Tab to the buttons, Left/Right between them, Cancel
        self.keys("IC")
        self.wait_for("New folder name")
        self.type("made")
        self.keys("Tab")
        self.keys("Right")
        self.keys("Enter")
        self.assertFalse(os.path.exists(os.path.join(self.root, "made")))
        # one-line prompt: arrows don't insert garbage, Esc cancels
        self.keys("s")
        self.wait_for("Save queue as")
        self.keys("Up")
        self.keys("Left")
        s = self.screen()
        self.assertIn("queue.json", s)
        self.assertNotIn("[A", s)
        self.keys("Escape")
        self.assertNotIn("Save queue as", self.screen())
        self.assertFalse(os.path.exists(os.path.join(self.root, "queue.json")))
        # Esc in the rsync flags editor keeps the flags
        self.keys("c")
        self.keys("F8")
        self.keys("e")
        self.wait_for("rsync flags:")
        self.type(" --delete")
        self.keys("Escape")
        s = self.wait_for("RSYNC OPTIONS")
        self.assertNotIn("--delete ", s.split("Reference")[0])
        # pre-filled flags can be extended instead of retyped
        self.keys("e")
        self.type(" -v")
        self.keys("Enter")
        self.wait_for("--itemize-changes -v")
        self.keys("Escape")

    def test_non_ascii_typing(self):
        # Raw pty instead of tmux: tmux 2.3's send-keys -l double-encodes
        # non-ASCII text when no client is attached.
        import pty
        with open(os.path.join(self.root, "old.txt"), "w") as f:
            f.write("x")
        pid, fd = pty.fork()
        if pid == 0:
            os.chdir(self.root)
            os.environ["TERM"] = "xterm"
            os.execvp("python3", ["python3", APP])
        try:
            time.sleep(1.5)
            # F2 (xterm keypad mode: ESC O Q), Ctrl-U, type, Enter
            for chunk in (b"\x1bOQ", b"\x15", "ünï 名前.txt".encode("utf-8"), b"\r"):
                os.write(fd, chunk)
                time.sleep(0.5)
            end = time.time() + 5
            while time.time() < end and "ünï 名前.txt" not in os.listdir(self.root):
                time.sleep(0.2)
            self.assertIn("ünï 名前.txt", os.listdir(self.root))
        finally:
            os.kill(pid, 15)
            os.waitpid(pid, 0)
            os.close(fd)

    def test_live_progress_and_log(self):
        os.makedirs(os.path.join(self.root, "src", "big"))
        for i in range(3):
            with open(os.path.join(self.root, "src", "big", "f{0}".format(i)), "wb") as f:
                f.write(os.urandom(400000))
        # enough small files that the log is longer than the screen
        for i in range(60):
            with open(os.path.join(self.root, "src", "big", "small{0:02d}".format(i)), "w") as f:
                f.write("x")
        os.makedirs(os.path.join(self.root, "dst"))
        self.start(os.path.join(self.root, "src"))
        self.keys("Tab")
        self.keys("Left")
        self.wait_for("/dst")
        self.keys("Up")
        self.keys("Right")
        self.wait_for("/tmp/ui/dst")
        self.keys("Tab")
        self.keys("c")
        self.keys("F8")
        self.keys("e")
        self.keys("C-u")
        self.type("-a -u --stats --itemize-changes --bwlimit=400")
        self.keys("Enter")
        self.keys("Enter")
        s = self.wait_for("| copying |")
        self.assertIn("> #1 /tmp/ui/src/big", s)
        self.assertIn("| reviewed ", s)
        self.assertIn(" of 63 files | copied ", s)
        self.assertIn("now: f", s)
        self.assertIn("0/1 items, 0% done | ETA:", s.splitlines()[-1] if s.splitlines()[-1].strip()
                      else s.splitlines()[-2])
        # queue view shows it too
        self.keys("t")
        self.wait_for("| copying |")
        # run log follows the copy
        self.keys("g")
        self.wait_for("RUN LOG")
        self.wait_for("copied: f")
        self.wait_for("Finished [SYNCED OK]", timeout=30)
        self.keys("Up")
        self.wait_for("scrolled, End: follow")
        self.keys("End")
        self.keys("Escape")
        self.wait_for("QUEUE EDITOR")

    def test_small_terminal(self):
        subprocess.check_call(["tmux", "new-session", "-d", "-s", self.SESSION, "-x", "40",
                               "-y", "12", "-c", self.root, "python3 {0}; sleep 600".format(APP)])
        s = self.wait_for("Terminal too small")
        self.assertIn("need 48 x 15", s)
        # keys must not act on the invisible UI
        self.keys("c")
        self.keys("s")
        self.keys("Enter")
        self.assertIn("Terminal too small", self.screen())
        self.assertFalse(os.path.exists(os.path.join(self.root, "queue.json")))
        self.keys("q")
        time.sleep(1)
        self.assertNotIn("Terminal too small", self.screen())
        self.assertIsNone(self.find_app_pid())

    def test_sighup_closes_ssh_login(self):
        self.write_settings("keyuser", KEYFILE)
        before = set(glob.glob("/tmp/mcrsync-*/*.sock"))
        self.start()
        self.keys("o")
        for _ in range(5):
            self.keys("Enter", delay=0.3)
        self.wait_for("SOURCE [ssh keyuser@")
        socks = sorted(set(glob.glob("/tmp/mcrsync-*/*.sock")) - before)
        self.assertEqual(len(socks), 1)
        os.kill(self.app_pid(), 1)
        time.sleep(2)
        rc = subprocess.call(["ssh", "-S", socks[0], "-O", "check", "-p", str(PORT),
                              "keyuser@" + HOST], stderr=subprocess.DEVNULL)
        self.assertNotEqual(rc, 0, "ssh master still running after SIGHUP")
        self.assertFalse(os.path.exists(os.path.dirname(socks[0])))


if __name__ == "__main__":
    unittest.main()
