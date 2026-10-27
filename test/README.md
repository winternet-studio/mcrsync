# mcrsync test rig

Docker Compose setup with:

* **client**: Python 3.4 on Debian stretch (same Python as the Synology chroot; rsync 3.1.2 and OpenSSH 7.4, the oldest supported versions). The repo is mounted read-only at `/app`.
* **remote-alpine**: busybox userland, stands in for a Synology NAS.
* **remote-debian**: GNU userland, stands in for a regular Linux server.

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

Outside the rig (no `MCRSYNC_TEST_HOSTS`), only the local tests run.

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
