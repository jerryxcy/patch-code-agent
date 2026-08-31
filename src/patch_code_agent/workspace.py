import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path

from patch_code_agent.sources import is_ignored_source_path


@dataclass(frozen=True, slots=True)
class RunWorkspace:
    path: Path
    source_revision: str


class RunWorkspaceStore:
    """Creates durable, isolated Run Workspaces below one data root."""

    def __init__(self, data_root: Path) -> None:
        self._data_root = data_root.resolve()

    def create(self, run_id: str, source_root: Path) -> RunWorkspace:
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
    return {name for name in names if is_ignored_source_path(Path(name))}


def _tree_revision(root: Path) -> str:
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
