# mcrsync

A Midnight-Commander-style `rsync` queue manager built for Python 3.4+ by Claude; specs by me.

`mcrsync` provides a visual, dual-pane terminal interface for building, editing, and executing large batches of `rsync` file transfers. It was originally designed for managing localized backup routines on older Network Attached Storage (NAS) hardware, offering modern convenience while maintaining legacy compatibility.

## Features

* **Legacy Friendly:** Targets Python 3.4+. Purposely avoids newer features (like f-strings or `os.scandir`) targetting older environments.
* **Dual-Pane Navigation:** Browse your source and target directories side-by-side. 
* **Safe by Design ("Kid Gloves"):** Built to prevent catastrophic mistakes. You can create directories, but the tool intentionally lacks the ability to delete or remove files and folders. Furthermore, it strictly monitors file modification times to prevent overwriting newer files in the target directory.
* **Background Sizing:** File sizes are calculated lazily in the background as you build your queue, keeping the UI responsive.
* **Resumable Queues:** Save your entire queue and custom `rsync` options to a JSON file. You can pause, quit, reload, and resume right where you left off.
* **Conflict Verification:** A dedicated "Verify" view lets you review skipped files, renamed items, and errors after a run, allowing you to manually resolve conflicts (Skip, Rename existing, or Auto-rename incoming). Note: this feature has not been tested!

---

## Dependencies

* **Python 3.4 or higher** (Uses strictly standard libraries: `curses`, `json`, `threading`, `subprocess`, etc.)
* **rsync** installed on the host system.
* **GNU Screen or tmux (Highly Recommended):** Because large transfers can take hours, it is highly recommended to run this script inside a multiplexer like `screen` so you can detach and safely close your SSH session without interrupting the queue.

---

## Example Workflow

1. **Launch:** Run the program (preferably inside `screen`):
   ```bash
   python3 mcrsync.py
   ```
2. **Build the Queue:** Use `Tab` to switch between the left (Source) and right (Target) panes. Highlight a file or directory on the left, navigate to your desired destination on the right, and press `c` to add it to the queue. As you add items, their sizes will compute in the background.
3. **Review and Run:** Once your queue is built, press `F8` to open the Execution Options.
4. **Customize Flags:** (Optional but recommended) Press `e` to edit your `rsync` flags (see the *Advanced `rsync` Options* section below).
5. **Start:** Press `Enter` to confirm and begin the queue.
6. **Save:** Once the queue starts, press `s` to save your progress to a `.json` file. The file saves both the remaining queue and your custom `rsync` flags in case you need to load (`l`) and resume later. 

*(Note: The ETA displayed at the bottom of the screen is a rough estimate based on total byte size. It only recalculates when a queued item finishes transferring).*

---

## Advanced `rsync` Options

By default, the script uses `-a -u --stats --itemize-changes`. However, depending on your backup target, these might not be desirable for some reason (i.e. standard archive flags might cause permission errors.)

If you only care about transferring the actual files and want to ignore users/permissions, press `F8` then `e` and use this robust alternative set of flags:

```text
-a -u --whole-file --no-perms --no-owner --no-group --partial --partial-dir=.rsync-partial --stats --itemize-changes
```

**What these flags do:**
* `-a`: Archive mode (recurses into directories).
* `-u`: Update mode (skips files that are newer on the receiver). **Crucial for safety.**
* `--whole-file`: Disables delta-transfer algorithm. Faster for local transfers or when CPU/Disk IO on the NAS is a bigger bottleneck than network speed.
* `--no-perms --no-owner --no-group`: Ignores permission mappings. Ideal for backing up to a drive with a different filesystem or when user accounts don't match between the source and target.
* `--partial` and `--partial-dir=.rsync-partial`: Keeps partially transferred files if the connection drops, putting them in a hidden folder so large files can resume properly next time.
* `--stats --itemize-changes`: Required by `mcrsync` to parse the output and populate the "Verify" tab with exact file counts and actions.

---

## Keyboard Shortcuts

### Global Keys (Available in all views)
* `?` or `h`: Show Help.
* `t`: Toggle between NAV (browser) view and QUEUE editor view.
* `v`: Open/close the VERIFY view (useful to inspect after a run).
* `s`: Save the current queue and rsync options to a JSON file.
* `l`: Load a queue from a JSON file.
* `F8`: Open the Execution/Options screen to run the queue.
* `p`: Pause / resume queue execution (takes effect between items, not mid-file).
* `q`: Quit (asks for confirmation).

### NAV View (File Browser)
* `Tab`: Switch active pane between SOURCE (left) and TARGET (right).
* `Left`: Go to the parent directory.
* `Right` / `Enter`: Enter the highlighted directory.
* `F5`: Refresh both panes from disk.
* `c`: Queue a copy (Left pane selection -> Right pane current directory).
* `F2`: Rename the selected file/folder.
* `Insert`: Create a new folder in the active pane.

### QUEUE View (Editor)
* `Up` / `Down` / `PgUp` / `PgDn`: Navigate the list.
* `d` or `Delete`: Remove the selected item from the queue.

### VERIFY View (Post-Run Review)
* `Tab`: Cycle through COPIED, SKIPPED, RENAMED, and ERRORS tabs.
* `Enter`: (On a SKIPPED row) Resolve a conflict by choosing to Skip, Rename the existing target file, or Auto-rename the incoming file.

---

## Status Codes
While monitoring your queue, items will display one of the following statuses:
* **NOT SYNCED:** Queued, but not yet attempted.
* **IN PROGRESS:** Currently being copied by `rsync`.
* **SYNCED OK:** Completed cleanly.
* **SYNC FAIL:** `rsync` returned a hard error.
* **INCOMPLETE:** The copy ran, but some files were skipped because the target copy was newer than the source. Check the Verify (`v`) view to manually resolve.
