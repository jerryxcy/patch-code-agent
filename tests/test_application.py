from pathlib import Path

from patch_code_agent.application import PatchCodeAgent, PatchRunStatusReader
from patch_code_agent.model import ScriptedModel


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
