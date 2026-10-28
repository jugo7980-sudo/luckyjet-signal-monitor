import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import app_v1


class Gate:
    def __init__(self):
        self.current_streak = []


def test_seed_private_streak_keeps_only_current_sub_two_run():
    gate = Gate()
    app_v1.seed_private_streak(
        gate,
        ["1.40x", "1.30x", "1.20x", "2.10x", "1.10x"],
    )
    assert gate.current_streak == ["1.20x", "1.30x", "1.40x"]


def test_seed_private_streak_handles_invalid_values():
    gate = Gate()
    app_v1.seed_private_streak(gate, ["1.50x", "bad", "1.40x"])
    assert gate.current_streak == ["1.40x", "1.50x"]
