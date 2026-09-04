"""Fixed safety limits for the prototype workflow."""

from typing import Literal

MAX_REPAIR_ATTEMPTS = 3
MAX_FILES_READ = 12
MAX_FILES_CHANGED = 3
MAX_TOOL_EXECUTIONS = 20
MAX_MODEL_REQUESTS = 8

LimitName = Literal["files_read", "files_changed", "tool_executions", "model_requests"]


class RunLimitExceededError(RuntimeError):
    """Stop before a fixed workflow limit is exceeded."""

    def __init__(
        self,
        *,
        limit_name: LimitName,
        limit: int,
        used: int,
        model_requests: int = 0,
        tool_executions: int = 0,
        files_read: tuple[str, ...] = (),
    ) -> None:
        super().__init__(f"Run limit reached: {limit_name} ({used}/{limit})")
        self.limit_name = limit_name
        self.limit = limit
        self.used = used
        self.model_requests = model_requests
        self.tool_executions = tool_executions
        self.files_read = files_read

    def record_inspection(
        self,
        *,
        tool_executions: int,
        files_read: tuple[str, ...],
    ) -> None:
        """Attach phase-local usage before the error crosses the model seam."""
        self.tool_executions = tool_executions
        self.files_read = files_read
