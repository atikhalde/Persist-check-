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

from nse_scanner import scan1m, screen, screen_pdf, state


@pytest.fixture(autouse=True)
def _no_repository_ledgers(tmp_path: Path, monkeypatch):
    tmp_path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(state, "STATE_PATH", tmp_path / "scan_state.json")
    monkeypatch.setattr(state, "_scratch", None)
    monkeypatch.setattr(screen, "STATE_PATH", tmp_path / "screen_state.json")
    monkeypatch.setattr(scan1m, "LEDGER", tmp_path / "scan1m_state.json")
    monkeypatch.setattr(screen_pdf, "REPORT_DIR", tmp_path / "reports")
    yield
