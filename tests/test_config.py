import os

import pytest
from pydantic import ValidationError

from pm_research.config import Settings


def test_settings_loads_with_required_rpc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POLYGON_WSS_URL", "wss://example.com")
    monkeypatch.setenv("POLYGON_HTTPS_URL", "https://example.com")
    s = Settings()
    assert s.polygon_wss_url == "wss://example.com"
    assert s.alchemy_block_range_limit == 2000


def test_settings_invalid_block_range(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POLYGON_WSS_URL", "wss://example.com")
    monkeypatch.setenv("POLYGON_HTTPS_URL", "https://example.com")
    monkeypatch.setenv("ALCHEMY_BLOCK_RANGE_LIMIT", "0")
    with pytest.raises(ValidationError):
        Settings()


def test_settings_missing_rpc() -> None:
    # Without any env, required fields must fail
    env_keys = ["POLYGON_WSS_URL", "POLYGON_HTTPS_URL"]
    for k in env_keys:
        os.environ.pop(k, None)
    with pytest.raises(ValidationError):
        Settings()
