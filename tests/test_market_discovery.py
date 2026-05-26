"""Test market discovery filter handles all horizons: 5m, 15m, hourly."""
import json

from pm_research.collectors.polymarket_clob import PolymarketClobCollector
from pm_research.storage.raw_writer import RawWriter


def _make_collector(tmp_path: object) -> PolymarketClobCollector:
    writer = RawWriter.__new__(RawWriter)
    writer._queue = type("Q", (), {"empty": lambda s: True, "put_nowait": lambda s, x: None})()  # type: ignore[assignment]
    writer._dropped = 0
    allowed = frozenset(["btc", "eth", "xrp", "sol", "doge"])
    return PolymarketClobCollector(writer=writer, allowed_assets=allowed)  # type: ignore[arg-type]


def _market(slug: str, accepting: bool = True, neg_risk: bool = False) -> dict:  # type: ignore[type-arg]
    token_ids = ["111", "222"]
    return {
        "slug": slug,
        "acceptingOrders": accepting,
        "negRisk": neg_risk,
        "clobTokenIds": json.dumps(token_ids),
        "conditionId": "0xcondition",
    }


def test_5m_market_discovered(tmp_path: object) -> None:
    c = _make_collector(tmp_path)
    c._consider_market(_market("btc-updown-5m-1779868500"))
    assert "111" in c._subscribed


def test_15m_market_discovered(tmp_path: object) -> None:
    c = _make_collector(tmp_path)
    c._consider_market(_market("btc-updown-15m-1779868500"))
    assert "111" in c._subscribed


def test_hourly_market_discovered(tmp_path: object) -> None:
    c = _make_collector(tmp_path)
    c._consider_market(_market("bitcoin-up-or-down-may-27-2026-4am-et"))
    assert "111" in c._subscribed


def test_neg_risk_skipped(tmp_path: object) -> None:
    c = _make_collector(tmp_path)
    c._consider_market(_market("btc-updown-5m-1779868500", neg_risk=True))
    assert "111" not in c._subscribed


def test_not_accepting_orders_skipped(tmp_path: object) -> None:
    c = _make_collector(tmp_path)
    c._consider_market(_market("btc-updown-15m-1779868500", accepting=False))
    assert "111" not in c._subscribed


def test_unknown_asset_skipped(tmp_path: object) -> None:
    c = _make_collector(tmp_path)
    c._consider_market(_market("link-updown-15m-1779868500"))
    assert "111" not in c._subscribed


def test_eth_15m_discovered(tmp_path: object) -> None:
    c = _make_collector(tmp_path)
    c._consider_market(_market("eth-updown-15m-1779868800"))
    assert "111" in c._subscribed
