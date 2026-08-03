from pathlib import Path

from orchestrator.config import load_config
from orchestrator.config.loader import configured_secret_names


def test_config_loads_treidin_project_setup() -> None:
    config = load_config(Path("config"))
    assert config.application.timezone == "America/Montevideo"
    assert config.repository("treidin-repository").default_branch == "main"
    assert config.project("Treidin").repository == "treidin-repository"
    assert config.schedules == []


def test_secret_names_are_metadata_only() -> None:
    config = load_config(Path("config"))
    names = configured_secret_names(config)
    assert "TELEGRAM_BOT_TOKEN" in names
    assert all("token" not in name.casefold() or name.isupper() for name in names)


def test_concrete_config_files_take_precedence_over_examples(tmp_path: Path) -> None:
    (tmp_path / "projects").mkdir()
    (tmp_path / "orchestrator.example.yaml").write_text(
        "application:\n  name: example\n", encoding="utf-8"
    )
    (tmp_path / "orchestrator.yaml").write_text(
        "application:\n  name: actual\n", encoding="utf-8"
    )
    (tmp_path / "repositories.example.yaml").write_text(
        "repositories:\n  - id: example\n    display_name: Example\n    local_path: C:/example\n",
        encoding="utf-8",
    )
    (tmp_path / "repositories.yaml").write_text(
        "repositories:\n  - id: actual\n    display_name: Actual\n    local_path: C:/actual\n",
        encoding="utf-8",
    )
    (tmp_path / "projects" / "example-project.example.yaml").write_text(
        "projects:\n  - id: example-project\n    display_name: Example\n    repository: example\n",
        encoding="utf-8",
    )
    (tmp_path / "projects" / "actual.yaml").write_text(
        "projects:\n  - id: actual-project\n    display_name: Actual\n    repository: actual\n",
        encoding="utf-8",
    )

    config = load_config(tmp_path)

    assert config.application.name == "actual"
    assert [repository.id for repository in config.repositories] == ["actual"]
    assert [project.id for project in config.projects] == ["actual-project"]
