import logging

from luckyjet_fast_poll import PollSummary


def test_success_logs_aggregate_and_failures_remain_visible(caplog):
    caplog.set_level(logging.INFO, logger='luckyjet.fast_poll')
    summary = PollSummary()
    for second in range(60):
        summary.record(20, now=float(second))
    assert len(caplog.records) == 1
    summary.record(20, now=60.0)
    assert len(caplog.records) == 2
    assert 'success=60' in caplog.messages[-1]
    summary.record(0, now=61.0)
    assert caplog.records[-1].levelno == logging.WARNING
    summary.record(20, now=62.0)
    assert 'recovered' in caplog.messages[-1]
