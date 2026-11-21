"""Shared test hygiene.

Two rules, both learned the hard way:

1. **No test may write the repository's real ledgers.** ``state.STATE_PATH``,
   ``screen.STATE_PATH`` and ``scan1m.LEDGER`` are anchored to the repository on
   purpose (the runner's working directory *is* the repository), so a test that
   forgets to patch one silently leaves ``state/scan_state.json`` behind — which a
   later local ``python -m nse_scanner live`` would then restore as its own
   positions. The autouse fixture below points all three at a per-test directory;
   a test that wants its own file still patches over it (as ``ScreenCase`` does).
2. **No test may write a real audit PDF into ``reports/``.** Same idea, same fix.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from types import SimpleNamespace

from nse_scanner import live_screen, scan1m, screen, screen_pdf, state


@pytest.fixture(autouse=True)
def _no_repository_ledgers(tmp_path: Path, monkeypatch):
    tmp_path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(state, "STATE_PATH", tmp_path / "scan_state.json")
    monkeypatch.setattr(state, "_scratch", None)
    monkeypatch.setattr(screen, "STATE_PATH", tmp_path / "screen_state.json")
    monkeypatch.setattr(scan1m, "LEDGER", tmp_path / "scan1m_state.json")
    monkeypatch.setattr(screen_pdf, "REPORT_DIR", tmp_path / "reports")

    # The screen pass now runs at the TOP of every 5m cycle, before bots 1/2 do
    # anything — so every existing test of that cycle would otherwise reach Yahoo
    # through it. An empty market read means the pass does its real work on an
    # empty tape: no network, no sends, and the wiring is still exercised.
    def empty_read(uni, *, last=None):
        from nse_scanner.session import to_ist

        return (SimpleNamespace(dir_state=0, dir_label="", dir_conf=0, regime="",
                                pick_sector="", sectors={},
                                bar_time=to_ist(last).strftime("%H:%M") if last else ""),
                {}, {})

    monkeypatch.setattr(live_screen, "analyse", empty_read)
    monkeypatch.setattr(live_screen, "name_frames", lambda *a, **k: {})
    yield
