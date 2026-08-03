from __future__ import annotations

import json
import os
from collections.abc import Sequence
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from orchestrator.domain import Intent
from orchestrator.observability.logging import get_logger

logger = get_logger(__name__)


class IntentClassification(BaseModel):
    """The only model output the Telegram gateway needs for routing."""

    model_config = ConfigDict(extra="ignore")

    intent: Intent
    project_id: str | None = None
    confidence: float = Field(ge=0, le=1)
    reason: str | None = None


class IntentClassifier(Protocol):
    async def classify(
        self,
        text: str,
        *,
        project_ids: Sequence[str],
        state: str | None,
        has_reply_context: bool,
    ) -> IntentClassification:
        """Classify a user message without executing any requested action."""


CLASSIFIER_INSTRUCTIONS = """
You classify one incoming Telegram message for a repository orchestration gateway.
Return only the structured classification requested by the response schema. Never
execute, propose, or describe an action beyond the classification.

Choose exactly one intent:
- GLOBAL_QUESTION: asks about the orchestrator, its runtime, credentials, scheduling, or general operation.
- PROJECT_QUESTION: asks about a configured project's architecture, behavior, files, modules, or repository.
- CODE_ANALYSIS: asks to inspect or analyze code without requesting a change.
- PIPELINE_ANALYSIS: asks about CI, builds, workflows, pipeline runs, or pipeline failures.
- FEATURE_REQUEST: asks to add or implement behavior.
- FIX_REQUEST: asks to fix a bug, error, failure, or incorrect behavior.
- REFACTOR_REQUEST: asks to restructure or clean up code without changing its intended behavior.
- TEST_REQUEST: asks to add, update, run, or improve tests or coverage.
- APPROVE_PROPOSAL: approves a pending implementation proposal.
- REJECT_PROPOSAL: rejects a pending implementation proposal.
- ASK_ABOUT_CHANGE: asks for an explanation of an existing change, diff, or implementation.
- REQUEST_DIFF_DETAIL: asks for more detailed patch or diff information.
- REQUEST_REVISION: asks to modify or revise an existing proposed change, usually in reply to a job.
- APPROVE_PUSH: explicitly authorizes pushing the exact approved HEAD and creating the draft PR.
- REJECT_PUSH: explicitly refuses or cancels a pending push approval.
- CANCEL_JOB: explicitly cancels or stops a pending job.
- SHOW_STATUS: asks for current job, worker, or service status.
- UNKNOWN: ambiguous, conversational, or unrelated text that cannot be routed safely.

Select project_id only from the supplied configured project IDs. Use null when the
message is not project-specific or no project can be identified. Treat the message
language and accents naturally; do not require exact keywords. Use the current job
state and reply-context flag to interpret short replies such as approvals.
""".strip()


class NullIntentClassifier:
    async def classify(
        self,
        _text: str,
        *,
        project_ids: Sequence[str],
        state: str | None,
        has_reply_context: bool,
    ) -> IntentClassification:
        del project_ids, state, has_reply_context
        return IntentClassification(intent=Intent.UNKNOWN, confidence=0, reason="classifier_disabled")


class OpenAIIntentClassifier:
    def __init__(
        self,
        api_key_env: str,
        model: str,
        *,
        min_confidence: float = 0.45,
        max_output_tokens: int = 250,
    ) -> None:
        self.api_key_env = api_key_env
        self.model = model
        self.min_confidence = min_confidence
        self.max_output_tokens = max_output_tokens

    async def classify(
        self,
        text: str,
        *,
        project_ids: Sequence[str],
        state: str | None,
        has_reply_context: bool,
    ) -> IntentClassification:
        api_key = os.getenv(self.api_key_env)
        if not api_key:
            logger.warning(
                "Intent classification disabled because its API key is absent",
                extra={"event_type": "INTENT_CLASSIFICATION_CREDENTIAL_MISSING"},
            )
            return IntentClassification(intent=Intent.UNKNOWN, confidence=0, reason="api_key_missing")

        try:
            from openai import AsyncOpenAI

            client = AsyncOpenAI(api_key=api_key)
            response = await client.responses.parse(
                model=self.model,
                instructions=CLASSIFIER_INSTRUCTIONS,
                input=self._build_input(text, project_ids, state, has_reply_context),
                text_format=IntentClassification,
                max_output_tokens=self.max_output_tokens,
            )
            parsed = response.output_parsed
            if parsed is None:
                raise ValueError("OpenAI returned no parsed intent classification")
            if parsed.confidence < self.min_confidence:
                return parsed.model_copy(
                    update={
                        "intent": Intent.UNKNOWN,
                        "project_id": None,
                        "reason": "below_confidence_threshold",
                    }
                )
            return parsed
        except Exception:
            logger.exception("Intent classification failed", extra={"event_type": "INTENT_CLASSIFICATION_FAILURE"})
            return IntentClassification(intent=Intent.UNKNOWN, confidence=0, reason="classification_failed")

    @staticmethod
    def _build_input(
        text: str,
        project_ids: Sequence[str],
        state: str | None,
        has_reply_context: bool,
    ) -> str:
        context = {
            "configured_project_ids": list(project_ids),
            "current_job_state": state,
            "has_reply_context": has_reply_context,
        }
        return f"Context:\n{json.dumps(context, ensure_ascii=False)}\n\nMessage:\n---\n{text}\n---"
