from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
import asyncio
import fcntl
import time
from typing import AsyncIterator, BinaryIO


@asynccontextmanager
async def interprocess_lock(path: Path, *, timeout_seconds: float) -> AsyncIterator[None]:
    """Hold one advisory process lock without blocking the event loop while waiting."""

    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        await asyncio.to_thread(_acquire, handle, timeout_seconds)
        yield
    finally:
        await asyncio.to_thread(_release, handle)


def _acquire(handle: BinaryIO, timeout_seconds: float) -> None:
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while True:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError as exc:
            if time.monotonic() >= deadline:
                raise TimeoutError("artifact publish lock timed out") from exc
            time.sleep(0.05)


def _release(handle: BinaryIO) -> None:
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()
