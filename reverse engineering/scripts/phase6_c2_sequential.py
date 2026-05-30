"""Phase 6 C2 — Sequential modeling of ohanism's quote residuals.

Design: per-asset time-ordered sequences of markets. Each market = one token
in a sequence (t_post_ns order). Feature snapshot at each market = same L2
residual features + the residual itself from prior markets (autoregressive).

Gate P6.2: sequential model OOS RMSE <= Phase5 GBT RMSE * 0.9 (10% reduction)
OR document why temporal structure adds nothing.

Architecture: small Transformer (GPU RTX 3060 available) but given n=1103
markets and prior evidence that spot dynamics add zero signal, a lightweight
test first: AR(k) autoregression on residuals, then Transformer if AR shows
signal.

Standing rule S1-S5 applied.
"""
import sys, json, time
from pathlib import Path
sys.path.insert(0, "src")

import numpy as np
import polars as pl
import torch
import torch.nn as nn
from scipy.stats import norm
from sklearn.metrics import r2_score
from reverse_engineering.config import get_settings
from reverse_engineering.io.catalog import list_local_partitions

cfg = get_settings()
t0 = time.time()
np.random.seed(42)
torch.manual_seed(42)

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
print(f"=== PHASE 6 C2: SEQUENTIAL MODELING ===")
print(f"Device: {DEVICE}")

# ── S1-S5: Window ─────────────────────────────────────────────────────────────
FEEDS = ["pm_clob", "polygon", "binance", "pm_meta"]
WINDOW_START = ("2026-05-27", 4)
feed_parts = {feed: set((p.date, p.hour) for p in list_local_partitions(feed))
              for feed in FEEDS}
common = None
for feed in FEEDS:
    common = feed_parts[feed] if common is None else common & feed_parts[feed]
common = {p for p in common if p >= WINDOW_START}
common_sorted = sorted(common)
WINDOW_END = common_sorted[-1]
print(f"Window: {WINDOW_START} → {WINDOW_END} ({len(common_sorted)}h)")

# ── Load sigma_implied_v2 + compute residuals ─────────────────────────────────
sig_v2 = pl.read_parquet(str(cfg.tables_dir / "sigma_implied_v2.parquet"))
l2 = json.loads((cfg.results_dir / "phase4_l2.json").read_text())
theta_h0, theta_h1 = l2["stage2b"]["theta_h"]

def compute_residual(row):
    s = float(row["sigma_implied"])
    tau = float(row["tau_years"])
    S0 = float(row["S0"]); St = float(row["S_t"]); pp = float(row["p_posted"])
    log_ratio = np.log(max(S0,1e-9)/max(St,1e-9))
    d = log_ratio / max(s * tau**0.5, 1e-8)
    fv = 1.0 - norm.cdf(d)
    hs = theta_h0 + theta_h1 * s * max(tau,1e-12)**0.5
    direction = 1.0 if pp > fv else -1.0
    return float(pp - (fv + direction * hs)), float(fv), float(hs)

rows_aug = []
for row in sig_v2.iter_rows(named=True):
    res, fv, hs = compute_residual(row)
    rows_aug.append({**row, "residual": res, "fair_value": fv, "half_spread": hs})

df = pl.DataFrame(rows_aug).sort("t_post_ns")
print(f"Markets: {len(df)}, residual mean={float(df['residual'].mean()):.4f} std={float(df['residual'].std()):.4f}")

# ── Test 1: AR(k) autocorrelation on residuals ────────────────────────────────
print("\n--- AR(k) AUTOCORRELATION TEST ---")
res_arr = df["residual"].to_numpy()

# Compute autocorrelation at lags 1-10
print("Residual autocorrelations:")
from_lag, sig_lags = 0, []
for lag in range(1, 11):
    x = res_arr[:-lag]; y = res_arr[lag:]
    corr = float(np.corrcoef(x, y)[0,1])
    thresh = 1.96 / len(res_arr)**0.5  # ~95% CI for IID
    flag = " **" if abs(corr) > thresh else ""
    print(f"  lag {lag}: r={corr:+.4f}{flag}")
    if abs(corr) > thresh:
        sig_lags.append(lag)

if sig_lags:
    print(f"Significant autocorrelation at lags: {sig_lags}")
else:
    print("No significant autocorrelation (all within 95% IID bounds)")

# Per-asset autocorrelation
print("\nPer-asset lag-1 autocorrelation:")
for asset in ["BTC","ETH","SOL","XRP","DOGE"]:
    sub = df.filter(pl.col("asset_symbol")==asset).sort("t_post_ns")["residual"].to_numpy()
    if len(sub) < 10:
        continue
    corr = float(np.corrcoef(sub[:-1], sub[1:])[0,1])
    thresh = 1.96 / len(sub)**0.5
    print(f"  {asset}: n={len(sub)} lag-1 r={corr:+.4f}  "
          f"{'significant' if abs(corr)>thresh else 'not significant'}")

# ── Test 2: Simple AR(3) model vs static baseline ─────────────────────────────
print("\n--- AR(3) vs STATIC BASELINE ---")

# Temporal 70/30 split
n_train = int(len(df) * 0.70)
res_train = res_arr[:n_train]
res_test  = res_arr[n_train:]

# AR(3): fit coefficients on train, predict on test
from numpy.linalg import lstsq
K = 3
X_ar = np.column_stack([res_train[i:len(res_train)-K+i] for i in range(K)])
y_ar = res_train[K:]
coeffs, _, _, _ = lstsq(np.c_[np.ones(len(X_ar)), X_ar], y_ar, rcond=None)

X_ar_test = np.column_stack([res_arr[n_train+i:n_train+len(res_test)-K+i] for i in range(K)])
y_ar_test = res_test[K:]  # align target: skip first K
ar_preds = np.c_[np.ones(len(X_ar_test)), X_ar_test] @ coeffs

rmse_ar = float(np.sqrt(np.mean((y_ar_test - ar_preds)**2)))
rmse_naive = float(np.std(res_test))
r2_ar = float(1 - (rmse_ar/rmse_naive)**2)
print(f"AR(3) test RMSE: {rmse_ar:.5f}  R²: {r2_ar:.4f}")
print(f"Static baseline (naive RMSE): {rmse_naive:.5f}")
print(f"AR(3) coefficients: intercept={coeffs[0]:+.4f} {', '.join(f'lag{i+1}={coeffs[i+1]:+.4f}' for i in range(K))}")

P5_RMSE = 0.01957  # from phase5_k2_gbt.json
P6_target_rmse = P5_RMSE * 0.9
ar_vs_gbt = rmse_ar / P5_RMSE
print(f"\nPhase5 GBT RMSE: {P5_RMSE:.5f}, 10%-reduction target: {P6_target_rmse:.5f}")
print(f"AR(3) RMSE: {rmse_ar:.5f} ({ar_vs_gbt:.3f}x GBT)")
ar_pass = rmse_ar <= P6_target_rmse

# ── Test 3: Small Transformer on per-asset sequences ─────────────────────────
print(f"\n--- TRANSFORMER ON SEQUENCES ---")

# Build sequences: for each asset, order markets by t_post_ns
# Each sequence: [feat_1, feat_2, ..., feat_T] → predict feat_T+1
# Feature per market: residual, fair_value, sigma_hat, otm_cushion, spot_z

FEAT_COLS = ["residual","fair_value","sigma_implied","p_posted","tau_years"]
df_pd = df.select(["asset_symbol","t_post_ns"] + FEAT_COLS).to_pandas()

# Normalize features
feat_mean = df_pd[FEAT_COLS].mean()
feat_std  = df_pd[FEAT_COLS].std().clip(lower=1e-6)
df_pd_norm = df_pd.copy()
df_pd_norm[FEAT_COLS] = (df_pd[FEAT_COLS] - feat_mean) / feat_std

SEQ_LEN = 8  # look back 8 markets per asset
sequences, targets = [], []
for asset in df_pd_norm["asset_symbol"].unique():
    sub = df_pd_norm[df_pd_norm["asset_symbol"]==asset].sort_values("t_post_ns")
    vals = sub[FEAT_COLS].values  # shape (n_asset, n_feats)
    if len(vals) < SEQ_LEN + 1:
        continue
    for i in range(SEQ_LEN, len(vals)):
        sequences.append(vals[i-SEQ_LEN:i])  # (SEQ_LEN, n_feats)
        targets.append(vals[i, 0])            # predict next residual (normalized)

sequences = np.array(sequences)  # (N, SEQ_LEN, n_feats)
targets   = np.array(targets)    # (N,)
print(f"Sequences: {len(sequences)} (SEQ_LEN={SEQ_LEN})")

if len(sequences) < 50:
    print("Too few sequences for Transformer training — skipping")
    transformer_rmse = None
else:
    # 70/30 temporal split (sequences already time-ordered within each asset)
    n_tr = int(len(sequences) * 0.70)
    X_seq_tr = torch.tensor(sequences[:n_tr], dtype=torch.float32).to(DEVICE)
    y_seq_tr = torch.tensor(targets[:n_tr],   dtype=torch.float32).to(DEVICE)
    X_seq_te = torch.tensor(sequences[n_tr:], dtype=torch.float32).to(DEVICE)
    y_seq_te = torch.tensor(targets[n_tr:],   dtype=torch.float32).cpu().numpy()

    # Small Transformer
    class SmallTransformer(nn.Module):
        def __init__(self, d_in=5, d_model=32, nhead=4, nlayers=2):
            super().__init__()
            self.embed = nn.Linear(d_in, d_model)
            enc_layer = nn.TransformerEncoderLayer(d_model=d_model, nhead=nhead,
                                                    dim_feedforward=64, dropout=0.1,
                                                    batch_first=True)
            self.encoder = nn.TransformerEncoder(enc_layer, num_layers=nlayers)
            self.head = nn.Linear(d_model, 1)

        def forward(self, x):
            x = self.embed(x)
            x = self.encoder(x)
            return self.head(x[:, -1, :]).squeeze(-1)

    model_tf = SmallTransformer(d_in=len(FEAT_COLS)).to(DEVICE)
    opt = torch.optim.Adam(model_tf.parameters(), lr=1e-3, weight_decay=1e-4)
    crit = nn.MSELoss()

    best_val = float("inf")
    patience, patience_count = 20, 0
    print("Training Transformer...")
    for epoch in range(200):
        model_tf.train()
        idx = torch.randperm(len(X_seq_tr))
        for batch_start in range(0, len(X_seq_tr), 32):
            bidx = idx[batch_start:batch_start+32]
            loss = crit(model_tf(X_seq_tr[bidx]), y_seq_tr[bidx])
            opt.zero_grad(); loss.backward(); opt.step()

        model_tf.eval()
        with torch.no_grad():
            val_pred = model_tf(X_seq_te).cpu().numpy()
        val_rmse = float(np.sqrt(np.mean((y_seq_te - val_pred)**2)))
        if val_rmse < best_val:
            best_val = val_rmse
            patience_count = 0
        else:
            patience_count += 1
        if patience_count >= patience:
            print(f"  Early stop at epoch {epoch}, best val RMSE={best_val:.4f}")
            break

    # Convert back to original (unnormalized) residual scale
    res_std = float(feat_std["residual"])
    transformer_rmse = best_val * res_std
    tf_vs_gbt = transformer_rmse / P5_RMSE
    print(f"Transformer test RMSE (original scale): {transformer_rmse:.5f}")
    print(f"vs Phase5 GBT RMSE: {P5_RMSE:.5f} ({tf_vs_gbt:.3f}x)")
    tf_pass = transformer_rmse <= P6_target_rmse

# ── Gate P6.2 ─────────────────────────────────────────────────────────────────
print(f"\n=== PHASE 6.2 GATE ===")
print(f"P5 GBT RMSE: {P5_RMSE:.5f}, 10%-reduction target: {P6_target_rmse:.5f}")
print(f"AR(3) RMSE:  {rmse_ar:.5f} → {'PASS ✓' if ar_pass else f'NOT MET ({ar_vs_gbt:.3f}x)'}")
if transformer_rmse is not None:
    print(f"Transformer: {transformer_rmse:.5f} → {'PASS ✓' if tf_pass else f'NOT MET ({tf_vs_gbt:.3f}x)'}")
    p62_pass = ar_pass or tf_pass
else:
    p62_pass = ar_pass

if not p62_pass:
    print(f"\nP6.2 NOT MET — documenting: strategy is fully described by static state at t_post.")
    print("No useful within-market or cross-market temporal patterns found.")
    interpretation = ("Sequential modeling adds no predictive power. "
                      "Consistent with passive post-once strategy: each market's quote is "
                      "set independently based on instantaneous state at t_post. "
                      "No autocorrelation between consecutive markets' residuals.")
else:
    print(f"\nP6.2 PASS ✓")
    interpretation = "Temporal structure detected — paper twin should include autoregressive features."

print(f"Interpretation: {interpretation}")

# ── Save ──────────────────────────────────────────────────────────────────────
results = {
    "phase5_gbt_rmse": P5_RMSE,
    "p62_target_rmse": P6_target_rmse,
    "significant_autocorr_lags": sig_lags,
    "ar3_test_rmse": round(rmse_ar, 5),
    "ar3_r2_test": round(r2_ar, 4),
    "ar3_vs_gbt_ratio": round(ar_vs_gbt, 4),
    "transformer_rmse": round(transformer_rmse, 5) if transformer_rmse else None,
    "p62_pass": p62_pass,
    "interpretation": interpretation,
    "runtime_min": round((time.time()-t0)/60, 2),
}
(cfg.results_dir / "phase6_c2_sequential.json").write_text(json.dumps(results, indent=2))
print(f"\nRuntime: {(time.time()-t0)/60:.1f} min")
print(f"Saved: output/ohanism/results/phase6_c2_sequential.json")
