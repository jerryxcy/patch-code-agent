"""Gemini 3.7 Flash adapter for opt-in synthetic Fixture Live Smoke Runs."""

import json
from base64 import b64encode
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from time import sleep
from typing import Protocol

from pydantic import BaseModel

from patch_code_agent.budgets import ResourceBudgetExceededError
from patch_code_agent.fixtures import bundled_fixture_roots
from patch_code_agent.inspection import InspectionTools
from patch_code_agent.model import (
    CandidatePatch,
    CandidateRequest,
    Diagnosis,
    DiagnosisRequest,
    ModelGatewayResult,
    Plan,
    PlanningRequest,
)

_MODEL_ID = "gemini-3.7-flash"
_TRANSIENT_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})


class GeminiProviderError(RuntimeError):
    """Normalized one-request provider failure raised by a transport."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.transient = status_code in _TRANSIENT_STATUS_CODES


class GeminiInconclusiveError(RuntimeError):
    """Signal that an opt-in Live Smoke Run could not establish a provider result."""

    inconclusive = True

    def __init__(self, message: str, *, model_requests: int) -> None:
        super().__init__(message)
        self.model_requests = model_requests


class GeminiTranscriptPersistenceError(RuntimeError):
    """Preserve provider accounting when a transcript cannot be made durable."""

    def __init__(self, *, model_requests: int) -> None:
        super().__init__("Gemini transcript could not be persisted")
        self.model_requests = model_requests


@dataclass(frozen=True, slots=True)
class GeminiFunctionCall:
    """Normalized function call independent of the optional provider SDK."""

    name: str
    arguments: Mapping[str, object]
    call_id: str | None = None


@dataclass(frozen=True, slots=True)
class GeminiTurn:
    """One provider response containing either tool calls or structured output."""

    output: object | None = None
    function_calls: tuple[GeminiFunctionCall, ...] = ()
    model_content: object | None = None


class GeminiTransport(Protocol):
    """One actual remote request at the credential-owning client boundary."""

    def generate(
        self,
        *,
        prompt: str,
        schema: type[BaseModel],
        conversation: Sequence[object],
    ) -> GeminiTurn:
        """Return one normalized provider turn or raise ``GeminiProviderError``."""


class GoogleGenAITransport:
    """Credential-owning ``google-genai`` transport loaded only for Live Smoke Runs."""

    def __init__(self, api_key: str, *, model_id: str = _MODEL_ID) -> None:
        try:
            from google import genai
        except ImportError as error:
            raise RuntimeError(
                "Gemini support is not installed; run `uv sync --extra gemini`"
            ) from error
        self._client = genai.Client(api_key=api_key)
        self._model_id = model_id

    def generate(
        self,
        *,
        prompt: str,
        schema: type[BaseModel],
        conversation: Sequence[object],
    ) -> GeminiTurn:
        contents: list[object] = [
            {"role": "user", "parts": [{"text": prompt}]},
            *conversation,
        ]
        try:
            response = self._client.models.generate_content(
                model=self._model_id,
                contents=contents,
                config={
                    "tools": [{"function_declarations": _TOOL_DECLARATIONS}],
                    "automatic_function_calling": {"disable": True},
                    "response_mime_type": "application/json",
                    "response_json_schema": schema.model_json_schema(),
                    "temperature": 0,
                },
            )
        except Exception as error:
            raise GeminiProviderError(
                str(error) or "Gemini request failed",
                status_code=getattr(error, "code", None),
            ) from error
        calls = tuple(
            GeminiFunctionCall(
                name=str(call.name),
                arguments=dict(call.args or {}),
                call_id=getattr(call, "id", None),
            )
            for call in (response.function_calls or ())
        )
        if calls:
            return GeminiTurn(
                function_calls=calls,
                # Gemini 3 tool turns can contain opaque thought signatures. Reuse the SDK
                # object exactly instead of rebuilding only the visible function calls.
                model_content=response.candidates[0].content,
            )
        parsed = response.parsed
        if parsed is not None:
            return GeminiTurn(
                output=parsed,
                model_content=response.candidates[0].content,
            )
        try:
            return GeminiTurn(
                output=json.loads(response.text),
                model_content=response.candidates[0].content,
            )
        except (TypeError, json.JSONDecodeError) as error:
            raise GeminiProviderError("Gemini returned no structured output") from error


class GeminiTranscriptWriter:
    """Append human-inspectable model turns below one harness-owned run directory."""

    def __init__(self, data_root: Path) -> None:
        self._data_root = data_root.resolve()

    def record(
        self,
        *,
        run_id: str,
        phase: str,
        request_number: int,
        prompt: str,
        conversation: Sequence[object],
        turn: GeminiTurn | None = None,
        error: GeminiProviderError | None = None,
    ) -> None:
        """Persist one credential-free provider request/result as JSON Lines."""
        run_root = (self._data_root / run_id).resolve()
        if not run_id or not run_root.is_relative_to(self._data_root):
            raise ValueError("Invalid Run Identifier for Gemini transcript")
        transcript_root = run_root / "model-transcripts"
        transcript_root.mkdir(exist_ok=True)
        entry: dict[str, object] = {
            "request_number": request_number,
            "prompt": prompt,
            "conversation": _jsonable(conversation),
        }
        if turn is not None:
            entry["response"] = {
                "function_calls": [
                    {
                        "name": call.name,
                        "arguments": _jsonable(call.arguments),
                        "id": call.call_id,
                    }
                    for call in turn.function_calls
                ],
                "output": _jsonable(turn.output),
                "model_content": _jsonable(turn.model_content),
            }
        else:
            assert error is not None
            # Provider messages can contain request URLs or SDK details. The stable status and
            # retry classification are sufficient for audit without risking credential leakage.
            entry["provider_error"] = {
                "status_code": error.status_code,
                "transient": error.transient,
            }
        with (transcript_root / f"{phase}.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, sort_keys=True) + "\n")


class GeminiModelGateway:
    """Bounded tool-calling Model Gateway used only with registered synthetic Fixtures."""

    synthetic_only = True
    model_id = _MODEL_ID

    def __init__(
        self,
        transport: GeminiTransport,
        *,
        backoff: Callable[[float], None] = sleep,
        transcript_writer: GeminiTranscriptWriter | None = None,
    ) -> None:
        self._transport = transport
        self._backoff = backoff
        self._transcript_writer = transcript_writer
        self.allowed_fixture_roots = tuple(root.resolve() for root in bundled_fixture_roots())

    @classmethod
    def from_api_key(cls, api_key: str, data_root: Path) -> "GeminiModelGateway":
        """Create the credential-owning client only after explicit Live Smoke opt-in."""
        if not api_key.strip():
            raise ValueError("GEMINI_API_KEY is empty")
        return cls(
            GoogleGenAITransport(api_key),
            transcript_writer=GeminiTranscriptWriter(data_root),
        )

    def create_plan(self, request: PlanningRequest, tools: InspectionTools) -> object:
        prompt = (
            "Create a minimal repair Plan for this synthetic fixture. Inspect only with the "
            f"declared tools.\nIssue: {request.issue}\nVerification argv: "
            f"{json.dumps(request.verification)}{_correction(request.validation_errors)}"
        )
        return self._run(
            prompt,
            Plan,
            tools,
            request.model_requests_remaining,
            run_id=request.run_id,
            phase="planning",
        )

    def create_candidate(self, request: CandidateRequest, tools: InspectionTools) -> object:
        prompt = (
            "Create complete text replacements for the smallest repair. Read every file before "
            "replacing it and use the observed SHA-256. Do not create, delete, or rename files."
            f"\nIssue: {request.issue}\nPlan: {request.plan.model_dump_json()}"
            f"\nEditable paths: {json.dumps(request.editable_paths)}\nAttempt: {request.attempt}"
            f"\nDiagnosis: {request.diagnosis.model_dump_json() if request.diagnosis else 'none'}"
            f"{_correction(request.validation_errors)}"
        )
        return self._run(
            prompt,
            CandidatePatch,
            tools,
            request.model_requests_remaining,
            run_id=request.run_id,
            phase=f"candidate-{request.attempt}",
        )

    def create_diagnosis(self, request: DiagnosisRequest, tools: InspectionTools) -> object:
        prompt = (
            "Diagnose why the synthetic fixture still fails and propose the next incremental "
            f"strategy.\nIssue: {request.issue}\nPlan: {request.plan.model_dump_json()}"
            f"\nAttempt: {request.attempt}\nVerification excerpt: "
            f"{request.verification_output_excerpt}\nVerification artifact: "
            f"{request.verification_artifact_path}{_correction(request.validation_errors)}"
        )
        return self._run(
            prompt,
            Diagnosis,
            tools,
            request.model_requests_remaining,
            run_id=request.run_id,
            phase=f"diagnosis-{request.attempt}",
        )

    def _run(
        self,
        prompt: str,
        schema: type[BaseModel],
        tools: InspectionTools,
        allowance: int,
        *,
        run_id: str,
        phase: str,
    ) -> ModelGatewayResult:
        conversation: list[object] = []
        requests = 0
        while requests < allowance:
            try:
                turn, consumed = self._request_with_retry(
                    prompt=prompt,
                    schema=schema,
                    conversation=conversation,
                    allowance=allowance - requests,
                    run_id=run_id,
                    phase=phase,
                    request_offset=8 - allowance + requests,
                )
            except Exception as error:
                if hasattr(error, "model_requests"):
                    error.model_requests += requests
                raise
            requests += consumed
            if turn.function_calls:
                responses = []
                model_parts = []
                for call in turn.function_calls:
                    try:
                        response_payload = {"result": _execute_tool(call, tools)}
                    except ValueError as error:
                        # Invalid model arguments reveal no workspace data. Return the bounded
                        # host rejection so Gemini can correct the call within the same budget.
                        response_payload = {"error": str(error)}
                    except ResourceBudgetExceededError as error:
                        error.model_requests += requests
                        raise
                    function_call = {"name": call.name, "args": dict(call.arguments)}
                    if call.call_id is not None:
                        function_call["id"] = call.call_id
                    model_parts.append({"function_call": function_call})
                    function_response = {
                        "name": call.name,
                        "response": response_payload,
                    }
                    if call.call_id is not None:
                        function_response["id"] = call.call_id
                    responses.append({"function_response": function_response})
                conversation.extend(
                    [
                        turn.model_content
                        or {"role": "model", "parts": model_parts},
                        {"role": "user", "parts": responses},
                    ]
                )
                continue
            if turn.output is None:
                raise GeminiInconclusiveError(
                    "Gemini returned neither tool calls nor structured output",
                    model_requests=requests,
                )
            return ModelGatewayResult(output=turn.output, model_requests=requests)
        raise ResourceBudgetExceededError(
            budget_name="model_requests",
            budget_limit=8,
            budget_used=8,
            model_requests=requests,
        )

    def _request_with_retry(
        self,
        *,
        prompt: str,
        schema: type[BaseModel],
        conversation: Sequence[object],
        allowance: int,
        run_id: str,
        phase: str,
        request_offset: int,
    ) -> tuple[GeminiTurn, int]:
        consumed = 0
        while consumed < min(3, allowance):
            consumed += 1
            try:
                turn = self._transport.generate(
                    prompt=prompt,
                    schema=schema,
                    conversation=conversation,
                )
                self._record_transcript(
                    run_id=run_id,
                    phase=phase,
                    request_number=request_offset + consumed,
                    prompt=prompt,
                    conversation=conversation,
                    turn=turn,
                    model_requests=consumed,
                )
                return turn, consumed
            except GeminiProviderError as error:
                self._record_transcript(
                    run_id=run_id,
                    phase=phase,
                    request_number=request_offset + consumed,
                    prompt=prompt,
                    conversation=conversation,
                    error=error,
                    model_requests=consumed,
                )
                if not error.transient:
                    raise GeminiInconclusiveError(
                        str(error), model_requests=consumed
                    ) from error
                if consumed >= 3:
                    raise GeminiInconclusiveError(
                        str(error), model_requests=consumed
                    ) from error
                if consumed >= allowance:
                    raise GeminiInconclusiveError(
                        str(error),
                        model_requests=consumed,
                    ) from error
                self._backoff(float(2 ** (consumed - 1)))
        raise AssertionError("Retry loop must return or raise")

    def _record_transcript(
        self,
        *,
        run_id: str,
        phase: str,
        request_number: int,
        prompt: str,
        conversation: Sequence[object],
        turn: GeminiTurn | None = None,
        error: GeminiProviderError | None = None,
        model_requests: int,
    ) -> None:
        if self._transcript_writer is None:
            return
        try:
            self._transcript_writer.record(
                run_id=run_id,
                phase=phase,
                request_number=request_number,
                prompt=prompt,
                conversation=conversation,
                turn=turn,
                error=error,
            )
        except Exception as persistence_error:
            raise GeminiTranscriptPersistenceError(
                model_requests=model_requests
            ) from persistence_error


def _execute_tool(call: GeminiFunctionCall, tools: InspectionTools) -> object:
    match call.name:
        case "list_files":
            return asdict(tools.list_files())
        case "read_file":
            return asdict(tools.read_file(str(call.arguments.get("path", ""))))
        case "search_code":
            return asdict(tools.search_code(str(call.arguments.get("query", ""))))
        case _:
            raise ValueError(f"Gemini requested unknown inspection tool: {call.name}")


def _correction(errors: tuple[str, ...]) -> str:
    return f"\nCorrect these schema validation errors: {json.dumps(errors)}" if errors else ""


def _jsonable(value: object) -> object:
    """Convert provider SDK values to deterministic JSON without credential-bearing reprs."""
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, bytes):
        return {"base64": b64encode(value).decode("ascii")}
    if isinstance(value, BaseModel):
        return _jsonable(value.model_dump(mode="python", by_alias=True, exclude_none=True))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, Sequence):
        return [_jsonable(item) for item in value]
    raise TypeError(f"Unsupported Gemini transcript value: {type(value).__name__}")


_TOOL_DECLARATIONS = [
    {
        "name": "list_files",
        "description": "List visible UTF-8 text files in stable order.",
        "parameters_json_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "read_file",
        "description": "Read one bounded workspace-relative UTF-8 text file.",
        "parameters_json_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "search_code",
        "description": "Search visible text files for a case-sensitive literal.",
        "parameters_json_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
]
