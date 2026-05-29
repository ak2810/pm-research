"""Gamma REST API client for market metadata lookup.

Used to enrich ohanism fills with asset_symbol, horizon, outcome_side,
endDate, startDate for markets not captured in pm_clob new_market events.

Rate limiting: 0.15s between requests (free tier, no auth required).
Caches results to output/cache/gamma_cid_lookup.parquet to avoid re-fetching.
"""

from __future__ import annotations

import json
import re
import time
from typing import TYPE_CHECKING, Any

import polars as pl
import requests
import structlog

from reverse_engineering.config import get_settings

if TYPE_CHECKING:
    from pathlib import Path

log = structlog.get_logger(__name__)

_GAMMA_BASE = "https://gamma-api.polymarket.com"
_ASSET_PAT = re.compile(r"^([a-z]+)-updown-(5m|15m|1h)-(\d+)$")
_HORIZON_S: dict[str, int] = {"5m": 300, "15m": 900, "1h": 3600}
_SLEEP_S = 0.15
_CACHE_FILE = "gamma_cid_lookup.parquet"


def _cache_path() -> Path:
    return get_settings().cache_dir / _CACHE_FILE


def _load_cached_cids() -> dict[str, dict[str, Any]]:
    """Load previously fetched metadata from cache.

    Keys are normalised to "slug:{slug}" when a slug is available.
    Falls back to the raw condition_id column for legacy entries.
    """
    path = _cache_path()
    if not path.exists():
        return {}
    df = pl.read_parquet(str(path))
    result: dict[str, dict[str, Any]] = {}
    for row in df.iter_rows(named=True):
        meta = dict(row)
        slug_val = meta.get("slug", "")
        # Prefer "slug:..." key so fetch_markets_by_slug_range cache-hits correctly
        key = f"slug:{slug_val}" if slug_val else row.get("condition_id", "")
        if key:
            result[key] = meta
    return result


def _save_cached_cids(cid_map: dict[str, dict[str, Any]]) -> None:
    """Persist CID→metadata to cache."""
    if not cid_map:
        return
    rows = [{"condition_id": cid, **meta} for cid, meta in cid_map.items()]
    df = pl.DataFrame(rows)
    path = _cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(str(path))


def fetch_markets_by_slug_range(
    date_unix_start: int,
    date_unix_end: int,
) -> pl.DataFrame:
    """Fetch all crypto updown market metadata for fills in a time window.

    Enumerates all 5m/15m/1h slugs that RESOLVE in [date_unix_start, date_unix_end]
    and queries Gamma by slug. Caches results. Returns token_id → metadata.

    Args:
        date_unix_start: Window start (Unix seconds, inclusive).
        date_unix_end: Window end (Unix seconds, inclusive).

    Returns:
        DataFrame with token_id, market, asset_symbol, horizon, outcome_side,
        start_date_unix, end_date_unix columns.
    """
    horizons: list[tuple[str, int]] = [("5m", 300), ("15m", 900), ("1h", 3600)]
    assets = ["btc", "eth", "sol", "xrp", "doge"]

    # Build all slug candidates for this window
    slugs: list[tuple[str, str, str]] = []
    for horizon_name, horizon_s in horizons:
        first_slot = (date_unix_start // horizon_s) * horizon_s
        slot = first_slot
        while slot <= date_unix_end + horizon_s:
            for asset in assets:
                slug = f"{asset}-updown-{horizon_name}-{slot}"
                slugs.append((slug, asset.upper(), horizon_name))
            slot += horizon_s

    log.info("slug_range_query_start", slugs=len(slugs), window=(date_unix_start, date_unix_end))

    cached = _load_cached_cids()
    records: list[dict[str, Any]] = []
    _new_since_save = 0
    _save_every = 200  # save cache every 200 new slugs fetched

    for slug, asset_sym, horizon_name in slugs:
        cache_key = f"slug:{slug}"
        if cache_key in cached:
            meta = cached[cache_key]
        else:
            url = f"{_GAMMA_BASE}/events?slug={slug}"
            try:
                resp = requests.get(url, timeout=15)
                resp.raise_for_status()
                data = resp.json()
            except requests.RequestException as exc:
                log.warning("gamma_slug_fetch_failed", slug=slug, error=str(exc))
                time.sleep(_SLEEP_S)
                continue

            if not data:
                time.sleep(_SLEEP_S)
                continue

            ev = data[0]
            mkts = ev.get("markets", [])
            if not mkts:
                time.sleep(_SLEEP_S)
                continue

            mkt = mkts[0]
            cid = mkt.get("conditionId", "")
            clob_raw = mkt.get("clobTokenIds", "[]")
            try:
                token_ids = json.loads(clob_raw) if isinstance(clob_raw, str) else clob_raw
            except (json.JSONDecodeError, TypeError):
                token_ids = []

            end_date_str = mkt.get("endDate", "")
            try:
                from datetime import datetime

                end_unix = datetime.fromisoformat(end_date_str.replace("Z", "+00:00")).timestamp()
            except ValueError:
                slug_int = int(slug.rsplit("-", 1)[-1])
                horizon_s_val = {"5m": 300, "15m": 900, "1h": 3600}[horizon_name]
                end_unix = float(slug_int + horizon_s_val)

            slug_int = int(slug.rsplit("-", 1)[-1])
            meta = {
                "slug": slug,
                "asset_symbol": asset_sym,
                "horizon": horizon_name,
                "start_date_unix": float(slug_int),
                "end_date_unix": end_unix,
                "token_ids_json": json.dumps(token_ids),
                "condition_id": cid,
                "neg_risk": False,
            }
            cached[cache_key] = meta
            _new_since_save += 1
            if _new_since_save >= _save_every:
                _save_cached_cids(cached)
                _new_since_save = 0
            time.sleep(_SLEEP_S)

        token_ids = json.loads(meta.get("token_ids_json", "[]"))
        for i, tid in enumerate(token_ids):
            outcome_side = "Up" if i == 0 else "Down"
            records.append(
                {
                    "token_id": str(tid),
                    "market": meta.get("condition_id", ""),
                    "asset_symbol": meta["asset_symbol"],
                    "horizon": meta["horizon"],
                    "outcome_side": outcome_side,
                    "start_date_unix": meta["start_date_unix"],
                    "end_date_unix": meta["end_date_unix"],
                }
            )

    _save_cached_cids(cached)
    log.info("slug_range_query_complete", records=len(records))

    if not records:
        return pl.DataFrame(
            schema={
                "token_id": pl.Utf8,
                "market": pl.Utf8,
                "asset_symbol": pl.Utf8,
                "horizon": pl.Utf8,
                "outcome_side": pl.Utf8,
                "start_date_unix": pl.Float64,
                "end_date_unix": pl.Float64,
            }
        )

    return pl.DataFrame(records).unique(subset=["token_id"])


def fetch_market_by_cid(condition_id: str) -> dict[str, Any] | None:
    """Fetch market metadata from Gamma by condition_id.

    Args:
        condition_id: 0x-prefixed hex condition ID.

    Returns:
        Dict with slug, asset_symbol, horizon, start_date_unix, end_date_unix,
        token_ids (list), neg_risk (bool). None if not a crypto updown market
        or if fetch fails.
    """
    url = f"{_GAMMA_BASE}/markets?conditionId={condition_id}"
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        log.warning("gamma_fetch_failed", condition_id=condition_id[:20], error=str(exc))
        return None

    # Response is either a dict (single) or a list
    if isinstance(data, list):
        if not data:
            return None
        mkt = data[0]
    elif isinstance(data, dict):
        mkt = data
    else:
        return None

    # Verify this is the right market
    returned_cid = mkt.get("conditionId", "")
    if returned_cid.lower() != condition_id.lower():
        log.debug("gamma_cid_mismatch", requested=condition_id[:20], returned=returned_cid[:20])
        return None

    slug = mkt.get("slug", "")
    m = _ASSET_PAT.match(slug)
    if not m:
        return None

    if mkt.get("negRisk"):
        return None

    asset_symbol = m.group(1).upper()
    horizon = m.group(2)
    start_ts = int(m.group(3))
    end_ts = start_ts + _HORIZON_S[horizon]

    clob_raw = mkt.get("clobTokenIds", "[]")
    try:
        token_ids: list[str] = json.loads(clob_raw) if isinstance(clob_raw, str) else clob_raw
    except (json.JSONDecodeError, TypeError):
        token_ids = []

    return {
        "slug": slug,
        "asset_symbol": asset_symbol,
        "horizon": horizon,
        "start_date_unix": float(start_ts),
        "end_date_unix": float(end_ts),
        "token_ids_json": json.dumps(token_ids),
        "neg_risk": bool(mkt.get("negRisk", False)),
    }


def build_market_lookup_from_cids(
    condition_ids: list[str],
) -> pl.DataFrame:
    """Fetch and cache market metadata for a list of condition IDs.

    Skips CIDs already in cache. Returns a DataFrame with one row per
    token_id (Up and Down for each market).

    Args:
        condition_ids: List of 0x condition ID strings.

    Returns:
        DataFrame with columns: token_id, market (condition_id), asset_symbol,
        horizon, outcome_side, start_date_unix, end_date_unix.
    """
    cached = _load_cached_cids()
    missing = [cid for cid in condition_ids if cid not in cached]

    log.info(
        "gamma_lookup_start",
        total=len(condition_ids),
        cached=len(cached),
        missing=len(missing),
    )

    for i, cid in enumerate(missing):
        result = fetch_market_by_cid(cid)
        if result is not None:
            cached[cid] = result
        if i < len(missing) - 1:
            time.sleep(_SLEEP_S)

    if missing:
        _save_cached_cids(cached)
        log.info("gamma_lookup_fetched", fetched=len(missing), total_cached=len(cached))

    records: list[dict[str, Any]] = []
    for cid, meta in cached.items():
        if cid not in set(condition_ids):
            continue
        token_ids: list[str] = json.loads(meta.get("token_ids_json", "[]"))
        for i, tid in enumerate(token_ids):
            outcome_side = "Up" if i == 0 else "Down"
            records.append(
                {
                    "token_id": tid,
                    "market": cid,
                    "asset_symbol": meta["asset_symbol"],
                    "horizon": meta["horizon"],
                    "outcome_side": outcome_side,
                    "start_date_unix": meta["start_date_unix"],
                    "end_date_unix": meta["end_date_unix"],
                }
            )

    if not records:
        return pl.DataFrame(
            schema={
                "token_id": pl.Utf8,
                "market": pl.Utf8,
                "asset_symbol": pl.Utf8,
                "horizon": pl.Utf8,
                "outcome_side": pl.Utf8,
                "start_date_unix": pl.Float64,
                "end_date_unix": pl.Float64,
            }
        )

    return pl.DataFrame(records).unique(subset=["token_id"])
