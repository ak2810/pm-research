import subprocess
import time


def check_ntp_drift(max_drift_ms: int = 50) -> None:
    """Exit 1 if system clock drift exceeds max_drift_ms (via chronyc)."""
    try:
        result = subprocess.run(
            ["chronyc", "tracking"],  # noqa: S603, S607
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        for line in result.stdout.splitlines():
            if "System time" in line:
                parts = line.split()
                drift_s = float(parts[3])
                drift_ms = abs(drift_s) * 1000
                if drift_ms > max_drift_ms:
                    raise SystemExit(
                        f"NTP drift {drift_ms:.1f}ms exceeds {max_drift_ms}ms limit"
                    )
                return
    except FileNotFoundError:
        pass  # chronyc not available (dev environment); skip check


def now_ns() -> int:
    return time.time_ns()


def ms_to_ns(ms_str: str) -> int:
    return int(ms_str) * 1_000_000
