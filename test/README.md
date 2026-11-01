# mcrsync test rig

Docker Compose setup with:

* **client**: Python 3.4 on Debian stretch (same Python as the Synology chroot; rsync 3.1.2, OpenSSH 7.4 and rclone 1.60.1, the oldest supported versions). The repo is mounted read-only at `/app`. To test another rclone: `docker compose build --build-arg RCLONE_VERSION=1.68.2 client`.
* **remote-alpine**: busybox userland, stands in for a Synology NAS.
* **remote-debian**: GNU userland, stands in for a regular Linux server.
* **rclone-s3**: a recent rclone serving S3 (key `test`, secret `testsecret`): object storage, where folders only exist with files in them and renames are copy + delete.
* **rclone-dav**: a recent rclone serving plain WebDAV, which can't store modification times.

The rclone tests write their own rclone config (local, alias, S3, WebDAV and an unreachable remote) to a temp folder. `fixtures/encrypted-rclone.conf` is an encrypted config with the password `secret`.

Both remotes listen on port **2222** and have two users:

| User      | Login                                        |
|-----------|----------------------------------------------|
| `keyuser` | key only: `/root/.ssh/id_test` in the client |
| `pwuser`  | password `secret`                            |

The key pair is generated once into a Docker volume by the `keygen` service.

## Automated tests

From the `test/` directory:

```bash
docker compose build
docker compose run --rm -T -w /tmp client python3 -m unittest discover -s /app/test -v
```

In Git Bash on Windows, prefix the `run` command with `MSYS_NO_PATHCONV=1`, or `/tmp` gets rewritten into a Windows path.

| File              | Covers |
|-------------------|--------|
| `test_mcrsync.py` | settings parser, flag detection, rsync output parsing, queue items; local runs; every remote operation against both hosts (listing odd file names, rename, mkdir, size, push, pull, dry run, conflicts, connection loss) |
| `test_safety.py`  | validation of queue files (incl. their rsync flags) and form input (option injection); remote commands independent of the login shell; display of undecodable names; run order when the queue changes mid-run; that nothing is overwritten, moved or created unexpectedly: renames, odd target shapes, dry runs, stale conflicts, failed resolutions, wrong-host sockets, stale sockets, shell banners, symlinks planted at temp-file names |
| `test_progress.py` | live progress: rsync output splitting and parsing, phases (incl. deleting and dry runs), reviewed vs. copied counts (up-to-date files, conflicts, stale estimates), run-log lines; counts and size estimates locally and over SSH in both directions |
| `test_ui.py`      | the real curses UI driven through tmux: password and key login, push, pull, dry run, conflict resolution, connection lost mid-run, malicious queue file, queue-file flags confirmation, flag checkboxes and command preview, save-overwrite question, running item protected, undisplayable file names, arrow keys and Esc in popups and prompts, non-ASCII typing, too-small terminal, live progress line and run log, SIGHUP cleanup |
| `test_attacks.py` | attacks that must fail: extra paths smuggled in as flags, risky and unknown flags (incl. rclone backend flags that redirect data and `-command` flags) for both tools, `..`/absolute names in tool output, a fake rclone printing hostile names and garbage, symlinks planted on a remote (local folder, local-type remote, S3) or already in the target, rsync replacing a pulled link on the next run |
| `test_rclone.py`  | rclone remote names, queue files and item combinations, flag checks (risky and refused flags), JSON log parsing and progress; against a local-type remote: listing, rename/mkdir without replacing, push/pull, conflicts and resolutions, dry run, single files, cloud to cloud, copies into themselves (incl. aliases), odd target shapes, symlinks, encrypted config, too old or missing rclone, pausing on exit codes 5/7/8 and unreachable remotes; S3 (folders, renames, conflicts) and WebDAV without modification times; the UI connect flow, pane rules, flag editor, malicious queue file and the busy spinner during a slow listing |

Outside the rig (no `MCRSYNC_TEST_HOSTS`), only the local tests run; the rclone tests that don't need the S3/WebDAV servers run whenever `rclone` is installed.

## Manual checks

Things the automated tests can't judge, like readability and layout on your own terminal:

```bash
docker compose run --rm client
python3 /app/mcrsync.py /tmp
```

1. Resize the terminal small and large; the form, messages and panes should stay readable.
2. Try a wrong password three times: "Connection FAILED" with ssh's reason, and the pane stays local.
3. An unknown host key: the terminal shows ssh's fingerprint question (delete `~/.ssh/known_hosts` first).
4. Read the help screen (`h`) for anything unclear.
