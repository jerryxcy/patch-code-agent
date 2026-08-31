"""Discover bundled fixtures and translate manifests into Patch Run contracts.

A Fixture Manifest stores paths because its Issue lives inside the synthetic repository. Loading
resolves those paths, reads the Issue, and creates the same source-neutral ``PatchRunContract`` used
by trusted repositories. This keeps fixture packaging concerns out of the execution graph.
"""

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
    """Validated on-disk metadata for a registered Fixture Repository.

    Attributes:
        fixture_id: Unique lowercase kebab-case identifier used by the ``run`` CLI command.
        issue_path: Fixture-relative Markdown/text file containing the Issue.
        verification: Controlled argv tuple used for baseline and later Verification.
        editable_paths: Fixture-relative files that future patches may modify.

    Example:
        >>> manifest = FixtureManifest(
        ...     fixture_id="cart-discount",
        ...     issue_path="issue.md",
        ...     verification=("pytest", "-q"),
        ...     editable_paths=("cart.py",),
        ... )
        >>> manifest.issue_path
        'issue.md'
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    fixture_id: RepositorySourceId
    issue_path: RelativeSourcePath
    verification: VerificationArgv
    editable_paths: EditablePaths


@dataclass(frozen=True, slots=True)
class FixtureRepository:
    """A registered synthetic repository and its validated contract.

    The original manifest remains available for registry/UI metadata, while ``contract`` contains
    the fully loaded Issue text consumed by Patch Run execution.

    Attributes:
        manifest: Validated path-based metadata read from ``fixture.toml``.
        root: Resolved root directory containing the synthetic repository.
        contract: Source-neutral contract with the Issue file replaced by its loaded text.

    Example:
        >>> manifest = FixtureManifest(
        ...     fixture_id="cart-discount",
        ...     issue_path="issue.md",
        ...     verification=("pytest",),
        ...     editable_paths=("cart.py",),
        ... )
        >>> fixture = FixtureRepository(
        ...     manifest=manifest,
        ...     root=Path("examples/tiny_repo").resolve(),
        ...     contract=PatchRunContract(
        ...         issue="# Incorrect discount calculation",
        ...         verification=manifest.verification,
        ...         editable_paths=manifest.editable_paths,
        ...     ),
        ... )
        >>> fixture.issue_title
        'Incorrect discount calculation'
    """

    manifest: FixtureManifest
    root: Path
    contract: PatchRunContract

    @property
    def issue_title(self) -> str:
        """Return the first non-empty Issue line as a compact CLI label."""
        first_line = next(line for line in self.contract.issue.splitlines() if line.strip())
        return first_line.removeprefix("# ").strip()

    def as_repository_source(self) -> RepositorySource:
        """Expose a fixture through the source-neutral Repository Source interface."""
        return RepositorySource(
            kind="fixture",
            source_id=self.manifest.fixture_id,
            root=self.root,
            contract=self.contract,
        )


class FixtureRegistry:
    """Registry of bundled Fixture Repositories keyed by fixture identifier."""

    def __init__(self, repositories: tuple[FixtureRepository, ...]) -> None:
        """Index validated repositories while enforcing unique identifiers."""
        self._repositories = {repository.manifest.fixture_id: repository for repository in repositories}
        if len(self._repositories) != len(repositories):
            raise ValueError("Fixture identifiers must be unique")

    def list(self) -> tuple[FixtureRepository, ...]:
        """Return all fixtures in stable identifier order."""
        return tuple(self._repositories[key] for key in sorted(self._repositories))

    def get(self, fixture_id: str) -> FixtureRepository:
        """Look up one fixture and translate a missing key into a domain error."""
        try:
            return self._repositories[fixture_id]
        except KeyError as error:
            raise ValueError(f"Unknown Fixture Repository: {fixture_id}") from error


def bundled_fixture_roots() -> tuple[Path, ...]:
    """Locate packaged fixtures, falling back to the source checkout layout.

    Installed wheels include ``cart_discount`` beside this module. During repository development,
    the equivalent fixture lives under ``examples/tiny_repo`` instead.
    """
    installed_fixture = Path(__file__).resolve().parent / "cart_discount"
    if installed_fixture.is_dir():
        return (installed_fixture,)
    project_root = Path(__file__).resolve().parents[3]
    return (project_root / "examples" / "tiny_repo",)


def load_fixture_registry(roots: tuple[Path, ...]) -> FixtureRegistry:
    """Validate fixture roots and build an identifier-indexed registry."""
    return FixtureRegistry(tuple(_load_fixture_repository(root) for root in roots))


def _load_fixture_repository(root: Path) -> FixtureRepository:
    """Validate one fixture tree and normalize its manifest and Issue.

    Symlinks, missing files, ignored editable paths, malformed TOML, unknown fields, and empty Issue
    text all fail during registry loading—before a Run Identifier, workspace, or subprocess exists.
    """
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
