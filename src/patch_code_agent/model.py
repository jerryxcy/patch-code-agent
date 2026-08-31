"""Define typed planning output and the replaceable Model Gateway seam."""

from dataclasses import dataclass
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from patch_code_agent.inspection import InspectionTools
from patch_code_agent.sources import RelativeSourcePath, VerificationArgv

PlanText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4096)]


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


class ModelGateway(Protocol):
    """Model adapter that can inspect only through host-provided tools."""

    @property
    def model_id(self) -> str:
        """Return the stable identifier recorded for this model adapter."""

    def create_plan(self, request: PlanningRequest, tools: InspectionTools) -> object:
        """Use bounded inspection and return untrusted structured Plan data."""


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

    Example:
        >>> ScriptedModel().model_id
        'scripted'
    """

    model_id: str = "scripted"
    inspection_calls: tuple[ScriptedInspectionCall, ...] | None = None

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
