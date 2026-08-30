from __future__ import annotations

from pathlib import Path
from types import TracebackType

import fcntl
import time


class InterProcessLock:
    """为共享旧数据提供有界的 WSL 进程间互斥。"""

    def __init__(self, path: Path, *, timeout_seconds: float = 15.0) -> None:
        self.path = Path(path)
        self.timeout_seconds = timeout_seconds
        self._file = None

    def __enter__(self) -> "InterProcessLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("a+", encoding="utf-8")
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    self._file.close()
                    self._file = None
                    raise TimeoutError(f"等待共享状态锁超时: {self.path}") from None
                time.sleep(0.05)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type
        del exc_value
        del traceback
        if self._file is None:
            return
        fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        self._file.close()
        self._file = None
