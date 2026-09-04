"""
Copy the daily outputs to a cloud folder so a second computer (e.g. a work
laptop on a different OneDrive) gets them automatically.

Uses rclone. One-time setup on this machine:

    rclone config
      n                     (new remote)
      name> gdrive
      Storage> drive        (Google Drive)
      client_id>            (leave blank)
      client_secret>        (leave blank)
      scope> 3              (drive.file - rclone only sees files it creates)
      service_account_file> (leave blank)
      Edit advanced config> n
      Use auto config> y    (a browser opens - sign in, allow)
      Shared Drive> n
      y                     (keep it)  then  q

After that this script (last step of run_all.py) pushes to gdrive:<REMOTE_DIR>
every run. On the other computer, install the Google Drive desktop app (or open
drive.google.com) and the files appear in that folder.

A missing rclone / unconfigured remote is not an error - the local files and the
hosted dashboard are still the source of truth.
"""

import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REMOTE = "gdrive"                     # rclone remote name (see setup above)
REMOTE_DIR = "SportsVolume"           # folder created in that drive
FILES = [
    "unified_sports_volume.xlsx",
    "unified_sports_volume.csv",
    "dashboard.html",
    "VOLUME_METHODOLOGY.md",
]


def _find_rclone() -> str | None:
    p = shutil.which("rclone") or shutil.which("rclone.exe")
    if p:
        return p
    root = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                        "Microsoft", "WinGet", "Packages")
    for dirpath, _dirs, files in os.walk(root) if os.path.isdir(root) else []:
        if "rclone.exe" in files:
            return os.path.join(dirpath, "rclone.exe")
    return None


def main() -> int:
    rclone = _find_rclone()
    if not rclone:
        print("push_to_cloud: rclone not installed - skipping. See this file's header for setup.")
        return 0

    try:
        remotes = subprocess.run([rclone, "listremotes"], capture_output=True,
                                 text=True, timeout=30).stdout
    except Exception as e:  # noqa: BLE001
        print(f"push_to_cloud: rclone listremotes failed ({e}) - skipping.")
        return 0
    if f"{REMOTE}:" not in remotes:
        print(f"push_to_cloud: rclone remote '{REMOTE}:' not configured yet - skipping. "
              "Run `rclone config` (see this file's header).")
        return 0

    dest = f"{REMOTE}:{REMOTE_DIR}"
    pushed, failed = [], []
    for name in FILES:
        path = os.path.join(HERE, name)
        if not os.path.exists(path):
            continue
        # No --no-traverse: Google Drive allows duplicate filenames in one
        # folder, and skipping the directory listing (what --no-traverse does)
        # makes rclone miss the existing file and upload a second copy instead
        # of updating it in place. A full listing of this small folder is cheap.
        r = subprocess.run([rclone, "copy", path, dest],
                           capture_output=True, text=True, timeout=180)
        (pushed if r.returncode == 0 else failed).append(name)
        if r.returncode != 0:
            print(f"  {name}: rclone exit {r.returncode} {r.stderr.strip()[:200]}")

    print(f"push_to_cloud: {len(pushed)} file(s) -> {dest}"
          + (f"  (failed: {failed})" if failed else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
