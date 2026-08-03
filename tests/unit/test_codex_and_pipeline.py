from orchestrator.codex.runner import CodexRunner
from orchestrator.config.models import CodexSettings, ModelSpec
from orchestrator.pipelines.analyzer import FailureClass, classify_failure


def test_structured_codex_output_is_validated() -> None:
    parsed = CodexRunner.parse_structured_output('{"result_type":"answer","answer":"safe"}')
    assert parsed is not None
    assert parsed.result_type == "answer"
    assert parsed.answer == "safe"


def test_model_routing_rejects_unconfigured_models() -> None:
    runner = CodexRunner(CodexSettings(default_model=ModelSpec(name="luna", reasoning_effort="high"), allowed_models=["luna"]))
    assert runner.choose_model().name == "luna"


def test_pipeline_failure_classification_uses_evidence() -> None:
    assert classify_failure("pytest AssertionError: expected 2 got 1") == FailureClass.TEST
    assert classify_failure("401 unauthorized credential missing") == FailureClass.CREDENTIALS

