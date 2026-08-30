from dataclasses import dataclass
from typing import Protocol


class ModelGateway(Protocol):
    """Model dependency owned by the PatchCodeAgent application."""

    @property
    def model_id(self) -> str:
        """Return the stable identifier recorded for this model adapter."""


@dataclass(frozen=True, slots=True)
class ScriptedModel:
    """Deterministic model adapter used while the scaffold has no model calls."""

    model_id: str = "scripted"
