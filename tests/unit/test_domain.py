from orchestrator.domain import (
    Intent,
    classify_intent,
    paths_are_allowed,
    redact_secrets,
    sanitize_branch_name,
)


def test_branch_name_is_safe_and_namespaced() -> None:
    value = sanitize_branch_name("TRADING FIX 2026/08/03", prefix="fix")
    assert value.startswith("fix/")
    assert " " not in value
    assert ".." not in value


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
