"""Print the real tool schemas of the configured `linkedin` mcporter server.

Use it to verify the argument names used in signal_tracker/linkedin/client.py.
Needs LINKEDIN_ENABLED=true; makes no LinkedIn request (lists tools only).
"""

from __future__ import annotations

import shutil
import subprocess
import sys

from signal_tracker.config import get_settings


def main() -> int:
    settings = get_settings()
    if not settings.linkedin_enabled:
        print("LINKEDIN_ENABLED is false.")
        return 1
    binary = shutil.which(settings.linkedin_mcporter_bin)
    if binary is None:
        print(f"{settings.linkedin_mcporter_bin!r} not found in PATH.")
        return 1
    proc = subprocess.run(
        [binary, "list", "linkedin", "--schema"],
        capture_output=True, text=True, timeout=60, check=False,
    )
    print(proc.stdout or proc.stderr)
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
