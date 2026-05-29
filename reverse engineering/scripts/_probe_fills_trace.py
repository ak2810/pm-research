"""Print raw activity fills for the two failing positions (Down tokens lost)
to diagnose the MTM overstatement."""
import sys, json, time
sys.path.insert(0, "src")
import requests

PROXY = "0x89b5cdaaa4866c1e738406712012a630b4078beb"
BASE  = "https://data-api.polymarket.com"

# Load positions
pos_r = requests.get(BASE + "/positions?user=" + PROXY + "&limit=500", timeout=20)
positions = pos_r.json()

# Target: Down token positions that lost (curPrice~0)
target_cids = {}
for p in positions:
    cid = p["conditionId"].lower()
    cur = float(p.get("curPrice", 0.5))
    out = p.get("outcome", "?")
    if cur < 0.01 and out == "Down":
        target_cids[cid] = p
        print(f"Target: {cid[:30]}  outcome={out} size={p['size']:.2f} avg={p['avgPrice']:.4f} "
              f"cash={float(p.get('cashPnl',0) or 0):.3f} real={float(p.get('realizedPnl',0) or 0):.3f}")

# Fetch activity
acts = []
for offset in range(0, 5000, 500):
    r = requests.get(BASE + f"/activity?user={PROXY}&limit=500&offset={offset}", timeout=20)
    chunk = r.json()
    if not isinstance(chunk, list) or not chunk:
        break
    acts.extend(chunk)
    if len(chunk) < 500:
        break
    time.sleep(0.2)

trades = [a for a in acts if a.get("type") == "TRADE"]

for cid, pos in target_cids.items():
    fills = [t for t in trades if t.get("conditionId","").lower() == cid]
    print(f"\n=== {cid[:30]} (Down position, Up won) — {len(fills)} fills ===")
    total_our = 0.0
    for i, f in enumerate(fills):
        side    = f.get("side","?")
        outcome = f.get("outcome","?")
        price   = float(f.get("price", 0))
        size    = float(f.get("size", 0))

        # canonical
        if (side == "BUY" and outcome == "Up") or (side == "SELL" and outcome == "Down"):
            cs = 1.0
        else:
            cs = -1.0

        price_f = price if outcome == "Up" else (1.0 - price)
        mtm = cs * (1 - price_f) * size   # up_wins=1

        total_our += mtm
        print(f"  [{i+1:2}] side={side:<5} outcome={outcome:<5} price={price:.4f} size={size:.4f}  "
              f"cs={cs:+.0f}  price_f={price_f:.4f}  mtm={mtm:+.4f}")

    api_total = float(pos.get("cashPnl",0) or 0) + float(pos.get("realizedPnl",0) or 0)
    print(f"  Our MTM sum:    {total_our:+.4f}  (only Down-token position)")
    print(f"  API total:      {api_total:+.4f}")
    print(f"  Gap:            {total_our - api_total:+.4f}")

    # Also check the corresponding Up position in the same market
    up_pos = next((p2 for p2 in positions
                   if p2.get("conditionId","").lower() == cid
                   and p2.get("outcome","") == "Up"), None)
    if up_pos:
        print(f"  Matching Up pos: size={up_pos['size']:.2f} avg={up_pos['avgPrice']:.4f} "
              f"cash={float(up_pos.get('cashPnl',0) or 0):+.3f} real={float(up_pos.get('realizedPnl',0) or 0):+.3f}")
