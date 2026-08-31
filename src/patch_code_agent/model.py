"""Define the narrow model dependency used by the application layer.

The current milestone does not call a model yet, so the protocol exposes only stable adapter
identity. Later planning/editing work can deepen this boundary without coupling graph nodes to a
specific provider SDK. ``ScriptedModel`` remains deterministic for required acceptance tests.
"""

from dataclasses import dataclass
from typing import Protocol


class ModelGateway(Protocol):
    """Model dependency owned by the PatchCodeAgent application."""

    @property
    def model_id(self) -> str:
        """Return the stable identifier recorded for this model adapter."""


@dataclass(frozen=True, slots=True)
class ScriptedModel:
    """Deterministic model adapter used while the scaffold has no model calls.

    Attributes:
        model_id: Stable adapter identifier that future Run Reports can record.

    Example:
        >>> model = ScriptedModel()
        >>> model.model_id
        'scripted'
    """

    model_id: str = "scripted"
