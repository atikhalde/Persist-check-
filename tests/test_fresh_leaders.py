from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from nse_scanner import audit, fresh_leaders, scan1m

BAR = datetime.fromisoformat('2026-11-23T10:00:00+05:30')


def snapshot(label, side=1, persist=80, intra=1.0, score=50, move=0.4, rank=5):
    return {'b': label, 'sectors': {'IT': {'side': side, 'persist': persist,
            'intra': intra, 'n': ['TCS'], 'rank': [score], 'mv': [move], 'rk': [rank]}}}


def ledger(side=1):
    return {'session': str(BAR.date()), 'stats': {'board': {'bars': [
        snapshot('09:50', side, 75, side * 0.5, 35, side * -0.4, 9),
        snapshot('09:55', side, 85, side * 0.8, 53, side * 0.4, 5)]}}}


def test_strengthening_both_sides():
    for side in (1, -1):
        result = fresh_leaders.assessment(ledger(side), BAR, 'IT', side, 'TCS')
        assert result['status'] == 'preferred'
        assert result['score_delta'] == 18
        assert result['rank'] == 5
        assert result['sector_move_delta_pp'] == 0.3
        assert result['name_move_delta_pp'] == 0.8


def test_weak_sector_or_name_not_preferred():
    for key, value in [('persist', 74), ('intra', 0.4), ('rank', [34]),
                       ('mv', [-0.5]), ('rk', [6])]:
        st = ledger()
        st['stats']['board']['bars'][1]['sectors']['IT'][key] = value
        assert fresh_leaders.assessment(st, BAR, 'IT', 1, 'TCS')['status'] == 'other'


def test_no_lookahead_gaps_stale_session_or_reversal():
    st = ledger()
    st['stats']['board']['bars'].append(snapshot('10:00', persist=99, score=99, move=4))
    assert fresh_leaders.assessment(st, BAR, 'IT', 1, 'TCS')['board_bar'] == '09:55'
    assert fresh_leaders.assessment(st, BAR + timedelta(minutes=5), 'IT', 1, 'TCS')['board_bar'] == '10:00'
    st['stats']['board']['bars'] = st['stats']['board']['bars'][:1]
    assert fresh_leaders.assessment(st, BAR, 'IT', 1, 'TCS')['status'] == 'unknown'
    st = ledger()
    st['session'] = '2026-11-20'
    assert fresh_leaders.assessment(st, BAR, 'IT', 1, 'TCS')['status'] == 'unknown'
    st = ledger()
    st['stats']['board']['bars'][0]['sectors']['IT']['side'] = -1
    assert fresh_leaders.assessment(st, BAR, 'IT', 1, 'TCS')['status'] == 'unknown'


def test_missing_malformed_or_nonfinite_data_falls_back():
    for key, value in [('rank', []), ('mv', [float('nan')]), ('rk', [None]),
                       ('persist', None), ('n', [])]:
        st = ledger()
        st['stats']['board']['bars'][1]['sectors']['IT'][key] = value
        assert fresh_leaders.assessment(st, BAR, 'IT', 1, 'TCS')['status'] == 'unknown'


def test_stable_order_and_audit_evidence():
    st = ledger()
    candidates = [{'raw': 'UNKNOWN'}, {'raw': 'TCS'}, {'raw': 'OTHER'}]
    sectors = {c['raw']: 'IT' for c in candidates}
    sides = {c['raw']: 1 for c in candidates}
    fresh_leaders.prioritize(candidates, st, BAR, sectors, sides, stage='confirm')
    assert [c['raw'] for c in candidates] == ['TCS', 'UNKNOWN', 'OTHER']
    fresh_leaders.prioritize(candidates, st, BAR, sectors, sides, stage='confirm')
    assert len(st['stats']['fresh_leaders']) == 3
    result = audit.build(st, bot='1m')
    assert result['diagnostics']['fresh_leaders']['2026-11-23 10:00|confirm|TCS']['status'] == 'preferred'


def test_entry_count_is_candle_specific_and_zero_cap_is_unlimited():
    st = {}
    assert scan1m._entry_candle_count(st, BAR) == 0
    scan1m._entry_candle_add(st, BAR)
    assert scan1m._entry_candle_count(st, BAR) == 1
    assert scan1m._entry_candle_count(st, BAR + timedelta(minutes=5)) == 0
    with patch.dict('os.environ', {'SCAN1M_ALERT_CAP': '0'}):
        assert scan1m.alert_cap() == 0


def test_existing_rank_tie_break_is_stable():
    st = ledger()
    candidates = [{'raw': 'MISSING1', 'best': SimpleNamespace(score=1)},
                  {'raw': 'MISSING2', 'best': SimpleNamespace(score=2)}]
    fresh_leaders.prioritize(candidates, st, BAR, {}, {}, stage='projection')
    assert [c['raw'] for c in candidates] == ['MISSING1', 'MISSING2']
