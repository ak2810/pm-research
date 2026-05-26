"""Polymarket CLOB WebSocket schemas.

Wire format facts (live-captured 2026-05-26, docs/VERIFIED_FACTS.md):
- All prices/sizes are JSON strings → Decimal via str, never float.
- All timestamps are millisecond strings → int64 ns via ms_to_ns().
- book frames have NO event_type field — detected by bids+asks presence.
- Server sends either a single object or an array per recv frame.
- Field naming: snake_case throughout.
"""
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator


class _Base(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)


# ── Order book level ──────────────────────────────────────────────────────────

class Level(BaseModel):
    model_config = ConfigDict(frozen=True)

    price: Decimal
    size: Decimal

    @field_validator("price", "size", mode="before")
    @classmethod
    def parse_decimal(cls, v: object) -> Decimal:
        return Decimal(str(v))


# ── Server message types ──────────────────────────────────────────────────────

class BookMsg(_Base):
    """Snapshot sent on subscribe and after gap recovery. No event_type field."""
    market: str
    asset_id: str
    timestamp: str
    hash: str
    bids: list[Level]
    asks: list[Level]

    @property
    def timestamp_ns(self) -> int:
        return int(self.timestamp) * 1_000_000


class PriceChange(_Base):
    asset_id: str
    price: Decimal
    size: Decimal
    side: Literal["BUY", "SELL"]
    hash: str
    best_bid: Decimal
    best_ask: Decimal

    @field_validator("price", "size", "best_bid", "best_ask", mode="before")
    @classmethod
    def parse_decimal(cls, v: object) -> Decimal:
        return Decimal(str(v))


class PriceChangeMsg(_Base):
    event_type: Literal["price_change"]
    market: str
    price_changes: list[PriceChange]
    timestamp: str

    @property
    def timestamp_ns(self) -> int:
        return int(self.timestamp) * 1_000_000


class FeeSchedule(_Base):
    exponent: str
    rate: str
    taker_only: bool
    rebate_rate: str


class EventMessage(_Base):
    id: str
    ticker: str
    slug: str
    title: str
    description: str


class NewMarketMsg(_Base):
    event_type: Literal["new_market"]
    id: str
    question: str
    market: str
    slug: str
    description: str
    assets_ids: list[str]
    outcomes: list[str]
    event_message: EventMessage
    timestamp: str
    tags: list[object]
    condition_id: str
    active: bool
    clob_token_ids: list[str]
    sports_market_type: str
    line: str
    game_start_time: str
    order_price_min_tick_size: str
    group_item_title: str
    taker_base_fee: str
    fees_enabled: bool
    fee_schedule: FeeSchedule

    @property
    def timestamp_ns(self) -> int:
        return int(self.timestamp) * 1_000_000


class MarketResolvedMsg(_Base):
    """Actual wire shape (live-captured 2026-05-26):
    {id, market, assets_ids, winning_asset_id, winning_outcome,
     event_message, timestamp, event_type, tags}
    Note: event_message can be None on settlement.
    """
    event_type: Literal["market_resolved"]
    id: str
    market: str
    assets_ids: list[str]
    winning_asset_id: str
    winning_outcome: str
    event_message: object | None = None
    timestamp: str
    tags: list[object] = []

    @property
    def timestamp_ns(self) -> int:
        return int(self.timestamp) * 1_000_000


class LastTradePriceMsg(_Base):
    event_type: Literal["last_trade_price"]
    asset_id: str
    market: str
    price: Decimal
    side: Literal["BUY", "SELL"]
    size: Decimal
    fee_rate_bps: str
    timestamp: str

    @field_validator("price", "size", mode="before")
    @classmethod
    def parse_decimal(cls, v: object) -> Decimal:
        return Decimal(str(v))

    @property
    def timestamp_ns(self) -> int:
        return int(self.timestamp) * 1_000_000


class TickSizeChangeMsg(_Base):
    event_type: Literal["tick_size_change"]
    asset_id: str
    market: str
    old_tick_size: str
    new_tick_size: str
    timestamp: str

    @property
    def timestamp_ns(self) -> int:
        return int(self.timestamp) * 1_000_000


class BestBidAskMsg(_Base):
    event_type: Literal["best_bid_ask"]
    asset_id: str
    market: str
    best_bid: Decimal
    best_ask: Decimal
    spread: Decimal
    timestamp: str

    @field_validator("best_bid", "best_ask", "spread", mode="before")
    @classmethod
    def parse_decimal(cls, v: object) -> Decimal:
        return Decimal(str(v))

    @property
    def timestamp_ns(self) -> int:
        return int(self.timestamp) * 1_000_000


# ── Internal collector events (not from server) ───────────────────────────────

class SubscribeAck(_Base):
    event_type: Literal["subscribe_ack"]
    asset_ids: list[str]
    timestamp: str


class DisconnectEvent(_Base):
    event_type: Literal["disconnect"]
    reason: str
    timestamp: str


class ReconnectEvent(_Base):
    event_type: Literal["reconnect"]
    attempt: int
    timestamp: str


class HeartbeatEvent(_Base):
    event_type: Literal["heartbeat"]
    timestamp: str


class MarketClosedEvent(_Base):
    event_type: Literal["market_closed"]
    market: str
    condition_id: str
    timestamp: str


# ── Dispatch ──────────────────────────────────────────────────────────────────

def parse_ws_frame(raw: dict[str, object]) -> (
    BookMsg
    | PriceChangeMsg
    | NewMarketMsg
    | MarketResolvedMsg
    | LastTradePriceMsg
    | TickSizeChangeMsg
    | BestBidAskMsg
):
    """Parse a single WS frame dict into the appropriate model.

    book frames are identified by presence of bids+asks (no event_type).
    All other types dispatch on event_type.
    """
    if "bids" in raw and "asks" in raw:
        return BookMsg.model_validate(raw)

    etype = raw.get("event_type")
    if etype == "price_change":
        return PriceChangeMsg.model_validate(raw)
    if etype == "new_market":
        return NewMarketMsg.model_validate(raw)
    if etype == "market_resolved":
        return MarketResolvedMsg.model_validate(raw)
    if etype == "last_trade_price":
        return LastTradePriceMsg.model_validate(raw)
    if etype == "tick_size_change":
        return TickSizeChangeMsg.model_validate(raw)
    if etype == "best_bid_ask":
        return BestBidAskMsg.model_validate(raw)

    raise ValueError(f"Unknown WS frame event_type={etype!r}")
