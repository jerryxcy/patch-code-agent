"""Define typed model outputs and the replaceable Model Gateway seam."""

import hashlib
from dataclasses import dataclass
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from patch_code_agent.inspection import InspectionTools
from patch_code_agent.sources import RelativeSourcePath, VerificationArgv

PlanText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4096)]
DiagnosisText = PlanText
ContentHash = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class Plan(BaseModel):
    """Runtime-validated model understanding produced before a Candidate Patch.

    Attributes:
        issue_summary: Concise explanation of the defect or requested change.
        relevant_files: One to twelve source paths connected to the Issue.
        repair_strategy: Intended smallest repair, without directly writing files.
        verification_strategy: How the declared Verification will demonstrate success.

    Example:
        >>> Plan(
        ...     issue_summary="Discount is subtracted instead of multiplied",
        ...     relevant_files=("cart.py", "test_cart.py"),
        ...     repair_strategy="Apply the discount to the subtotal.",
        ...     verification_strategy="Run pytest test_cart.py.",
        ... ).relevant_files
        ('cart.py', 'test_cart.py')
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    issue_summary: PlanText
    relevant_files: tuple[RelativeSourcePath, ...] = Field(min_length=1, max_length=12)
    repair_strategy: PlanText
    verification_strategy: PlanText


@dataclass(frozen=True, slots=True)
class PlanningRequest:
    """Bounded context passed to a model for the single Plan request.

    Attributes:
        issue: Validated Issue text from the source-neutral Patch Run Contract.
        verification: Controlled argv the Plan should use as its success criterion.
    """

    issue: str
    verification: VerificationArgv


class FileReplacement(BaseModel):
    """One complete text-file replacement proposed by a model.

    The host treats this as untrusted data. ``expected_sha256`` must identify the exact UTF-8
    content returned by a preceding model read, while ``new_content`` contains the complete
    replacement rather than a model-generated diff.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: RelativeSourcePath
    expected_sha256: ContentHash
    new_content: str


class CandidatePatch(BaseModel):
    """Bounded structured model proposal validated before host diff generation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    replacements: tuple[FileReplacement, ...] = Field(min_length=1, max_length=3)


class Diagnosis(BaseModel):
    """Runtime-validated explanation of one failed Repair Attempt.

    Attributes:
        failure_summary: Concise explanation of what Verification still reports.
        evidence: Specific bounded evidence taken from Verification or workspace inspection.
        next_strategy: Incremental change the next Candidate Patch should make.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    failure_summary: DiagnosisText
    evidence: DiagnosisText
    next_strategy: DiagnosisText


@dataclass(frozen=True, slots=True)
class DiagnosisRequest:
    """Bounded failure context supplied for a Diagnosis model request."""

    issue: str
    plan: Plan
    attempt: int
    verification_output_excerpt: str
    verification_artifact_path: str


@dataclass(frozen=True, slots=True)
class CandidateRequest:
    """Context supplied for one Candidate Patch request.

    ``editable_paths`` informs the model of the contract allowlist but is never trusted as
    enforcement: the host validates every returned replacement independently.
    """

    issue: str
    plan: Plan
    editable_paths: tuple[RelativeSourcePath, ...]
    attempt: int
    diagnosis: Diagnosis | None = None


class ModelGateway(Protocol):
    """Model adapter that can inspect only through host-provided tools."""

    @property
    def model_id(self) -> str:
        """Return the stable identifier recorded for this model adapter."""

    def create_plan(self, request: PlanningRequest, tools: InspectionTools) -> object:
        """Use bounded inspection and return untrusted structured Plan data."""

    def create_candidate(self, request: CandidateRequest, tools: InspectionTools) -> object:
        """Use bounded inspection and return untrusted structured replacements."""

    def create_diagnosis(self, request: DiagnosisRequest, tools: InspectionTools) -> object:
        """Use bounded failure evidence and return an untrusted structured Diagnosis."""


@dataclass(frozen=True, slots=True)
class ScriptedInspectionCall:
    """One deterministic tool request issued by ``ScriptedModel`` in a security scenario.

    Attributes:
        operation: Host tool to invoke: ``list``, ``read``, or ``search``.
        argument: Required path/query for read/search; unused for list.

    Example:
        >>> ScriptedInspectionCall("read", "cart.py").argument
        'cart.py'
    """

    operation: Literal["list", "read", "search"]
    argument: str | None = None


@dataclass(frozen=True, slots=True)
class ScriptedModel:
    """Deterministic offline adapter that exercises the real inspection interface.

    Attributes:
        model_id: Stable adapter identifier recorded with the Plan.
        inspection_calls: Optional explicit script for security scenarios. ``None`` selects the
            normal list/read/search workflow.
        repair_failures: Number of deterministic failing Repair Attempts before success.

    Example:
        >>> ScriptedModel().model_id
        'scripted'
    """

    model_id: str = "scripted"
    inspection_calls: tuple[ScriptedInspectionCall, ...] | None = None
    repair_failures: int = 0

    def create_plan(self, request: PlanningRequest, tools: InspectionTools) -> object:
        """Perform a stable inspection script and return a schema-shaped Plan."""
        if self.inspection_calls is None:
            listed = tools.list_files()
            relevant_files = tuple(path for path in listed.paths if path.endswith(".py"))[:2]
            for path in relevant_files:
                tools.read_file(path)
            tools.search_code("discount")
        else:
            observed_paths: list[str] = []
            for call in self.inspection_calls:
                match call.operation:
                    case "list":
                        observed_paths.extend(tools.list_files().paths)
                    case "read":
                        if call.argument is None:
                            raise ValueError("Scripted read requires a path")
                        observed_paths.append(tools.read_file(call.argument).path)
                    case "search":
                        if call.argument is None:
                            raise ValueError("Scripted search requires a query")
                        tools.search_code(call.argument)
            relevant_files = tuple(dict.fromkeys(observed_paths)) or ("cart.py",)
        first_line = next(line for line in request.issue.splitlines() if line.strip())
        return {
            "issue_summary": first_line.removeprefix("# ").strip(),
            "relevant_files": relevant_files,
            "repair_strategy": "Apply the smallest change that addresses the reported Issue.",
            "verification_strategy": f"Run: {' '.join(request.verification)}",
        }

    def create_candidate(self, request: CandidateRequest, tools: InspectionTools) -> object:
        """Read one editable file and return a deterministic complete replacement."""
        path = request.editable_paths[0]
        observed = tools.read_file(path)
        old_content = observed.content
        incorrect_line = "    return sum(prices) - discount\n"
        corrected_lines = "    subtotal = sum(prices)\n    return subtotal * (1 - discount)\n"
        corrected_return = "    return subtotal * (1 - discount)\n"
        failed_line = "    return subtotal - discount\n"
        if request.attempt <= self.repair_failures:
            if incorrect_line in old_content:
                new_content = old_content.replace(
                    incorrect_line,
                    "    subtotal = sum(prices)\n" + failed_line,
                    1,
                )
            else:
                separator = "" if old_content.endswith("\n") else "\n"
                new_content = (
                    f"{old_content}{separator}# Scripted failing attempt {request.attempt}\n"
                )
        elif incorrect_line in old_content:
            new_content = old_content.replace(incorrect_line, corrected_lines, 1)
        elif failed_line in old_content:
            new_content = old_content.replace(failed_line, corrected_return, 1)
        else:
            separator = "" if old_content.endswith("\n") else "\n"
            new_content = f"{old_content}{separator}# Scripted Candidate Patch\n"
        return {
            "replacements": [
                {
                    "path": path,
                    "expected_sha256": hashlib.sha256(old_content.encode("utf-8")).hexdigest(),
                    "new_content": new_content,
                }
            ]
        }

    def create_diagnosis(self, request: DiagnosisRequest, tools: InspectionTools) -> object:
        """Inspect the current failing state and explain the next incremental repair."""
        listed = tools.list_files()
        editable = next((path for path in listed.paths if path.endswith(".py")), "cart.py")
        observed = tools.read_file(editable)
        return {
            "failure_summary": f"Repair Attempt {request.attempt} still fails Verification.",
            "evidence": request.verification_output_excerpt or observed.content[:256],
            "next_strategy": "Correct the remaining calculation in the current workspace state.",
        }
