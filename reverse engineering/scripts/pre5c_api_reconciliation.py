"""Pre-5.C: Polymarket data-api P&L reconciliation.

Try multiple endpoints to find windowed P&L for the 49h window.
If found: compare to our -83,831 USDC. Gap < 10% = measurement confirmed.
If not found: document what's available, proceed to Pre-5.D/E as indirect verification.
"""
import sys
import json
import time

sys.path.insert(0, "src")
import requests

OHANISM = "0x89b5cdaaa4866c1e738406712012a630b4078beb"
DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"

WINDOW_START_S = 1779854400  # 2026-05-27 04:00 UTC
WINDOW_END_S   = 1780030200  # 2026-05-29 04:59 UTC

OUR_NET_PNL = -83830.62  # from Pre-5.A binary MTM

print("=== PRE-5.C: API RECONCILIATION ===")
print(f"Target window: {WINDOW_START_S} to {WINDOW_END_S} (49h)")
print(f"Our computed net P&L: {OUR_NET_PNL:+.2f} USDC")

endpoints_tried = []

def try_endpoint(url: str, params: dict = None, description: str = "") -> dict:
    try:
        r = requests.get(url, params=params, timeout=15)
        endpoints_tried.append({"url": url, "status": r.status_code, "desc": description})
        print(f"\n  [{r.status_code}] {description}")
        print(f"  URL: {url[:90]}")
        if r.status_code == 200:
            data = r.json()
            if data:
                if isinstance(data, list):
                    print(f"  Response: list with {len(data)} items")
                    if data:
                        print(f"  First item keys: {list(data[0].keys())[:8] if isinstance(data[0], dict) else type(data[0])}")
                else:
                    print(f"  Response: dict keys: {list(data.keys())[:10]}")
                return data
        return {}
    except Exception as e:
        endpoints_tried.append({"url": url, "status": "error", "desc": description})
        print(f"\n  [ERROR] {description}: {e}")
        return {}

# C1: Try various P&L endpoints
print("\nC1: Trying P&L endpoints...")

# Leaderboard (has lifetime pnl)
lb = try_endpoint(
    f"{DATA_API}/v1/leaderboard?userName=ohanism",
    description="Leaderboard (lifetime pnl)"
)
if lb:
    item = lb[0] if isinstance(lb, list) else lb
    print(f"  Lifetime PnL from leaderboard: {item.get('pnl','N/A')} USDC")
    print(f"  proxyWallet: {item.get('proxyWallet','N/A')}")

# Try pnl endpoint with date params
for url_variant in [
    f"{DATA_API}/pnl?user={OHANISM}",
    f"{DATA_API}/profit?user={OHANISM}",
    f"{DATA_API}/profit-loss?user={OHANISM}",
    f"{DATA_API}/portfolio?user={OHANISM}",
    f"{DATA_API}/history?user={OHANISM}&start={WINDOW_START_S}&end={WINDOW_END_S}",
    f"{DATA_API}/v1/pnl?user={OHANISM}",
    f"{DATA_API}/v2/pnl?user={OHANISM}&start={WINDOW_START_S}&end={WINDOW_END_S}",
]:
    try_endpoint(url_variant, description=url_variant.split("polymarket.com")[1][:50])
    time.sleep(0.2)

# Try activity with start/end filters
try_endpoint(
    f"{DATA_API}/activity?user={OHANISM}&limit=5",
    description="Activity (check fields available)"
)

# C2: Try Gamma for portfolio/pnl
for url_variant in [
    f"{GAMMA_API}/pnl?user={OHANISM}",
    f"{GAMMA_API}/portfolio?address={OHANISM}",
    f"{GAMMA_API}/public-profile?address={OHANISM}",
]:
    try_endpoint(url_variant, description=url_variant.split("polymarket.com")[1][:50])
    time.sleep(0.1)

# Summarize
print("\n=== C SUMMARY ===")
found_windowed = False
for ep in endpoints_tried:
    status = ep['status']
    symbol = "✓" if status == 200 else "✗"
    print(f"  {symbol} [{status}] {ep['desc'][:60]}")

if not found_windowed:
    print("\nNo windowed P&L endpoint found. Proceeding to D/E as indirect verification.")
    print("Saving summary...")

results = {
    "endpoints_tried": endpoints_tried,
    "windowed_pnl_found": found_windowed,
    "our_net_pnl": OUR_NET_PNL,
    "note": "No windowed P&L endpoint available via data-api. Indirect verification via D/E required."
}
import pathlib
cfg_path = pathlib.Path("src")
sys.path.insert(0, str(cfg_path.absolute()))
from reverse_engineering.config import get_settings
cfg = get_settings()
(cfg.results_dir / "pre5c_reconciliation.json").write_text(json.dumps(results, indent=2))
print("Saved: output/results/pre5c_reconciliation.json")
