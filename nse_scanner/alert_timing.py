"""Record scanner-side times; an API success is not proof of client receipt."""
from __future__ import annotations

from datetime import timedelta


def record(st, *, kind, name, bar, decision, send_start, send_end, success, signal_bar=None):
    close = bar + timedelta(minutes=5)
    row = {'kind': kind, 'name': name, 'signal_bar': (signal_bar or bar).isoformat(),
           'decision_bar': bar.isoformat(),
           'signal_bar_close': ((signal_bar or bar) + timedelta(minutes=5)).isoformat(),
           'decision_bar_close': close.isoformat(), 'decision_at': decision.isoformat(),
           'send_started_at': send_start.isoformat(), 'send_response_at': send_end.isoformat(),
           'send_succeeded': bool(success),
           'response_after_close_s': round((send_end - close).total_seconds(), 3)}
    st.setdefault('stats', {}).setdefault('alert_timing', []).append(row)
    return row
