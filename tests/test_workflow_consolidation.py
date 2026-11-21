"""The three scanner workflows retain bot 4's loop and manual tools."""
from pathlib import Path
import subprocess

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / '.github' / 'workflows'


def test_only_three_scanner_workflows():
    assert {p.name for p in WORKFLOWS.glob('*.yml')} == {
        'live-scan.yml', 'live-scan-1m.yml', 'screen-bot4.yml'
    }


def test_bot4_modes_and_schedule():
    workflow = yaml.safe_load((WORKFLOWS / 'screen-bot4.yml').read_text())
    events = workflow[True]  # PyYAML parses the YAML 1.1 key "on" as True
    assert events['workflow_dispatch']['inputs']['mode']['options'] == [
        'loop', 'status', 'audit-pdf', 'check'
    ]
    assert [x['cron'] for x in events['schedule']] == [
        '39 3 * * 1-5', '9,39 4-9 * * 1-5'
    ]
    jobs = workflow['jobs']
    assert "github.event_name == 'schedule' || inputs.mode == 'loop'" in jobs['guard']['if']
    assert "inputs.mode != 'loop'" in jobs['report']['if']
    guard = jobs['guard']['steps'][0]['run']
    assert 'actions/workflows/screen-bot4.yml/runs' in guard
    assert r'== \"NSE screen · bot 4\"' in guard
    handoff = next(s for s in jobs['screen']['steps'] if s.get('name') == 'Hand over to a fresh run')
    assert 'gh workflow run screen-bot4.yml' in handoff['run']
    report = jobs['report']['steps']
    finder = next(s for s in report if s.get('id') == 'board')
    assert 'screen4-2' in finder['run'] and 'board-2' in finder['run']
    assert any('bot4-check' in s.get('run', '') for s in report)
    assert any('screen-pdf' in s.get('run', '') for s in report)
    for job in jobs.values():
        for step in job['steps']:
            if 'run' in step:
                result = subprocess.run(['bash', '-n'], input=step['run'], text=True,
                                        capture_output=True)
                assert result.returncode == 0, (step.get('name'), result.stderr)
