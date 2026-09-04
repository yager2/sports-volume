"""
Republish the hosted dashboard artifact from the freshly-rebuilt local
dashboard.html, using a headless Claude Code call.

Runs as the last step of run_all.py so the shared artifact link - which opens
from any device / browser with no OneDrive and no sign-in to personal accounts -
stays current without anyone clicking "Update data".

Needs the `claude` CLI on PATH, signed in to the account that owns the artifact
(the same one you use in Claude Code interactively). A failure here never fails
the daily run: the local dashboard.html / .xlsx / .csv are the source of truth,
and the artifact can always be refreshed by hand.
"""

import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ARTIFACT_URL = "https://claude.ai/code/artifact/b52f0107-bb69-411c-9e68-97662a65ef32"

PROMPT = (
    f"Republish the existing artifact at {ARTIFACT_URL} from the local file "
    "dashboard.html in the current directory. Call the Artifact tool once: "
    f"action 'publish', file_path 'dashboard.html', url '{ARTIFACT_URL}', "
    "favicon the chart-increasing emoji. Do NOT edit dashboard.html or any "
    "other file, and do NOT change the title, description, or capabilities. "
    "Reply with just DONE on success or FAILED <reason> on failure."
)


def _find_claude() -> str | None:
    for name in ("claude", "claude.exe"):
        p = shutil.which(name)
        if p:
            return p
    # Task Scheduler's PATH often omits the per-user install dir.
    guess = os.path.expanduser(r"~\.local\bin\claude.exe")
    return guess if os.path.exists(guess) else None


def main() -> int:
    claude = _find_claude()
    if not claude:
        print("publish_dashboard: `claude` CLI not found - hosted artifact not refreshed.")
        return 0

    cmd = [
        claude, "-p", PROMPT,
        "--allowedTools", "Artifact", "Read",
        "--disallowedTools", "Bash", "Edit", "Write",
        "--model", "claude-haiku-4-5-20251001",
        "--max-budget-usd", "0.50",
        "--output-format", "text",
    ]
    try:
        r = subprocess.run(cmd, cwd=HERE, timeout=360, capture_output=True, text=True)
    except subprocess.TimeoutExpired:
        print("publish_dashboard: timed out - hosted artifact not refreshed this run.")
        return 0

    out = (r.stdout or "").strip()
    print(f"publish_dashboard: {out[-600:] or '(no output)'}")
    if r.returncode != 0:
        print(f"publish_dashboard: claude exited {r.returncode} "
              f"{(r.stderr or '')[-400:]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
