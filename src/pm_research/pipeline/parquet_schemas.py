"""Polars dtype schemas for each feed's Parquet output.

Money: Decimal(38, 18).
Timestamps: Int64 (nanoseconds UTC — not Timestamp type, avoids TZ ambiguity).
Addresses: String (lowercase hex).
Large integers (uint256 token IDs): String (decimal).
bytes32: String (lowercase hex, no 0x).
"""
import polars as pl

_DECIMAL = pl.Decimal(precision=38, scale=18)
_NS = pl.Int64
_ADDR = pl.String
_HASH = pl.String
_UINT256 = pl.String


PM_CLOB_BOOK = {
    "feed": pl.String,
    "t_recv_ns": _NS,
    "market": _ADDR,
    "asset_id": _UINT256,
    "timestamp_ns": _NS,
    "hash": _HASH,
    # bids / asks stored as JSON strings (nested structure → stringify)
    "bids_json": pl.String,
    "asks_json": pl.String,
    "event_type": pl.String,
}

PM_CLOB_PRICE_CHANGE = {
    "feed": pl.String,
    "t_recv_ns": _NS,
    "market": _ADDR,
    "timestamp_ns": _NS,
    "asset_id": _UINT256,
    "price": _DECIMAL,
    "size": _DECIMAL,
    "side": pl.String,
    "hash": _HASH,
    "best_bid": _DECIMAL,
    "best_ask": _DECIMAL,
    "event_type": pl.String,
}

POLYGON_ORDER_FILLED = {
    "feed": pl.String,
    "t_recv_ns": _NS,
    "block_number": pl.Int64,
    "block_hash": _HASH,
    "tx_hash": _HASH,
    "log_index": pl.Int32,
    "event": pl.String,
    "order_hash": _HASH,
    "maker": _ADDR,
    "taker": _ADDR,
    "side": pl.Int8,
    "token_id": _UINT256,
    "maker_amount_raw": _UINT256,
    "maker_amount_decimal": _DECIMAL,
    "taker_amount_raw": _UINT256,
    "taker_amount_decimal": _DECIMAL,
    "fee_raw": _UINT256,
    "fee_decimal": _DECIMAL,
    "builder": _HASH,
    "metadata": _HASH,
    "exchange": _ADDR,
}

POLYGON_ERC20_TRANSFER = {
    "feed": pl.String,
    "t_recv_ns": _NS,
    "block_number": pl.Int64,
    "block_hash": _HASH,
    "tx_hash": _HASH,
    "log_index": pl.Int32,
    "event": pl.String,
    "token": _ADDR,
    "from_": _ADDR,
    "to": _ADDR,
    "amount_raw": _UINT256,
    "amount_decimal": _DECIMAL,
}

BINANCE_AGG_TRADE = {
    "feed": pl.String,
    "t_recv_ns": _NS,
    "stream": pl.String,
    "e": pl.String,
    "E": _NS,
    "s": pl.String,
    "a": pl.Int64,
    "p": _DECIMAL,
    "q": _DECIMAL,
    "T": _NS,
    "m": pl.Boolean,
}
