"""Expose a bounded, read-only view of one Run Workspace to a model adapter."""

import hashlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

from patch_code_agent.budgets import ResourceBudgetExceededError
from patch_code_agent.sources import is_ignored_source_path, validate_relative_path

_MAX_FILE_BYTES = 100 * 1024
_MAX_LISTED_FILES = 256
_MAX_SEARCH_BYTES = 32 * 1024
_MAX_SEARCH_QUERY_CHARACTERS = 256


@dataclass(frozen=True, slots=True)
class FileList:
    """Bounded file paths visible to the model.

    Attributes:
        paths: Stable workspace-relative POSIX paths passing the text-file policy.
        truncated: Whether more than 256 visible paths existed.

    Example:
        >>> FileList(paths=("cart.py", "test_cart.py"), truncated=False).paths[0]
        'cart.py'
    """

    paths: tuple[str, ...]
    truncated: bool


@dataclass(frozen=True, slots=True)
class FileContent:
    """UTF-8 text returned by one successful read operation.

    Attributes:
        path: Validated workspace-relative path that was read.
        content: Complete decoded text, limited to 100 KiB before decoding.

    Example:
        >>> FileContent(path="cart.py", content="VALUE = 1\\n").path
        'cart.py'
    """

    path: str
    content: str


@dataclass(frozen=True, slots=True)
class SearchResult:
    """Bounded literal-search response formatted as path, line, and text.

    Attributes:
        text: UTF-8 response whose encoded size never exceeds 32 KiB.
        truncated: Whether matching output was cut at the byte boundary.

    Example:
        >>> SearchResult("cart.py:1:discount = 0.1\\n", False).truncated
        False
    """

    text: str
    truncated: bool


class InspectionTools(Protocol):
    """The complete workspace authority exposed to a Model Gateway."""

    def list_files(self) -> FileList:
        """List visible UTF-8 text files in stable order."""

    def read_file(self, path: str) -> FileContent:
        """Read one bounded regular UTF-8 text file."""

    def search_code(self, query: str) -> SearchResult:
        """Search visible text files for a case-sensitive literal query."""


class WorkspaceInspector:
    """Host implementation of bounded list, read, and search operations."""

    def __init__(
        self,
        workspace: Path,
        *,
        prior_tool_executions: int = 0,
        previously_read: tuple[str, ...] = (),
    ) -> None:
        self._workspace = workspace.resolve()
        self._prior_tool_executions = prior_tool_executions
        self._previously_read = set(previously_read)
        self._tool_executions = 0
        self._files_read: set[str] = set()
        self._read_hashes: dict[str, str] = {}

    @property
    def tool_executions(self) -> int:
        """Return the number of model-requested operations, including rejected ones."""
        return self._tool_executions

    @property
    def files_read(self) -> tuple[str, ...]:
        """Return successfully read paths in stable order."""
        return tuple(sorted(self._files_read))

    @property
    def read_hashes(self) -> dict[str, str]:
        """Return a copy of SHA-256 preimages observed by explicit model reads."""
        return dict(self._read_hashes)

    def list_files(self) -> FileList:
        """List at most 256 visible regular UTF-8 text files."""
        self._claim_tool_execution()
        paths = self._visible_text_paths()
        return FileList(
            paths=tuple(paths[:_MAX_LISTED_FILES]),
            truncated=len(paths) > _MAX_LISTED_FILES,
        )

    def read_file(self, path: str) -> FileContent:
        """Validate and read one path without following any symlink segment."""
        self._claim_tool_execution()
        relative, candidate = self._resolve_regular_file(path)
        self._claim_file_read(relative)
        content = self._read_text(candidate)
        self._read_hashes[relative] = hashlib.sha256(content.encode("utf-8")).hexdigest()
        return FileContent(path=relative, content=content)

    def search_code(self, query: str) -> SearchResult:
        """Return a deterministic byte-bounded literal search across visible text."""
        self._claim_tool_execution()
        if not query or len(query) > _MAX_SEARCH_QUERY_CHARACTERS:
            raise ValueError("Search query must contain 1 to 256 characters")

        output = bytearray()
        truncated = False
        for relative in self._visible_text_paths():
            _, candidate = self._resolve_regular_file(relative)
            self._claim_file_read(relative)
            for line_number, line in enumerate(self._read_text(candidate).splitlines(), 1):
                if query not in line:
                    continue
                encoded = f"{relative}:{line_number}:{line}\n".encode()
                remaining = _MAX_SEARCH_BYTES - len(output)
                if len(encoded) > remaining:
                    output.extend(encoded[:remaining])
                    truncated = True
                    return SearchResult(output.decode("utf-8", errors="ignore"), truncated)
                output.extend(encoded)
        return SearchResult(output.decode("utf-8"), truncated)

    def _claim_tool_execution(self) -> None:
        if self._prior_tool_executions + self._tool_executions >= 20:
            raise ResourceBudgetExceededError(
                budget_name="tool_executions",
                budget_limit=20,
                budget_used=20,
                tool_executions=self._tool_executions,
                files_read=self.files_read,
            )
        self._tool_executions += 1

    def _claim_file_read(self, relative: str) -> None:
        if relative in self._previously_read or relative in self._files_read:
            return
        if len(self._previously_read | self._files_read) >= 12:
            raise ResourceBudgetExceededError(
                budget_name="files_read",
                budget_limit=12,
                budget_used=12,
                tool_executions=self._tool_executions,
                files_read=self.files_read,
            )
        self._files_read.add(relative)

    def _visible_text_paths(self) -> list[str]:
        paths: list[str] = []
        for candidate in self._workspace.rglob("*"):
            relative_path = candidate.relative_to(self._workspace)
            if is_ignored_source_path(relative_path) or self._has_symlink_segment(relative_path):
                continue
            if not candidate.is_file() or candidate.stat().st_size > _MAX_FILE_BYTES:
                continue
            try:
                self._read_text(candidate)
            except ValueError:
                continue
            paths.append(relative_path.as_posix())
        return sorted(paths)

    def _resolve_regular_file(self, path: str) -> tuple[str, Path]:
        validate_relative_path(path)
        relative_path = Path(*PurePosixPath(path).parts)
        if is_ignored_source_path(relative_path):
            raise ValueError(f"Inspection path is hidden or ignored: {path}")
        if self._has_symlink_segment(relative_path):
            raise ValueError(f"Inspection path must not contain a symbolic link: {path}")
        candidate = self._workspace.joinpath(*relative_path.parts).resolve()
        if not candidate.is_relative_to(self._workspace):
            raise ValueError(f"Inspection path escapes the Run Workspace: {path}")
        if not candidate.is_file():
            raise ValueError(f"Inspection path is not a regular file: {path}")
        return relative_path.as_posix(), candidate

    def _has_symlink_segment(self, relative_path: Path) -> bool:
        candidate = self._workspace
        for part in relative_path.parts:
            candidate /= part
            if candidate.is_symlink():
                return True
        return False

    @staticmethod
    def _read_text(path: Path) -> str:
        if path.stat().st_size > _MAX_FILE_BYTES:
            raise ValueError(f"Inspection file exceeds 100 KiB: {path.name}")
        content = path.read_bytes()
        if b"\x00" in content:
            raise ValueError(f"Inspection file is binary: {path.name}")
        try:
            return content.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError(f"Inspection file is not UTF-8: {path.name}") from error
