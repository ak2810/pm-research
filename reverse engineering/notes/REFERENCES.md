# REFERENCES

Papers and documentation cited in this project.

---

## Market Making Theory

1. **Avellaneda & Stoikov (2008)** — "High-frequency trading in a limit order
   book." *Quantitative Finance*, 8(3), 217–224.
   Core A-S market maker model: inventory aversion γ, half-spread formula,
   reservation price.

2. **Glosten & Milgrom (1985)** — "Bid, ask and transaction prices in a
   specialist market with heterogeneously informed traders." *Journal of
   Financial Economics*, 14(1), 71–100.
   Adverse selection framework; spread decomposition into informed/uninformed.

3. **Black (1976)** — "The pricing of commodity contracts." *Journal of
   Financial Economics*, 3(1-2), 167-179.
   Zero-drift GBM for futures/binary options; base for the digital fair-value
   formula.

## Volatility Estimation

4. **RiskMetrics Technical Document (1996)** — J.P. Morgan.
   EWMA volatility update rule; λ=0.94 daily / λ=0.97 intraday calibration.

5. **Parkinson (1980)** — "The extreme value method for estimating the variance
   of the rate of return." *Journal of Business*, 53(1), 61–65.
   High-low Parkinson range estimator.

## Inverse Reinforcement Learning

6. **Ziebart et al. (2008)** — "Maximum entropy inverse reinforcement learning."
   *AAAI*, 2008.
   MaxEnt IRL foundation for Layer 5.

## Polymarket / Digital Options

7. **Polymarket CLOB Documentation** — https://docs.polymarket.com
   API endpoints, market structure, fee schedule.

8. **CTF Exchange V2 ABI** — Polygonscan verified source
   https://polygonscan.com/address/0xE111180000d2663C0091e4f400237545B87B996B#code
