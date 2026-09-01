import json
import sys
from pathlib import Path

import pytest

from patch_code_agent.application import PatchCodeAgent, PatchRunStatusReader
from patch_code_agent.model import ScriptedModel


class SyntheticOnlyModel:
    model_id = "synthetic-only"
    synthetic_only = True
    allowed_fixture_roots: tuple[Path, ...] = ()

    def create_plan(self, request, tools):
        return ScriptedModel().create_plan(request, tools)

    def create_candidate(self, request, tools):
        return ScriptedModel().create_candidate(request, tools)

    def create_diagnosis(self, request, tools):
        return ScriptedModel().create_diagnosis(request, tools)


def test_status_reader_sees_committed_checkpoint_still_in_wal(tmp_path: Path) -> None:
    data_root = tmp_path / "runs"
    application = PatchCodeAgent(
        model_gateway=ScriptedModel(),
        data_root=data_root,
    )

    try:
        application.start_patch_run(
            fixture_id="cart-discount",
            run_id="wal-backed-run",
        )
        status = PatchRunStatusReader(data_root).get("wal-backed-run")
    finally:
        application.close()

    assert status.run_id == "wal-backed-run"
    assert status.source_kind == "fixture"
    assert status.source_id == "cart-discount"
    assert len(status.source_revision) == 64
    assert status.phase == "pending_approval"
    assert status.candidate_artifact is not None


def test_synthetic_only_model_refuses_trusted_repository_before_model_request(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "trusted"
    repository.mkdir()
    (repository / "cart.py").write_text("VALUE = 1\n")
    contract = tmp_path / "contract.toml"
    contract.write_text(
        f'''source_id = "trusted"
issue = "Do not send this source"
verification = {json.dumps([sys.executable, "-c", "raise SystemExit(1)"])}
editable_paths = ["cart.py"]
'''
    )
    data_root = tmp_path / "runs"
    application = PatchCodeAgent(
        model_gateway=SyntheticOnlyModel(),
        data_root=data_root,
    )

    try:
        with pytest.raises(ValueError, match="only accepts bundled synthetic"):
            application.start_trusted_patch_run(
                repository=repository,
                contract_path=contract,
                run_id="private-run",
            )
    finally:
        application.close()

    assert not (data_root / "private-run").exists()
