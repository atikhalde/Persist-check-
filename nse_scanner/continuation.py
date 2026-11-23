"""Causal 5m continuation trigger after the primary sector becomes the pick."""
from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pandas as pd

from .session import to_ist


def note_pick(st, sector, side, bar):
    """Remember the first decision candle of the current primary sector and side."""
    key = f'{sector}|{side}'
    if (st.get('continuation_pick') or {}).get('key') != key:
        st['continuation_pick'] = {'key': key, 'since': bar.isoformat()}


def trigger(st, sector, side, bar, frame, fires):
    """A new close crossing the prior 5m extreme, after the primary pick.

    Require three consecutive session bars. The preceding close must not have
    already crossed its own preceding extreme; this prevents announcing the same
    uninterrupted move on every candle. A past same-direction fire supplies the
    stock context, not an executable price or a current signal.
    """
    marker = st.get('continuation_pick') or {}
    if marker.get('key') != f'{sector}|{side}' or side not in (-1, 1):
        return None
    try:
        since = pd.Timestamp(marker['since'])
        target = pd.Timestamp(bar)
        if since.tzinfo is None or target.tzinfo is None or target <= since:
            return None
        if frame is None or frame.empty:
            return None
        bars = frame.loc[frame.index <= target].tail(3)
        if len(bars) != 3:
            return None
        times = [to_ist(t) for t in bars.index]
        if (times[-1] != target or any(t.date() != target.date() for t in times)
                or times[1] - times[0] != timedelta(minutes=5)
                or times[2] - times[1] != timedelta(minutes=5)):
            return None
        old, prev, curr = (bars.iloc[i] for i in range(3))
        close, prevclose = float(curr['Close']), float(prev['Close'])
        if side == 1:
            crossed = close > float(prev['High']) and prevclose <= float(old['High'])
            level = float(prev['High'])
        else:
            crossed = close < float(prev['Low']) and prevclose >= float(old['Low'])
            level = float(prev['Low'])
        want = 'BUY' if side == 1 else 'SELL'
        earlier = [s for s in fires if s.side == want and s.time < bar.strftime('%H:%M')]
        if not crossed or not earlier:
            return None
        prior = max(earlier, key=lambda s: s.time)
        return replace(prior, name=f'NEW CONTINUATION {want}', time=bar.strftime('%H:%M'),
                       price=close, sl=None, score=0, strength='not re-scored',
                       reason=f'New 5m close through prior extreme {level:.2f}; sector picked before this candle; prior fire {prior.time}')
    except (KeyError, TypeError, ValueError, IndexError, AttributeError):
        return None
