"""Validate repository sources and normalize them into Patch Run inputs.

Bundled fixtures and explicitly trusted local repositories have different file representations,
but graph execution should not care where a source came from. This module defines their shared
``RepositorySource`` and ``PatchRunContract`` boundary, including bounded Issue text, controlled
Verification argv, editable paths, containment checks, and the no-symlink policy.
"""

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
    """Reject absolute, empty, or traversing paths from source contracts.

    Contracts use POSIX separators even when executed on another platform. Rejecting ``.`` and
    ``..`` segments here keeps later filesystem resolution simple and prevents policy bypasses.
    """
    path = PurePosixPath(value)
    if path.is_absolute() or not value or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("Repository Source paths must be relative and without traversal")
    return value


def is_ignored_source_path(relative_path: Path) -> bool:
    """Return whether a path is outside the bounded source view.

    The same predicate is reused while copying workspaces and inspecting files so runtime caches,
    virtual environments, hidden metadata, and compiled Python files cannot appear through one
    path after being excluded by another.
    """
    return any(
        part.startswith(".")
        or part in _IGNORED_SOURCE_NAMES
        or part.endswith((".pyc", ".pyo"))
        for part in relative_path.parts
    )


def _validate_issue(value: str) -> str:
    """Require Issue text to contain something beyond whitespace."""
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
    """Validated Issue, Verification, and edit policy for one Repository Source.

    Pydantic enforces size limits and rejects unknown fields before any workspace or subprocess is
    created. Frozen instances keep the contract stable throughout a Patch Run.

    Attributes:
        issue: Non-empty problem statement, limited to 32,768 characters.
        verification: Non-empty argv tuple executed directly without shell parsing.
        editable_paths: Non-empty tuple of bounded, relative, non-traversing source paths.

    Example:
        >>> contract = PatchRunContract(
        ...     issue="Fix the incorrect cart discount",
        ...     verification=("pytest", "-q", "test_cart.py"),
        ...     editable_paths=("cart.py",),
        ... )
        >>> contract.verification
        ('pytest', '-q', 'test_cart.py')
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    issue: IssueText
    verification: VerificationArgv
    editable_paths: EditablePaths


class TrustedRepositoryContract(PatchRunContract):
    """File representation of a Trusted Repository's Patch Run Contract.

    Attributes:
        source_id: Lowercase kebab-case identifier supplied by the external TOML contract.
        issue: Inherited problem statement describing the requested repair.
        verification: Inherited controlled argv executed with host authority after explicit opt-in.
        editable_paths: Inherited allowlist of repository-relative paths.

    Example:
        >>> contract = TrustedRepositoryContract(
        ...     source_id="trusted-cart",
        ...     issue="Fix total calculation",
        ...     verification=("pytest",),
        ...     editable_paths=("cart.py",),
        ... )
        >>> contract.source_id
        'trusted-cart'
    """

    source_id: RepositorySourceId


@dataclass(frozen=True, slots=True)
class RepositorySource:
    """Immutable repository content prepared for Patch Run execution.

    Attributes:
        kind: Source adapter category, either ``fixture`` or ``trusted``.
        source_id: Stable contract/registry identifier displayed by the CLI.
        root: Resolved directory whose visible contents will seed the Run Workspace.
        contract: Source-neutral Issue, Verification argv, and edit policy.

    Example:
        >>> source = RepositorySource(
        ...     kind="fixture",
        ...     source_id="cart-discount",
        ...     root=Path("examples/tiny_repo").resolve(),
        ...     contract=PatchRunContract(
        ...         issue="Fix the discount",
        ...         verification=("pytest",),
        ...         editable_paths=("cart.py",),
        ...     ),
        ... )
        >>> source.kind
        'fixture'
    """

    kind: RepositorySourceKind
    source_id: RepositorySourceId
    root: Path
    contract: PatchRunContract


def load_trusted_repository(repository: Path, contract_path: Path) -> RepositorySource:
    """Load an explicitly trusted local repository and its external TOML contract.

    Keeping the contract outside the repository prevents repository content from silently
    changing its own Issue, Verification command, or edit policy. This validates structure and
    containment only; execution still has host authority and therefore requires CLI opt-in.
    """
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
    """Reject symlinks so copied source content cannot escape its declared root.

    The walk does not follow directory links, but every discovered entry is checked explicitly so
    both file and directory symlinks fail before workspace creation.
    """
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
    """Resolve a contract path while enforcing containment and source-view policy.

    Validation first applies the ignored-path rule, then resolves the candidate and checks it is a
    real file beneath ``root``. The caller-provided ``kind`` only improves domain error messages.
    """
    normalized_path = Path(*PurePosixPath(relative_path).parts)
    if is_ignored_source_path(normalized_path):
        raise ValueError(f"Repository Source {kind} is ignored: {relative_path}")
    candidate = root.joinpath(*normalized_path.parts).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError(f"Repository Source {kind} escapes its root: {relative_path}")
    if not candidate.is_file():
        raise ValueError(f"Repository Source {kind} does not exist: {relative_path}")
    return candidate
