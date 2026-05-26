import time

from pm_research.clock import ms_to_ns, now_ns


def test_now_ns_positive() -> None:
    ns = now_ns()
    assert ns > 0


def test_now_ns_monotone() -> None:
    a = now_ns()
    time.sleep(0.001)
    b = now_ns()
    assert b > a


def test_ms_to_ns() -> None:
    assert ms_to_ns("1000") == 1_000_000_000
    assert ms_to_ns("1779782571250") == 1_779_782_571_250_000_000


def test_ms_to_ns_zero() -> None:
    assert ms_to_ns("0") == 0
