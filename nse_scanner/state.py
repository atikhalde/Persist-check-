"""Persist per-session dedupe and open trades across IST sessions."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = ROOT / "state" / "scan_state.json"


class InvalidStateError(RuntimeError):
    """Existing alert ledger cannot be trusted; stop instead of resending."""

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
        result = json.loads(p.read_text(encoding="utf-8"))
        if (not isinstance(result, dict) or "session" not in result or
                not isinstance(result.get("sent"), dict) or
                not isinstance(result.get("positions", {}), dict)):
            raise ValueError("invalid session/sent/positions ledger")
        return result
    except (OSError, ValueError) as exc:
        # Failing open would resend already-delivered entries on every run.
        raise InvalidStateError(f"Invalid scanner state at {p}; refusing to send alerts: {exc}") from exc


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
            "positions": state.get("positions", {}),  # trail until an exit, even across sessions
        }
    state.setdefault("positions", {})
    state.setdefault("alerts", {})
    state.setdefault("sent", {})
    state.setdefault("market", {})
    state.setdefault("sector", {})
    return state
