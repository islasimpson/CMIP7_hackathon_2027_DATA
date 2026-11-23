"""A run lock that works across Casper/Derecho nodes on GLADE.

fcntl/flock locks are not reliable between nodes on this filesystem, so instead:
  * the lock file is created atomically (O_CREAT | O_EXCL) and records host, pid, start time;
  * the owner touches it every HEARTBEAT seconds while running;
  * a lock whose heartbeat is older than STALE seconds is treated as left over from a
    crashed or killed run and is taken over;
  * the file is removed when the run ends normally.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

HEARTBEAT = 60
STALE = 600


class LockHeld(RuntimeError):
    pass


class RunLock:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _describe(self) -> str:
        try:
            return self.path.read_text().strip() or "unknown owner"
        except OSError:
            return "unknown owner"

    def acquire(self) -> None:
        me = f"host={os.uname().nodename} pid={os.getpid()} started={dt.datetime.now():%Y-%m-%d %H:%M:%S}"
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                try:
                    age = time.time() - self.path.stat().st_mtime
                except FileNotFoundError:
                    continue                      # vanished in between; try again
                if age < STALE:
                    raise LockHeld(f"another cmip7arch check/update is running ({self._describe()}; "
                                   f"heartbeat {age:.0f}s ago). Lock file: {self.path}")
                log.warning("taking over stale lock (%s; last heartbeat %.0f min ago)",
                            self._describe(), age / 60)
                self.path.unlink(missing_ok=True)
                continue
            with os.fdopen(fd, "w") as f:
                f.write(me + "\n")
            self._thread = threading.Thread(target=self._beat, daemon=True)
            self._thread.start()
            return
        raise LockHeld(f"could not acquire {self.path}")

    def _beat(self) -> None:
        while not self._stop.wait(HEARTBEAT):
            try:
                os.utime(self.path)
            except OSError:
                pass

    def release(self) -> None:
        self._stop.set()
        self.path.unlink(missing_ok=True)

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False
