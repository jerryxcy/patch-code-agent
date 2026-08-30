from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints

_MAX_EDITABLE_PATHS = 256
_MAX_ISSUE_CHARACTERS = 32_768
_MAX_PATH_CHARACTERS = 1_024
_MAX_SOURCE_ID_CHARACTERS = 128
_MAX_VERIFICATION_ARGUMENTS = 32
_MAX_VERIFICATION_ARGUMENT_CHARACTERS = 4_096
_IGNORED_SOURCE_NAMES = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "env",
        "venv",
    }
)


def validate_relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or not value or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("Repository Source paths must be relative and without traversal")
    return value


def is_ignored_source_path(relative_path: Path) -> bool:
    """Return whether a Repository Source path is outside the bounded source view."""
    return any(
        part.startswith(".")
        or part in _IGNORED_SOURCE_NAMES
        or part.endswith((".pyc", ".pyo"))
        for part in relative_path.parts
    )


def _validate_issue(value: str) -> str:
    if not value.strip():
        raise ValueError("Patch Run Contract Issue must not be empty")
    return value


type RepositorySourceKind = Literal["fixture", "trusted"]
type RepositorySourceId = Annotated[
    str,
    StringConstraints(
        max_length=_MAX_SOURCE_ID_CHARACTERS,
        pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
    ),
]
type IssueText = Annotated[
    str,
    StringConstraints(max_length=_MAX_ISSUE_CHARACTERS),
    AfterValidator(_validate_issue),
]
type VerificationArgument = Annotated[
    str,
    StringConstraints(min_length=1, max_length=_MAX_VERIFICATION_ARGUMENT_CHARACTERS),
]
type VerificationArgv = Annotated[
    tuple[VerificationArgument, ...],
    Field(min_length=1, max_length=_MAX_VERIFICATION_ARGUMENTS),
]
type RelativeSourcePath = Annotated[
    str,
    StringConstraints(max_length=_MAX_PATH_CHARACTERS),
    AfterValidator(validate_relative_path),
]
type EditablePaths = Annotated[
    tuple[RelativeSourcePath, ...],
    Field(min_length=1, max_length=_MAX_EDITABLE_PATHS),
]


class PatchRunContract(BaseModel):
    """Validated Issue, Verification, and edit policy for one Repository Source."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    issue: IssueText
    verification: VerificationArgv
    editable_paths: EditablePaths


class TrustedRepositoryContract(PatchRunContract):
    """File representation of a Trusted Repository's Patch Run Contract."""

    source_id: RepositorySourceId


@dataclass(frozen=True, slots=True)
class RepositorySource:
    """Immutable repository content prepared for Patch Run execution."""

    kind: RepositorySourceKind
    source_id: RepositorySourceId
    root: Path
    contract: PatchRunContract


def load_trusted_repository(repository: Path, contract_path: Path) -> RepositorySource:
    reject_source_symlinks(repository)
    if contract_path.is_symlink():
        raise ValueError(f"Patch Run Contract must not be a symbolic link: {contract_path}")
    resolved_root = repository.resolve()
    if contract_path.resolve().is_relative_to(resolved_root):
        raise ValueError("Patch Run Contract must be outside the Repository Source")
    try:
        with contract_path.open("rb") as contract_file:
            contract = TrustedRepositoryContract.model_validate(tomllib.load(contract_file))
    except (OSError, ValueError) as error:
        raise ValueError(f"Invalid Patch Run Contract at {contract_path}: {error}") from error

    for editable_path in contract.editable_paths:
        resolve_source_file(resolved_root, editable_path, "editable path")
    return RepositorySource(
        kind="trusted",
        source_id=contract.source_id,
        root=resolved_root,
        contract=contract,
    )


def reject_source_symlinks(root: Path) -> None:
    if root.is_symlink():
        raise ValueError(f"Repository Source must not be a symbolic link: {root}")
    if not root.is_dir():
        raise ValueError(f"Repository Source directory does not exist: {root}")
    for directory, directory_names, file_names in os.walk(root, followlinks=False):
        for name in (*directory_names, *file_names):
            path = Path(directory) / name
            if path.is_symlink():
                relative_path = path.relative_to(root)
                raise ValueError(
                    f"Repository Source must not contain a symbolic link: {relative_path}"
                )


def resolve_source_file(root: Path, relative_path: str, kind: str) -> Path:
    normalized_path = Path(*PurePosixPath(relative_path).parts)
    if is_ignored_source_path(normalized_path):
        raise ValueError(f"Repository Source {kind} is ignored: {relative_path}")
    candidate = root.joinpath(*normalized_path.parts).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError(f"Repository Source {kind} escapes its root: {relative_path}")
    if not candidate.is_file():
        raise ValueError(f"Repository Source {kind} does not exist: {relative_path}")
    return candidate
