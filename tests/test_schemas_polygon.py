"""Test Polygon schema construction and decimal conversion."""

from pm_research.schemas.polygon import Erc20Transfer, OrderFilled, _raw_to_decimal


def test_raw_to_decimal_6dp() -> None:
    assert _raw_to_decimal(1_000_000) == "1"
    assert _raw_to_decimal(1_500_000) == "1.5"
    assert _raw_to_decimal(100) == "0.0001"


def test_order_filled_from_decoded() -> None:
    of = OrderFilled.from_decoded(
        order_hash="0xabc",
        maker="0xMaker",
        taker="0xTaker",
        side=0,
        token_id=12345,
        maker_amount=5_000_000,
        taker_amount=5_000_000,
        fee=50_000,
        builder=b"\x00" * 32,
        metadata=b"\xff" * 32,
        exchange="0xE111180000d2663C0091e4f400237545B87B996B",
        block_number=65_000_000,
        block_hash="0xblock",
        tx_hash="0xtx",
        log_index=0,
        t_recv_ns=1_779_782_571_250_000_000,
    )
    assert of.event == "OrderFilled"
    assert of.maker_amount_decimal == "5"
    assert of.fee_decimal == "0.05"
    assert of.builder == "00" * 32
    assert of.metadata == "ff" * 32
    assert of.side == 0


def test_erc20_transfer_model() -> None:
    t = Erc20Transfer(
        event="Transfer",
        token="0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB",
        from_="0xsender",
        to="0xrecipient",
        amount_raw="10000000",
        amount_decimal="10",
        block_number=65_000_001,
        block_hash="0xbh",
        tx_hash="0xtx",
        log_index=1,
        t_recv_ns=0,
    )
    assert t.amount_decimal == "10"
