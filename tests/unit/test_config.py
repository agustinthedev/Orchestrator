from pathlib import Path

from orchestrator.config import load_config
from orchestrator.config.loader import configured_secret_names


def test_example_config_loads_multiple_logical_concepts() -> None:
    config = load_config(Path("config"))
    assert config.application.timezone == "America/Montevideo"
    assert config.repository("example-repository").default_branch == "main"
    assert config.project("example-project").repository == "example-repository"
    assert config.schedules[0].workflow == "daily_code_review"


def test_secret_names_are_metadata_only() -> None:
    config = load_config(Path("config"))
    names = configured_secret_names(config)
    assert "TELEGRAM_BOT_TOKEN" in names
    assert all("token" not in name.casefold() or name.isupper() for name in names)

