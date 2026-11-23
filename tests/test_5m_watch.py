from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from nse_scanner import audit, pipeline
from nse_scanner.stock import StockSignal

BAR = datetime.fromisoformat('2026-11-23T10:00:00+05:30')


def test_watch_send_failure_records_attempt_but_does_not_consume_or_open():
    sig = StockSignal('ABC', 90, 'ENTRY BUY', 'BUY', '09:20', 100, 70, 'strong', 1, 0, 'test')
    item = {'raw': 'ABC', 'sector': 'IT', 'sig': sig, 'price': 105,
            'fire_bar': BAR - timedelta(minutes=40)}
    st = {'session': str(BAR.date())}
    md = SimpleNamespace(pick_sector='IT')
    with patch.object(pipeline, 'now_ist', return_value=BAR + timedelta(minutes=5, seconds=3)), \
         patch.object(pipeline.telegram, 'send', return_value=False), \
         patch.object(pipeline, '_save'):
        pipeline._send_late_leaders(st, md, BAR, [item])
    assert 'ABC' not in st.get('catchup', {})
    assert not st.get('positions') and not st.get('sent')
    event = audit.build(st)['diagnostics']['alert_timing'][0]
    assert event['kind'] == '5m-watch' and not event['send_succeeded']
    assert event['signal_bar'] == item['fire_bar'].isoformat()
    assert event['decision_bar'] == BAR.isoformat()
