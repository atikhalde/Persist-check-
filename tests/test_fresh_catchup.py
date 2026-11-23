from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from nse_scanner import pipeline, scan1m


def test_catchup_entry_limit_preserves_informational_messages_and_persists():
    bar = datetime.fromisoformat('2026-11-23T10:00:00+05:30')
    sig = SimpleNamespace(side='BUY', time='09:50', code=90, name='ENTRY BUY')
    hit = {'sig': sig, 'price': 100, 'r': 0.1, 'age': 10}
    st = {'session': str(bar.date()), 'stats': {}}
    sends = []
    with patch.dict('os.environ', {'SCAN1M_ALERT_CAP': '1'}), \
         patch.object(scan1m, 'catchup_candidate', return_value=hit), \
         patch.object(scan1m, 'save_ledger') as save, \
         patch.object(pipeline, 'take_late_entry', return_value=True) as take, \
         patch.object(scan1m.telegram, 'send_bot3', side_effect=lambda *a, **kw: sends.append(a[0]) or True), \
         patch.object(scan1m, 'format_catchup', return_value='Not an entry alert'):
        out = scan1m._catch_up(st, {'sector': 'IT', 'side': 1}, ['A', 'B'],
                               {'A': object(), 'B': object()}, {'A': [sig], 'B': [sig]},
                               {}, bar, bar, sector_of={'A': 'IT', 'B': 'IT'},
                               side_of={'A': 1, 'B': 1})
    assert take.call_count == 1
    assert scan1m._entry_candle_count(st, bar) == 1
    assert any(x.get('late_entry') for x in out)
    assert any(x.get('catchup') for x in out)
    assert sends == ['Not an entry alert']
    assert st['stats']['rejected']['candle_cap']['B@10:00']
    assert save.call_count >= 2
