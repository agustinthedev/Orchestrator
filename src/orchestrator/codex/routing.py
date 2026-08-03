from __future__ import annotations

from dataclasses import dataclass

from orchestrator.config.models import CodexSettings, ModelSpec


@dataclass(frozen=True)
class EscalationRequest:
    task: str
    current: ModelSpec
    recommended: ModelSpec
    reason: str
    requires_user_approval: bool = True


class ModelRoutingPolicy:
    """Routes configured work and makes expensive escalation an explicit decision."""

    def __init__(self, settings: CodexSettings) -> None:
        self.settings = settings

    def request_escalation(
        self,
        *,
        task: str,
        current: ModelSpec,
        recommended: ModelSpec,
        reason: str,
    ) -> EscalationRequest | None:
        if not self.settings.escalation_enabled or current == recommended:
            return None
        if recommended.name not in self.settings.allowed_models:
            raise ValueError(f"Escalation model is not allowed: {recommended.name}")
        if recommended.reasoning_effort not in self.settings.allowed_reasoning_efforts:
            raise ValueError(f"Escalation reasoning effort is not allowed: {recommended.reasoning_effort}")
        return EscalationRequest(
            task=task,
            current=current,
            recommended=recommended,
            reason=reason,
            requires_user_approval=self.settings.require_user_approval_for_expensive_models
            or recommended.name in self.settings.expensive_models,
        )

