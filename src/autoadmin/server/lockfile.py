"""A file lock the launcher holds while it runs.

The OS drops the lock when the process dies, however it dies, so unlike a pid
it can't be mistaken for an unrelated process after a crash or a reboot.
Stdlib only: the launcher uses it.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import BinaryIO

if sys.platform == "win32":
    import msvcrt

    def _try_lock(f: BinaryIO) -> None:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(f: BinaryIO) -> None:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _try_lock(f: BinaryIO) -> None:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(f: BinaryIO) -> None:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def hold(path: Path) -> BinaryIO:
    """Take the lock; keep the returned file open for as long as it should be held."""
    path.parent.mkdir(parents=True, exist_ok=True)
    f = path.open("a+b")
    try:
        _try_lock(f)
    except OSError:
        f.close()
        raise
    return f


def held(path: Path) -> bool:
    try:
        f = path.open("a+b")
    except FileNotFoundError:
        return False
    except OSError:
        return True
    with f:
        try:
            _try_lock(f)
        except OSError:
            return True
        _unlock(f)
        return False
