"""Persist last market/sector lock and per-stock alerts (dedupe + rank upgrade)."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = ROOT / "state" / "scan_state.json"

# Higher rank replaces a weaker same-day signal (matches TV table).
RANK = {
    90: 1,
    290: 1,
    80: 2,
    280: 2,
}


def signal_rank(code: int) -> int:
    if 101 <= code <= 220:
        return 3
    return RANK.get(int(code), 0)


def load(path: Path | None = None) -> dict:
    p = path or STATE_PATH
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save(state: dict, path: Path | None = None) -> None:
    p = path or STATE_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
    tmp.replace(p)


def reset_if_new_day(state: dict, session: str) -> dict:
    if state.get("session") != session:
        return {
            "session": session,
            "market": {},
            "sector": {},
            "alerts": {},
            "sent": {},
        }
    state.setdefault("alerts", {})
    state.setdefault("sent", {})
    state.setdefault("market", {})
    state.setdefault("sector", {})
    return state
