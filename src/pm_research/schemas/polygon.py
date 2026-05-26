"""Polygon on-chain event schemas for V2 contracts.

All contract addresses in docs/VERIFIED_FACTS.md.
V2 OrderFilled includes builder + metadata (bytes32) fields — new vs V1.
OrderCancelled does NOT exist in V2.
Amounts: amount_raw (uint256 string) + amount_decimal (6dp string).
"""
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

_DECIMALS = 6
_UNIT = Decimal(10 ** _DECIMALS)


def _raw_to_decimal(raw: str | int) -> str:
    return str(Decimal(str(raw)) / _UNIT)


class _Base(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    block_number: int
    block_hash: str
    tx_hash: str
    log_index: int
    t_recv_ns: int


# ── CTF Exchange V2 ───────────────────────────────────────────────────────────

class OrderFilled(_Base):
    """OrderFilled(bytes32 orderHash, address maker, address taker, uint8 side,
    uint256 tokenId, uint256 makerAmountFilled, uint256 takerAmountFilled,
    uint256 fee, bytes32 builder, bytes32 metadata)
    """
    event: Literal["OrderFilled"]
    order_hash: str
    maker: str
    taker: str
    side: Literal[0, 1]              # 0=BUY 1=SELL
    token_id: str                    # uint256 as decimal string
    maker_amount_raw: str
    maker_amount_decimal: str        # 6dp
    taker_amount_raw: str
    taker_amount_decimal: str        # 6dp
    fee_raw: str
    fee_decimal: str                 # 6dp
    builder: str                     # bytes32 hex (no 0x)
    metadata: str                    # bytes32 hex (no 0x)
    exchange: str                    # contract address

    @classmethod
    def from_decoded(
        cls,
        *,
        order_hash: str,
        maker: str,
        taker: str,
        side: int,
        token_id: int,
        maker_amount: int,
        taker_amount: int,
        fee: int,
        builder: bytes,
        metadata: bytes,
        exchange: str,
        block_number: int,
        block_hash: str,
        tx_hash: str,
        log_index: int,
        t_recv_ns: int,
    ) -> "OrderFilled":
        return cls(
            event="OrderFilled",
            order_hash=order_hash,
            maker=maker.lower(),
            taker=taker.lower(),
            side=side,  # type: ignore[arg-type]
            token_id=str(token_id),
            maker_amount_raw=str(maker_amount),
            maker_amount_decimal=_raw_to_decimal(maker_amount),
            taker_amount_raw=str(taker_amount),
            taker_amount_decimal=_raw_to_decimal(taker_amount),
            fee_raw=str(fee),
            fee_decimal=_raw_to_decimal(fee),
            builder=builder.hex(),
            metadata=metadata.hex(),
            exchange=exchange.lower(),
            block_number=block_number,
            block_hash=block_hash,
            tx_hash=tx_hash,
            log_index=log_index,
            t_recv_ns=t_recv_ns,
        )


class OrdersMatched(_Base):
    """OrdersMatched(bytes32 takerOrderHash, address takerOrderMaker, uint8 side,
    uint256 tokenId, uint256 makerAmountFilled, uint256 takerAmountFilled)
    """
    event: Literal["OrdersMatched"]
    taker_order_hash: str
    taker_order_maker: str
    side: Literal[0, 1]
    token_id: str
    maker_amount_raw: str
    maker_amount_decimal: str
    taker_amount_raw: str
    taker_amount_decimal: str
    exchange: str


class OrderPreapproved(_Base):
    event: Literal["OrderPreapproved"]
    order_hash: str
    exchange: str


class FeeCharged(_Base):
    event: Literal["FeeCharged"]
    receiver: str
    amount_raw: str
    amount_decimal: str
    exchange: str


# ── Conditional Tokens (ERC-1155) ─────────────────────────────────────────────

class TransferSingle(_Base):
    event: Literal["TransferSingle"]
    operator: str
    from_: str
    to: str
    token_id: str                    # outcome token id (uint256 decimal string)
    amount_raw: str
    amount_decimal: str              # 6dp


class TransferBatch(_Base):
    event: Literal["TransferBatch"]
    operator: str
    from_: str
    to: str
    token_ids: list[str]
    amounts_raw: list[str]
    amounts_decimal: list[str]       # 6dp each


class PositionSplit(_Base):
    event: Literal["PositionSplit"]
    stakeholder: str
    collateral_token: str
    parent_collection_id: str
    condition_id: str
    partition: list[int]
    amount_raw: str
    amount_decimal: str


class PositionsMerge(_Base):
    event: Literal["PositionsMerge"]
    stakeholder: str
    collateral_token: str
    parent_collection_id: str
    condition_id: str
    partition: list[int]
    amount_raw: str
    amount_decimal: str


class PayoutRedemption(_Base):
    event: Literal["PayoutRedemption"]
    redeemer: str
    collateral_token: str
    parent_collection_id: str
    condition_id: str
    index_sets: list[int]
    payout_raw: str
    payout_decimal: str


class ConditionPreparation(_Base):
    event: Literal["ConditionPreparation"]
    condition_id: str
    oracle: str
    question_id: str
    outcome_slot_count: int


class ConditionResolution(_Base):
    event: Literal["ConditionResolution"]
    condition_id: str
    oracle: str
    question_id: str
    outcome_slot_count: int
    payout_numerators: list[int]


# ── ERC-20 Transfer (pUSD / USDC.e) ──────────────────────────────────────────

class Erc20Transfer(_Base):
    event: Literal["Transfer"]
    token: str                       # contract address (pUSD or USDC.e)
    from_: str
    to: str
    amount_raw: str
    amount_decimal: str              # 6dp
