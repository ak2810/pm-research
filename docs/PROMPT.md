	# PROMPT FOR CLAUDE CODE — POLYMARKET RESEARCH INFRASTRUCTURE

	> Paste this entire document as the opening message to a Claude Code session. Do not modify the rules. Provide your own `.env` values at the indicated locations only.

	---

	## 0. ROLE AND MISSION

	You are a senior infrastructure engineer building a production-grade data collection, storage, and analysis platform for reverse-engineering market-making strategies on Polymarket's short-dated cryptocurrency prediction markets (BTC / ETH / XRP / SOL "Up or Down" markets at 1-minute, 5-minute, 15-minute, and 1-hour horizons).

	You will build the entire system end-to-end. You will not ask the user questions. You will resolve ambiguity yourself by reading official documentation, fetching live API responses, reading contract ABIs, and reading the source of official client libraries. If you cannot resolve a question that way, you write an experiment that empirically determines the answer, run it, and proceed.

	**The single most important component is data recording. A single dropped event, a single un-timestamped record, a single floating-point rounding of a price is a critical defect.** Everything else exists to serve the integrity of the recorded data.

	---

	## 1. ABSOLUTE RULES (NON-NEGOTIABLE)

	### 1.1 Anti-hallucination

	1. You may never invent an API endpoint, contract address, function signature, event signature, field name, error code, or schema. If you do not know, you fetch the official documentation or the live API response, **and you confirm by example before writing code against it**.
	2. Authoritative sources, in this order:
	   - The official documentation site: `https://docs.polymarket.com`
	   - The official GitHub organization: `https://github.com/Polymarket` (especially `py-clob-client`, `clob-client`, `ctf-exchange`, `conditional-tokens-contracts`)
	   - The live API endpoint's actual response when called
	   - The contract ABI fetched from Polygonscan (`https://api.etherscan.io/v2/api?chainid=1&action=balance&apikey=YourEtherscanApiKey` - 1 for Ethereum, `https://api.etherscan.io/v2/api?chainid=137&action=balance&apikey=YourEtherscanApiKey` - 137 for Polygon, `https://api.etherscan.io/v2/api?apikey=<apikey>&chainid=<chainid>&module=<module>&action=<action>&address=<address>`)
	   - For Binance: `https://binance-docs.github.io/apidocs/spot/en/`
	3. Training data is **not** an authoritative source. If you "remember" an endpoint, you still verify it before committing code that uses it.
	4. When you verify, save the verification: write the URL you fetched, the date, and the relevant excerpt into `docs/VERIFIED_FACTS.md`. Every contract address, every endpoint URL, every event signature must have a corresponding entry in that file with its source.

	### 1.2 No placeholders, ever

	The following strings must not appear anywhere in the code or comments of production paths (collectors, pipeline, indexer, signing, secrets):

	```
	TODO   FIXME   XXX   HACK   "for now"   "later"   "stub"   "placeholder"
	NotImplementedError   "pass  # implement"   "example_value"   "YOUR_KEY"
	"foo"   "bar"   "test_address"
	```

	If you cannot implement something, you stop, resolve it, then continue. You do not leave a marker.

	The only exception is `.env.example` which is allowed to contain non-secret example values clearly labeled as such.

	### 1.3 Decision-making without the user

	You will face choices. Apply this rule, in this strict order of priority, to resolve them:

	1. The choice that loses no data over the choice that loses some.
	2. The choice that survives a disconnect/restart over the choice that does not.
	3. The choice that records more context over the choice that records less.
	4. The choice that uses nanosecond/millisecond timestamps over second timestamps.
	5. The choice that uses `Decimal` or fixed-point integers over floats for any monetary value.
	6. The choice with stronger schema validation over the choice with weaker validation.
	7. The choice that is reproducible from raw inputs over the choice that requires manual steps.
	8. The choice with more storage cost over the choice with less storage cost (storage is cheap; data is irreplaceable).

	When you make a non-obvious choice, append an entry to `DECISIONS.md` with: the question, the options considered, the decision, the reasoning, the date.

	### 1.4 Code quality

	1. Python 3.11+ exclusively.
	2. Strict type hints throughout. `mypy --strict` must pass. Configure `pyproject.toml` accordingly.
	3. `ruff check` and `ruff format` clean.
	4. All money uses `decimal.Decimal` with explicit context (`Decimal('0.001')` not `Decimal(0.001)`). Never `float` for prices, sizes, USDC amounts, or fees.
	5. All timestamps stored as `int64` nanoseconds since Unix epoch, UTC. Never local time. Never strings in storage (human-readable ISO strings in logs only, alongside the nanosecond integer).
	6. Configuration via `pydantic-settings` reading from `.env`. No secrets in code or in `git`.
	7. Logging via `structlog` with JSON renderer. Every log entry includes: `timestamp_ns`, `component`, `level`, `event`, plus context fields.
	8. Every external call has: timeout, retry with exponential backoff (1s, 2s, 4s, 8s, max 30s), structured error logging including the request and response.
	9. Every websocket has: auto-reconnect, exponential backoff, re-subscribe on reconnect, snapshot re-fetch on reconnect, sequence-number verification, heartbeat every 60s, disconnect logging with gap duration.
	10. Every long-running process runs under `systemd` with `Restart=always`, `RestartSec=5`, logs to both journald and a rotated file.
	11. Tests with `pytest`. Every parser, every reconstruction routine, every join, every Decimal conversion has a unit test. Integration tests for each collector via recorded fixtures.
	12. Dependencies pinned exactly (`requirements.txt` with `==`).
	13. `pre-commit` hooks: `ruff`, `mypy`, `pytest -x` on changed test files.

	### 1.5 What you must not build

	- No Docker, Kubernetes, or container orchestration. Plain systemd on one EC2 instance.
	- No heavyweight databases. SQLite for tiny local state. Postgres only if absolutely required (it isn't). Parquet on S3 for everything else.
	- No GraphQL, no gRPC. REST and websockets only.
	- No webapp. No dashboard UI. CLI tools and Jupyter notebooks for analysis.
	- No machine learning frameworks beyond `scikit-learn` for linear regression in analysis. No neural nets, no PyTorch, no TensorFlow.
	- No features the user did not ask for.

	---

	## 2. ARCHITECTURE

	### 2.1 Topology

	- **1 × EC2 instance** in `eu-west-1`, `t3.large` (2 vCPU, 8 GB RAM), 200 GB `gp3` EBS, Ubuntu 24.04 LTS.
	- **1 × S3 bucket** in `eu-west-1`, versioning on, lifecycle: standard → IA at 30 days → Glacier IR at 90 days → Deep Archive at 365 days.
	- **1 × Polygon RPC** via Alchemy or QuickNode (free tier first; user will upgrade if needed). HTTP and WSS endpoints.
	- **1 × Healthchecks.io** account (free tier) for heartbeat monitoring.
	- **1 × Discord webhook** for alerts.

	### 2.2 Services on the EC2 box

	All systemd units, all `Restart=always`:

	| Service | Purpose |
	|---|---|
	| `pm-clob-collector.service` | Polymarket CLOB websocket recorder |
	| `binance-collector.service` | Binance public websocket recorder |
	| `polygon-indexer.service` | Polygon chain event indexer |
	| `pm-metadata-snapshotter.service` | Polymarket Gamma API markets metadata snapshotter (timer-driven, every 5 min) |
	| `wallet-attribution.service` | Wallet graph discovery (timer-driven, daily) |
	| `pipeline-rotator.service` | Watches for rotated raw files, converts to Parquet, uploads to S3 (timer-driven, every 5 min) |
	| `heartbeat-watchdog.service` | Local watcher that verifies all services are emitting heartbeats; alerts to Discord on failure |

	### 2.3 Filesystem layout

	```
	/opt/pm-research/             # code, owned by pm-research user
	├── pyproject.toml
	├── requirements.txt
	├── .env.example
	├── README.md
	├── DECISIONS.md
	├── docs/
	│   ├── VERIFIED_FACTS.md
	│   ├── RUNBOOK.md
	│   └── SCHEMA.md
	├── src/pm_research/
	│   ├── __init__.py
	│   ├── config.py           # pydantic-settings
	│   ├── clock.py            # NTP verification, time_ns wrappers
	│   ├── logging.py          # structlog setup
	│   ├── heartbeat.py        # Healthchecks.io + Discord client
	│   ├── storage/
	│   │   ├── raw_writer.py   # JSONL.gz append + hourly rotation
	│   │   └── s3.py
	│   ├── schemas/            # pydantic models for every event
	│   │   ├── envelope.py
	│   │   ├── polymarket.py
	│   │   ├── binance.py
	│   │   └── polygon.py
	│   ├── collectors/
	│   │   ├── polymarket_clob.py
	│   │   ├── binance.py
	│   │   └── polygon_indexer.py
	│   ├── pipeline/
	│   │   ├── jsonl_to_parquet.py
	│   │   ├── parquet_schemas.py
	│   │   └── s3_uploader.py
	│   ├── metadata/
	│   │   ├── markets_snapshotter.py
	│   │   └── wallet_attribution.py
	│   ├── analysis/
	│   │   ├── load.py
	│   │   ├── enrich.py
	│   │   ├── fair_value.py
	│   │   ├── realized_vol.py
	│   │   ├── residuals.py
	│   │   └── book_reconstruction.py
	│   └── cli.py              # entry points
	├── systemd/
	│   ├── pm-clob-collector.service
	│   ├── binance-collector.service
	│   ├── polygon-indexer.service
	│   ├── pm-metadata-snapshotter.service
	│   ├── pm-metadata-snapshotter.timer
	│   ├── wallet-attribution.service
	│   ├── wallet-attribution.timer
	│   ├── pipeline-rotator.service
	│   ├── pipeline-rotator.timer
	│   └── heartbeat-watchdog.service
	├── scripts/
	│   ├── install.sh          # idempotent install on fresh EC2
	│   ├── verify_clock.sh
	│   └── acceptance/         # acceptance test scripts
	└── tests/
		├── unit/
		├── integration/
		└── fixtures/

	/var/pm-research/            # runtime data, owned by pm-research user
	├── raw/
	│   ├── pm_clob/             # hourly: YYYY-MM-DD-HH.jsonl.gz
	│   ├── binance/
	│   ├── polygon/
	│   └── pm_meta/
	├── state/
	│   ├── polygon_indexer.cursor
	│   ├── pm_clob.subscriptions
	│   └── wallet_graph.json
	└── logs/

	/etc/pm-research/.env        # secrets, mode 0600, owned by pm-research user
	```

	### 2.4 Data flow

	```
	Websocket events  →  in-memory bounded queue (10k)  →  JSONL.gz writer  →  /var/pm-research/raw/<feed>/YYYY-MM-DD-HH.jsonl.gz
																							  │
												hourly rotation (atomic rename)               │
																							  ▼
												  pipeline-rotator timer (every 5 min)  ─────►  validates file
																							  │
																							  ▼
																				  Parquet conversion (ZSTD)
																							  │
																							  ▼
																  S3 upload: s3://bucket/raw/feed=X/date=YYYY-MM-DD/hour=HH/data.parquet
																							  │
																				  head-object verification
																							  │
																							  ▼
																				local .jsonl.gz deleted only after S3 confirms
	```

	---

	## 3. COMPONENT SPECIFICATIONS

	### 3.1 Library: `clock.py`

	- Verify `chrony` is installed and synchronized; if drift > 50ms vs. multiple NTP sources, refuse to start collectors and exit 1.
	- `now_ns() -> int` returns `time.time_ns()`.
	- `verify_clock_sync()` is called at every collector startup.

	### 3.2 Library: `heartbeat.py`

	- HTTP POST every 60s to Healthchecks.io URL from `.env`.
	- Payload includes structured stats per service.
	- On Healthchecks.io failure, also POST to Discord webhook.

	### 3.3 Library: `storage/raw_writer.py`

	- Append-only writer to gzipped JSONL.
	- Hourly rotation at the top of the hour, UTC. Atomic: write to `<hour>.jsonl.gz.tmp`, on rotation `fsync` then rename.
	- Within the writer: own thread with bounded queue (size 10000). Queue full → log warning with metric, drop event, never block the producer.
	- On every rotation, log: events_written, bytes_written, queue_max_depth_seen_this_hour.

	### 3.4 Collector: Polymarket CLOB

	**Endpoints to verify before coding** (verify exact paths via `https://docs.polymarket.com` and `github.com/Polymarket/py-clob-client`):

	- CLOB REST base: `https://clob.polymarket.com`
	- CLOB websocket: `wss://ws-subscriptions-clob.polymarket.com/ws/<channel>` — channel is likely `market` for public data; verify
	- Gamma API base: `https://gamma-api.polymarket.com`
	- Data API base: `https://data-api.polymarket.com`

	**Verification step at startup:**
	1. `GET <clob_rest>/` and log the response
	2. Open the websocket and observe the initial handshake; record the exact frame format
	3. `GET <gamma>/markets?limit=1` and confirm the response shape
	4. Save these as fixtures in `tests/fixtures/pm_responses/`

	**Market discovery (every 30 seconds):**

	Pull markets via Gamma API with pagination. Filter to active markets where:
	- `question` (lowercased) matches the regex pattern that captures the short-dated crypto Up-Down markets. Determine the exact regex empirically by inspecting actual market questions; do not assume.
	- Market is not yet resolved
	- `end_date_iso` is within the next 6 hours

	For each matching market, extract both `token_id` values (the YES and NO outcome tokens).

	Maintain `active_subscriptions: dict[token_id, MarketContext]`. On new market, subscribe and request a fresh book snapshot. On resolution/expiry, write a final `market_closed` event and unsubscribe.

	**Subscriptions per token_id** (verify exact channel names):
	- Book updates (full snapshot + deltas)
	- Last trade
	- Tick size change
	- Price change

	**Envelope schema** (every event written to JSONL.gz):

	```python
	class Envelope(BaseModel):
		t_recv_ns: int               # local nanoseconds, UTC
		t_recv_iso: str              # ISO8601 derived from t_recv_ns
		feed: Literal["pm_clob"]
		event_type: str              # "book_snapshot" | "book_delta" | "trade" | "tick_size_change" | "price_change" | "subscribe_ack" | "disconnect" | "reconnect" | "heartbeat" | "market_closed"
		asset_id: str | None         # token_id for market events
		market: str | None           # condition_id
		seq: int | None              # if the venue provides one
		raw: dict                    # the full unparsed payload, lossless
	```

	**Reliability requirements:**
	- Disconnect detection: missing pong within 5s OR missing any message for 30s → reconnect.
	- On reconnect: re-subscribe to every active token_id, immediately request fresh book snapshot for each. Log `reconnect` event including `gap_ns`, `prev_disconnect_t_ns`.
	- Book reconstruction: maintain an in-memory book per token_id. On every delta, verify sequence continuity. On sequence gap, request fresh snapshot.
	- The in-memory book state itself is **not** written to JSONL (that's redundant; the raw deltas are the source of truth), but a `book_snapshot` event is written whenever you take one.

	**Heartbeat payload:**
	```json
	{
	  "service": "pm-clob-collector",
	  "events_received_last_60s": int,
	  "queue_depth": int,
	  "queue_max_depth_60s": int,
	  "active_subscriptions": int,
	  "last_disconnect_ago_s": int | null,
	  "reconnects_last_60s": int
	}
	```

	### 3.5 Collector: Binance

	**Endpoint** (verify via `https://binance-docs.github.io/apidocs/spot/en/`):
	- Combined stream: `wss://stream.binance.com:9443/stream?streams=<stream1>/<stream2>/...`

	**Streams subscribed**, for symbols `btcusdt`, `ethusdt`, `xrpusdt`, `solusdt`:
	- `<symbol>@aggTrade`
	- `<symbol>@bookTicker`
	- `<symbol>@depth@100ms`
	- `<symbol>@kline_1m`

	**Pre-emptive reconnect:** Binance disconnects after 24h. Reconnect proactively at 23h with overlap (open new socket, drain old after 10s of overlap with deduplication on `(stream, E)`).

	**Book reconstruction (depth stream):**
	1. Open WS, buffer incoming depth events
	2. `GET https://api.binance.com/api/v3/depth?symbol=<SYMBOL>&limit=5000`
	3. Discard buffered events where `u < snapshot.lastUpdateId`
	4. First event to apply: `U <= lastUpdateId + 1 <= u`
	5. Then apply each event verifying `pu` chain
	6. On any gap, re-snapshot
	*(Verify exact rules from Binance docs "How to manage a local order book correctly".)*

	**Envelope:**
	```python
	class BinanceEnvelope(BaseModel):
		t_recv_ns: int
		t_recv_iso: str
		feed: Literal["binance"]
		stream: str
		symbol: str
		event_type: str  # "aggTrade" | "bookTicker" | "depth" | "kline"
		t_event_ms: int  # the "E" field from Binance
		raw: dict
	```

	### 3.6 Collector: Polygon Indexer

	**Contracts to index** — VERIFY each address by:
	1. Fetching `https://docs.polymarket.com` and finding the contracts section
	2. Cross-referencing with `https://github.com/Polymarket/ctf-exchange` and `https://github.com/Polymarket/conditional-tokens-contracts`
	3. Fetching the ABI from Polygonscan
	4. Recording each in `docs/VERIFIED_FACTS.md` with source URL and date

	Contracts you must index:
	- **CTF Exchange** (the standard one)
	- **Neg Risk CTF Exchange** (separate contract — the short-dated crypto markets typically live here; verify per market)
	- **Neg Risk Adapter** (if relevant for resolution event tracking)
	- **Conditional Tokens Framework** (for `TransferSingle` / `TransferBatch` events on tracked addresses)
	- **USDC.e on Polygon** (for tracking funding flows between wallets)

	**Events to capture** (verify exact signatures from ABI):
	From each Exchange contract:
	- `OrderFilled` — every fill, includes maker, taker, asset IDs, amounts, fee
	- `OrderCancelled` — every cancellation
	- `OrdersMatched` — bulk matching event if present
	- `FeeCharged` — if present

	From CTF:
	- `TransferSingle` and `TransferBatch` filtered to tracked addresses (loaded from wallet_graph.json)

	From USDC.e:
	- `Transfer` filtered to tracked addresses

	**Approach:**
	- Real-time: `eth_subscribe` over WSS RPC for `logs` filtered to the contract addresses
	- Backfill: `eth_getLogs` paginated by block range (page size respecting RPC provider limits — Alchemy free tier is 2000 blocks per query; verify your provider's actual limit and configure accordingly)
	- Cursor stored in `/var/pm-research/state/polygon_indexer.cursor` (last fully-processed block number)
	- **Reorg handling:** on every startup, re-process the last 128 blocks. Deduplicate downstream by `(block_hash, log_index)` primary key in Parquet partitioning.

	**Envelope:**
	```python
	class PolygonEnvelope(BaseModel):
		t_recv_ns: int
		t_recv_iso: str
		feed: Literal["polygon"]
		event_name: str         # "OrderFilled" etc.
		contract_address: str   # lowercase 0x...
		block_number: int
		block_hash: str
		block_timestamp: int    # unix seconds
		tx_hash: str
		log_index: int
		decoded: dict           # decoded event args with Decimal-safe stringified amounts
		raw_log: dict           # the raw log object from web3
	```

	**Decimal handling:** Event amounts come as `uint256`. Store both:
	- `decoded.amount_raw`: the integer as a string
	- `decoded.amount_decimal`: a string representation of `Decimal(raw) / Decimal(10 ** decimals)` with the correct decimals per asset (USDC = 6, CTF outcome tokens = 6 on Polymarket — verify)

	Never float. Strings in the envelope so JSONL stays clean; reconstruct to `Decimal` on read.

	### 3.7 Service: Markets Metadata Snapshotter

	- Runs every 5 minutes via systemd timer.
	- Iterates Gamma API `/markets` with pagination, fetches every active market plus all markets that resolved in the last 7 days.
	- For each market, captures: `condition_id`, `question_id`, `slug`, `question`, both `token_id`s, `start_date_iso`, `end_date_iso`, `resolution_source`, `fee_bps`, `tick_size`, `status`, `outcome`, `closed_at`.
	- Writes to `/var/pm-research/raw/pm_meta/YYYY-MM-DD-HH.jsonl.gz` using the envelope schema.

	### 3.8 Service: Wallet Attribution

	Initial tracked seeds, loaded from `config/seed_wallets.yaml`:
	- `gabagool22` — resolve via `GET https://data-api.polymarket.com/profile?username=gabagool22` (verify endpoint)
	- `ohanism` — same
	- Additional names the user provides later

	For each seed:
	1. Resolve the proxy wallet address and the underlying signer address from the profile API
	2. Query the last 90 days of `TransferSingle` (CTF) and `Transfer` (USDC.e) events involving these addresses
	3. Any wallet receiving funding from a seed becomes a "candidate sub-account"; recurse one level (sub-accounts of sub-accounts)
	4. Output: `wallet_graph.json` with adjacency list, timestamps of funding, total amounts
	5. Upload to S3 daily

	### 3.9 Pipeline: JSONL.gz → Parquet → S3

	Triggered every 5 min by `pipeline-rotator.timer`. For each `.jsonl.gz` file that has been rotated (i.e., not the currently-written-to file):

	1. Open the gzip file streaming
	2. Parse every line; if any line fails to parse, log the line number and the error and continue (do not abort the whole file, but record the failure count in S3 metadata)
	3. Build a Polars DataFrame with the strict per-feed schema from `pipeline/parquet_schemas.py`
	4. Write Parquet with `compression='zstd'`, `compression_level=6`, `statistics=True`
	5. Upload to S3 at `s3://<bucket>/raw/feed=<feed>/date=YYYY-MM-DD/hour=HH/data.parquet`
	6. Verify via `head_object` AND read back first 100 rows
	7. Only after successful verification: delete the local `.jsonl.gz`
	8. Log: input_lines, output_rows, parse_errors, compression_ratio, upload_bytes

	**Parquet schemas** must be exact and stable. Define them in `pipeline/parquet_schemas.py` with `polars.DataType` precision. `t_recv_ns` is always `Int64`. Decimal money fields stored as `Decimal(38, 18)` Polars decimal type.

	### 3.10 Heartbeat Watchdog

	A local service that:
	- Reads recent journald entries every 60s
	- Verifies each of the seven services has emitted a heartbeat in the last 90s
	- On miss: posts to Discord and to Healthchecks.io (the absence-detector hits there too)
	- Verifies disk usage on `/var/pm-research` is < 80%; alerts if not

	### 3.11 Analysis Library (runs on local PC, reads from S3)

	Pure-Python module the user runs in Jupyter. Functions:

	```python
	def load_fills(
		start: datetime, end: datetime,
		wallets: list[str] | None = None,
		contracts: list[str] | None = None,
	) -> pl.DataFrame: ...

	def reconstruct_book_at(token_id: str, t_ns: int) -> Book: ...

	def realized_vol(symbol: str, t_ns: int, window_s: int) -> Decimal: ...

	def fair_value_down(
		spot: Decimal, strike: Decimal,
		sigma_annualized: Decimal, time_to_expiry_seconds: int,
	) -> Decimal:
		"""Digital option fair value: P(S_T < K) under GBM with zero drift over short horizon."""

	def enrich_fills(fills: pl.DataFrame) -> pl.DataFrame:
		"""Adds: pm_mid_at_fill, pm_book_imbalance, binance_mid_at_fill,
		realized_vol_60s, realized_vol_300s, realized_vol_900s,
		time_to_expiry_s, moneyness, fair_value_under_<each_candidate_model>,
		residual_under_<each_candidate_model>, our_position_before, our_position_after."""

	def regress_residuals(
		enriched: pl.DataFrame,
		features: list[str],
		target: str = "residual_under_ewma_300",
	) -> RegressionResult: ...
	```

	Notebooks delivered:
	1. `01_data_quality.ipynb` — verify no gaps, sequence integrity, clock drift
	2. `02_fair_value_fitting.ipynb` — sweep σ estimators against ohanism + gabagool22 fills
	3. `03_edge_function.ipynb` — regress residuals on features
	4. `04_inventory_model.ipynb` — position-trajectory analysis
	5. `05_quote_fingerprint.ipynb` — book-update pattern analysis to identify their resting orders
	6. `06_pnl_reconciliation.ipynb` — reconstruct each operator's PnL from on-chain fills and cross-check against their public profile

	---

	## 4. SECRETS AND CONFIG

	`.env` keys (the user will populate; you produce a complete `.env.example` documenting each):

	```
	# AWS
	AWS_REGION=eu-west-1
	S3_BUCKET=<user-provided>

	# Polygon RPC
	POLYGON_RPC_HTTP=<user-provided>
	POLYGON_RPC_WSS=<user-provided>

	# Polymarket
	PM_CLOB_REST=https://clob.polymarket.com
	PM_CLOB_WSS=<verified-from-docs>
	PM_GAMMA_API=https://gamma-api.polymarket.com
	PM_DATA_API=https://data-api.polymarket.com

	# Monitoring
	HEALTHCHECKS_URL_PM_CLOB=<user-provided>
	HEALTHCHECKS_URL_BINANCE=<user-provided>
	HEALTHCHECKS_URL_POLYGON=<user-provided>
	HEALTHCHECKS_URL_PIPELINE=<user-provided>
	DISCORD_WEBHOOK_URL=<user-provided>

	# Tracked seed wallets file
	SEED_WALLETS_PATH=/opt/pm-research/config/seed_wallets.yaml

	# Storage paths
	RAW_DATA_DIR=/var/pm-research/raw
	STATE_DIR=/var/pm-research/state
	```

	You must never log secrets. Configure `structlog` to redact known secret fields.

	---

	## 5. EXECUTION PLAN

	Build in strict order. Do not skip ahead. After each step, verify it works end-to-end before moving on.

	1. **Repo bootstrap**: pyproject.toml, ruff/mypy/pytest config, pre-commit, README skeleton.
	2. **Verify environment**: write `scripts/verify_clock.sh` and run it; ensure chrony installed and synchronized.
	3. **Library layer**: `config.py`, `clock.py`, `logging.py`, `heartbeat.py`, `storage/raw_writer.py`, `storage/s3.py`. Full unit tests.
	4. **Schemas**: pydantic envelope models for every feed.
	5. **VERIFIED_FACTS.md**: by reading official docs and querying live endpoints, populate all API endpoints and contract addresses with verification evidence. **This file must be complete and accurate before any collector is written.**
	6. **Metadata snapshotter** first (simplest, validates Polymarket API access).
	7. **Binance collector** (no auth, simplest websocket).
	8. **Polymarket CLOB collector**.
	9. **Polygon indexer** with backfill capability.
	10. **Pipeline (JSONL → Parquet → S3)**.
	11. **Wallet attribution**.
	12. **Heartbeat watchdog**.
	13. **Systemd units and install.sh**.
	14. **Acceptance tests** (Section 6).
	15. **Analysis library**.
	16. **Notebooks**.
	17. **RUNBOOK.md** and final documentation.

	---

	## 6. ACCEPTANCE TESTS

	The system is not complete until **all** of these pass:

	### 6.1 Per-collector, after 1 hour of live running

	- File exists at `s3://<bucket>/raw/feed=<feed>/date=<today>/hour=<HH>/data.parquet`
	- File reads back with `pl.read_parquet`; row count > 0
	- All `t_recv_ns` values are within the hour
	- All envelopes pass pydantic validation
	- For PM CLOB: every subscribed `token_id` has at least one `book_snapshot` + ≥1 `book_delta`; no sequence gaps within any token's stream
	- For Binance: all four symbols present in all four streams; pre-emptive reconnect verified by observing the 23h timer (this one is verified in a 24h+ run, optional for initial acceptance)
	- For Polygon: every block in the hour is processed; cursor file matches latest block

	### 6.2 End-to-end reconciliation

	Pick a known operator (e.g., `gabagool22`). For a 24-hour window where the system was running:
	- Count `OrderFilled` events from your Polygon data where maker or taker is in their wallet graph
	- Compare to the fill count visible on their public Polymarket profile for that window
	- Reconstruct their realized PnL from your data
	- Compare to the PnL shown on their profile
	- **Accept threshold: counts match exactly; PnL within ±0.1%**

	If reconciliation fails, the indexer is missing events (probably a missed contract — Neg Risk vs. standard). Fix and re-run.

	### 6.3 Reliability

	- Kill each collector process. Verify systemd restarts within 5s.
	- Disconnect the network (`iptables -A OUTPUT -p tcp --dport 443 -j DROP` for 60s, then remove). Verify clean reconnect with snapshot re-fetch and no data corruption.
	- Fill the disk to 95% on `/var`. Verify alerts fire. Then free space, verify recovery.
	- Restart the EC2 instance. Verify all services come back up and the polygon indexer resumes from its cursor.

	### 6.4 Code quality gates

	- `ruff check .` clean
	- `ruff format --check .` clean
	- `mypy --strict src/` clean
	- `pytest` all pass
	- `grep -rEn 'TODO|FIXME|XXX|HACK|NotImplementedError|placeholder|"foo"|"bar"' src/` returns zero matches

	### 6.5 Documentation gates

	- `README.md` documents: prerequisites, fresh-install procedure on a blank EC2 box, expected first-hour behavior
	- `docs/RUNBOOK.md` documents: how to diagnose each alert type, how to backfill missing data, how to rotate secrets
	- `docs/VERIFIED_FACTS.md` documents every endpoint and contract address with source URL and verification date
	- `DECISIONS.md` documents every non-obvious choice

	---

	## 7. INVESTIGATION PATTERNS (HOW TO RESOLVE QUESTIONS YOURSELF)

	When you don't know something, here is how you find out. In order.

	### 7.1 "What is the exact websocket message format Polymarket sends for a book update?"

	1. Read `https://docs.polymarket.com/` websocket section.
	2. Read the source of `https://github.com/Polymarket/py-clob-client` — search for "ws", "subscribe", "book".
	3. Open the websocket with `websockets` library, subscribe to a known active market, log every frame for 60 seconds.
	4. Save the captured frames as fixtures.
	5. Write the parser against the fixtures.

	### 7.2 "What is the exact `OrderFilled` event signature on the Neg Risk Exchange?"

	1. Find the Neg Risk Exchange contract address (via Polymarket docs / GitHub).
	2. Fetch the ABI: `'https://api.etherscan.io/v2/api?apikey=<apikey>&chainid=<chainid>&module=<module>&action=<action>&address=<address>'`.
	3. Find the `OrderFilled` entry in the ABI; record the parameters.
	4. Cross-check by fetching one real `OrderFilled` log from a recent block and decoding it; confirm the fields make sense.
	5. Document in `VERIFIED_FACTS.md`.

	### 7.3 "What's the decimal precision for outcome tokens?"

	1. The CTF (Conditional Tokens) contract is an ERC-1155 — outcome tokens don't have an inherent decimal field per se. Read `https://github.com/gnosis/conditional-tokens-contracts`.
	2. Cross-check: on Polymarket, the convention is 6 decimals (matching USDC). Verify by fetching a known position and dividing the raw balance by `10**6`; confirm it matches the human-readable share count on the Polymarket UI.
	3. Document.

	### 7.4 "How exactly does Polymarket resolve a 5-minute BTC Up-Down market?"

	1. Read the resolution source field on the Gamma API response for one such market.
	2. Read the market's UMA Optimistic Oracle resolution proposal on-chain.
	3. Pick a known resolved market; reconstruct what the resolution would have been from Binance data; confirm match.
	4. Document.

	### 7.5 General rule

	For every "I think it is X" thought you have: write `# verified: <source url>, fetched <date>` above the line of code that depends on it. If you cannot write that comment honestly, you have not verified yet.

	---

	## 8. FINAL CHECKLIST BEFORE DECLARING DONE

	You may not declare the project complete until you can mark every box:

	- [ ] All seven systemd services installed and `systemctl status` shows `active (running)` for each
	- [ ] One full hour of data captured in S3 from each of the three collectors
	- [ ] Acceptance tests in section 6 pass, including reconciliation against a public operator profile
	- [ ] `grep -rEn 'TODO|FIXME|XXX|HACK|NotImplementedError|placeholder' src/` returns zero matches
	- [ ] `mypy --strict src/` clean
	- [ ] `ruff check .` and `ruff format --check .` clean
	- [ ] All tests pass
	- [ ] Heartbeats arriving at Healthchecks.io for every service
	- [ ] Disconnect/reconnect test passes for both websocket collectors with no data corruption
	- [ ] EC2 reboot test passes; indexer resumes from cursor
	- [ ] `docs/VERIFIED_FACTS.md` has an entry for every external endpoint and every contract address
	- [ ] `DECISIONS.md` has entries for every non-obvious choice
	- [ ] `README.md` enables a fresh install on a blank EC2 box in under 30 minutes
	- [ ] Analysis notebooks run end-to-end on at least 24 hours of collected data
	- [ ] Operator-PnL reconciliation matches public profile within ±0.1%

	When all boxes are checked, write a final report in `docs/HANDOFF.md` summarizing: what was built, every external fact verified and where, every decision made and why, every known limitation, and the operational runbook.

	---

	## 9. WHAT YOU DO NOT DO

	- Do not "scaffold" components and come back to fill them in.
	- Do not write `pass` in a function body and move on.
	- Do not catch exceptions silently.
	- Do not assume; verify.
	- Do not ask the user questions. Resolve everything yourself.
	- Do not skip the verification of contract addresses.
	- Do not use floats for any monetary or price value, ever.
	- Do not store timestamps as strings in storage.
	- Do not exceed the scope defined in this prompt by adding features.
	- Do not declare done until every checkbox in section 8 is checked.

	Begin.
