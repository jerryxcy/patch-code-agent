"""Load bounded Fixture Repository sources from Patch Run manifests."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

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


type RepositorySourceKind = Literal["fixture"]
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
        protected_paths: Source files that remain immutable even if an unsafe allowlist includes
            them, such as a Fixture Issue, manifest, or Verification test.

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
    protected_paths: tuple[RelativeSourcePath, ...] = ()


class PatchRunManifest(BaseModel):
    """Validated ``patch-run.toml`` representation for a Fixture Repository.

    Attributes:
        source_id: Lowercase kebab-case identifier used by CLI commands and output.
        issue: Optional inline problem statement; mutually exclusive with ``issue_path``.
        issue_path: Optional repository-relative problem statement file.
        verification: Controlled argv used for baseline and later Verification.
        editable_paths: Repository-relative files that Candidate Patches may modify.

    Example:
        >>> manifest = PatchRunManifest(
        ...     source_id="cart-discount",
        ...     issue_path="issue.md",
        ...     verification=("pytest",),
        ...     editable_paths=("cart.py",),
        ... )
        >>> manifest.source_id
        'cart-discount'
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: RepositorySourceId
    issue: IssueText | None = None
    issue_path: RelativeSourcePath | None = None
    verification: VerificationArgv
    editable_paths: EditablePaths

    @model_validator(mode="after")
    def require_one_issue_source(self) -> PatchRunManifest:
        """Require exactly one inline Issue or Issue file."""
        if (self.issue is None) == (self.issue_path is None):
            raise ValueError("exactly one of issue or issue_path is required")
        return self


@dataclass(frozen=True, slots=True)
class RepositorySource:
    """Immutable repository content prepared for Patch Run execution.

    Attributes:
        kind: Fixture Repository source category.
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


def load_repository_source(repository: Path) -> RepositorySource:
    """Load one Fixture Repository through its ``patch-run.toml`` manifest."""
    reject_source_symlinks(repository)
    resolved_root = repository.resolve()
    manifest_path = resolved_root / "patch-run.toml"
    try:
        with manifest_path.open("rb") as manifest_file:
            manifest = PatchRunManifest.model_validate(tomllib.load(manifest_file))
    except (OSError, ValueError) as error:
        raise ValueError(f"Invalid Patch Run Manifest at {manifest_path}: {error}") from error

    issue = manifest.issue
    if manifest.issue_path is not None:
        issue_path = resolve_source_file(resolved_root, manifest.issue_path, "Issue")
        issue = issue_path.read_text(encoding="utf-8")
    for editable_path in manifest.editable_paths:
        resolve_source_file(resolved_root, editable_path, "editable path")
    try:
        contract = PatchRunContract(
            issue=issue,
            verification=manifest.verification,
            editable_paths=manifest.editable_paths,
            protected_paths=_protected_paths(resolved_root, manifest),
        )
    except ValueError as error:
        raise ValueError(f"Invalid Patch Run Manifest at {manifest_path}: {error}") from error
    return RepositorySource(
        kind="fixture",
        source_id=manifest.source_id,
        root=resolved_root,
        contract=contract,
    )


def _protected_paths(root: Path, manifest: PatchRunManifest) -> tuple[str, ...]:
    """Protect the manifest, Issue file, and file arguments used by Verification."""
    protected = ["patch-run.toml"]
    if manifest.issue_path is not None:
        protected.append(manifest.issue_path)
    for argument in manifest.verification:
        candidate_value = argument.split("::", 1)[0]
        try:
            validate_relative_path(candidate_value)
        except ValueError:
            continue
        candidate = root.joinpath(*Path(candidate_value).parts)
        if candidate.is_file():
            protected.append(Path(candidate_value).as_posix())
    return tuple(dict.fromkeys(protected))


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
