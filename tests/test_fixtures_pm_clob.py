"""Test Polymarket schemas against saved live-capture fixtures."""
import json
from decimal import Decimal
from pathlib import Path

from pm_research.schemas.polymarket import (
    BookMsg,
    NewMarketMsg,
    PriceChangeMsg,
    parse_ws_frame,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "pm_clob" / "2026-05-26_btc5m"


def _load(name: str) -> dict:  # type: ignore[type-arg]
    return json.loads((FIXTURE_DIR / name).read_text())


def test_book_fixture_parses() -> None:
    raw = _load("book.json")
    msg = BookMsg.model_validate(raw)
    assert msg.hash == "3c15c202a0d87c611d1af43fcc02098a770139db"
    assert msg.bids[0].price == Decimal("0.01")
    assert msg.bids[0].size == Decimal("16903.48")
    assert msg.timestamp_ns == 1_779_782_571_250_000_000


def test_book_no_event_type_dispatch() -> None:
    raw = _load("book.json")
    assert "event_type" not in raw
    result = parse_ws_frame(raw)
    assert isinstance(result, BookMsg)


def test_price_change_fixture_parses() -> None:
    raw = _load("price_change.json")
    msg = PriceChangeMsg.model_validate(raw)
    assert len(msg.price_changes) == 2
    assert msg.price_changes[0].side == "BUY"
    assert msg.price_changes[0].best_bid == Decimal("0.5")
    assert msg.price_changes[1].side == "SELL"
    assert msg.timestamp_ns == 1_779_782_572_795_000_000


def test_new_market_fixture_parses() -> None:
    raw = _load("new_market.json")
    msg = NewMarketMsg.model_validate(raw)
    assert msg.active is False
    assert msg.slug == "btc-updown-5m-1779868500"
    assert len(msg.clob_token_ids) == 2
    assert msg.fee_schedule.taker_only is True
    assert msg.fee_schedule.rate == "0.07"


def test_new_market_dispatch() -> None:
    raw = _load("new_market.json")
    result = parse_ws_frame(raw)
    assert isinstance(result, NewMarketMsg)


def test_all_fixtures_decimal_no_float_loss() -> None:
    book = BookMsg.model_validate(_load("book.json"))
    # Spot-check a few levels
    assert str(book.bids[1].size) == "3954.05"
    assert str(book.bids[6].size) == "556.42"
    assert str(book.bids[25].size) == "1510.59"
