"""Create isolated Run Workspaces and fingerprint their source snapshot.

Every Patch Run receives a new copy beneath the external data root. Repository Sources are never
edited in place, and runtime directories such as virtual environments or caches are excluded from
the copy. A deterministic SHA-256 revision records exactly which visible paths and bytes seeded the
workspace so later reports can identify the starting snapshot.
"""

import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path

from patch_code_agent.sources import is_ignored_source_path


@dataclass(frozen=True, slots=True)
class RunWorkspace:
    """Location and content revision of one copied Repository Source.

    ``source_revision`` describes the initial copied tree; later Patch Run mutations do not change
    this identity value.

    Attributes:
        path: Absolute directory containing the Run's mutable copy of repository content.
        source_revision: Deterministic SHA-256 digest of initial relative paths and file bytes.

    Example:
        >>> workspace = RunWorkspace(
        ...     path=Path("/tmp/patch-runs/run-123/workspace"),
        ...     source_revision="9f86d081884c7d659a2feaa0c55ad015",
        ... )
        >>> workspace.path.name
        'workspace'
    """

    path: Path
    source_revision: str


class RunWorkspaceStore:
    """Creates durable, isolated Run Workspaces below one data root."""

    def __init__(self, data_root: Path) -> None:
        """Anchor all future per-run workspaces below a resolved data root."""
        self._data_root = data_root.resolve()

    def create(self, run_id: str, source_root: Path) -> RunWorkspace:
        """Copy a source tree into new per-run storage without runtime artifacts.

        Source and data roots may not contain one another: overlap could recursively copy prior
        runs or place mutable Run data inside the supposedly immutable source. ``exist_ok=False``
        also makes accidental Run Identifier reuse fail instead of overwriting earlier evidence.
        """
        resolved_source_root = source_root.resolve()
        if self._data_root.is_relative_to(resolved_source_root) or resolved_source_root.is_relative_to(
            self._data_root
        ):
            raise ValueError("Run storage must not overlap the Repository Source")
        self._data_root.mkdir(parents=True, exist_ok=True)
        run_root = self._data_root / run_id
        run_root.mkdir(parents=False, exist_ok=False)
        workspace = run_root / "workspace"
        shutil.copytree(resolved_source_root, workspace, ignore=_ignore_runtime_files)
        return RunWorkspace(
            path=workspace,
            source_revision=_tree_revision(workspace),
        )


def _ignore_runtime_files(_directory: str, names: list[str]) -> set[str]:
    """Adapt the shared source-view policy to ``shutil.copytree``."""
    return {name for name in names if is_ignored_source_path(Path(name))}


def _tree_revision(root: Path) -> str:
    """Hash relative paths and bytes into a deterministic source revision.

    Paths are sorted and separated from contents with NUL bytes, preventing ambiguous concatenated
    inputs. Files are streamed in chunks so revision calculation does not load a repository-sized
    payload into memory.
    """
    digest = hashlib.sha256()
    for path in sorted(path for path in root.rglob("*") if path.is_file()):
        relative_path = path.relative_to(root).as_posix()
        digest.update(relative_path.encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0")
        with path.open("rb") as source_file:
            for chunk in iter(lambda: source_file.read(64 * 1024), b""):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()
