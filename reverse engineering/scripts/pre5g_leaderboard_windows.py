"""Pre-5.G: Query Polymarket leaderboard with time-window parameters.
Find ohanism's monthly/weekly/today/all P&L.
Compare against our 49h window of -83,831 USDC.
"""
import sys, json, time
import requests

PROXY    = "0x89b5cdaaa4866c1e738406712012a630b4078beb"
USERNAME = "ohanism"
BASE     = "https://data-api.polymarket.com"

print("=== PRE-5.G: LEADERBOARD WINDOW SURVEY ===\n")

results = {}

# ── G1. Try all window × category combinations ──────────────────────────────
windows    = ["today", "weekly", "monthly", "all", "1d", "7d", "30d", "ytd",
              "daily", "day", "week", "month"]
categories = ["crypto", "sports", "all", ""]

def probe(url):
    try:
        r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        return r.status_code, r.text[:2000]
    except Exception as e:
        return -1, str(e)

print("G1: Probing leaderboard endpoints...")
hits = []
for w in windows:
    for cat in categories:
        cat_part = f"&category={cat}" if cat else ""
        url = BASE + f"/v1/leaderboard?window={w}{cat_part}&limit=500"
        sc, body = probe(url)
        if sc == 200:
            try:
                data = json.loads(body[:10000])
            except Exception:
                continue
            if isinstance(data, list) and len(data) > 0 and "pnl" in data[0]:
                hits.append((w, cat, url, len(data)))
                print(f"  HIT  window={w:<8} cat={cat:<8} n={len(data)}")
                # Find ohanism in this window
                for row in data:
                    if (str(row.get("proxyWallet","")).lower() == PROXY.lower() or
                            str(row.get("userName","")).lower() == USERNAME.lower()):
                        pnl = row.get("pnl", "?")
                        vol = row.get("vol", "?")
                        rank = row.get("rank", "?")
                        print(f"      FOUND ohanism: rank={rank} pnl={pnl} vol={vol}")
                        results[f"{w}/{cat}"] = {"rank": rank, "pnl": pnl, "vol": vol,
                                                  "window": w, "cat": cat}
            elif sc == 200:
                pass  # 200 but not leaderboard data
        elif sc not in (-1, 404):
            print(f"  {sc}  window={w:<8} cat={cat}")

# ── G1b. Try userName filter directly ───────────────────────────────────────
print("\nG1b: userName-filtered leaderboard variants...")
for w in ["today","weekly","monthly","all",""]:
    for cat in ["crypto",""]:
        w_part  = f"&window={w}" if w else ""
        cat_part = f"&category={cat}" if cat else ""
        url = BASE + f"/v1/leaderboard?userName={USERNAME}{w_part}{cat_part}"
        sc, body = probe(url)
        if sc == 200:
            try:
                data = json.loads(body[:5000])
            except Exception:
                continue
            if isinstance(data, list) and len(data) > 0:
                row = data[0]
                pnl = row.get("pnl","?")
                vol = row.get("vol","?")
                rank = row.get("rank","?")
                key = f"userName/{w or 'none'}/{cat or 'none'}"
                if key not in results or str(results.get(key,{}).get("pnl","?")) != str(pnl):
                    print(f"  HIT  window={w or 'none':<8} cat={cat or 'none':<8} "
                          f"rank={rank} pnl={pnl} vol={vol}")
                    results[key] = {"rank":rank,"pnl":pnl,"vol":vol,"window":w,"cat":cat}

# ── G1c. Scrape public profile / stats page ──────────────────────────────────
print("\nG1c: Gamma profile / stats endpoints...")
for path in [
    f"/profile?address={PROXY}",
    f"/stats?address={PROXY}",
    f"/user-stats?user={PROXY}",
    f"/pnl-stats?user={PROXY}",
    f"/earnings?user={PROXY}",
    "/leaderboard?window=monthly&category=crypto",
    "/leaderboard?window=weekly&category=crypto",
]:
    url = "https://gamma-api.polymarket.com" + path
    sc, body = probe(url)
    if sc == 200 and len(body) > 10:
        print(f"  {sc}  {path}")
        print(f"       {body[:300]}")

# ── G2/G3. Summary ───────────────────────────────────────────────────────────
print("\n=== G2/G3: SUMMARY OF FOUND WINDOWS ===")
if not results:
    print("No windowed leaderboard data retrieved. All routes returned 404 or no ohanism row.")
    print("Possible: ohanism not in top-N of those windows, or API uses different param names.")
else:
    for key, v in results.items():
        print(f"  {key:<30} pnl={v['pnl']}  vol={v['vol']}  rank={v['rank']}")

# ── G4. Arithmetic check ─────────────────────────────────────────────────────
print("\n=== G4: ARITHMETIC COMPATIBILITY CHECK ===")
OUR_WINDOW_PNL = -83831
KNOWN_MONTHLY  = 173508  # from external snapshot (top-5)
KNOWN_WEEKLY   = 26296   # from external snapshot (top-10)
FILL_RATE_HR   = 50586 / 49          # fills/hour observed
REBATE_HR      = 3141 / 49           # USDC/hour rebate observed

print(f"Our 49h window P&L:   {OUR_WINDOW_PNL:>+12,.0f} USDC")
print(f"External monthly P&L: {KNOWN_MONTHLY:>+12,.0f} USDC (verified public leaderboard)")
print(f"External weekly P&L:  {KNOWN_WEEKLY:>+12,.0f} USDC (verified public leaderboard)")
print()
print(f"Observed fill rate: {FILL_RATE_HR:.0f} fills/hr | rebate rate: {REBATE_HR:.1f} USDC/hr")
print()

# If our 49h is inside the monthly window:
remaining_days_in_month = 30 - 49/24  # ~28 days of month outside our window
remaining_hrs = remaining_days_in_month * 24
remaining_rebate = REBATE_HR * remaining_hrs
required_mtm_rest_of_month = (KNOWN_MONTHLY - OUR_WINDOW_PNL) - remaining_rebate

print(f"If our 49h is INSIDE a 30-day monthly window:")
print(f"  Rest of month rebate (est): +{remaining_rebate:,.0f} USDC")
print(f"  Required MTM from rest of month: {required_mtm_rest_of_month:+,.0f} USDC")
print(f"  That implies MTM rate outside window: {required_mtm_rest_of_month/remaining_hrs:+.1f} USDC/hr")
print(f"  Our window MTM rate:                  {(OUR_WINDOW_PNL - 3141)/49:+.1f} USDC/hr")
ratio = abs(required_mtm_rest_of_month/remaining_hrs) / abs((OUR_WINDOW_PNL-3141)/49)
print(f"  Ratio (required/observed): {ratio:.1f}x higher MTM rate outside window")
print()
if ratio > 3:
    print("  IMPLAUSIBLE: rest-of-month MTM rate would need to be {:.0f}x our window rate.".format(ratio))
    print("  Our -83,831 USDC is arithmetically incompatible with monthly +173,508 USDC.")
else:
    print("  PLAUSIBLE: rest-of-month can compensate (only {:.1f}x rate needed).".format(ratio))

# Save
out = {
    "leaderboard_windows_found": results,
    "external_monthly_pnl": KNOWN_MONTHLY,
    "external_weekly_pnl": KNOWN_WEEKLY,
    "our_window_pnl": OUR_WINDOW_PNL,
    "required_rest_of_month_mtm": required_mtm_rest_of_month,
    "mtm_ratio": ratio,
}
import pathlib
out_path = pathlib.Path("output/results/pre5g_leaderboard.json")
out_path.write_text(json.dumps(out, indent=2))
print(f"\nSaved: {out_path}")
