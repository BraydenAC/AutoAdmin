"""Owner-only files on Linux and Windows. Stdlib only: the launcher uses it."""

from __future__ import annotations

import getpass
import os
import subprocess
import sys
from pathlib import Path


def windows_user() -> str:
    domain = os.environ.get("USERDOMAIN")
    return f"{domain}\\{getpass.getuser()}" if domain else getpass.getuser()


def restrict_to_owner(path: Path) -> None:
    """Only the current user may read the file."""
    if sys.platform == "win32":
        # No chmod on Windows: drop inherited ACL entries and grant only this user.
        subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{windows_user()}:F"],
                       check=True, capture_output=True,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        path.chmod(0o600)
