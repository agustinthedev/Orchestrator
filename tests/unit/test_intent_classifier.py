import openai
import pytest

from orchestrator.domain import Intent
from orchestrator.intent_classifier import (
    IntentClassification,
    NullIntentClassifier,
    OpenAIIntentClassifier,
)


@pytest.mark.asyncio
async def test_null_classifier_is_safe_by_default() -> None:
    result = await NullIntentClassifier().classify(
        "anything",
        project_ids=["project"],
        state=None,
        has_reply_context=False,
    )

    assert result.intent == Intent.UNKNOWN


@pytest.mark.asyncio
async def test_openai_classifier_uses_structured_output(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    class FakeResponses:
        async def parse(self, **kwargs):
            calls.append(kwargs)
            return type(
                "ParsedResponse",
                (),
                {"output_parsed": IntentClassification(intent=Intent.PROJECT_QUESTION, project_id="project", confidence=0.96)},
            )()

    class FakeClient:
        def __init__(self, *, api_key: str) -> None:
            assert api_key == "test-key"
            self.responses = FakeResponses()

    monkeypatch.setenv("TEST_OPENAI_KEY", "test-key")
    monkeypatch.setattr(openai, "AsyncOpenAI", FakeClient)
    classifier = OpenAIIntentClassifier("TEST_OPENAI_KEY", "test-model")

    result = await classifier.classify(
        "Revisa la arquitectura de Treidin",
        project_ids=["project"],
        state=None,
        has_reply_context=False,
    )

    assert result.intent == Intent.PROJECT_QUESTION
    assert calls[0]["model"] == "test-model"
    assert calls[0]["text_format"] is IntentClassification
    assert '"project"' in str(calls[0]["input"])
    assert "Revisa la arquitectura" in str(calls[0]["input"])


@pytest.mark.asyncio
async def test_low_confidence_result_becomes_unknown(monkeypatch) -> None:
    class FakeResponses:
        async def parse(self, **_kwargs):
            return type(
                "ParsedResponse",
                (),
                {"output_parsed": IntentClassification(intent=Intent.FEATURE_REQUEST, confidence=0.2)},
            )()

    class FakeClient:
        def __init__(self, *, api_key: str) -> None:
            assert api_key == "test-key"
            self.responses = FakeResponses()

    monkeypatch.setenv("TEST_OPENAI_KEY", "test-key")
    monkeypatch.setattr(openai, "AsyncOpenAI", FakeClient)
    classifier = OpenAIIntentClassifier("TEST_OPENAI_KEY", "test-model", min_confidence=0.45)

    result = await classifier.classify(
        "Maybe change something",
        project_ids=["project"],
        state=None,
        has_reply_context=False,
    )

    assert result.intent == Intent.UNKNOWN
    assert result.project_id is None
