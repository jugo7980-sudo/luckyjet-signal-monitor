"""A code deployment must never invoke a destructive Redis reset."""
from pathlib import Path


def test_automatic_startup_does_not_delete_user_history():
    source = (Path(__file__).resolve().parents[1] / "app_v1.py").read_text()
    assert "apply_complete_data_reset" not in source
    assert "Full data reset applied" not in source
    assert "await monitor.load_state(config)" in source
    assert "await redis.flushdb()" not in source
    assert "redis.unlink(" not in source
