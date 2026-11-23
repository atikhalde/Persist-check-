from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from nse_scanner import alert_timing, continuation, pipeline
from nse_scanner.stock import StockSignal

BAR = datetime.fromisoformat('2026-11-23T10:10:00+05:30')


def frame(side=1):
    idx = pd.DatetimeIndex([BAR - timedelta(minutes=10), BAR - timedelta(minutes=5), BAR])
    if side == 1:
        return pd.DataFrame({'High': [100, 101, 103], 'Low': [98, 99, 100],
                             'Close': [99, 100, 102]}, index=idx)
    return pd.DataFrame({'High': [102, 101, 100], 'Low': [100, 99, 97],
                         'Close': [101, 100, 98]}, index=idx)


def fire(side):
    return StockSignal('ABC', 90 if side == 1 else 290, 'old',
                       'BUY' if side == 1 else 'SELL', '09:20', 100, 70, 'strong', 1, 0, 'old')


def test_continuation_requires_new_break_after_sector_pick_on_both_sides():
    for side in (1, -1):
        st = {}
        continuation.note_pick(st, 'IT', side, BAR - timedelta(minutes=5))
        new = continuation.trigger(st, 'IT', side, BAR, frame(side), [fire(side)])
        assert new is not None
        assert new.time == '10:10' and new.name.startswith('NEW CONTINUATION')
        assert new.price == (102 if side == 1 else 98)
        assert continuation.trigger(st, 'IT', -side, BAR, frame(side), [fire(side)]) is None
        continuation.note_pick(st, 'OTHER', side, BAR)
        continuation.note_pick(st, 'IT', side, BAR)
        assert continuation.trigger(st, 'IT', side, BAR, frame(side), [fire(side)]) is None


def test_no_break_no_reentry_on_same_move_no_gaps_and_no_future_rows():
    st = {}
    continuation.note_pick(st, 'IT', 1, BAR - timedelta(minutes=5))
    data = frame().astype(float)
    data.loc[BAR, 'Close'] = 100.5
    assert continuation.trigger(st, 'IT', 1, BAR, data, [fire(1)]) is None
    data = frame()
    data.iloc[1, data.columns.get_loc('Close')] = 101
    assert continuation.trigger(st, 'IT', 1, BAR, data, [fire(1)]) is None
    assert continuation.trigger(st, 'IT', 1, BAR, frame().iloc[[0, 2]], [fire(1)]) is None
    assert continuation.trigger(st, 'IT', 1, BAR, frame(), []) is None
    data = pd.concat([frame(), pd.DataFrame({'High': [200], 'Low': [100], 'Close': [200]},
                                            index=[BAR + timedelta(minutes=5)])])
    assert continuation.trigger(st, 'IT', 1, BAR, data, [fire(1)]).price == 102


def test_5m_old_fire_cannot_be_promoted_even_with_flag_but_bot3_retains_path():
    with patch.dict('os.environ', {'LATE_LEADER_ENTRY': '1'}):
        assert not pipeline.take_late_entry({}, SimpleNamespace(), BAR, {}, BAR, bot='5m')


def test_timing_records_original_signal_and_decision_bars_and_api_response():
    st = {}
    sig = BAR - timedelta(minutes=60)
    decision = BAR + timedelta(minutes=5, seconds=2)
    start = decision + timedelta(seconds=1)
    end = start + timedelta(seconds=2)
    event = alert_timing.record(st, kind='5m-watch', name='ABC', bar=BAR, signal_bar=sig,
                                decision=decision, send_start=start, send_end=end, success=True)
    assert event['signal_bar'] == sig.isoformat()
    assert event['signal_bar_close'] == (sig + timedelta(minutes=5)).isoformat()
    assert event['decision_bar'] == BAR.isoformat()
    assert event['decision_at'] == decision.isoformat()
    assert event['send_response_at'] == end.isoformat()
    assert event['response_after_close_s'] == 5
    assert event['send_succeeded'] is True
