import json
from datetime import UTC, datetime

from patch_code_agent.reporting import RunAuditStore, RunEvent


def test_run_event_uses_stable_id_timestamp_and_append_deduplication(tmp_path) -> None:
    timestamp = datetime(2026, 9, 1, 8, 30, tzinfo=UTC)
    store = RunAuditStore(tmp_path, timestamp_factory=lambda: timestamp)
    state = {
        "run_id": "audit-run",
        "status": "validated",
        "attempt": 0,
        "model_requests": 0,
        "tool_executions": 0,
    }

    first = store.append_event(state, "validate")
    replayed = store.append_event(state, "validate")

    lines = (tmp_path / "audit-run" / "events.jsonl").read_text().splitlines()
    assert first == replayed
    assert first.occurred_at == "2026-09-01T08:30:00Z"
    assert len(lines) == 1
    assert RunEvent.model_validate(json.loads(lines[0])) == first
