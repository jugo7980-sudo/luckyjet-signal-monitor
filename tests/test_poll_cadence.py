from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from luckyjet_signal.settings import Config
from luckyjet_signal import direct_only
import luckyjet_fast_poll


def test_default_poll_interval_is_one_tenth_second():
    assert Config.__dataclass_fields__["poll_seconds"].default == 0.1


def test_source_backoff_sequence_and_success_reset_to_point_one():
    delay = 0.1
    seen = []
    for _ in range(5):
        delay = direct_only.next_poll_delay(False, delay)
        seen.append(delay)
    assert seen == [0.25, 0.5, 1.0, 2.0, 2.0]
    assert direct_only.next_poll_delay(True, delay) == 0.1


def test_poll_sleep_targets_start_to_start_cadence_not_request_plus_sleep():
    assert direct_only.poll_sleep_seconds(10.0, 0.1, 10.04) == 0.06
    assert direct_only.poll_sleep_seconds(10.0, 0.1, 10.10) == 0.0
    assert direct_only.poll_sleep_seconds(10.0, 0.1, 10.25) == 0.0


def test_preferred_source_hedge_is_faster_than_healthy_poll_interval():
    assert luckyjet_fast_poll.PREFERRED_HEDGE_SECONDS <= 0.05
