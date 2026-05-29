"""Pre-warm Gamma slug cache for the full 49h window.

Fetches all slugs not yet in the cache. Uses 0.05s sleep (reduced from 0.15)
to fit within 600s timeout. Saves cache every 200 new slugs (durable on timeout).
With 2315 missing slugs at ~0.25s each = ~579s < 600s.

Usage: run once; then A3 runs instantly from the warm cache.
"""
import sys
import time

sys.path.insert(0, "src")

from reverse_engineering.io.gamma import _load_cached_cids, _save_cached_cids
import requests
import json
from typing import Any
from datetime import datetime

GAMMA_BASE = "https://gamma-api.polymarket.com"
WINDOW_START = 1779854400  # 2026-05-27 04:00 UTC
WINDOW_END   = 1780030200  # 2026-05-29 04:59 UTC
SLEEP_S = 0.05             # fast warm; not for production rate-limit
SAVE_EVERY = 200

def warm() -> None:
    horizons = [("5m", 300), ("15m", 900)]
    assets = ["btc", "eth", "sol", "xrp", "doge"]
    slugs = []
    for hn, hs in horizons:
        slot = (WINDOW_START // hs) * hs
        while slot <= WINDOW_END + hs:
            for a in assets:
                slugs.append((f"{a}-updown-{hn}-{slot}", a.upper(), hn))
            slot += hs

    cached = _load_cached_cids()
    missing = [(s, a, h) for s, a, h in slugs if f"slug:{s}" not in cached]
    print(f"Window slugs: {len(slugs)}, cached: {len(slugs)-len(missing)}, to fetch: {len(missing)}")

    new_count = 0
    t0 = time.time()
    for idx, (slug, asset_sym, horizon_name) in enumerate(missing):
        url = f"{GAMMA_BASE}/events?slug={slug}"
        try:
            resp = requests.get(url, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            time.sleep(SLEEP_S)
            continue

        if not data:
            time.sleep(SLEEP_S)
            continue

        ev = data[0]
        mkts = ev.get("markets", [])
        if not mkts:
            time.sleep(SLEEP_S)
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
            end_unix = datetime.fromisoformat(end_date_str.replace("Z", "+00:00")).timestamp()
        except ValueError:
            slug_int = int(slug.rsplit("-", 1)[-1])
            end_unix = float(slug_int + {"5m": 300, "15m": 900, "1h": 3600}[horizon_name])

        slug_int = int(slug.rsplit("-", 1)[-1])
        meta: dict[str, Any] = {
            "slug": slug,
            "asset_symbol": asset_sym,
            "horizon": horizon_name,
            "start_date_unix": float(slug_int),
            "end_date_unix": end_unix,
            "token_ids_json": json.dumps(token_ids),
            "condition_id": cid,
            "neg_risk": False,
        }
        cached[f"slug:{slug}"] = meta
        new_count += 1
        if new_count % SAVE_EVERY == 0:
            _save_cached_cids(cached)
            elapsed = time.time() - t0
            rate = new_count / elapsed
            remaining = len(missing) - (idx + 1)
            eta_s = remaining / rate if rate > 0 else 0
            print(f"  Saved {new_count}/{len(missing)} new slugs | rate={rate:.1f}/s | eta={eta_s:.0f}s")
        time.sleep(SLEEP_S)

    _save_cached_cids(cached)
    elapsed = time.time() - t0
    total = len(_load_cached_cids())
    print(f"\nDone: {new_count} new slugs in {elapsed:.0f}s | total cache={total}")

warm()
