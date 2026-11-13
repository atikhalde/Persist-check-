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


# A dry-run LOOP needs a ledger to behave like the real one (the early pass
# reads the previous candle's pick, a confirm needs its early alert), but it
# must never touch the real ledger. It gets its own scratch file instead.
_scratch: Path | None = None


def use_scratch(on: bool = True) -> None:
    """Route load/save to an empty throw-away ledger (dry-run live loop)."""
    global _scratch
    _scratch = STATE_PATH.with_name("scan_state.dry.json") if on else None
    if _scratch is not None and _scratch.exists():
        _scratch.unlink()


def scratch_active() -> bool:
    return _scratch is not None


def active_path() -> Path:
    return _scratch or STATE_PATH


def load(path: Path | None = None) -> dict:
    p = path or active_path()
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
    p = path or active_path()
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
            "early": {},
            "scan": {},
            "positions": state.get("positions", {}),  # trail until an exit, even across sessions
        }
    state.setdefault("positions", {})
    state.setdefault("alerts", {})
    state.setdefault("sent", {})
    state.setdefault("market", {})
    state.setdefault("sector", {})
    state.setdefault("early", {})   # pre-close heads-up alerts awaiting the close
    state.setdefault("scan", {})    # last completed bar's sector pick
    return state
