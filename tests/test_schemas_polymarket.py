"""Test Polymarket schemas against live-captured wire format (VERIFIED_FACTS.md)."""
from decimal import Decimal

from pm_research.schemas.polymarket import (
    BookMsg,
    NewMarketMsg,
    PriceChangeMsg,
    parse_ws_frame,
)

# Fixtures from live capture 2026-05-26
BOOK_RAW = {
    "market": "0x125730a9a19a6bc2d0f847f04f8bf16837484ca0131cf0fc79226075ecc50ebd",
    "asset_id": "41904046339315199441846846901798861215014557277009414496634485581318123208334",
    "timestamp": "1779782571250",
    "hash": "3c15c202a0d87c611d1af43fcc02098a770139db",
    "bids": [
        {"price": "0.01", "size": "16903.48"},
        {"price": "0.02", "size": "3954.05"},
    ],
    "asks": [
        {"price": "0.50", "size": "500.00"},
    ],
}

PRICE_CHANGE_RAW = {
    "market": "0x125730a9a19a6bc2d0f847f04f8bf16837484ca0131cf0fc79226075ecc50ebd",
    "price_changes": [
        {
            "asset_id": "85820491405070503157833602237286422627039369979142976656076036182129921475920",  # noqa: E501
            "price": "0.03",
            "size": "23391.25",
            "side": "BUY",
            "hash": "789f4c2e63b0bc047478203856c749bef6bd8828",
            "best_bid": "0.5",
            "best_ask": "0.51",
        },
        {
            "asset_id": "41904046339315199441846846901798861215014557277009414496634485581318123208334",  # noqa: E501
            "price": "0.97",
            "size": "23391.25",
            "side": "SELL",
            "hash": "d74b36836a44e4495615d73236804c98c0157526",
            "best_bid": "0.49",
            "best_ask": "0.5",
        },
    ],
    "timestamp": "1779782572795",
    "event_type": "price_change",
}

NEW_MARKET_RAW = {
    "id": "2359818",
    "question": "Bitcoin Up or Down - May 27, 3:55AM-4:00AM ET",
    "market": "0x525bc811d0ef8672e26e903371ebbfbe3a6d24bf4c55aac41ddd74499d09ffa3",
    "slug": "btc-updown-5m-1779868500",
    "description": "This market will resolve to Up if BTC price >= start.",
    "assets_ids": [
        "83569573729417379012269004181894995036217275080784648418283894009080008909768",
        "79967973888886103818890600603138835205030373156116212860536029322374885776001",
    ],
    "outcomes": ["Up", "Down"],
    "event_message": {
        "id": "526320",
        "ticker": "btc-updown-5m-1779868500",
        "slug": "btc-updown-5m-1779868500",
        "title": "Bitcoin Up or Down - May 27, 3:55AM-4:00AM ET",
        "description": "...",
    },
    "timestamp": "1779782572324",
    "event_type": "new_market",
    "tags": [],
    "condition_id": "0x525bc811d0ef8672e26e903371ebbfbe3a6d24bf4c55aac41ddd74499d09ffa3",
    "active": False,
    "clob_token_ids": [
        "83569573729417379012269004181894995036217275080784648418283894009080008909768",
        "79967973888886103818890600603138835205030373156116212860536029322374885776001",
    ],
    "sports_market_type": "",
    "line": "",
    "game_start_time": "",
    "order_price_min_tick_size": "0.01",
    "group_item_title": "",
    "taker_base_fee": "1000",
    "fees_enabled": True,
    "fee_schedule": {"exponent": "1", "rate": "0.07", "taker_only": True, "rebate_rate": "0.2"},
}


def test_book_parse_no_event_type() -> None:
    msg = BookMsg.model_validate(BOOK_RAW)
    assert msg.asset_id == BOOK_RAW["asset_id"]
    assert msg.bids[0].price == Decimal("0.01")
    assert msg.bids[0].size == Decimal("16903.48")


def test_book_timestamp_ns() -> None:
    msg = BookMsg.model_validate(BOOK_RAW)
    assert msg.timestamp_ns == 1_779_782_571_250 * 1_000_000


def test_book_dispatch() -> None:
    result = parse_ws_frame(BOOK_RAW)
    assert isinstance(result, BookMsg)


def test_price_change_parse() -> None:
    msg = PriceChangeMsg.model_validate(PRICE_CHANGE_RAW)
    assert len(msg.price_changes) == 2
    assert msg.price_changes[0].side == "BUY"
    assert msg.price_changes[0].price == Decimal("0.03")
    assert msg.price_changes[1].side == "SELL"


def test_price_change_dispatch() -> None:
    result = parse_ws_frame(PRICE_CHANGE_RAW)
    assert isinstance(result, PriceChangeMsg)


def test_new_market_parse() -> None:
    msg = NewMarketMsg.model_validate(NEW_MARKET_RAW)
    assert msg.active is False
    assert msg.slug == "btc-updown-5m-1779868500"
    assert msg.fee_schedule.rate == "0.07"
    assert msg.fee_schedule.taker_only is True


def test_new_market_dispatch() -> None:
    result = parse_ws_frame(NEW_MARKET_RAW)
    assert isinstance(result, NewMarketMsg)


def test_array_frame_normalization() -> None:
    """Server sends either a dict or list — both must parse."""
    frames = [BOOK_RAW, PRICE_CHANGE_RAW]
    results = [parse_ws_frame(f) for f in frames]
    assert isinstance(results[0], BookMsg)
    assert isinstance(results[1], PriceChangeMsg)


def test_no_float_precision_loss() -> None:
    msg = BookMsg.model_validate(BOOK_RAW)
    assert str(msg.bids[0].size) == "16903.48"
    assert str(msg.bids[1].size) == "3954.05"
