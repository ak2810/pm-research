"""Binance WebSocket schemas.

Combined stream format: each frame is {"stream": "<name>", "data": {...}}.
"""
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator


class _Base(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)


class AggTrade(_Base):
    """@aggTrade stream."""
    e: Literal["aggTrade"]
    E: int          # event time ms
    s: str          # symbol
    a: int          # agg trade id
    p: Decimal      # price
    q: Decimal      # quantity
    f: int          # first trade id
    l: int          # last trade id  # noqa: E741
    T: int          # trade time ms
    m: bool         # maker side

    @field_validator("p", "q", mode="before")
    @classmethod
    def parse_decimal(cls, v: object) -> Decimal:
        return Decimal(str(v))


class BookTicker(_Base):
    """@bookTicker stream."""
    u: int          # order book update id
    s: str          # symbol
    b: Decimal      # best bid price
    B: Decimal      # best bid qty
    a: Decimal      # best ask price
    A: Decimal      # best ask qty

    @field_validator("b", "B", "a", "A", mode="before")
    @classmethod
    def parse_decimal(cls, v: object) -> Decimal:
        return Decimal(str(v))


class DepthLevel(BaseModel):
    model_config = ConfigDict(frozen=True)

    price: Decimal
    qty: Decimal

    @field_validator("price", "qty", mode="before")
    @classmethod
    def parse_decimal(cls, v: object) -> Decimal:
        return Decimal(str(v))


class DepthUpdate(_Base):
    """@depth@100ms partial depth update."""
    e: Literal["depthUpdate"]
    E: int          # event time ms
    s: str          # symbol
    U: int          # first update id in event
    u: int          # final update id in event
    b: list[list[Any]]   # bids [[price, qty], ...]
    a: list[list[Any]]   # asks

    def bids_parsed(self) -> list[DepthLevel]:
        return [DepthLevel(price=row[0], qty=row[1]) for row in self.b]

    def asks_parsed(self) -> list[DepthLevel]:
        return [DepthLevel(price=row[0], qty=row[1]) for row in self.a]


class KlineData(_Base):
    t: int          # kline start ms
    T: int          # kline end ms
    s: str          # symbol
    i: str          # interval
    f: int          # first trade id
    L: int          # last trade id
    o: Decimal      # open
    c: Decimal      # close
    h: Decimal      # high
    l: Decimal      # low  # noqa: E741
    v: Decimal      # base asset volume
    n: int          # number of trades
    x: bool         # is kline closed

    @field_validator("o", "c", "h", "l", "v", mode="before")
    @classmethod
    def parse_decimal(cls, v: object) -> Decimal:
        return Decimal(str(v))


class KlineMsg(_Base):
    """@kline_1m stream."""
    e: Literal["kline"]
    E: int
    s: str
    k: KlineData


class CombinedFrame(_Base):
    """Wrapper frame from combined stream endpoint."""
    stream: str
    data: dict[str, Any]
