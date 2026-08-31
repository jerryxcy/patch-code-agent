from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from patch_code_agent.sources import (
    EditablePaths,
    PatchRunContract,
    RelativeSourcePath,
    RepositorySource,
    RepositorySourceId,
    VerificationArgv,
    reject_source_symlinks,
    resolve_source_file,
)


class FixtureManifest(BaseModel):
    """Validated contract for a registered Fixture Repository."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    fixture_id: RepositorySourceId
    issue_path: RelativeSourcePath
    verification: VerificationArgv
    editable_paths: EditablePaths


@dataclass(frozen=True, slots=True)
class FixtureRepository:
    """A registered synthetic repository and its validated contract."""

    manifest: FixtureManifest
    root: Path
    contract: PatchRunContract

    @property
    def issue_title(self) -> str:
        first_line = next(line for line in self.contract.issue.splitlines() if line.strip())
        return first_line.removeprefix("# ").strip()

    def as_repository_source(self) -> RepositorySource:
        return RepositorySource(
            kind="fixture",
            source_id=self.manifest.fixture_id,
            root=self.root,
            contract=self.contract,
        )


class FixtureRegistry:
    """Registry of bundled Fixture Repositories keyed by fixture identifier."""

    def __init__(self, repositories: tuple[FixtureRepository, ...]) -> None:
        self._repositories = {repository.manifest.fixture_id: repository for repository in repositories}
        if len(self._repositories) != len(repositories):
            raise ValueError("Fixture identifiers must be unique")

    def list(self) -> tuple[FixtureRepository, ...]:
        return tuple(self._repositories[key] for key in sorted(self._repositories))

    def get(self, fixture_id: str) -> FixtureRepository:
        try:
            return self._repositories[fixture_id]
        except KeyError as error:
            raise ValueError(f"Unknown Fixture Repository: {fixture_id}") from error


def bundled_fixture_roots() -> tuple[Path, ...]:
    installed_fixture = Path(__file__).resolve().parent / "cart_discount"
    if installed_fixture.is_dir():
        return (installed_fixture,)
    project_root = Path(__file__).resolve().parents[3]
    return (project_root / "examples" / "tiny_repo",)


def load_fixture_registry(roots: tuple[Path, ...]) -> FixtureRegistry:
    return FixtureRegistry(tuple(_load_fixture_repository(root) for root in roots))


def _load_fixture_repository(root: Path) -> FixtureRepository:
    reject_source_symlinks(root)
    manifest_path = root / "fixture.toml"
    try:
        with manifest_path.open("rb") as manifest_file:
            manifest = FixtureManifest.model_validate(tomllib.load(manifest_file))
    except (OSError, ValueError) as error:
        raise ValueError(f"Invalid Fixture Manifest at {manifest_path}: {error}") from error

    resolved_root = root.resolve()
    issue_path = resolve_source_file(resolved_root, manifest.issue_path, "Issue")
    for editable_path in manifest.editable_paths:
        resolve_source_file(resolved_root, editable_path, "editable path")

    issue = issue_path.read_text(encoding="utf-8")
    if not issue.strip():
        raise ValueError("Fixture Issue must not be empty")
    try:
        contract = PatchRunContract(
            issue=issue,
            verification=manifest.verification,
            editable_paths=manifest.editable_paths,
        )
    except ValueError as error:
        raise ValueError(f"Invalid Fixture Manifest at {manifest_path}: {error}") from error

    return FixtureRepository(
        manifest=manifest,
        root=resolved_root,
        contract=contract,
    )
