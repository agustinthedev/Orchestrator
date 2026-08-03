from pathlib import Path

from orchestrator.codex.routing import ModelRoutingPolicy
from orchestrator.codex.runner import CodexRunner
from orchestrator.config.models import CodexSettings, ModelSpec
from orchestrator.pipelines.analyzer import FailureClass, classify_failure
from orchestrator.workflows.engine import OrchestratorEngine


def test_prompt_includes_context_once_and_keeps_the_exact_question(tmp_path: Path) -> None:
    (tmp_path / "project_question.md").write_text("Answer directly.\n{{context}}", encoding="utf-8")
    engine = object.__new__(OrchestratorEngine)
    engine.prompts_root = tmp_path

    prompt = engine._prompt("project_question", "User's exact request: ¿Qué hace Treidin?")

    assert prompt.count("User's exact request") == 1
    assert prompt.startswith("Answer directly.")


def test_structured_codex_output_is_validated() -> None:
    parsed = CodexRunner.parse_structured_output('{"result_type":"answer","answer":"safe"}')
    assert parsed is not None
    assert parsed.result_type == "answer"
    assert parsed.answer == "safe"


def test_structured_codex_output_is_extracted_from_jsonl_agent_message() -> None:
    output = '{"type":"item.completed","item":{"type":"agent_message","text":"{\\"result_type\\":\\"answer\\",\\"answer\\":\\"safe\\"}"}}'
    parsed = CodexRunner.parse_structured_output(output)
    assert parsed is not None
    assert parsed.answer == "safe"


def test_model_routing_rejects_unconfigured_models() -> None:
    runner = CodexRunner(CodexSettings(default_model=ModelSpec(name="luna", reasoning_effort="high"), allowed_models=["luna"]))
    assert runner.choose_model().name == "luna"
    assert runner.choose_model(task="daily_code_review").reasoning_effort == "high"
    assert runner.choose_model(task="implementation").reasoning_effort == "extra_high"


def test_pipeline_failure_classification_uses_evidence() -> None:
    assert classify_failure("pytest AssertionError: expected 2 got 1") == FailureClass.TEST
    assert classify_failure("401 unauthorized credential missing") == FailureClass.CREDENTIALS


def test_expensive_model_escalation_is_explicit() -> None:
    settings = CodexSettings(
        default_model=ModelSpec(name="luna", reasoning_effort="high"),
        allowed_models=["luna", "terra"],
        expensive_models=["terra"],
    )
    request = ModelRoutingPolicy(settings).request_escalation(
        task="complex_change",
        current=ModelSpec(name="luna", reasoning_effort="high"),
        recommended=ModelSpec(name="terra", reasoning_effort="extra_high"),
        reason="Multiple shared modules are affected.",
    )
    assert request is not None
    assert request.requires_user_approval
