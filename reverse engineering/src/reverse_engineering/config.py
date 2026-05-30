"""Configuration loaded from parent project .env file.

Reads AWS credentials, Polygon RPC, and storage paths from
c:\\users\\avych\\pm-research\\.env. All fields required for S3 sync and EC2
health-check operations.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_PARENT_ENV = Path(__file__).parents[3] / ".env"


class Settings(BaseSettings):
    """Runtime configuration sourced from parent .env file."""

    model_config = SettingsConfigDict(
        env_file=str(_PARENT_ENV),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # AWS
    aws_access_key_id: str = Field(default="")
    aws_secret_access_key: str = Field(default="")
    aws_default_region: str = Field(default="eu-west-1")
    s3_bucket: str = Field(default="pm-research-data")

    # Polygon RPC (used to derive block timestamps)
    polygon_https_url: str = Field(default="")

    # Target — identifies the bot/strategy under analysis.
    # All analysis artifacts (tables, plots, results, models) are scoped
    # to output/<target>/. The shared feed cache is at output/cache/.
    target: str = Field(default="ohanism")

    # Local paths
    @property
    def cache_dir(self) -> Path:
        """Local Parquet cache synced from S3 — SHARED across all targets."""
        return Path(__file__).parents[2] / "output" / "cache"

    @property
    def output_dir(self) -> Path:
        """Target-scoped output root: output/<target>/"""
        return Path(__file__).parents[2] / "output" / self.target

    @property
    def tables_dir(self) -> Path:
        """Target-scoped tables directory."""
        return self.output_dir / "tables"

    @property
    def models_dir(self) -> Path:
        """Target-scoped models directory (gitignored; hash+S3 URI in DECISIONS)."""
        return self.output_dir / "models"

    @property
    def plots_dir(self) -> Path:
        """Target-scoped plots directory."""
        return self.output_dir / "plots"

    @property
    def results_dir(self) -> Path:
        """Target-scoped results directory."""
        return self.output_dir / "results"

    @property
    def s3_prefix(self) -> str:
        """S3 prefix for raw feed data."""
        return "raw"

    @property
    def s3_re_prefix(self) -> str:
        """S3 prefix for reverse-engineering artifacts."""
        return "reverse-engineering"

    # EC2
    ec2_host: str = Field(default="ubuntu@34.244.229.19")
    ec2_key_path: str = Field(default="C:/Users/avych/pm-research-key.pem")

    # Cache cap
    cache_max_gb: float = Field(default=200.0)


_settings: Settings | None = None
_target_settings: dict[str, Settings] = {}


def get_settings() -> Settings:
    """Return singleton Settings instance (target='ohanism')."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def get_settings_for(target: str) -> Settings:
    """Return Settings for the specified target bot/strategy."""
    global _target_settings
    if target not in _target_settings:
        _target_settings[target] = Settings(target=target)
    return _target_settings[target]
