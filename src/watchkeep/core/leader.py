"""One syncing process per database.

The terminal UI and the headless `watchkeep run` can be open at the same time
against the same state.db. Without coordination both would poll every source,
doubling API calls and racing on the same snapshot. Whoever holds an exclusive
lock on a file next to the database syncs; the other only re-reads what the
syncing process writes, and takes over as soon as the lock is free.

`flock` locks are released by the OS when the process exits, so a crash never
leaves a stale lock behind. Where `fcntl` is missing (Windows) every process
syncs, as before.
"""

from __future__ import annotations

import os
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - not POSIX
    fcntl = None  # type: ignore[assignment]


class SyncLeader:
    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> bool:
        """Try to become the syncing process. Never blocks. True if this process holds the lock."""
        if self._fd is not None or fcntl is None:
            return True
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None

    def other_is_syncing(self) -> bool:
        """True if another process holds the lock right now. Leaves no lock behind."""
        if self._fd is not None or fcntl is None:
            return False
        if not self.acquire():
            return True
        self.release()
        return False
