"""Quick probe: sum all current positions P&L and compare to leaderboard."""
import requests

PROXY = "0x89b5cdaaa4866c1e738406712012a630b4078beb"
BASE  = "https://data-api.polymarket.com"

r = requests.get(BASE + "/positions?user=" + PROXY + "&limit=500", timeout=20)
rows = r.json()
print(f"Total: {len(rows)} positions")
print()

total_cash = 0.0
total_realized = 0.0
for row in rows:
    cash     = float(row.get("cashPnl", 0) or 0)
    realized = float(row.get("realizedPnl", 0) or 0)
    total_cash     += cash
    total_realized += realized
    cid = row["conditionId"][:20] + "..."
    outcome = str(row.get("outcome", "?"))[:4]
    sz = float(row["size"])
    ap = float(row["avgPrice"])
    cp = float(row["curPrice"])
    print(
        f"  {cid} {outcome:<5} sz={sz:>8.2f} avgP={ap:.4f} "
        f"cashPnl={cash:>9.3f} realPnl={realized:>9.3f} curP={cp:.4f}"
    )

print()
print(f"Sum cashPnl:     {total_cash:>12.3f}")
print(f"Sum realizedPnl: {total_realized:>12.3f}")
print(f"Sum total:       {total_cash + total_realized:>12.3f}")
print()
LEADERBOARD = -1382.6536746211664
print(f"Leaderboard pnl: {LEADERBOARD:>12.4f}")
diff = (total_cash + total_realized) - LEADERBOARD
print(f"Difference:       {diff:>12.3f}")
pct = abs(diff / LEADERBOARD) * 100 if LEADERBOARD else float("nan")
print(f"Gap %:            {pct:>12.1f}%")
