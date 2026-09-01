"""Replay-safe host application of approved structured file replacements."""

import hashlib
import json
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from patch_code_agent.candidate import (
    CandidatePatchReference,
    load_candidate_patch,
    render_unified_diff,
)
from patch_code_agent.inspection import WorkspaceInspector
from patch_code_agent.sources import validate_relative_path

ApplyOutcome = Literal["applied", "already_applied", "workspace_changed", "partial_apply"]
_MAX_WORKSPACE_FILE_BYTES = 100 * 1024


class WorkspaceChangedError(RuntimeError):
    """Signal that an apply-time revalidation no longer matches the approved preimage."""


class ApplySummary(BaseModel):
    """Bounded durable result of checking or applying one approved Candidate Patch."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    attempt: int = Field(ge=1, le=3)
    outcome: ApplyOutcome
    files_changed: tuple[str, ...]
    error_kind: str | None = None


class CumulativeDiffReference(BaseModel):
    """Checkpoint-safe identity of the human-inspectable cumulative diff."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: Literal["cumulative.diff"] = "cumulative.diff"
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class PatchApplier:
    """Validate before/after hashes and apply all replacements as one host-owned operation."""

    def __init__(self, data_root: Path) -> None:
        self._data_root = data_root.resolve()

    def candidate_paths(
        self,
        *,
        run_id: str,
        reference: CandidatePatchReference,
    ) -> tuple[str, ...]:
        """Return the validated replacement paths for pre-apply budget enforcement."""
        artifact = load_candidate_patch(self._data_root, run_id, reference).artifact
        return tuple(replacement.path for replacement in artifact.candidate.replacements)

    def apply_once(
        self,
        *,
        run_id: str,
        workspace: Path,
        reference: CandidatePatchReference,
    ) -> ApplySummary:
        """Apply all-before, accept all-after, and fail closed for every mixed state."""
        candidate_result = load_candidate_patch(self._data_root, run_id, reference)
        attempt = candidate_result.artifact.attempt
        attempt_root = self._data_root / run_id / "attempts" / str(attempt)
        result_path = attempt_root / "apply.json"
        completion_path = attempt_root / ".apply-complete"
        persisted = (
            _load_apply_summary(result_path, completion_path)
            if result_path.exists() or completion_path.exists()
            else None
        )

        replacements = candidate_result.artifact.candidate.replacements
        current_contents: dict[str, str] = {}
        states: dict[str, Literal["before", "after", "unknown"]] = {}
        try:
            for replacement in replacements:
                content = WorkspaceInspector(workspace).read_file(replacement.path).content
                current_contents[replacement.path] = content
                current_hash = _text_sha256(content)
                after_hash = _text_sha256(replacement.new_content)
                if current_hash == replacement.expected_sha256:
                    states[replacement.path] = "before"
                elif current_hash == after_hash:
                    states[replacement.path] = "after"
                else:
                    states[replacement.path] = "unknown"
        except (OSError, ValueError):
            summary = ApplySummary(
                attempt=attempt,
                outcome="workspace_changed",
                files_changed=(),
                error_kind="workspace_changed",
            )
            return _finish_apply_check(
                result_path=result_path,
                completion_path=completion_path,
                persisted=persisted,
                observed=summary,
            )

        observed_states = set(states.values())
        if "unknown" in observed_states:
            summary = ApplySummary(
                attempt=attempt,
                outcome="workspace_changed",
                files_changed=(),
                error_kind="workspace_changed",
            )
        elif observed_states == {"before", "after"}:
            summary = ApplySummary(
                attempt=attempt,
                outcome="partial_apply",
                files_changed=tuple(
                    sorted(path for path, value in states.items() if value == "after")
                ),
                error_kind="partial_apply",
            )
        elif observed_states == {"after"}:
            summary = ApplySummary(
                attempt=attempt,
                outcome="already_applied",
                files_changed=tuple(sorted(states)),
            )
        else:
            if persisted is not None:
                return _replay_apply_summary(persisted, states)
            _persist_initial_preimages(
                attempt_root,
                current_contents,
            )
            changed: list[str] = []
            try:
                for replacement in replacements:
                    _atomic_replace_text(
                        workspace=workspace,
                        relative_path=replacement.path,
                        expected_sha256=replacement.expected_sha256,
                        new_content=replacement.new_content,
                    )
                    changed.append(replacement.path)
            except WorkspaceChangedError:
                for path in reversed(changed):
                    _atomic_replace_text(
                        workspace=workspace,
                        relative_path=path,
                        expected_sha256=_text_sha256(
                            next(
                                replacement.new_content
                                for replacement in replacements
                                if replacement.path == path
                            )
                        ),
                        new_content=current_contents[path],
                    )
                summary = ApplySummary(
                    attempt=attempt,
                    outcome="workspace_changed",
                    files_changed=(),
                    error_kind="workspace_changed",
                )
            else:
                summary = ApplySummary(
                    attempt=attempt,
                    outcome="applied",
                    files_changed=tuple(sorted(changed)),
                )

        return _finish_apply_check(
            result_path=result_path,
            completion_path=completion_path,
            persisted=persisted,
            observed=summary,
        )

    def persist_cumulative_diff(
        self,
        *,
        run_id: str,
        workspace: Path,
        reference: CandidatePatchReference,
    ) -> CumulativeDiffReference:
        """Persist the aggregate diff from first approved preimages to current workspace."""
        candidate = load_candidate_patch(self._data_root, run_id, reference)
        run_root = self._data_root / run_id
        preimages: dict[str, str] = {}
        for attempt in range(1, candidate.artifact.attempt + 1):
            attempt_preimages = _load_initial_preimages(run_root / "attempts" / str(attempt))
            for relative_path, content in (attempt_preimages or {}).items():
                preimages.setdefault(relative_path, content)
        if not preimages:
            cumulative = candidate.diff
        else:
            parts: list[str] = []
            for relative_path, before in sorted(preimages.items()):
                after = WorkspaceInspector(workspace).read_file(relative_path).content
                parts.append(
                    render_unified_diff(
                        path=relative_path,
                        before=before,
                        after=after,
                    )
                )
            cumulative = "".join(parts)
        diff_bytes = cumulative.encode("utf-8")
        checksum = hashlib.sha256(diff_bytes).hexdigest()
        path = run_root / "cumulative.diff"
        if path.exists():
            if hashlib.sha256(path.read_bytes()).hexdigest() != checksum:
                raise RuntimeError("Cumulative diff does not match the successful Candidate Patch")
        else:
            path.write_bytes(diff_bytes)
        return CumulativeDiffReference(sha256=checksum)


def _persist_initial_preimages(attempt_root: Path, current_contents: dict[str, str]) -> None:
    """Persist one attempt's immutable before contents prior to any workspace write."""
    artifact_path = attempt_root / "preimages.json"
    completion_path = attempt_root / ".preimages-complete"
    existing = _load_initial_preimages(attempt_root)
    artifact_bytes = (json.dumps(current_contents, indent=2, sort_keys=True) + "\n").encode("utf-8")
    checksum = hashlib.sha256(artifact_bytes).hexdigest()
    if existing is not None:
        if existing != current_contents:
            raise RuntimeError("Attempt preimages do not match their immutable artifact")
        return
    artifact_path.write_bytes(artifact_bytes)
    completion_path.write_text(checksum + "\n", encoding="utf-8")


def _load_initial_preimages(attempt_root: Path) -> dict[str, str] | None:
    artifact_path = attempt_root / "preimages.json"
    completion_path = attempt_root / ".preimages-complete"
    if not artifact_path.exists() and not completion_path.exists():
        return None
    if not artifact_path.is_file() or not completion_path.is_file():
        raise RuntimeError("Attempt preimages have an incomplete replay ledger")
    artifact_bytes = artifact_path.read_bytes()
    if (
        completion_path.read_text(encoding="utf-8").strip()
        != hashlib.sha256(artifact_bytes).hexdigest()
    ):
        raise RuntimeError("Attempt preimages do not match their completion checksum")
    loaded = json.loads(artifact_bytes)
    if not isinstance(loaded, dict) or not all(
        isinstance(path, str) and isinstance(content, str) for path, content in loaded.items()
    ):
        raise RuntimeError("Attempt preimages have an invalid artifact schema")
    return loaded


def _atomic_replace_text(
    *,
    workspace: Path,
    relative_path: str,
    expected_sha256: str,
    new_content: str,
) -> None:
    with _open_parent_directory(workspace, relative_path) as (parent_fd, filename):
        current, mode = _read_regular_text(parent_fd, filename, relative_path)
        if _text_sha256(current) != expected_sha256:
            raise WorkspaceChangedError(f"Run Workspace changed while applying: {relative_path}")
        temporary_name = f".patch-code-agent-{uuid4().hex}.tmp"
        temporary_fd: int | None = None
        try:
            temporary_fd = os.open(
                temporary_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                mode,
                dir_fd=parent_fd,
            )
            with os.fdopen(temporary_fd, "wb") as temporary:
                temporary_fd = None
                temporary.write(new_content.encode("utf-8"))
                temporary.flush()
                os.fchmod(temporary.fileno(), mode)
                os.fsync(temporary.fileno())
            os.replace(
                temporary_name,
                filename,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            temporary_name = ""
            os.fsync(parent_fd)
        except OSError as error:
            raise WorkspaceChangedError(
                f"Run Workspace changed while applying: {relative_path}"
            ) from error
        finally:
            if temporary_fd is not None:
                os.close(temporary_fd)
            if temporary_name:
                try:
                    os.unlink(temporary_name, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass


@contextmanager
def _open_parent_directory(
    workspace: Path,
    relative_path: str,
) -> Iterator[tuple[int, str]]:
    """Open every path segment without following symlinks and anchor later writes."""
    validate_relative_path(relative_path)
    parts = PurePosixPath(relative_path).parts
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    directory_fd: int | None = None
    try:
        directory_fd = os.open(workspace, directory_flags)
        for part in parts[:-1]:
            next_fd = os.open(part, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        yield directory_fd, parts[-1]
    except (OSError, ValueError) as error:
        raise WorkspaceChangedError(
            f"Run Workspace path is no longer safe: {relative_path}"
        ) from error
    finally:
        if directory_fd is not None:
            os.close(directory_fd)


def _read_regular_text(
    parent_fd: int,
    filename: str,
    relative_path: str,
) -> tuple[str, int]:
    """Read one bounded regular file through its already validated parent directory."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        file_fd = os.open(filename, flags, dir_fd=parent_fd)
        with os.fdopen(file_fd, "rb") as source:
            metadata = os.fstat(source.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise WorkspaceChangedError(
                    f"Run Workspace path is not a regular file: {relative_path}"
                )
            content = source.read(_MAX_WORKSPACE_FILE_BYTES + 1)
    except OSError as error:
        raise WorkspaceChangedError(
            f"Run Workspace path is no longer readable: {relative_path}"
        ) from error
    if len(content) > _MAX_WORKSPACE_FILE_BYTES or b"\x00" in content:
        raise WorkspaceChangedError(f"Run Workspace file is no longer text: {relative_path}")
    try:
        decoded = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise WorkspaceChangedError(
            f"Run Workspace file is no longer UTF-8: {relative_path}"
        ) from error
    return decoded, stat.S_IMODE(metadata.st_mode)


def _finish_apply_check(
    *,
    result_path: Path,
    completion_path: Path,
    persisted: ApplySummary | None,
    observed: ApplySummary,
) -> ApplySummary:
    """Persist a first result or reconcile a replay with the current workspace state."""
    if persisted is not None:
        if persisted.outcome in {"applied", "already_applied"}:
            if observed.outcome == "already_applied":
                return persisted
            return observed
        return persisted
    _persist_apply_summary(result_path, completion_path, observed)
    return observed


def _replay_apply_summary(
    persisted: ApplySummary,
    states: dict[str, Literal["before", "after", "unknown"]],
) -> ApplySummary:
    """Fail closed when a completed successful apply no longer has all-after content."""
    if persisted.outcome not in {"applied", "already_applied"}:
        return persisted
    observed_states = set(states.values())
    if observed_states == {"before", "after"}:
        return ApplySummary(
            attempt=persisted.attempt,
            outcome="partial_apply",
            files_changed=tuple(sorted(path for path, value in states.items() if value == "after")),
            error_kind="partial_apply",
        )
    return ApplySummary(
        attempt=persisted.attempt,
        outcome="workspace_changed",
        files_changed=(),
        error_kind="workspace_changed",
    )


def _persist_apply_summary(
    result_path: Path,
    completion_path: Path,
    summary: ApplySummary,
) -> None:
    result_bytes = (summary.model_dump_json(indent=2) + "\n").encode("utf-8")
    result_path.write_bytes(result_bytes)
    completion_path.write_text(hashlib.sha256(result_bytes).hexdigest() + "\n", encoding="utf-8")


def _load_apply_summary(result_path: Path, completion_path: Path) -> ApplySummary:
    if not result_path.is_file() or not completion_path.is_file():
        raise RuntimeError("Candidate application has an incomplete replay ledger")
    result_bytes = result_path.read_bytes()
    if (
        completion_path.read_text(encoding="utf-8").strip()
        != hashlib.sha256(result_bytes).hexdigest()
    ):
        raise RuntimeError("Candidate application does not match its completion checksum")
    return ApplySummary.model_validate_json(result_bytes)


def _text_sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()
