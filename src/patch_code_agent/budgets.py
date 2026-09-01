"""Define fixed MVP Resource Budgets and derive their durable usage."""

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

BudgetName = Literal[
    "repair_attempts",
    "files_read",
    "files_changed",
    "tool_executions",
    "model_requests",
    "verification_seconds",
    "active_seconds",
]


class ResourceBudgetExceededError(RuntimeError):
    """Stop a model/tool request at the host boundary when no allowance remains."""

    def __init__(
        self,
        *,
        budget_name: BudgetName,
        budget_limit: float,
        budget_used: float,
        model_requests: int = 0,
        tool_executions: int = 0,
        files_read: tuple[str, ...] = (),
    ) -> None:
        super().__init__(f"Resource Budget exhausted: {budget_name}")
        self.budget_name = budget_name
        self.budget_limit = budget_limit
        self.budget_used = budget_used
        self.model_requests = model_requests
        self.tool_executions = tool_executions
        self.files_read = files_read

    def record_inspection(
        self,
        *,
        tool_executions: int,
        files_read: tuple[str, ...],
    ) -> None:
        """Attach phase-local usage when a model-request allowance is exhausted."""
        self.tool_executions = tool_executions
        self.files_read = files_read


class CountBudget(BaseModel):
    """Integer limit and current durable usage for a countable resource."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    limit: int = Field(ge=1)
    used: int = Field(ge=0)


class DurationBudget(BaseModel):
    """Seconds limit and current durable usage for a timed resource."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    limit: float = Field(gt=0)
    used: float = Field(ge=0)


class ResourceBudgets(BaseModel):
    """Complete fixed MVP budget view stored or reconstructed from bounded state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    repair_attempts: CountBudget
    files_read: CountBudget
    files_changed: CountBudget
    tool_executions: CountBudget
    model_requests: CountBudget
    verification_seconds: DurationBudget
    active_seconds: DurationBudget

    @classmethod
    def from_state(cls, state: Mapping[str, object]) -> "ResourceBudgets":
        """Derive one consistent budget view from the graph's durable counters."""
        baseline = state.get("baseline_verification")
        repair = state.get("verification")
        verification_used = max(
            _duration_seconds(baseline),
            _duration_seconds(repair),
            float(state.get("verification_duration_max", 0.0)),
        )
        return cls(
            repair_attempts=CountBudget(limit=3, used=int(state.get("attempt", 0))),
            files_read=CountBudget(limit=12, used=len(state.get("files_read", []))),
            files_changed=CountBudget(limit=3, used=len(state.get("files_changed", []))),
            tool_executions=CountBudget(
                limit=20,
                used=int(state.get("tool_executions", 0)),
            ),
            model_requests=CountBudget(
                limit=8,
                used=int(state.get("model_requests", 0)),
            ),
            verification_seconds=DurationBudget(limit=60.0, used=verification_used),
            active_seconds=DurationBudget(
                limit=300.0,
                used=float(state.get("active_duration_seconds", 0.0)),
            ),
        )

    def first_exceeded(self) -> tuple[BudgetName, int | float, int | float] | None:
        """Return the first non-attempt Resource Budget whose usage exceeds its limit."""
        for name in (
            "files_read",
            "files_changed",
            "tool_executions",
            "model_requests",
            "verification_seconds",
            "active_seconds",
        ):
            budget = getattr(self, name)
            if budget.used > budget.limit:
                return name, budget.limit, budget.used
        return None


def _duration_seconds(value: object) -> float:
    if not isinstance(value, Mapping):
        return 0.0
    duration = value.get("duration_seconds", 0.0)
    return float(duration) if isinstance(duration, int | float) else 0.0
