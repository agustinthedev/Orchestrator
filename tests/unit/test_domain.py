from orchestrator.domain import (
    Intent,
    classify_intent,
    descriptive_branch_label,
    descriptive_commit_message,
    paths_are_allowed,
    redact_secrets,
    sanitize_branch_name,
)


def test_branch_name_is_safe_and_namespaced() -> None:
    value = sanitize_branch_name("TRADING FIX 2026/08/03", prefix="fix")
    assert value.startswith("fix/")
    assert " " not in value
    assert ".." not in value


def test_descriptive_branch_label_uses_the_requested_file() -> None:
    assert descriptive_branch_label("Hacé los cambios solo en el archivo README") == "update-readme"


def test_descriptive_commit_message_uses_the_actual_change() -> None:
    subject = descriptive_commit_message("Hacé los cambios solo en el archivo README", ["README.md"])
    assert subject == "docs: update README documentation"
    assert "orchestrator" not in subject.casefold()
    assert "codex" not in subject.casefold()


def test_scope_enforcement_blocks_forbidden_and_unlisted_paths() -> None:
    allowed, violations = paths_are_allowed(
        ["src/calculator.py", ".env", "docs/readme.md"],
        allowed_write_paths=["src"],
        forbidden_paths=[".env"],
    )
    assert not allowed
    assert set(violations) == {".env", "docs/readme.md"}


def test_intent_classification_never_treats_explanation_as_push() -> None:
    assert classify_intent("Why did you push this?", has_reply_context=True) == Intent.ASK_ABOUT_CHANGE
    assert classify_intent("Push it", has_reply_context=True) == Intent.APPROVE_PUSH
    assert classify_intent("Review the latest nightly pipeline") == Intent.PIPELINE_ANALYSIS
    assert classify_intent("Analiza el estado actual del proyecto Treidin") == Intent.PROJECT_QUESTION


def test_intent_classification_understands_spanish_change_reply() -> None:
    text = "¿Podrías hacer ese cambio, específicamente solo la actualización del README?"

    assert classify_intent(text, has_reply_context=True) == Intent.REQUEST_REVISION


def test_secret_redaction() -> None:
    assert "super-secret" not in redact_secrets("token=super-secret", ["super-secret"])
    assert "[REDACTED]" in redact_secrets("api_key=abc123")


def test_redacts_telegram_bot_urls_and_bearer_tokens() -> None:
    bot_path = "/bot" + "1234567890" + ":" + "abcdefghijklmnopqrstuvwxyz_123456"
    message = f"POST https://api.telegram.org{bot_path}/sendMessage Authorization: Bearer abc.def.ghi"
    redacted = redact_secrets(message)
    assert "1234567890:" not in redacted
    assert "abc.def.ghi" not in redacted
    assert "[REDACTED]" in redacted
