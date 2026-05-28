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

    # Local paths
    @property
    def cache_dir(self) -> Path:
        """Local Parquet cache synced from S3."""
        return Path(__file__).parents[2] / "output" / "cache"

    @property
    def tables_dir(self) -> Path:
        """Output tables directory."""
        return Path(__file__).parents[2] / "output" / "tables"

    @property
    def models_dir(self) -> Path:
        """Output models directory (gitignored; hash+S3 URI in DECISIONS)."""
        return Path(__file__).parents[2] / "output" / "models"

    @property
    def plots_dir(self) -> Path:
        """Output plots directory."""
        return Path(__file__).parents[2] / "output" / "plots"

    @property
    def results_dir(self) -> Path:
        """Output results directory."""
        return Path(__file__).parents[2] / "output" / "results"

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


def get_settings() -> Settings:
    """Return singleton Settings instance."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
