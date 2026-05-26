"""Verify keccak256 topic0 hashes match canonical ABI event signatures.

Regression guard: if any signature or hash is wrong, on-chain log decoding
silently drops every event of that type.
"""
import pytest
from eth_utils import keccak  # type: ignore[import-untyped]

from pm_research.collectors.polygon_indexer import _TOPIC

_SIGNATURES = {
    "OrderFilled": "OrderFilled(bytes32,address,address,uint8,uint256,uint256,uint256,uint256,bytes32,bytes32)",  # noqa: E501
    "OrdersMatched": "OrdersMatched(bytes32,address,uint8,uint256,uint256,uint256)",
    "OrderPreapproved": "OrderPreapproved(bytes32)",
    "FeeCharged": "FeeCharged(address,uint256)",
    "Transfer": "Transfer(address,address,uint256)",
    "TransferSingle": "TransferSingle(address,address,address,uint256,uint256)",
    "TransferBatch": "TransferBatch(address,address,address,uint256[],uint256[])",
    "PositionSplit": "PositionSplit(address,address,bytes32,bytes32,uint256[],uint256)",
    "PositionsMerge": "PositionsMerge(address,address,bytes32,bytes32,uint256[],uint256)",
    "PayoutRedemption": "PayoutRedemption(address,address,bytes32,bytes32,uint256[],uint256)",
    "ConditionPreparation": "ConditionPreparation(bytes32,address,bytes32,uint256)",
    "ConditionResolution": "ConditionResolution(bytes32,address,bytes32,uint256,uint256[])",
}


@pytest.mark.parametrize("name,sig", _SIGNATURES.items())
def test_topic0_matches_keccak(name: str, sig: str) -> None:
    expected = "0x" + keccak(text=sig).hex()
    assert _TOPIC[name] == expected, (
        f"{name}: _TOPIC has {_TOPIC[name]!r}, keccak({sig!r}) = {expected!r}"
    )
