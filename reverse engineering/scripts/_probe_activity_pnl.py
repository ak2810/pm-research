"""Test: reconstruct fills from activity endpoint for current positions,
compute our P&L formula, compare to API cashPnl + realizedPnl.
This is a direct per-position formula validation using POST-window data.
"""
import sys, json, time
sys.path.insert(0, "src")
import requests

PROXY = "0x89b5cdaaa4866c1e738406712012a630b4078beb"
BASE  = "https://data-api.polymarket.com"

# ── Load all current positions ───────────────────────────────────────────────
pos_r = requests.get(BASE + "/positions?user=" + PROXY + "&limit=500", timeout=20)
positions = {p["conditionId"].lower(): p for p in pos_r.json()}
print(f"Current positions: {len(positions)}")

# Identify resolved positions (curPrice near 0 or 1)
resolved = {cid: p for cid, p in positions.items()
            if float(p.get("curPrice", 0.5)) < 0.01 or float(p.get("curPrice", 0.5)) > 0.99}
print(f"Clearly resolved:  {len(resolved)}")
for cid, p in resolved.items():
    print(f"  {cid[:22]}  outcome={p.get('outcome','?'):<4} curP={float(p['curPrice']):.4f}  "
          f"cashPnl={float(p.get('cashPnl',0) or 0):>8.3f}  realPnl={float(p.get('realizedPnl',0) or 0):>8.3f}")

# ── Fetch all recent activity ────────────────────────────────────────────────
print(f"\nFetching activity...")
acts = []
for offset in range(0, 5000, 500):
    r = requests.get(BASE + f"/activity?user={PROXY}&limit=500&offset={offset}", timeout=20)
    chunk = r.json()
    if not isinstance(chunk, list) or len(chunk) == 0:
        break
    acts.extend(chunk)
    if len(chunk) < 500:
        break
    time.sleep(0.3)
print(f"Total activity rows: {len(acts)}")

trades = [a for a in acts if a.get("type") == "TRADE"]
print(f"TRADE rows: {len(trades)}")

# ── Match activity to resolved positions ────────────────────────────────────
print(f"\n=== PER-POSITION COMPARISON ===")
print(f"{'conditionId':<24} {'outcome':<6} {'n_fills':>7} {'api_cash+real':>14} "
      f"{'our_mtm':>10} {'gap':>8} {'gap%':>7}")
print("-" * 85)

comparison = []
for cid, pos in resolved.items():
    outcome_api = pos.get("outcome", "?")
    api_cash     = float(pos.get("cashPnl", 0) or 0)
    api_realized = float(pos.get("realizedPnl", 0) or 0)
    api_total    = api_cash + api_realized
    api_cur_p    = float(pos.get("curPrice", 0.5))

    # up_wins from curPrice
    if api_cur_p > 0.99:
        up_wins = 1
    elif api_cur_p < 0.01:
        up_wins = 0
    else:
        up_wins = None

    # Match fills by conditionId
    pos_fills = [t for t in trades if t.get("conditionId", "").lower() == cid]
    n = len(pos_fills)

    if n == 0 or up_wins is None:
        print(f"  {cid[:22]:<24} {outcome_api:<6} {n:>7}  NO DATA")
        continue

    # Compute our MTM from activity fills
    our_mtm = 0.0
    for f in pos_fills:
        side    = f.get("side", "?")       # BUY or SELL
        outcome = f.get("outcome", "?")    # Up or Down
        price   = float(f.get("price", 0))
        size    = float(f.get("size", 0))

        # canonical_sign: +1 if net long-Up
        if (side == "BUY" and outcome == "Up") or (side == "SELL" and outcome == "Down"):
            canonical_sign = 1.0
            price_f = price if outcome == "Up" else (1.0 - price)
        else:  # SELL Up or BUY Down
            canonical_sign = -1.0
            price_f = price if outcome == "Up" else (1.0 - price)

        our_mtm += canonical_sign * (up_wins - price_f) * size

    gap = our_mtm - api_total
    gap_pct = abs(gap / api_total) * 100 if abs(api_total) > 0.1 else float("nan")

    print(f"  {cid[:22]:<24} {outcome_api:<6} {n:>7}  "
          f"{api_total:>14.3f}  {our_mtm:>10.3f}  {gap:>8.3f}  "
          f"{gap_pct if gap_pct==gap_pct else float('nan'):>7.1f}%")
    comparison.append({"cid": cid, "n": n, "api_total": api_total, "our_mtm": our_mtm,
                        "gap": gap, "gap_pct": gap_pct})

# ── Aggregate ────────────────────────────────────────────────────────────────
if comparison:
    import statistics
    gaps = [c["gap"] for c in comparison]
    pcts = [c["gap_pct"] for c in comparison if c["gap_pct"] == c["gap_pct"]]
    print(f"\nN matched resolved positions: {len(comparison)}")
    print(f"Mean signed gap:  {statistics.mean(gaps):>+8.3f} USDC")
    print(f"Mean abs gap:     {statistics.mean(abs(g) for g in gaps):>+8.3f} USDC")
    if pcts:
        print(f"Median abs gap %: {statistics.median(pcts):>8.1f}%")
    print()
    if all(abs(c["gap_pct"]) < 5.0 for c in comparison if c["gap_pct"]==c["gap_pct"]):
        print("F5: PASS — all per-position gaps < 5%")
    elif statistics.mean(gaps) < -1:
        print(f"F5: SYSTEMATIC BIAS — we over-report losses by {abs(statistics.mean(gaps)):.2f} USDC/position")
    elif statistics.mean(gaps) > 1:
        print(f"F5: SYSTEMATIC BIAS — we over-report gains by {statistics.mean(gaps):.2f} USDC/position")
    else:
        print("F5: INCONCLUSIVE")
