"""Serialize mutating CLI commands with one advisory lock per Patch Run."""

import fcntl
import os
from pathlib import Path
from types import TracebackType
from typing import Self, TextIO
from uuid import UUID


class RunMutationLock:
    """Non-blocking cross-process exclusive lock for one durable Patch Run."""

    def __init__(self, data_root: Path, run_id: str) -> None:
        try:
            canonical_run_id = str(UUID(run_id))
        except ValueError as error:
            raise ValueError(f"Invalid Run Identifier: {run_id}") from error
        if canonical_run_id != run_id:
            raise ValueError(f"Invalid Run Identifier: {run_id}")
        self._run_root = data_root.resolve() / canonical_run_id
        self._run_id = canonical_run_id
        self._handle: TextIO | None = None

    def __enter__(self) -> Self:
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            directory_fd = os.open(self._run_root, directory_flags)
        except FileNotFoundError:
            raise ValueError(f"Unknown Run Identifier: {self._run_id}")
        except OSError as error:
            raise ValueError(f"Unsafe Patch Run lock directory: {self._run_id}") from error
        lock_flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        try:
            lock_fd = os.open(".mutation.lock", lock_flags, 0o600, dir_fd=directory_fd)
        except OSError as error:
            raise ValueError(f"Unsafe Patch Run lock file: {self._run_id}") from error
        finally:
            os.close(directory_fd)
        handle = os.fdopen(lock_fd, "a+", encoding="utf-8")
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
