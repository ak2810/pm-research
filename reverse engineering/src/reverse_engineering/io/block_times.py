"""Fetch and cache Polygon block_number → t_block_ns mapping.

Queries the Polygon RPC (polygon.drpc.org, no auth, free) via eth_getBlockByNumber
in batches of 100. Results cached to output/cache/block_times.parquet.

Memory strategy: processes block numbers in batches; only the cache parquet
and current batch (~100 rows) are in memory at once. Peak RAM: <1 MB.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import polars as pl
import requests
import structlog

from reverse_engineering.config import get_settings

if TYPE_CHECKING:
    from pathlib import Path

log = structlog.get_logger(__name__)

_BATCH_SIZE = 100
_SLEEP_BETWEEN_BATCHES = 0.05
_RETRY_SLEEP = 2.0
_MAX_RETRIES = 3
_BLOCK_TIMES_CACHE = "block_times.parquet"


def _block_times_path() -> Path:
    cfg = get_settings()
    return cfg.cache_dir / _BLOCK_TIMES_CACHE


def _load_cached() -> dict[int, int]:
    """Load existing block_number→t_block_ns from cache parquet."""
    path = _block_times_path()
    if not path.exists():
        return {}
    df = pl.read_parquet(str(path))
    return dict(zip(df["block_number"].to_list(), df["t_block_ns"].to_list(), strict=False))


def _save_cache(block_time_map: dict[int, int]) -> None:
    """Persist block_number→t_block_ns to cache parquet."""
    if not block_time_map:
        return
    path = _block_times_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    df = pl.DataFrame(
        {
            "block_number": pl.Series(list(block_time_map.keys()), dtype=pl.Int64),
            "t_block_ns": pl.Series(list(block_time_map.values()), dtype=pl.Int64),
        }
    )
    df.write_parquet(str(path))


def _fetch_batch(
    rpc_url: str,
    block_numbers: list[int],
) -> dict[int, int]:
    """Fetch one batch of block timestamps via JSON-RPC batch call.

    Args:
        rpc_url: Polygon HTTPS RPC endpoint.
        block_numbers: List of block numbers (up to _BATCH_SIZE).

    Returns:
        Dict mapping block_number → timestamp_ns.

    Raises:
        RuntimeError: On HTTP error or malformed response.
    """
    payload = [
        {
            "jsonrpc": "2.0",
            "method": "eth_getBlockByNumber",
            "params": ["0x" + format(b, "x"), False],
            "id": i,
        }
        for i, b in enumerate(block_numbers)
    ]

    for attempt in range(_MAX_RETRIES):
        try:
            resp = requests.post(rpc_url, json=payload, timeout=60)
            resp.raise_for_status()
            break
        except requests.RequestException as exc:
            if attempt == _MAX_RETRIES - 1:
                raise RuntimeError(f"RPC batch failed after {_MAX_RETRIES} retries: {exc}") from exc
            log.warning("rpc_retry", attempt=attempt, error=str(exc))
            time.sleep(_RETRY_SLEEP)

    result: dict[int, int] = {}
    for item in resp.json():
        block = (item.get("result") or {}) if isinstance(item, dict) else {}
        if not block:
            continue
        number_hex = block.get("number")
        timestamp_hex = block.get("timestamp")
        if number_hex and timestamp_hex:
            num = int(number_hex, 16)
            ts_ns = int(timestamp_hex, 16) * 1_000_000_000
            result[num] = ts_ns
    return result


def fetch_block_times(block_numbers: list[int]) -> dict[int, int]:
    """Return block_number→t_block_ns for all requested blocks.

    Uses local cache to avoid re-fetching. Fetches only missing blocks.

    Args:
        block_numbers: List of Polygon block numbers to resolve.

    Returns:
        Dict mapping every block_number (that could be fetched) → t_block_ns.
    """
    cfg = get_settings()
    rpc_url = cfg.polygon_https_url
    if not rpc_url:
        raise RuntimeError("POLYGON_HTTPS_URL not set in .env — required for block time derivation")

    cached = _load_cached()
    missing = [b for b in block_numbers if b not in cached]

    if not missing:
        log.info("block_times_all_cached", total=len(block_numbers))
        return {b: cached[b] for b in block_numbers if b in cached}

    log.info(
        "block_times_fetching",
        total=len(block_numbers),
        cached=len(cached),
        missing=len(missing),
    )

    fetched: dict[int, int] = {}
    batches = [missing[i : i + _BATCH_SIZE] for i in range(0, len(missing), _BATCH_SIZE)]

    for i, batch in enumerate(batches):
        batch_result = _fetch_batch(rpc_url, batch)
        fetched.update(batch_result)
        if i < len(batches) - 1:
            time.sleep(_SLEEP_BETWEEN_BATCHES)

    if fetched:
        log.info("block_times_fetched", count=len(fetched))
        updated = {**cached, **fetched}
        _save_cache(updated)

    all_results = {**cached, **fetched}
    return {b: all_results[b] for b in block_numbers if b in all_results}
