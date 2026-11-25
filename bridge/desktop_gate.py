"""Serialize desktop operations across the Bridge and its synchronous workers."""

import asyncio
import errno
import math
import os
import stat
import time
from contextlib import contextmanager
from threading import Event
from typing import Iterator

try:
    import fcntl
except ImportError:  # A local asyncio gate can run without POSIX file locking.
    fcntl = None


LockPath = str | os.PathLike[str]


class DesktopLockCancelled(RuntimeError):
    """A synchronous caller cancelled while waiting for the desktop gate."""


def _lock_path(path: LockPath | None) -> str | None:
    value = os.fspath(path) if path is not None else os.getenv("AAW_DESKTOP_LOCK")
    return os.path.abspath(value) if value else None


def _retry_interval(value: float) -> float:
    if not math.isfinite(value) or value <= 0:
        raise ValueError("The desktop lock retry interval must be finite and positive")
    return value


def _open_lock(path: str) -> int:
    if fcntl is None:
        raise OSError("A shared desktop gate requires POSIX flock")
    flags = os.O_RDWR | os.O_CREAT | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("The desktop lock path must be a regular file")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _try_lock(descriptor: int) -> bool:
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        if error.errno in {errno.EACCES, errno.EAGAIN, errno.EINTR, errno.EWOULDBLOCK}:
            return False
        raise
    return True


class AsyncDesktopGate:
    """An asyncio lock with optional cross-process exclusion via a shared file."""

    def __init__(self, path: LockPath | None = None, *, retry_interval: float = 0.05):
        self.path = _lock_path(path)
        self.retry_interval = _retry_interval(retry_interval)
        self._local = asyncio.Lock()
        self._descriptor: int | None = None

    def locked(self) -> bool:
        return self._local.locked()

    async def __aenter__(self) -> "AsyncDesktopGate":
        await self._local.acquire()
        descriptor = None
        try:
            if self.path is not None:
                descriptor = _open_lock(self.path)
                while not _try_lock(descriptor):
                    await asyncio.sleep(self.retry_interval)
            self._descriptor = descriptor
            return self
        except BaseException:
            try:
                if descriptor is not None:
                    os.close(descriptor)
            finally:
                self._local.release()
            raise

    async def __aexit__(self, _type, _error, _traceback) -> None:
        descriptor, self._descriptor = self._descriptor, None
        try:
            if descriptor is not None:
                os.close(descriptor)
        finally:
            self._local.release()


@contextmanager
def desktop_flock(
    path: LockPath | None = None, *, cancel: Event | None = None, retry_interval: float = 0.05,
) -> Iterator[None]:
    """Acquire the same file lock in a worker thread; cancel only the acquisition."""
    path = _lock_path(path)
    retry_interval = _retry_interval(retry_interval)
    if cancel is not None and cancel.is_set():
        raise DesktopLockCancelled("Desktop lock acquisition was cancelled")
    if path is None:
        yield
        return

    descriptor = _open_lock(path)
    try:
        while not _try_lock(descriptor):
            if cancel is None:
                time.sleep(retry_interval)
            elif cancel.wait(retry_interval):
                raise DesktopLockCancelled("Desktop lock acquisition was cancelled")
        if cancel is not None and cancel.is_set():
            raise DesktopLockCancelled("Desktop lock acquisition was cancelled")
        yield
    finally:
        os.close(descriptor)
