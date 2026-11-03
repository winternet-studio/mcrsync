# -*- coding: utf-8 -*-
"""Adversarial tests: what a hostile queue file, cloud storage, remote
host or tool output could make mcrsync do. Each test is an attack that
must fail: nothing written outside the target, nothing replaced, no extra
files copied, no data sent elsewhere unnoticed.

Most tests use a fake rclone that prints whatever the test wants; the
symlink tests need the real rclone (and S3 from the Docker rig)."""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
import mcrsync as m
from test_safety import write, read, snapshot
import test_rclone
from test_rclone import HAVE_RCLONE, SERVICES, log_line

R = m.RsyncFlags
C = m.RcloneFlags


def setUpModule():
    test_rclone.setUpModule()


def tearDownModule():
    test_rclone.tearDownModule()


class FlagAttackTest(unittest.TestCase):
    """Flags from a queue file are confirmed by the user, so everything
    that could hurt must be pointed out (risky) or at least flagged as
    unknown (unchecked) - and nothing may smuggle in an extra path."""

    def test_extra_paths_refused(self):
        # a bare token would be one more source (rsync) or argument (rclone)
        for flags in (R, C):
            for opts in (["-a", "/etc"], ["--update", "evil:bucket"], ["--exclude", "*.tmp"],
                         ["--"], ["-"], ["-u", "--", "-x"]):
                self.assertNotEqual(flags.problems(opts), "", (flags.TOOL, opts))
            self.assertEqual(flags.problems(flags.DEFAULTS), "")
        self.assertEqual(R.problems(["-a", "--exclude=*.tmp", "--chmod=Du+w"]), "")

    def test_rsync_risky(self):
        for o in ("--log-file=/home/me/.bashrc", "--temp-dir=/tmp", "-T/tmp", "-aT/x",
                  "--partial-dir=/x", "--partial-dir=../x", "--backup-dir=~/x", "-K",
                  "--keep-dirlinks", "-L", "--copy-links", "--copy-unsafe-links",
                  "-M--log-file=/x", "--remote-option=--x", "--write-batch=x",
                  "--only-write-batch=x", "--read-batch=x", "-b", "--backup", "--suffix=~",
                  "--rsync-path=sh -c x", "-e", "-avze", "--rsh=x", "--remove-source-files",
                  "--delete", "--delete-after", "--del", "--inplace", "--append-verify",
                  "--force", "-I", "--ignore-times", "--super"):
            self.assertEqual(R.risky([o]), [o], o)

    def test_rsync_known_and_unknown(self):
        robust = ["-a", "-u", "--whole-file", "--no-perms", "--no-owner", "--no-group",
                  "--partial", "--partial-dir=.rsync-partial", "--stats", "--itemize-changes"]
        for opts in (m.DEFAULT_RSYNC_OPTS, robust, ["-avzhP", "--exclude=*.tmp", "--bwlimit=1M"]):
            self.assertEqual((R.risky(opts), R.unchecked(opts)), ([], []), opts)
        unknown = ["-R", "--files-from=list", "--link-dest=/x", "--usermap=a:b", "-y0",
                   "--new-flag"]
        self.assertEqual(R.unchecked(unknown), unknown)

    def test_rclone_risky_and_unknown(self):
        self.assertEqual((C.risky(C.DEFAULTS), C.unchecked(C.DEFAULTS)), ([], []))
        for o in ("--webdav-bearer-token-command=curl evil", "--password-command=x",
                  "--sftp-ssh-command=x", "--config=/tmp/evil.conf", "--metadata-mapper=x",
                  "--backup-dir=s3:/old", "--no-check-dest", "--ignore-size", "-I", "-uL",
                  "--use-server-modtime", "--rc", "--delete-excluded"):
            self.assertEqual(C.risky([o]), [o], o)
        # backend flags can redirect data: unknown, so the user is told to check
        for o in ("--s3-endpoint=http://evil", "--b2-account=x", "--header=X: y",
                  "--drive-impersonate=x", "-x"):
            self.assertEqual(C.unchecked([o]), [o], o)
        self.assertEqual(C.unchecked(["-ulcM", "--exclude=a", "--transfers=8"]), [])

    def test_queue_file_with_extra_paths_refused(self):
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, "q.json")
            for key, opts in (("rsync_opts", ["-a", "/etc/shadow"]),
                              ("rclone_opts", ["--update", "evil:"])):
                with open(path, "w") as f:
                    json.dump({key: opts, "items": [{"src": "/a", "dst": "/b"}]}, f)
                with self.assertRaises(ValueError, msg=key):
                    m.QueueFile.read(path)
        finally:
            shutil.rmtree(tmp)

    def test_rsync_run_with_extra_path_copies_nothing(self):
        tmp = tempfile.mkdtemp()
        try:
            write(os.path.join(tmp, "src", "a"), "a")
            write(os.path.join(tmp, "secret", "s"), "s")
            it = m.QueueItem(os.path.join(tmp, "src"), os.path.join(tmp, "dst"), True)
            m.RsyncRunner(m.ConnectionRegistry()).run_item(
                it, lambda s: None, ["-a", os.path.join(tmp, "secret")])
            self.assertEqual(it.status, m.STATUS_FAIL)
            self.assertFalse(os.path.exists(os.path.join(tmp, "dst")))
        finally:
            shutil.rmtree(tmp)


class NameAttackTest(unittest.TestCase):
    """Names in tool output are joined to the item's folders for Verify
    and conflict resolution - they must not lead out of it."""

    def test_safe_relname(self):
        for ok in ("a", "a/b", ".hidden", "a..b", "..x/y", "x.."):
            self.assertTrue(m.Runner.safe_relname(ok), ok)
        for bad in ("", "/etc/passwd", "../x", "a/../../x", "a/..", ".", "./a", "a//b",
                    "a/", "a\0b"):
            self.assertFalse(m.Runner.safe_relname(bad), bad)

    def test_unsafe_names_ignored_in_results(self):
        for runner in (m.RsyncRunner(m.ConnectionRegistry()),
                       m.RcloneRunner(m.ConnectionRegistry())):
            it = m.QueueItem("/src", "/dst", True)
            runner._add_results(it, m.LOCAL_FS, m.LOCAL_FS, ["ok", "../../etc/x"],
                                ["/etc/passwd", "sub/fine", "a/../../b"], False)
            self.assertEqual([r[0] for r in it.copied_list], ["/src/ok"])
            self.assertEqual([c.dst_full for c in it.conflicts], ["/dst/sub/fine"])
            self.assertEqual(len(it.errors_list), 3)
            self.assertTrue(all("unexpected file name" in r[2] for r in it.errors_list))


FAKE = r"""#!/bin/sh
# fake rclone: answers from files the test wrote into $FAKE_DIR
case "$1" in
  version) echo "rclone v1.60.1" ;;
  listremotes) echo "evil:" ;;
  backend) echo '{"Name":"evil","Root":"","Precision":1,"Features":{"BucketBased":false}}' ;;
  lsjson)
    for a in "$@"; do [ "$a" = "-R" ] && exec cat "$FAKE_DIR/tree.json"; done
    for a in "$@"; do [ "$a" = "--stat" ] && exec cat "$FAKE_DIR/stat.json"; done
    cat "$FAKE_DIR/list.json" ;;
  copy|copyto) cat "$FAKE_DIR/copy.log" >&2; exit "${FAKE_RC:-0}" ;;
  *) echo "unexpected: $*" >&2; exit 1 ;;
esac
"""


class HostileRcloneTest(unittest.TestCase):
    """A remote (or a broken/compromised rclone) that reports nasty names
    and garbage. Uses a fake rclone, so the real one isn't needed."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.fake_dir = os.path.join(self.tmp, "fake")
        os.makedirs(self.fake_dir)
        # where a copy could write: checked to stay unchanged
        self.world = os.path.join(self.tmp, "world")
        write(os.path.join(self.world, "existing.txt"), "keep")
        fake = os.path.join(self.fake_dir, "rclone")
        with open(fake, "w") as f:
            f.write(FAKE)
        os.chmod(fake, 0o755)
        self.saved = m.RcloneCli.BINARY
        m.RcloneCli.BINARY = fake
        os.environ["FAKE_DIR"] = self.fake_dir
        self.answer("stat.json", '{"Path":"x","Name":"x","Size":1,"IsDir":false}')
        self.registry = m.ConnectionRegistry()
        self.spec = m.RcloneSpec("evil")
        self.assertEqual(self.registry.open_rclone(self.spec), "")
        self.fs = self.registry.fs_for(self.spec)

    def tearDown(self):
        m.RcloneCli.BINARY = self.saved
        os.environ.pop("FAKE_DIR", None)
        shutil.rmtree(self.tmp)

    def answer(self, name, text):
        with open(os.path.join(self.fake_dir, name), "wb") as f:
            f.write(text.encode("utf-8", "surrogateescape") if isinstance(text, str) else text)

    def test_listing_names(self):
        entries = [{"Name": n, "IsDir": d} for n, d in (
            ("../up", True), ("a/b", False), ("", False), (".", True), ("..", True),
            ("ok", False), ("dir", True))]
        entries += [{"Name": 5}, {"IsDir": True}, "x", None, {"Name": ["l"]}]
        self.answer("list.json", json.dumps(entries))
        self.assertEqual(self.fs.list_dir("/"), [("dir", True), ("ok", False)])
        for bad in ("not json", '{"a": 1}', "", '"str"'):
            self.answer("list.json", bad)
            with self.assertRaises(m.FsError, msg=bad):
                self.fs.list_dir("/")

    def pull(self, copy_log, tree=None, rc=0):
        self.answer("tree.json", "[\n" + ",\n".join(json.dumps(e) for e in (tree or [])) + "\n]")
        self.answer("copy.log", copy_log)
        os.environ["FAKE_RC"] = str(rc)
        dst = os.path.join(self.world, "dst")
        it = m.QueueItem("/bucket", dst, True, self.spec, None)
        before = snapshot(self.world)
        try:
            result = m.Transfers(self.registry).run_item(it, lambda s: None, m.DEFAULT_RCLONE_OPTS)
        finally:
            os.environ.pop("FAKE_RC", None)
        # the fake copies nothing, so neither may mcrsync
        self.assertEqual(snapshot(self.world), before)
        return result, it

    def test_traversal_names_in_log(self):
        lines = [log_line("info", "Copied (new)", n) for n in ("ok.txt", "../../escape",
                                                               "/etc/passwd")]
        lines += [log_line("debug", "Destination is newer than source, skipping", n)
                  for n in ("sub/fine.txt", "a/../../b")]
        result, it = self.pull("\n".join(lines))
        self.assertTrue(result)
        self.assertEqual(it.status, m.STATUS_INCOMPLETE)
        self.assertEqual([r[1] for r in it.copied_list], [os.path.join(self.world, "dst/ok.txt")])
        self.assertEqual([c.dst_full for c in it.conflicts],
                         [os.path.join(self.world, "dst/sub/fine.txt")])
        self.assertEqual(len(it.errors_list), 3)

    def test_garbage_log(self):
        lines = [
            "{not json", "[1, 2]", '{"msg": 5, "object": 6, "level": []}',
            '{"stats": [1]}', '{"stats": {"bytes": "x", "transfers": null, "speed": "fast",'
            ' "transferring": [5, {"name": 7}]}}',
            log_line("info", "Copied (new)", None),
            '{"level": "debug", "msg": "Unchanged skipping", "object": {"x": 1},'
            ' "objectType": "*s3.Object"}',
            "x" * 1000000,
        ]
        log = "\n".join(lines).encode("utf-8") + b"\n\xff\xfe broken utf-8 \x00\n"
        result, it = self.pull(log)
        self.assertTrue(result)
        self.assertEqual(it.status, m.STATUS_OK, it.error_text)
        self.assertEqual(it.copied_list, [])

    def test_hostile_listing_for_symlink_check(self):
        # odd entries in the source listing must neither crash nor be used
        tree = [{"Path": "../../etc", "IsDir": True}, {"Path": 5, "IsDir": True},
                {"Path": "ok/f", "IsDir": False, "Size": 1}, {"Path": "s", "Size": "big"},
                {"IsDir": True}]
        os.makedirs(os.path.join(self.world, "dst"))
        result, it = self.pull("", tree)
        self.assertEqual(it.status, m.STATUS_OK, it.error_text)

    def test_unknown_exit_code_with_secret_looking_output(self):
        # errors are shown, never parsed for anything else
        result, it = self.pull(log_line("error", "Failed to copy: $(rm -rf ~)"), rc=1)
        self.assertEqual(it.status, m.STATUS_FAIL)
        self.assertIn("$(rm -rf ~)", it.error_text)


class SymlinkEscapeBase(unittest.TestCase):
    """A remote that holds 'x.rclonelink' (pointing outside the target)
    plus a file 'x/evil': pulling it must never write 'evil' outside."""

    REMOTE = "loc"

    def setUp(self):
        if not HAVE_RCLONE:
            self.skipTest("rclone is not installed")
        self.tmp = tempfile.mkdtemp()
        self.outside = os.path.join(self.tmp, "outside")
        os.makedirs(self.outside)
        self.registry = m.ConnectionRegistry()
        self.runner = m.Transfers(self.registry)
        self.spec = m.RcloneSpec(self.REMOTE)
        self.assertEqual(self.registry.open_rclone(self.spec), "")
        src = os.path.join(self.tmp, "upload")
        write(os.path.join(src, "x.rclonelink"), self.outside)
        write(os.path.join(src, "x", "evil"), "pwn")
        write(os.path.join(src, "normal.txt"), "n")
        self.remote_src = self.upload(src)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def upload(self, src):
        """Put the planted files on the remote; returns their remote path."""
        return src     # a local-type remote reads them where they are

    def run_pull(self, dst_spec, dst):
        it = m.QueueItem(self.remote_src, dst, True, self.spec, dst_spec)
        self.runner.run_item(it, lambda s: None, m.DEFAULT_RCLONE_OPTS)
        self.assertEqual(os.listdir(self.outside), [], "wrote outside the target")
        return it

    def test_pull_to_local_folder(self):
        dst = os.path.join(self.tmp, "pulled")
        it = self.run_pull(None, dst)
        self.assertEqual(it.status, m.STATUS_OK, it.error_text)
        # the link stays a harmless file
        self.assertEqual(read(os.path.join(dst, "x.rclonelink")), self.outside)
        self.assertFalse(os.path.islink(os.path.join(dst, "x")))
        self.assertEqual(read(os.path.join(dst, "x", "evil")), "pwn")
        cmd = self.runner.item_cmd(m.QueueItem(self.remote_src, dst, True, self.spec, None),
                                   m.DEFAULT_RCLONE_OPTS)
        self.assertNotIn("--links", cmd)

    def test_pull_through_local_type_remote(self):
        loc = m.RcloneSpec("loc")
        self.registry.open_rclone(loc)
        dst = os.path.join(self.tmp, "pulled-loc")
        it = self.run_pull(loc, dst)
        self.assertEqual(it.status, m.STATUS_OK, it.error_text)
        self.assertFalse(os.path.islink(os.path.join(dst, "x")))

    def test_existing_symlink_in_target_refused(self):
        dst = os.path.join(self.tmp, "has-link")
        os.makedirs(dst)
        os.symlink(self.outside, os.path.join(dst, "x"))
        it = self.run_pull(None, dst)
        self.assertEqual(it.status, m.STATUS_FAIL)
        self.assertIn("symlink 'x'", it.error_text)
        self.assertEqual(sorted(os.listdir(dst)), ["x"])
        # deeper down, through an rclone local-type target too
        deep = os.path.join(self.tmp, "deep")
        os.makedirs(os.path.join(deep, "a"))
        os.symlink(self.outside, os.path.join(deep, "a", "b"))
        planted = os.path.join(self.tmp, "deep-src")
        write(os.path.join(planted, "a", "b", "c", "evil"), "pwn")
        loc = m.RcloneSpec("loc")
        self.registry.open_rclone(loc)
        it = m.QueueItem(planted, deep, True, self.spec, loc)
        self.runner.run_item(it, lambda s: None, m.DEFAULT_RCLONE_OPTS)
        self.assertEqual(it.status, m.STATUS_FAIL)
        self.assertIn("symlink 'a/b'", it.error_text)
        self.assertEqual(os.listdir(self.outside), [])

    def test_symlink_beside_unrelated_folders_is_fine(self):
        dst = os.path.join(self.tmp, "unrelated")
        os.makedirs(dst)
        os.symlink(self.outside, os.path.join(dst, "elsewhere"))
        it = self.run_pull(None, dst)
        self.assertEqual(it.status, m.STATUS_OK, it.error_text)

    def test_sync_never_deletes_through_target_symlink(self):
        """'rclone sync' following links would delete what an unrelated
        symlink in the target points to."""
        precious = os.path.join(self.outside, "precious")
        write(precious, "keep")
        dst = os.path.join(self.tmp, "synced")
        os.makedirs(dst)
        os.symlink(self.outside, os.path.join(dst, "elsewhere"))
        it = m.QueueItem(self.remote_src, dst, True, self.spec, None)
        for opts in (["-uL", "--delete"], ["--update", "--copy-links=true", "--delete"]):
            self.runner.run_item(it, lambda s: None, opts)
            self.assertEqual(it.status, m.STATUS_FAIL, opts)
            self.assertIn("-L/--copy-links", it.error_text)
        # what a local remote's 'copy_links = true' in rclone.conf does too;
        # rclone 1.68 follows links then even with --copy-links=false
        env = ("RCLONE_COPY_LINKS", "RCLONE_LOCAL_COPY_LINKS")
        loc = m.RcloneSpec("loc")
        self.registry.open_rclone(loc)
        deep = os.path.join(self.tmp, "deep")
        os.makedirs(os.path.join(deep, "a"))
        os.symlink(self.outside, os.path.join(deep, "a", "b"))
        for name in env:
            os.environ[name] = "true"
        try:
            for target, spec, link in ((dst, None, "elsewhere"), (deep, loc, "a/b")):
                it = m.QueueItem(self.remote_src, target, True, self.spec, spec)
                self.runner.run_item(it, lambda s: None, m.DEFAULT_RCLONE_OPTS + ["--delete"])
                self.assertEqual(it.status, m.STATUS_FAIL)
                self.assertIn("symlink to a folder, '{0}'".format(link), it.error_text)
            # without --delete it is a plain copy, as before
            self.runner.run_item(it, lambda s: None, ["--update"])
            self.assertEqual(it.status, m.STATUS_OK, it.error_text)
        finally:
            for name in env:
                del os.environ[name]
        self.assertEqual(read(precious), "keep")


@unittest.skipUnless("s3" in SERVICES, "needs the rclone-s3 service of the Docker rig")
class SymlinkEscapeS3Test(SymlinkEscapeBase):
    REMOTE = "s3"

    def upload(self, src):
        bucket = "/t" + uuid.uuid4().hex[:12]
        subprocess.check_call(["rclone", "copy", src, "s3:" + bucket + "/planted"],
                              stderr=subprocess.DEVNULL)
        return bucket + "/planted"

    test_pull_through_local_type_remote = None
    test_existing_symlink_in_target_refused = None


class RsyncSymlinkTest(unittest.TestCase):
    """rsync itself: a link pulled in one run, replaced by a folder with a
    file on the source in the next run (a compromised host could do this)."""

    def test_second_run_replaces_link(self):
        tmp = tempfile.mkdtemp()
        try:
            src, dst, outside = (os.path.join(tmp, n) for n in ("src", "dst", "outside"))
            os.makedirs(src)
            os.makedirs(outside)
            os.symlink(outside, os.path.join(src, "x"))
            runner = m.RsyncRunner(m.ConnectionRegistry())
            first = m.QueueItem(src, dst, True)
            runner.run_item(first, lambda s: None, m.DEFAULT_RSYNC_OPTS)
            self.assertTrue(os.path.islink(os.path.join(dst, "x")))
            os.unlink(os.path.join(src, "x"))
            write(os.path.join(src, "x", "evil"), "pwn")
            second = m.QueueItem(src, dst, True)
            runner.run_item(second, lambda s: None, m.DEFAULT_RSYNC_OPTS)
            self.assertEqual(os.listdir(outside), [])
            self.assertFalse(os.path.islink(os.path.join(dst, "x")))
        finally:
            shutil.rmtree(tmp)
