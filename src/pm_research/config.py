from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # AWS
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_default_region: str = "eu-west-1"
    s3_bucket: str = "pm-research-data"

    # Polygon RPC
    polygon_wss_url: str
    polygon_https_url: str
    alchemy_block_range_limit: int = 2000

    # Polygonscan
    polygonscan_api_key: str = ""

    # Storage
    data_dir: str = "/var/pm-research/data"
    state_dir: str = "/var/pm-research/state"
    log_dir: str = "/var/pm-research/logs"

    # Alerting
    discord_webhook_url: str = ""
    healthchecks_url: str = ""

    # Collector tuning
    gamma_discovery_interval_s: int = 30
    market_max_age_hours: int = 6
    binance_reconnect_at_s: int = 82800

    # Clock
    max_ntp_drift_ms: int = 50

    @field_validator("alchemy_block_range_limit")
    @classmethod
    def positive_block_range(cls, v: int) -> int:
        if v < 1:
            raise ValueError("alchemy_block_range_limit must be positive")
        return v


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()  # type: ignore[call-arg]
    return _settings
