# mcrsync

A Midnight-Commander-style `rsync` queue manager built for Python 3.4+ by Claude; specs by me.

`mcrsync` provides a visual, dual-pane terminal interface for building, editing, and executing large batches of `rsync` file transfers. It was originally designed for managing localized backup routines on older Network Attached Storage (NAS) hardware, offering modern convenience while maintaining legacy compatibility.

## Features

* **Legacy Friendly:** Targets Python 3.4+. Purposely avoids newer features (like f-strings or `os.scandir`) targetting older environments.
* **Dual-Pane Navigation:** Browse your source and target directories side-by-side. 
* **Remote over SSH:** Either pane can be switched to a remote host (press `o`), to push to or pull from another NAS or Linux server. Log in with a key file or a password; the password is asked by `ssh` itself and never stored. One login is shared by browsing, size estimates and all transfers, and a dropped connection pauses the run until you log in again. Host names and IPv4 addresses are supported (not IPv6).
* **Safe by Design ("Kid Gloves"):** Built to prevent catastrophic mistakes. You can create directories, but the tool intentionally lacks the ability to delete or remove files and folders. Furthermore, it strictly monitors file modification times to prevent overwriting newer files in the target directory.
* **Background Sizing:** Each item's total size and number of files (SIZE and FILES columns in the queue view) are calculated lazily in the background as you build your queue, keeping the UI responsive. This is everything rsync will review, whether or not it already exists on the target.
* **Live Progress:** While a run is going, a progress line shows the running item's phase (*preparing*, *scanning*, *copying*, *deleting*, *finishing*), how much of it has been reviewed against those totals (files already up to date on the target count too, so this reaches 100%), what was actually copied, speed, conflicts, elapsed time and the file being copied right now. For example: `> #3 photos | copying | reviewed 812.4M of 1.7G (47%) | 1,234 of 8,000 files | copied 4 files, 1.1M | 301.3K/s | 4m 05s | now: 2019/img_0123.jpg`. The status bar shows whole-queue progress and ETA in every view. A run log (`g`) lists every copied, created and deleted file, conflict, error and connection event with times.
* **Resumable Queues:** Save your entire queue and custom `rsync` options to a JSON file. You can pause, quit, reload, and resume right where you left off. Remote hosts are saved with the queue (never passwords).
* **Safe Dry Runs:** Tick `-n, --dry-run` on the `F8` screen to see what would be copied and which files would conflict, without touching anything. Dry-run results are marked `DRY RUN OK` / `DRY RUN INCOMPLETE`, so a later real run still processes every item.
* **Conflict Verification:** A dedicated "Verify" view lets you review skipped files, renamed items, and errors after a run, allowing you to manually resolve conflicts (Skip, Rename existing, or Auto-rename incoming). Resolutions re-check the conflict first and never overwrite an existing file. Covered by automated tests, locally and over SSH.
* **Refuses surprising copies:** A file whose target name is an existing folder, a folder whose target name is an existing file, and a folder copied into itself are reported as `SYNC FAIL` instead of being run.
* **Any file name:** Names with spaces, quotes, newlines, non-ASCII characters or a leading `-` or `.` work locally and remotely. Names that can't be displayed (control characters, invalid UTF-8) are shown with `?` instead of breaking the screen.

See [Safety](#safety) for the full list of safeguards.

---

## Dependencies

* **Python 3.4 or higher** (Uses strictly standard libraries: `curses`, `json`, `threading`, `subprocess`, etc.)
* **rsync** installed on the host system.
* **For remote panes:** the OpenSSH client (`ssh`) locally, and rsync 3.1 or newer plus a POSIX shell on the remote host (any Linux server or Synology NAS will do).
* **GNU Screen or tmux (Highly Recommended):** Because large transfers can take hours, it is highly recommended to run this script inside a multiplexer like `screen` so you can detach and safely close your SSH session without interrupting the queue. (Without one, closing the terminal ends mcrsync; it then logs out of any remote hosts cleanly.)
* **A terminal of at least 48 x 15 characters.** A smaller one shows a notice instead of the file manager (a running queue keeps going).

---

## Example Workflow

1. **Launch:** Run the program (preferably inside `screen`):
   ```bash
   python3 mcrsync.py
   ```
2. **Build the Queue:** Use `Tab` to switch between the left (Source) and right (Target) panes. Highlight a file or directory on the left, navigate to your desired destination on the right, and press `c` to add it to the queue. As you add items, their sizes will compute in the background.
   * **Remote host (optional):** In the pane that should be remote, press `o` and fill in user, host, port, base path and (optionally) a key file. Leave the key file blank to log in with a password: the screen switches to the plain terminal and `ssh` asks for it, then the file manager comes back. Only one pane can be remote at a time. Your entries (but not the password) are remembered in `~/.mcrsync.yaml`.
3. **Review and Run:** Once your queue is built, press `F8` to open the Execution Options.
4. **Customize Flags:** (Optional) Switch common flags on or off with `Up`/`Down` and `Space`, or press `e` to edit all flags as text (see the *Advanced `rsync` Options* section below). The screen shows the exact `rsync` command the first item will run with, and warns about flags that can delete or overwrite data or run commands.
5. **Start:** Press `Enter` to confirm and begin the queue.
6. **Save:** Once the queue starts, press `s` to save your progress to a `.json` file. The file saves both the remaining queue and your custom `rsync` flags in case you need to load (`l`) and resume later. 

*(Note: The ETA displayed at the bottom of the screen is a rough estimate based on total byte size. It only recalculates when a queued item finishes transferring).*

---

## Advanced `rsync` Options

By default, the script uses `-a -u --stats --itemize-changes`. However, depending on your backup target, these might not be desirable for some reason (i.e. standard archive flags might cause permission errors.)

If you only care about transferring the actual files and want to ignore users/permissions, press `F8` then `e` and use this robust alternative set of flags (flags without a checkbox, like `--no-perms`, are listed under *Other flags* on that screen):

```text
-a -u --whole-file --no-perms --no-owner --no-group --partial --partial-dir=.rsync-partial --stats --itemize-changes
```

**What these flags do:**
* `-a`: Archive mode (recurses into directories).
* `-u`: Update mode (skips files that are newer on the receiver). **Crucial for safety.**
* `--whole-file`: Disables delta-transfer algorithm. Faster for local transfers or when CPU/Disk IO on the NAS is a bigger bottleneck than network speed.
* `--no-perms --no-owner --no-group`: Ignores permission mappings. Ideal for backing up to a drive with a different filesystem or when user accounts don't match between the source and target.
* `--partial` and `--partial-dir=.rsync-partial`: Keeps partially transferred files if the connection drops, putting them in a hidden folder so large files can resume properly next time.
* `--stats --itemize-changes`: `--stats` fills in the Verify view's counts. `--itemize-changes` is no longer needed by `mcrsync` itself (see below), but is harmless.

`mcrsync` always adds `--info=skip1,progress2,name2` and its own `--out-format=%i %l %n` (overriding `-i`). Together they make rsync report files skipped because the target is newer (these become conflicts in the Verify view), live transfer progress, and one line per checked file with its size, including files that are already up to date. That is what the *reviewed* progress counts. For remote items it also adds `-s -e "ssh ..."` to use the shared login.

The *reviewed* percentage is measured against the item's size estimate. rsync's own progress percentage only counts transferred data, so it never reaches 100% when files are already up to date. A file being transferred counts with the bytes sent so far, so one big file doesn't jump to 100% at once. Files skipped as conflicts count as files, but rsync doesn't report their size, so with conflicts the percentage ends slightly below 100%.

---

## Keyboard Shortcuts

### Global Keys (Available in all views)
* `?` or `h`: Show Help.
* `t`: Toggle between NAV (browser) view and QUEUE editor view.
* `v`: Open/close the VERIFY view (useful to inspect after a run).
* `g`: Open/close the RUN LOG. It follows new lines; `Up`/`Down`/`PgUp`/`PgDn`/`Home` scroll back, `End` follows again. Keeps the last 5000 lines of the session (not saved to disk).
* `s`: Save the current queue and rsync options to a JSON file (asks before overwriting).
* `l`: Load a queue from a JSON file (not possible while a run is in progress). A file with invalid entries is refused as a whole; if its rsync flags differ from yours, they are shown in full (with a warning for flags that can delete data or run commands) and you choose whether to use them.
* `F8`: Open the Execution/Options screen to run the queue. Asks you to log in to any remote host the queue needs that isn't connected.
* `p`: Pause / resume queue execution (takes effect between items, not mid-file). If the run paused because an SSH connection was lost, `p` logs in again first.
* `q`: Quit (asks for confirmation).

### NAV View (File Browser)
* `Tab`: Switch active pane between SOURCE (left) and TARGET (right).
* `Left`: Go to the parent directory.
* `Right` / `Enter`: Enter the highlighted directory.
* `F5`: Refresh both panes from disk (offers to log in again if a remote pane lost its connection).
* `o`: Connect the active pane to a remote host via SSH, or disconnect it and return to the local folder.
* `c`: Queue a copy (Left pane selection -> Right pane current directory).
* `F2`: Rename the selected file/folder. Never replaces an existing file or folder.
* `Insert`: Create a new folder in the active pane.

### QUEUE View (Editor)
* `Up` / `Down` / `PgUp` / `PgDn`: Navigate the list.
* `d` or `Delete`: Remove the selected item from the queue. The item being copied right now can't be removed; other items can, also during a run.

### RSYNC OPTIONS Screen (`F8`)
The flags apply to every item in the queue.
* `Up` / `Down`: Move through the flag checkboxes. `Space`: Switch the selected flag on or off.
* `e`: Edit all flags as text (shell-style quoting). `d`: Restore the defaults.
* `Enter`: Confirm and start the run (asks again if `-u` is off). `Esc`: Go back without running.

### Typing Text (all popups and prompts)
* Fields start with the current value (e.g. the rsync flags, or `queue.json`), so you can edit instead of retyping.
* `Enter`: accept. `Esc`: cancel without changing anything. `Backspace`: delete a character. `Ctrl-U`: clear the field.
* In popups with buttons: `Tab` / `Down` moves to the next field or button, `Shift-Tab` / `Up` moves back, `Left` / `Right` switch between `[ OK ]` and `[ Cancel ]`.

### VERIFY View (Post-Run Review)
* `Tab`: Cycle through COPIED, SKIPPED, RENAMED, and ERRORS tabs.
* `Enter`: (On a SKIPPED row) Resolve a conflict by choosing to Skip, Rename the existing target file, or Auto-rename the incoming file.

---

## Status Codes
While monitoring your queue, items will display one of the following statuses:
* **NOT SYNCED:** Queued, but not yet attempted.
* **IN PROGRESS:** Currently being copied by `rsync`.
* **SYNCED OK:** Completed cleanly.
* **SYNC FAIL:** `rsync` returned a hard error, or the item was refused before running (see [Safety](#safety)). The ERRORS tab in Verify shows why.
* **INCOMPLETE:** The copy ran, but some files were skipped because the target copy was newer than the source. Check the Verify (`v`) view to manually resolve.
* **DRY RUN OK:** A dry run (`-n`) found nothing wrong. Nothing was copied, so a real run still processes the item.
* **DRY RUN INCOMPLETE:** A dry run found files that are newer in the target. They are listed in Verify, but can only be resolved after a real run.

---

## Safety

`mcrsync` never deletes anything itself, and is built so that it doesn't overwrite, move or create anything you didn't ask for, on either side.

**Copying**
* With the default `-u`, rsync never overwrites a target file that is newer than the source; those files become conflicts for you to resolve. Without `-u` you get a warning before the run.
* Items rsync would handle in a surprising way are refused (`SYNC FAIL`) instead of run: a file whose target name is an existing folder (rsync would copy *into* it), a folder whose target name is an existing file, and a folder copied into itself.
* A dry run (`-n`) creates nothing, not even target folders.

**Conflict resolution**
* Before acting, mcrsync re-checks that the target file is still newer than the source; if anything changed since the run, nothing is done.
* *Rename existing* and *Auto-rename incoming* never overwrite a file. If the copy fails after the existing file was renamed, it is put back.
* These copies use only `-t`, not your custom rsync flags (which might include e.g. `--remove-source-files`).

**Renaming and folders**
* `F2` and `Insert` never replace an existing file or folder, locally or remotely. `.` and `..` can't be used as names.

**Queue files and settings**
* A queue file is checked completely before anything is used: every path must be absolute, and user and host names must be plain names (so nothing in a file can be read as an `ssh` or `rsync` option). One invalid entry and the whole file is refused.
* rsync flags from a queue file are only used after you confirm them (see `l` above).
* Saving (queue or `~/.mcrsync.yaml`) writes a temporary file with a random name and then renames it, so a crash never leaves a half-written file, and a link planted by another user in a shared folder can't redirect the write. `s` asks before overwriting an existing file. A settings file mcrsync can't read is never overwritten.

**Remote hosts**
* Every connection has its own control socket, so commands always reach the host they are meant for.
* Remote commands don't depend on the remote user's login shell (bash, csh, fish, ...), and text printed by shell startup files can't be mistaken for file listings.
* The password is never seen or stored by mcrsync; queue files and settings contain host names and key file paths only.
* When mcrsync exits, including when the terminal is closed, it logs out of all remote hosts.

**What stays your responsibility:** flags you switch on yourself (`--delete` and the like) do what rsync says they do; the F8 screen marks the risky ones. Confirming an unknown host key in `ssh`'s prompt is also up to you.

---

## Testing

`test/` contains a Docker rig with a Python 3.4 client and two SSH test hosts (busybox and GNU userland), and an automated test suite covering the logic, every remote operation, the safeguards above, and the curses UI itself (driven through tmux, including `ssh`'s password prompts). See [test/README.md](test/README.md).
