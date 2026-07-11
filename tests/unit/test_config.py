"""RED-first tests for config.py: precedence, nesting, and the malformed-YAML error path.

Precedence contract (G4): env (BAM_ prefix, __ nesting) > YAML > code defaults.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from boss_ai_monitoring.config import BamSettings, ConfigError, load_settings


def test_defaults_apply_with_no_yaml_and_no_env(tmp_path: Path) -> None:
    settings = load_settings(config_path=tmp_path / "absent.yaml")

    assert settings.server.dashboard_port == 8000
    assert settings.server.otlp_port == 4318
    assert settings.server.dashboard_bind == "127.0.0.1"
    assert settings.server.otlp_bind == "127.0.0.1"
    assert settings.ingest.jsonl_scan_interval_s == 15
    assert settings.ingest.langsmith_poll_interval_s == 60
    assert settings.jobs.correction_scan_enabled is True


def test_missing_yaml_file_is_not_an_error(tmp_path: Path) -> None:
    settings = load_settings(config_path=tmp_path / "nope" / "config.yaml")

    assert isinstance(settings, BamSettings)


def test_yaml_overrides_defaults(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        "server:\n"
        "  dashboard_port: 9001\n"
        "store:\n"
        "  db_path: /tmp/from-yaml.duckdb\n"
        "jobs:\n"
        "  drift_check_enabled: false\n"
    )

    settings = load_settings(config_path=config)

    assert settings.server.dashboard_port == 9001
    assert settings.store.db_path == Path("/tmp/from-yaml.duckdb")
    assert settings.jobs.drift_check_enabled is False
    # Untouched keys still fall through to defaults.
    assert settings.server.otlp_port == 4318


def test_env_beats_yaml_with_double_underscore_nesting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("server:\n  dashboard_port: 9001\n  otlp_port: 9002\n")
    monkeypatch.setenv("BAM_SERVER__DASHBOARD_PORT", "7777")

    settings = load_settings(config_path=config)

    assert settings.server.dashboard_port == 7777  # env wins
    assert settings.server.otlp_port == 9002  # YAML still wins over the default


def test_env_beats_default_when_no_yaml_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BAM_STORE__DB_PATH", str(tmp_path / "env.duckdb"))

    settings = load_settings(config_path=tmp_path / "absent.yaml")

    assert settings.store.db_path == tmp_path / "env.duckdb"


def test_bam_config_env_var_selects_the_yaml_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "elsewhere.yaml"
    config.write_text("server:\n  dashboard_port: 8123\n")
    monkeypatch.setenv("BAM_CONFIG", str(config))

    settings = load_settings()

    assert settings.server.dashboard_port == 8123


def test_malformed_yaml_raises_a_clear_error_naming_file_and_line(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("server:\n  dashboard_port: 8000\n   bad_indent: oops\n")

    with pytest.raises(ConfigError) as excinfo:
        load_settings(config_path=config)

    message = str(excinfo.value)
    assert str(config) in message
    assert "line" in message.lower()


def test_yaml_that_is_not_a_mapping_raises_config_error(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("- just\n- a\n- list\n")

    with pytest.raises(ConfigError) as excinfo:
        load_settings(config_path=config)

    assert str(config) in str(excinfo.value)


def test_flat_bam_db_path_alias_is_honored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """BAM_DB_PATH is the documented flat alias for BAM_STORE__DB_PATH."""
    monkeypatch.setenv("BAM_DB_PATH", str(tmp_path / "flat.duckdb"))

    settings = load_settings(config_path=tmp_path / "absent.yaml")

    assert settings.store.db_path == tmp_path / "flat.duckdb"


def test_flat_claude_projects_dir_alias_is_honored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BAM_CLAUDE_PROJECTS_DIR", str(tmp_path / "projects"))

    settings = load_settings(config_path=tmp_path / "absent.yaml")

    assert settings.ingest.claude_projects_dir == tmp_path / "projects"


def test_nested_env_var_beats_flat_alias(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BAM_DB_PATH", str(tmp_path / "flat.duckdb"))
    monkeypatch.setenv("BAM_STORE__DB_PATH", str(tmp_path / "nested.duckdb"))

    settings = load_settings(config_path=tmp_path / "absent.yaml")

    assert settings.store.db_path == tmp_path / "nested.duckdb"


def test_empty_flat_alias_is_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An unset-but-present alias (the sample file ships `BAM_DB_PATH=`) must not blank the path."""
    monkeypatch.setenv("BAM_DB_PATH", "")

    settings = load_settings(config_path=tmp_path / "absent.yaml")

    assert (
        settings.store.db_path == Path("~/.local/share/boss-ai-monitoring/bam.duckdb").expanduser()
    )


def test_db_path_expands_user_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BAM_STORE__DB_PATH", "~/bam-home.duckdb")

    settings = load_settings()

    assert settings.store.db_path == Path.home() / "bam-home.duckdb"
    assert settings.store.db_path.is_absolute()
