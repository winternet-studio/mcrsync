# -*- coding: utf-8 -*-
"""Live progress: rsync output parsing, phases, run-log lines; local and
over SSH (remote tests need the Docker rig)."""

import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import mcrsync as m
from test_mcrsync import TEST_HOSTS, RemoteBase

P = m.Progress


class SegmentsTest(unittest.TestCase):
    def split(self, chunks):
        it = iter(chunks + [b""])
        return list(m.RsyncRunner.segments(lambda: next(it)))

    def test_cr_and_lf_across_chunk_boundaries(self):
        chunks = [b">f+++++++++ a\n   32,768   2%  0.00kB/s  0:00:00\r  400,0",
                  b"00  26%  448.28kB/s  0:00:00 (xfr#1, to-chk=3/6)\n>f++",
                  b"+++++++ b", b"\n"]
        self.assertEqual(self.split(chunks), [
            b">f+++++++++ a", b"   32,768   2%  0.00kB/s  0:00:00",
            b"  400,000  26%  448.28kB/s  0:00:00 (xfr#1, to-chk=3/6)", b">f+++++++++ b"])

    def test_trailing_data_without_newline(self):
        self.assertEqual(self.split([b"a\rb"]), [b"a", b"b"])
        self.assertEqual(self.split([]), [])


class ProgressParseTest(unittest.TestCase):
    def test_line_formats(self):
        p = P()
        p.phase = P.SCANNING
        self.assertTrue(p.feed_progress("         32,768   2%    0.00kB/s    0:00:00  "))
        self.assertEqual((p.bytes, p.speed, p.files), (32768, 0.0, 0))
        self.assertEqual(p.phase, P.COPYING)
        self.assertTrue(p.feed_progress("  400,000  26%  448.28kB/s  0:00:00 (xfr#1, ir-chk=1000/1006)"))
        self.assertEqual(p.files, 1)
        self.assertAlmostEqual(p.speed, 448.28 * 1024)
        # ir-chk=0 is not the end: incremental recursion is still scanning
        p.feed_progress("  400,000  26%  448.28kB/s  0:00:00 (xfr#1, ir-chk=0/1006)")
        self.assertEqual(p.phase, P.COPYING)
        self.assertTrue(p.feed_progress("  1,100,000 73%  1.50MB/s  0:00:02 (xfr#3, to-chk=2/6)"))
        self.assertEqual((p.files, p.phase), (3, P.COPYING))
        self.assertTrue(p.feed_progress("  9,999,999,999 100%  2.00GB/s  1:02:03 (xfr#9, to-chk=0/6)"))
        self.assertEqual(p.phase, P.FINISHING)
        self.assertEqual(p.bytes, 9999999999)

    def test_not_progress(self):
        p = P()
        for line in (">f+++++++++ 12 34%  file", "Number of files: 5", "rsync: error 12%",
                     "a.txt is newer", "", "sent 189 bytes  received 78 bytes  534.00 bytes/sec"):
            self.assertFalse(p.feed_progress(line), line)

    def test_reviewed_counts_unchanged_files_too(self):
        p = P(total_bytes=1000, total_files=4)
        p.phase = P.SCANNING
        p.feed_item(".", "d", 4096, "./")          # folders don't count
        p.feed_item(".", "f", 400, "same.txt")     # up to date: reviewed, not copied
        self.assertEqual((p.reviewed_bytes, p.reviewed_files, p.current), (400, 1, ""))
        self.assertEqual(p.phase, P.COPYING)
        # a file being sent counts with what was sent so far, not its size
        p.feed_item(">", "f", 300, "new.txt")
        self.assertEqual((p.reviewed_bytes, p.reviewed_files, p.current), (400, 1, "new.txt"))
        p.feed_progress("  100  10%  1.00kB/s  0:00:00")
        self.assertEqual((p.reviewed_bytes, p.reviewed_files), (500, 1))
        p.feed_progress("  300  30%  1.00kB/s  0:00:00 (xfr#1, to-chk=2/5)")
        self.assertEqual((p.reviewed_bytes, p.reviewed_files), (700, 2))
        p.feed_item("c", "L", 7, "link")           # symlink: not file data
        p.feed_item("h", "f", 200, "hardlink")     # hard link: nothing to send
        self.assertEqual((p.reviewed_bytes, p.reviewed_files, p.current), (900, 3, "hardlink"))
        p.feed_conflict()
        self.assertEqual((p.reviewed_files, p.conflicts, p.reviewed_bytes), (4, 1, 900))

    def test_dry_run_counts_listed_sizes(self):
        p = P(dry_run=True)
        p.feed_item(">", "f", 300, "a")
        p.feed_item(".", "f", 100, "b")
        self.assertEqual((p.copied_files, p.copied_bytes, p.reviewed_bytes), (1, 300, 400))

    def test_phases_from_items(self):
        p = P()
        p.phase = P.SCANNING
        p.feed_item(">", "f", 1, "a")
        self.assertEqual((p.phase, p.current), (P.COPYING, "a"))
        p.feed_item("*", "d", 0, "old")
        self.assertEqual((p.phase, p.deleted, p.current), (P.DELETING, 1, "old"))
        # deleting isn't overridden by the scan being complete
        p.feed_progress("  10 100%  1.00kB/s  0:00:00 (xfr#1, to-chk=0/2)")
        self.assertEqual(p.phase, P.DELETING)

    def test_describe(self):
        p = P(total_bytes=4 * 1024 ** 2, total_files=100)
        self.assertIn("preparing", p.describe())
        p.phase = P.SCANNING
        self.assertIn("building the file list", p.describe())
        self.assertIn("(4.0M, 100 files to review)", p.describe())
        p.feed_progress("  1,234,567  45%  2.00MB/s  0:00:12 (xfr#12, ir-chk=88/100)")
        p.feed_item(".", "f", 1024 ** 2, "old.jpg")
        p.feed_item(">", "f", 1, "photos/img 1.jpg")
        text = p.describe()
        for part in ("copying", "reviewed 2.2M of 4.0M (54%)", "13 of 100 files",
                     "copied 12 files, 1.2M", "2.0M/s", "now: photos/img 1.jpg"):
            self.assertIn(part, text)
        self.assertNotIn("45%", text)
        self.assertIn("| 0s |", text)
        p.started -= 125
        self.assertIn("| 2m 05s |", p.describe())
        p.started -= 3600
        self.assertIn("| 1h 2m |", p.describe())
        p.feed_conflict()
        self.assertIn("1 conflict |", p.describe())
        p.feed_item("*", "d", 0, "gone")
        self.assertIn("1 DELETED", p.describe())

    def test_describe_without_or_beyond_estimate(self):
        p = P()
        p.phase = P.COPYING
        p.feed_item(".", "f", 5000, "a")
        self.assertIn("reviewed 4.9K | 1 files", p.describe())
        # source grew since the estimate: never more than 100%
        q = P(total_bytes=100, total_files=1)
        q.phase = P.COPYING
        q.feed_item(".", "f", 300, "a")
        q.feed_item(".", "f", 300, "b")
        self.assertIn("reviewed 600.0B of 600.0B (100%) | 2 of 2 files", q.describe())
        # empty source
        e = P(total_bytes=0, total_files=0)
        e.phase = P.FINISHING
        self.assertIn("(100%)", e.describe())

    def test_describe_dry_run(self):
        d = P(dry_run=True, total_bytes=10, total_files=1)
        d.phase = P.SCANNING
        d.feed_progress("  10 1%  1.00kB/s  0:00:00 (xfr#1, to-chk=1/2)")
        d.feed_item(">", "f", 10, "x")
        text = d.describe()
        self.assertIn("DRY RUN", text)
        self.assertIn("would copy 1 file, 10.0B", text)
        self.assertNotIn("/s", text)


class DescribeLineTest(unittest.TestCase):
    def test_lines(self):
        d = m.RsyncRunner.describe_line
        self.assertEqual(d(">f+++++++++ 2048 a b", False), "copied: a b (2.0K)")
        self.assertEqual(d("<f.st...... 10 x", True), "would be copied: x (10.0B)")
        self.assertEqual(d("cd+++++++++ 4096 sub/", False), "created: sub/")
        self.assertEqual(d("*deleting   0 old\#012name", False), "DELETED: old\nname")
        self.assertEqual(d("a.txt is newer", False), "CONFLICT (target newer, not copied): a.txt")
        self.assertTrue(d("rsync: connection unexpectedly closed", False).startswith("rsync:"))
        self.assertIsNone(d(".d..t...... 4096 ./", False))
        self.assertIsNone(d(".f          100 same", False))
        self.assertIsNone(d("Number of files: 3", False))

    def test_parse_item(self):
        pi = m.RsyncRunner.parse_item
        self.assertEqual(pi(".f          1000 a b"), (".", "f", 1000, "a b"))
        self.assertEqual(pi("*deleting   0 x/"), ("*", "d", 0, "x/"))
        self.assertEqual(pi(">f+++++++++ 5 12 345"), (">", "f", 5, "12 345"))
        self.assertIsNone(pi("a.txt is newer"))
        self.assertIsNone(pi("Number of files: 3"))


def make_src(root, sizes):
    for rel, size in sizes.items():
        p = os.path.join(root, rel)
        if not os.path.isdir(os.path.dirname(p)):
            os.makedirs(os.path.dirname(p))
        with open(p, "wb") as f:
            f.write(os.urandom(size))


class Sampler(object):
    """Collects snapshots of item.progress while run_item runs."""

    def __init__(self, item):
        self.item = item
        self.seen = []
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.loop)

    def sample(self):
        p = self.item.progress
        if p is not None:
            self.seen.append((p.phase, p.bytes, p.files, p.current, p.describe()))

    def loop(self):
        while not self.stop.is_set():
            self.sample()
            time.sleep(0.05)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.thread.join()
        self.sample()   # the final state, which the loop may have missed

    def phases(self):
        out = []
        for s in self.seen:
            if not out or out[-1] != s[0]:
                out.append(s[0])
        return out


class LocalProgressTest(unittest.TestCase):
    SIZES = {"f1": 300000, "f2": 300000, "sub/g": 300000}

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.src = os.path.join(self.tmp, "src")
        make_src(self.src, self.SIZES)
        self.runner = m.RsyncRunner(m.ConnectionRegistry())
        self.logged = []

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_item(self, it, opts):
        with Sampler(it) as s:
            self.runner.run_item(it, self.logged.append, opts)
        return s

    def estimated_item(self, dst):
        it = m.QueueItem(self.src, dst, True)
        it.size_bytes, it.size_files = m.LOCAL_FS.size(self.src, True, threading.Event())
        return it

    def test_live_progress_and_final_counts(self):
        it = self.estimated_item(os.path.join(self.tmp, "dst"))
        self.assertEqual((it.size_bytes, it.size_files), (sum(self.SIZES.values()), 3))
        s = self.run_item(it, m.DEFAULT_RSYNC_OPTS + ["--bwlimit=600"])
        self.assertEqual(it.status, m.STATUS_OK, it.error_text)
        phases = s.phases()
        self.assertEqual(phases[-1], P.DONE)
        self.assertIn(P.COPYING, phases)
        byte_values = [b for ph, b, f, c, d in s.seen if ph == P.COPYING]
        self.assertGreater(len(set(byte_values)), 2, "bytes should count up while copying")
        self.assertEqual(byte_values, sorted(byte_values))
        self.assertTrue(any(c in ("f1", "f2", "sub/g") for ph, b, f, c, d in s.seen))
        p = it.progress
        self.assertEqual((p.files, p.bytes), (3, sum(self.SIZES.values())))
        self.assertEqual((p.reviewed_files, p.reviewed_bytes), (3, sum(self.SIZES.values())))
        self.assertTrue(any("(100%) | 3 of 3 files" in d for ph, b, f, c, d in s.seen))
        self.assertTrue(any(line.startswith("copied: sub/g (") for line in self.logged))
        self.assertFalse(any("%" in line for line in self.logged), "progress lines must not be logged")

    def test_up_to_date_files_count_as_reviewed(self):
        dst = os.path.join(self.tmp, "dst")
        self.runner.run_item(m.QueueItem(self.src, dst, True), lambda t: None, m.DEFAULT_RSYNC_OPTS)
        make_src(self.src, {"f2": 300000})   # only f2 changes ...
        later = time.time() + 10             # ... and must look newer to -u
        os.utime(os.path.join(self.src, "f2"), (later, later))
        it = self.estimated_item(dst)
        self.run_item(it, m.DEFAULT_RSYNC_OPTS)
        p = it.progress
        self.assertEqual((p.files, p.bytes), (1, 300000))
        self.assertEqual((p.reviewed_files, p.reviewed_bytes), (3, 900000))
        self.assertIn("reviewed 878.9K of 878.9K (100%) | 3 of 3 files | copied 1 file, 293.0K",
                      p.describe())

    def test_conflicts_counted(self):
        dst = os.path.join(self.tmp, "dst")
        make_src(dst, {"f1": 10})
        future = time.time() + 86400
        os.utime(os.path.join(dst, "f1"), (future, future))
        it = self.estimated_item(dst)
        self.run_item(it, m.DEFAULT_RSYNC_OPTS)
        self.assertEqual(it.status, m.STATUS_INCOMPLETE)
        p = it.progress
        self.assertEqual((p.conflicts, p.reviewed_files, p.reviewed_bytes), (1, 3, 600000))

    def test_deleting_phase(self):
        dst = os.path.join(self.tmp, "dst")
        make_src(dst, {"stale1": 10, "stale2": 10})
        it = m.QueueItem(self.src, dst, True)
        s = self.run_item(it, m.DEFAULT_RSYNC_OPTS + ["--delete"])
        self.assertEqual(it.status, m.STATUS_OK, it.error_text)
        self.assertEqual(it.progress.deleted, 2)
        self.assertIn("DELETED: stale1", self.logged)
        self.assertIn("DELETED: stale2", self.logged)

    def test_dry_run_progress(self):
        it = m.QueueItem(self.src, os.path.join(self.tmp, "dst"), True)
        self.run_item(it, m.DEFAULT_RSYNC_OPTS + ["-n"])
        self.assertEqual(it.status, m.STATUS_DRY_OK)
        self.assertTrue(it.progress.dry_run)
        self.assertTrue(any(line.startswith("would be copied: f1 (") for line in self.logged))
        self.assertIn("DRY RUN", it.progress.describe())

    def test_users_progress_flag_doesnt_break_counts(self):
        it = m.QueueItem(self.src, os.path.join(self.tmp, "dst"), True)
        self.run_item(it, m.DEFAULT_RSYNC_OPTS + ["--progress", "-v"])
        self.assertEqual(it.status, m.STATUS_OK, it.error_text)
        self.assertEqual(it.progress.bytes, sum(self.SIZES.values()))
        self.assertEqual(it.progress.files, 3)

    def test_failed_target_check_has_progress(self):
        it = m.QueueItem(self.src, os.path.join(self.src, "inside"), True)
        self.runner.run_item(it, self.logged.append, m.DEFAULT_RSYNC_OPTS)
        self.assertEqual(it.status, m.STATUS_FAIL)
        self.assertEqual(it.progress.phase, P.PREPARING)


@unittest.skipUnless(TEST_HOSTS, "MCRSYNC_TEST_HOSTS not set (run inside the Docker rig)")
class RemoteProgressTest(RemoteBase):
    def test_push_and_pull(self):
        sizes = {"a": 200000, "b": 200000, "d/c": 100000}
        for host in TEST_HOSTS:
            spec, fs, work = self.connect(host)
            src = os.path.join(self.tmp, host, "src")
            make_src(src, sizes)
            push = m.QueueItem(src, work + "/up", True, None, spec)
            with Sampler(push) as s:
                self.runner.run_item(push, lambda t: None, m.DEFAULT_RSYNC_OPTS + ["--bwlimit=500"])
            self.assertEqual(push.status, m.STATUS_OK, push.error_text)
            self.assertEqual((push.progress.files, push.progress.bytes), (3, 500000), host)
            self.assertIn(P.COPYING, s.phases())
            pull = m.QueueItem(work + "/up", os.path.join(self.tmp, host, "down"), True, spec, None)
            with Sampler(pull) as s:
                self.runner.run_item(pull, lambda t: None, m.DEFAULT_RSYNC_OPTS + ["--bwlimit=500"])
            self.assertEqual(pull.status, m.STATUS_OK, pull.error_text)
            self.assertEqual((pull.progress.files, pull.progress.bytes), (3, 500000), host)
            self.assertIn(P.COPYING, s.phases())
            # nothing changed: everything reviewed, nothing copied - both ways
            again = m.QueueItem(src, work + "/up", True, None, spec)
            again.size_bytes, again.size_files = m.LOCAL_FS.size(src, True, threading.Event())
            self.runner.run_item(again, lambda t: None, m.DEFAULT_RSYNC_OPTS)
            p = again.progress
            self.assertEqual((p.files, p.reviewed_files, p.reviewed_bytes), (0, 3, 500000), host)
            back = m.QueueItem(work + "/up", os.path.join(self.tmp, host, "down"), True, spec, None)
            back.size_bytes, back.size_files = fs.size(work + "/up", True, threading.Event())
            self.assertEqual((back.size_bytes, back.size_files), (500000, 3), host)
            self.runner.run_item(back, lambda t: None, m.DEFAULT_RSYNC_OPTS)
            p = back.progress
            self.assertEqual((p.files, p.reviewed_files, p.reviewed_bytes), (0, 3, 500000), host)
            self.assertIn("(100%) | 3 of 3 files | copied 0 files", p.describe())


class AppProgressTextTest(unittest.TestCase):
    def make_app(self):
        app = m.App.__new__(m.App)
        app.queue = [m.QueueItem("/a", "/b"), m.QueueItem("/c", "/d")]
        app.running = True
        app.paused = False
        app.reconnect_needed = []
        app.lock = threading.Lock()
        app.log_lines = []
        app.log_dropped = False
        return app

    def test_states(self):
        app = self.make_app()
        self.assertEqual(app.progress_text(), "Starting next item...")
        app.paused = True
        self.assertIn("press p to resume", app.progress_text())
        app.reconnect_needed = [m.ConnSpec("u", "h")]
        self.assertIn("connection lost", app.progress_text())
        app.paused = False
        app.reconnect_needed = []
        it = app.queue[1]
        it.status = m.STATUS_RUNNING
        it.progress = P()
        self.assertTrue(app.progress_text().startswith("#2 /c | preparing"))
        app.running = False
        self.assertEqual(app.progress_text(), "")

    def test_log_is_capped_and_timestamped(self):
        app = self.make_app()
        for i in range(m.App.LOG_MAX + 10):
            app.log("line {0}".format(i))
        self.assertEqual(len(app.log_lines), m.App.LOG_MAX)
        self.assertTrue(app.log_dropped)
        self.assertRegex(app.log_lines[-1], r"^\d\d:\d\d:\d\d line {0}$".format(m.App.LOG_MAX + 9))


if __name__ == "__main__":
    unittest.main()
