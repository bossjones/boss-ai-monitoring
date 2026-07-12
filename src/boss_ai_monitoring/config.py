"""Configuration for boss-ai-monitoring.

Precedence (G4): env vars (``BAM_`` prefix, ``__`` nesting) > YAML file > code defaults.

This module is the ONLY place allowed to read the process environment. Everything else takes a
:class:`BamSettings` instance. The YAML file is optional; a malformed one is a startup error that
names the file and the line, because a silently-ignored config is worse than a crash.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_CONFIG_FILENAME = "config.yaml"
CONFIG_PATH_ENV_VAR = "BAM_CONFIG"


class ConfigError(RuntimeError):
    """Raised when a config file exists but cannot be used."""


def _as_absolute_path(value: Any) -> Any:
    if value is None:
        return value
    path = Path(str(value)).expanduser()
    # Deliberately not .resolve(): it would rewrite /tmp to /private/tmp on macOS and make the
    # path we print from `bam config db-path` differ from the one the user configured.
    return path if path.is_absolute() else Path.cwd() / path


class ServerSettings(BaseModel):
    """Both binds are served from ONE FastAPI app by `bam serve` (see cli.py)."""

    dashboard_port: int = 8000
    dashboard_bind: str = "127.0.0.1"
    otlp_port: int = 4318
    otlp_bind: str = "127.0.0.1"


class StoreSettings(BaseModel):
    # validate_default keeps the before-validator honest for the default too, so db_path is
    # always absolute — `bam config db-path` must never print a literal "~".
    model_config = ConfigDict(validate_default=True)

    db_path: Path = Path("~/.local/share/boss-ai-monitoring/bam.duckdb")
    batch_size: int = 500
    flush_interval_ms: int = 1000
    # Where `bam snapshot` drops consistent copies for the duckdb CLI / marimo to read while the
    # app keeps running (OQ-05). The server picks the filename; callers never supply a path.
    #
    # Defaults to `<db_path>.parent / "snapshots"` so it FOLLOWS the database. A hardcoded
    # home-relative default would break Docker: the DB lives on the `bam_data:/data` volume
    # (`BAM_STORE__DB_PATH=/data/bam.duckdb`), so snapshots would land on the container's
    # ephemeral filesystem, outside the volume, and vanish on restart.
    snapshot_dir: Path = Path("~/.local/share/boss-ai-monitoring/snapshots")

    @model_validator(mode="before")
    @classmethod
    def _snapshot_dir_follows_db_path(cls, data: Any) -> Any:
        """Derive the default BEFORE field validation, so `snapshot_dir` stays a plain `Path`.

        Doing this in an `after` validator would force the field to be `Path | None`, leaking an
        Optional into every consumer for no reason.
        """
        if not isinstance(data, dict) or data.get("snapshot_dir"):
            return data
        db_path = data.get("db_path") or cls.model_fields["db_path"].default
        return {**data, "snapshot_dir": Path(_as_absolute_path(db_path)).parent / "snapshots"}

    @field_validator("db_path", "snapshot_dir", mode="before")
    @classmethod
    def _absolute(cls, value: Any) -> Any:
        return _as_absolute_path(value)


class IngestSettings(BaseModel):
    model_config = ConfigDict(validate_default=True)

    jsonl_scan_interval_s: int = 15
    claude_projects_dir: Path = Path("~/.claude/projects")
    langsmith_poll_interval_s: int = 60
    langsmith_project: str | None = None
    otlp_max_body_bytes: int = 10 * 1024 * 1024

    @field_validator("claude_projects_dir", mode="before")
    @classmethod
    def _absolute(cls, value: Any) -> Any:
        return _as_absolute_path(value)


class JobsSettings(BaseModel):
    correction_scan_enabled: bool = True
    drift_check_enabled: bool = True
    error_classification_enabled: bool = True
    interval_s: int = 300
    jitter_s: int = 30


class BamSettings(BaseSettings):
    """Top-level settings. Construct via :func:`load_settings`, not directly, in app code."""

    model_config = SettingsConfigDict(
        env_prefix="BAM_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    server: ServerSettings = Field(default_factory=ServerSettings)
    store: StoreSettings = Field(default_factory=StoreSettings)
    ingest: IngestSettings = Field(default_factory=IngestSettings)
    jobs: JobsSettings = Field(default_factory=JobsSettings)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: Any,
        env_settings: Any,
        dotenv_settings: Any,
        file_secret_settings: Any,
    ) -> tuple[Any, ...]:
        # Highest priority first. YAML arrives as init kwargs from load_settings(), so putting
        # env ahead of init is what makes env > YAML > defaults true.
        return (env_settings, init_settings, dotenv_settings, file_secret_settings)


def resolve_config_path(config_path: Path | str | None = None) -> Path:
    """Which YAML file we would read: explicit arg > ``$BAM_CONFIG`` > ``./config.yaml``."""
    if config_path is not None:
        return Path(config_path).expanduser()
    from_env = os.environ.get(CONFIG_PATH_ENV_VAR)
    if from_env:
        return Path(from_env).expanduser()
    return Path.cwd() / DEFAULT_CONFIG_FILENAME


def _load_yaml(path: Path) -> dict[str, Any]:
    """Parse the YAML file into a mapping. Missing file -> {}. Bad file -> ConfigError."""
    if not path.is_file():
        return {}

    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f"line {mark.line + 1}, column {mark.column + 1}" if mark else "line unknown"
        problem = getattr(exc, "problem", str(exc))
        raise ConfigError(f"Malformed YAML in {path} at {where}: {problem}") from exc
    except OSError as exc:
        raise ConfigError(f"Cannot read config file {path}: {exc}") from exc

    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(
            f"Config file {path} must contain a YAML mapping at the top level "
            f"(line 1), got {type(raw).__name__}."
        )
    return raw


# Flat env aliases documented in the sample env file. The nested spelling (BAM_STORE__DB_PATH)
# is canonical and outranks these, because it comes from the env source proper while these are
# folded in as init kwargs.
FLAT_ENV_ALIASES: dict[str, tuple[str, str]] = {
    "BAM_DB_PATH": ("store", "db_path"),
    "BAM_CLAUDE_PROJECTS_DIR": ("ingest", "claude_projects_dir"),
}


def _flat_alias_overrides() -> dict[str, dict[str, Any]]:
    overrides: dict[str, dict[str, Any]] = {}
    for env_var, (section, key) in FLAT_ENV_ALIASES.items():
        value = os.environ.get(env_var, "").strip()
        if value:  # an empty alias (the sample file ships `BAM_DB_PATH=`) means "not set"
            overrides.setdefault(section, {})[key] = value
    return overrides


def load_settings(config_path: Path | str | None = None) -> BamSettings:
    """Build settings from env > YAML > defaults."""
    path = resolve_config_path(config_path)
    data = _load_yaml(path)

    for section, values in _flat_alias_overrides().items():
        section_data = data.get(section)
        merged = dict(section_data) if isinstance(section_data, dict) else {}
        merged.update(values)
        data[section] = merged

    return BamSettings(**data)
