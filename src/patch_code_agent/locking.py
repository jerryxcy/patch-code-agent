"""Serialize mutating CLI commands with one advisory lock per Patch Run."""

import fcntl
from pathlib import Path
from types import TracebackType
from typing import Self, TextIO


class RunMutationLock:
    """Non-blocking cross-process exclusive lock for one durable Patch Run."""

    def __init__(self, data_root: Path, run_id: str) -> None:
        self._run_root = data_root.resolve() / run_id
        self._run_id = run_id
        self._handle: TextIO | None = None

    def __enter__(self) -> Self:
        if not self._run_root.is_dir():
            raise ValueError(f"Unknown Run Identifier: {self._run_id}")
        handle = (self._run_root / ".mutation.lock").open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            handle.close()
            raise ValueError(f"Patch Run is busy: {self._run_id}") from error
        self._handle = handle
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._handle is None:
            return
        fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        self._handle.close()
        self._handle = None
