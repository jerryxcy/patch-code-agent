import hashlib
import json
import sys
from collections import deque
from pathlib import Path
from types import SimpleNamespace

import pytest

from patch_code_agent.gemini import (
    GeminiFunctionCall,
    GeminiInconclusiveError,
    GeminiModelGateway,
    GeminiProviderError,
    GeminiTranscriptPersistenceError,
    GeminiTranscriptWriter,
    GeminiTurn,
    GoogleGenAITransport,
)
from patch_code_agent.inspection import WorkspaceInspector
from patch_code_agent.model import (
    CandidateRequest,
    Diagnosis,
    DiagnosisRequest,
    ModelGatewayResult,
    Plan,
    PlanningRequest,
)


class QueueTransport:
    def __init__(self, *results) -> None:
        self.results = deque(results)
        self.prompts: list[str] = []
        self.conversations: list[list[object]] = []

    def generate(self, *, prompt, schema, conversation):
        self.prompts.append(prompt)
        self.conversations.append(list(conversation))
        result = self.results.popleft()
        if isinstance(result, Exception):
            raise result
        return result


def _plan() -> Plan:
    return Plan(
        issue_summary="Discount is subtracted instead of multiplied",
        relevant_files=("cart.py",),
        repair_strategy="Multiply the subtotal by one minus the discount.",
        verification_strategy="Run the declared Verification command.",
    )


def test_gemini_adapter_uses_bounded_tools_for_all_typed_outputs(tmp_path: Path) -> None:
    source = "def total(prices, discount):\n    return sum(prices) - discount\n"
    (tmp_path / "cart.py").write_text(source)
    expected_hash = hashlib.sha256(source.encode()).hexdigest()
    model_content = {"role": "model", "parts": [{"opaque_thought_signature": "kept"}]}
    transport = QueueTransport(
        GeminiTurn(
            function_calls=(GeminiFunctionCall("list_files", {}, "call-1"),),
            model_content=model_content,
        ),
        GeminiTurn(output=_plan().model_dump()),
        GeminiTurn(
            function_calls=(GeminiFunctionCall("read_file", {"path": "cart.py"}),)
        ),
        GeminiTurn(
            output={
                "replacements": [
                    {
                        "path": "cart.py",
                        "expected_sha256": expected_hash,
                        "new_content": source.replace(
                            "return sum(prices) - discount",
                            "return sum(prices) * (1 - discount)",
                        ),
                    }
                ]
            }
        ),
        GeminiTurn(
            output={
                "failure_summary": "The first repair still failed.",
                "evidence": "One assertion failed.",
                "next_strategy": "Correct the remaining expression.",
            }
        ),
    )
    gateway = GeminiModelGateway(transport)
    inspector = WorkspaceInspector(tmp_path)

    plan_result = gateway.create_plan(
        PlanningRequest(
            issue="Fix discount",
            verification=(sys.executable, "-m", "pytest"),
            model_requests_remaining=8,
        ),
        inspector,
    )
    candidate_result = gateway.create_candidate(
        CandidateRequest(
            issue="Fix discount",
            plan=_plan(),
            editable_paths=("cart.py",),
            attempt=1,
            model_requests_remaining=6,
        ),
        inspector,
    )
    diagnosis_result = gateway.create_diagnosis(
        DiagnosisRequest(
            issue="Fix discount",
            plan=_plan(),
            attempt=1,
            verification_output_excerpt="1 failed",
            verification_artifact_path="attempts/1/verification.log",
            model_requests_remaining=4,
        ),
        inspector,
    )

    assert isinstance(plan_result, ModelGatewayResult)
    assert plan_result.model_requests == 2
    assert Plan.model_validate(plan_result.output) == _plan()
    assert isinstance(candidate_result, ModelGatewayResult)
    assert candidate_result.model_requests == 2
    assert isinstance(diagnosis_result, ModelGatewayResult)
    assert diagnosis_result.model_requests == 1
    assert Diagnosis.model_validate(diagnosis_result.output).next_strategy
    assert inspector.tool_executions == 2
    assert inspector.files_read == ("cart.py",)
    assert "do not call list_files" in transport.prompts[2]
    assert "do not read tests, issue files, or Patch Run manifests" in transport.prompts[2]
    assert transport.conversations[1][0] is model_content
    assert transport.conversations[1][1] == {
        "role": "user",
        "parts": [
            {
                "function_response": {
                    "name": "list_files",
                    "response": {
                        "result": {"paths": ("cart.py",), "truncated": False}
                    },
                    "id": "call-1",
                }
            }
        ],
    }
    candidate_read_result = transport.conversations[3][1]["parts"][0][
        "function_response"
    ]["response"]["result"]
    assert candidate_read_result["sha256"] == expected_hash


def test_google_transport_preserves_sdk_tool_content_and_builds_typed_config() -> None:
    model_content = object()
    response = SimpleNamespace(
        function_calls=(
            SimpleNamespace(name="read_file", args={"path": "cart.py"}, id="call-7"),
        ),
        candidates=(SimpleNamespace(content=model_content),),
        parsed=None,
        text="",
    )

    class FakeModels:
        def __init__(self) -> None:
            self.request = None

        def generate_content(self, **request):
            self.request = request
            return response

    models = FakeModels()
    transport = object.__new__(GoogleGenAITransport)
    transport._client = SimpleNamespace(models=models)
    transport._model_id = "gemini-3.7-flash"

    turn = transport.generate(prompt="Inspect first", schema=Plan, conversation=())

    assert turn.model_content is model_content
    assert turn.function_calls == (
        GeminiFunctionCall("read_file", {"path": "cart.py"}, "call-7"),
    )
    assert models.request["model"] == "gemini-3.7-flash"
    assert models.request["config"]["automatic_function_calling"] == {"disable": True}
    assert models.request["config"]["response_json_schema"] == Plan.model_json_schema()
    assert "temperature" not in models.request["config"]


def test_gemini_gateway_records_selected_supported_model() -> None:
    gateway = GeminiModelGateway(QueueTransport(), model_id="gemini-3.6-flash")

    assert gateway.model_id == "gemini-3.6-flash"

    with pytest.raises(ValueError, match="Unsupported Gemini model"):
        GeminiModelGateway(QueueTransport(), model_id="gemini-private")


def test_gemini_adapter_retries_transient_failures_twice_and_counts_requests() -> None:
    transport = QueueTransport(
        GeminiProviderError("busy", status_code=429),
        GeminiProviderError("unavailable", status_code=503),
        GeminiTurn(output=_plan().model_dump()),
    )
    backoffs: list[float] = []
    gateway = GeminiModelGateway(transport, backoff=backoffs.append)

    result = gateway.create_plan(
        PlanningRequest(
            issue="Fix discount",
            verification=("pytest",),
            model_requests_remaining=8,
        ),
        WorkspaceInspector(Path.cwd()),
    )

    assert isinstance(result, ModelGatewayResult)
    assert result.model_requests == 3
    assert backoffs == [1.0, 2.0]


def test_gemini_adapter_returns_bounded_tool_rejection_for_model_correction(
    tmp_path: Path,
) -> None:
    transport = QueueTransport(
        GeminiTurn(
            function_calls=(GeminiFunctionCall("read_file", {"path": "../secret"}),)
        ),
        GeminiTurn(output=_plan().model_dump()),
    )
    gateway = GeminiModelGateway(transport)

    result = gateway.create_plan(
        PlanningRequest(
            issue="Fix discount",
            verification=("pytest",),
            model_requests_remaining=8,
        ),
        WorkspaceInspector(tmp_path),
    )

    assert isinstance(result, ModelGatewayResult)
    assert result.model_requests == 2
    tool_turn = transport.conversations[1][1]
    assert tool_turn["role"] == "user"
    assert "must be relative" in tool_turn["parts"][0]["function_response"]["response"][
        "error"
    ]


def test_gemini_adapter_marks_provider_exhaustion_inconclusive() -> None:
    transport = QueueTransport(
        *(GeminiProviderError("busy", status_code=429) for _ in range(3))
    )
    gateway = GeminiModelGateway(transport, backoff=lambda _seconds: None)

    with pytest.raises(GeminiInconclusiveError) as captured:
        gateway.create_plan(
            PlanningRequest(
                issue="Fix discount",
                verification=("pytest",),
                model_requests_remaining=8,
            ),
            WorkspaceInspector(Path.cwd()),
        )

    assert captured.value.model_requests == 3
    assert captured.value.status_code == 429


def test_gemini_adapter_keeps_provider_failure_inconclusive_at_budget_edge() -> None:
    transport = QueueTransport(
        GeminiProviderError("busy", status_code=429),
        GeminiProviderError("busy", status_code=429),
    )
    gateway = GeminiModelGateway(transport, backoff=lambda _seconds: None)

    with pytest.raises(GeminiInconclusiveError) as captured:
        gateway.create_plan(
            PlanningRequest(
                issue="Fix discount",
                verification=("pytest",),
                model_requests_remaining=2,
            ),
            WorkspaceInspector(Path.cwd()),
        )

    assert captured.value.model_requests == 2


def test_gemini_adapter_counts_tool_turn_before_provider_exhaustion(tmp_path: Path) -> None:
    transport = QueueTransport(
        GeminiTurn(function_calls=(GeminiFunctionCall("list_files", {}),)),
        *(GeminiProviderError("busy", status_code=503) for _ in range(3)),
    )
    gateway = GeminiModelGateway(transport, backoff=lambda _seconds: None)

    with pytest.raises(GeminiInconclusiveError) as captured:
        gateway.create_plan(
            PlanningRequest(
                issue="Fix discount",
                verification=("pytest",),
                model_requests_remaining=8,
            ),
            WorkspaceInspector(tmp_path),
        )

    assert captured.value.model_requests == 4


def test_gemini_transcript_is_a_credential_free_run_artifact(tmp_path: Path) -> None:
    data_root = tmp_path / "runs"
    run_id = "live-run"
    (data_root / run_id).mkdir(parents=True)
    gateway = GeminiModelGateway(
        QueueTransport(
            GeminiTurn(
                output=_plan().model_dump(),
                model_content={"thought_signature": b"\xff"},
            )
        ),
        transcript_writer=GeminiTranscriptWriter(data_root),
    )

    result = gateway.create_plan(
        PlanningRequest(
            issue="Fix synthetic discount",
            verification=("pytest",),
            model_requests_remaining=8,
            run_id=run_id,
        ),
        WorkspaceInspector(tmp_path),
    )

    transcript = data_root / run_id / "model-transcripts" / "planning.jsonl"
    entry = json.loads(transcript.read_text())
    assert isinstance(result, ModelGatewayResult)
    assert entry["request_number"] == 1
    assert "Fix synthetic discount" in entry["prompt"]
    assert entry["response"]["output"] == _plan().model_dump(mode="json")
    assert entry["response"]["model_content"]["thought_signature"] == {"base64": "/w=="}
    assert "api_key" not in transcript.read_text().lower()


def test_transcript_failure_after_retry_preserves_actual_request_count(tmp_path: Path) -> None:
    class FailingWriter:
        def __init__(self) -> None:
            self.records = 0

        def record(self, **_entry) -> None:
            self.records += 1
            if self.records == 2:
                raise OSError("simulated transcript storage failure")

    writer = FailingWriter()
    gateway = GeminiModelGateway(
        QueueTransport(
            GeminiProviderError("busy", status_code=429),
            GeminiTurn(output=_plan().model_dump()),
        ),
        backoff=lambda _seconds: None,
        transcript_writer=writer,
    )

    with pytest.raises(GeminiTranscriptPersistenceError) as captured:
        gateway.create_plan(
            PlanningRequest(
                issue="Fix discount",
                verification=("pytest",),
                model_requests_remaining=8,
                run_id="live-run",
            ),
            WorkspaceInspector(tmp_path),
        )

    assert captured.value.model_requests == 2
