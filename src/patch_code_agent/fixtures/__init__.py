"""Discover bundled Fixture Repositories through the shared Patch Run manifest."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from patch_code_agent.sources import RepositorySource, load_repository_source


@dataclass(frozen=True, slots=True)
class FixtureRepository:
    """A registered synthetic repository backed by a normalized source."""

    source: RepositorySource

    @property
    def source_id(self) -> str:
        """Return the stable identifier used by the ``run`` command."""
        return self.source.source_id

    @property
    def issue_title(self) -> str:
        """Return the first non-empty Issue line as a compact CLI label."""
        first_line = next(line for line in self.source.contract.issue.splitlines() if line.strip())
        return first_line.removeprefix("# ").strip()

    def as_repository_source(self) -> RepositorySource:
        """Expose a fixture through the source-neutral Repository Source interface."""
        return self.source


class FixtureRegistry:
    """Registry of bundled Fixture Repositories keyed by fixture identifier."""

    def __init__(self, repositories: tuple[FixtureRepository, ...]) -> None:
        """Index validated repositories while enforcing unique identifiers."""
        self._repositories = {repository.source_id: repository for repository in repositories}
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
    """Locate packaged fixtures, falling back to the source checkout layout."""
    installed_fixture = Path(__file__).resolve().parent / "cart_discount"
    if installed_fixture.is_dir():
        return (installed_fixture,)
    project_root = Path(__file__).resolve().parents[3]
    return (project_root / "examples" / "tiny_repo",)


def load_fixture_registry(roots: tuple[Path, ...]) -> FixtureRegistry:
    """Load and validate the configured Fixture Repository roots."""
    return FixtureRegistry(
        tuple(FixtureRepository(load_repository_source(root)) for root in roots)
    )
