"""Validate structured model output with one bounded schema-correction request."""

from collections.abc import Callable

from pydantic import BaseModel, ValidationError


class InvalidModelOutputError(RuntimeError):
    """Carry actual request and inspection usage after both typed outputs fail."""

    def __init__(self, validation_error: ValidationError, *, model_requests: int) -> None:
        super().__init__("Model output remained invalid after one schema-correction request")
        self.validation_error = validation_error
        self.model_requests = model_requests
        self.tool_executions = 0
        self.files_read: tuple[str, ...] = ()

    def record_inspection(
        self,
        *,
        tool_executions: int,
        files_read: tuple[str, ...],
    ) -> None:
        """Attach host-counted tool usage before the exception crosses the builder seam."""
        self.tool_executions = tool_executions
        self.files_read = files_read


class ModelInvocationError(RuntimeError):
    """Carry actual usage when model infrastructure fails before typed output exists."""

    def __init__(self, cause: Exception, *, model_requests: int) -> None:
        super().__init__(str(cause) or "Model request failed")
        self.cause = cause
        self.model_requests = model_requests
        self.tool_executions = 0
        self.files_read: tuple[str, ...] = ()

    def record_inspection(
        self,
        *,
        tool_executions: int,
        files_read: tuple[str, ...],
    ) -> None:
        """Attach host-counted tool usage before the exception crosses the builder seam."""
        self.tool_executions = tool_executions
        self.files_read = files_read


def request_typed_output[StructuredModel: BaseModel](
    request: Callable[[], object],
    schema: type[StructuredModel],
) -> tuple[StructuredModel, int]:
    """Request at most twice and return the validated value plus actual request count."""
    last_error: ValidationError | None = None
    for model_requests in (1, 2):
        try:
            raw = request()
        except ValueError:
            # Host inspection-policy violations already have stable CLI errors and must not be
            # reclassified as failures of the model provider or transport.
            raise
        except Exception as error:
            raise ModelInvocationError(error, model_requests=model_requests) from error
        try:
            return schema.model_validate(raw), model_requests
        except ValidationError as error:
            last_error = error
    assert last_error is not None
    raise InvalidModelOutputError(last_error, model_requests=2)
