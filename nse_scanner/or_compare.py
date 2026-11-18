"""Opening-range comparison — measure OR 30 against OR 15 on finished sessions.

Measurement only. Nothing in the live loops imports this module, and the patch of
``market.OR_MINUTES`` is restored on the way out, so a comparison can never change
what a running scanner does.

Why it exists — 18 Nov: the 09:15–09:45 opening range was still being completed
while KALYANKJIL (09:20), CROMPTON / DIXON / HAVELLS / TITAN / VOLTAS (09:25) and
WHIRLPOOL (09:40) fired — seven of the twelve names of the sector the engine only
picked at **09:45**.

Which of them a shorter range rescues is not obvious, and the arithmetic is worth
stating: the gate is ``mom = bar_time - 09:15 >= OR_MINUTES``, so a pick becomes
possible on the **09:45** bar at OR 30 (judged 09:50, exactly as the live log
shows), on the **09:30** bar at OR 15, on **09:25** at OR 10 and on **09:20** at
OR 5. A fire is eligible only if its own candle is one where the pick was live —
so OR 15 rescues the 09:30–09:40 fires (WHIRLPOOL 09:40), OR 10 rescues the 09:25
cluster, and KALYANKJIL's 09:20 fire needs 5. That is the sort of thing to
measure rather than argue about, hence this tool.

Every day is replayed through the same engine the scanner uses: ``market.run_market``
for the direction and the pick, ``stock.scan_stock`` for the fires, the fire's own
EMA9 stop for the risk. What it does *not* replay is the rest of the live pipeline
(entry cap, OI gate, 1m projections, late-leader catch-ups), and it takes one entry
per name per session — so read it as a comparison of the two opening ranges, not as
a promise of what the bots would have sent.
"""

from __future__ import annotations

import contextlib
import logging
from datetime import date, datetime

import numpy as np
import pandas as pd

from . import data, market
from .market import SECTOR_ORDER, run_market
from .session import session_date, to_ist
from .stock import scan_stock

log = logging.getLogger("nse_scanner.or_compare")

DEFAULT_ORS = (30, 15)


@contextlib.contextmanager
def opening_range(minutes: int):
    """Run the market engine as if the opening range lasted ``minutes``."""
    saved = market.OR_MINUTES
    market.OR_MINUTES = int(minutes)
    try:
        yield
    finally:
        market.OR_MINUTES = saved


def _slice(df: pd.DataFrame | None, ts) -> pd.DataFrame | None:
    if df is None or df.empty:
        return df
    return df.loc[:ts]


def _sanitized(frames: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    from .data import sanitize_index_5m

    return {sym: (sanitize_index_5m(df) if df is not None else None) for sym, df in frames.items()}


class Tape:
    """One session's raw material, fetched once and reused for every OR value."""

    def __init__(self, target: date):
        self.target = target
        self.uni = data.load_universe()
        self.index_map = {name: self.uni["sectors"][name]["index"]
                          for name in SECTOR_ORDER if name in self.uni["sectors"]}
        symbols = [self.uni["nifty"], self.uni["vix"]] + list(self.index_map.values())
        log.info("fetching %d indices", len(symbols))
        self.m5 = _sanitized(data.fetch_many(symbols, "7d", "5m"))
        self.d1 = data.fetch_many(symbols, "90d", "1d")
        self.nifty_5 = self.m5.get(self.uni["nifty"])
        if self.nifty_5 is None or self.nifty_5.empty:
            raise RuntimeError("no Nifty 5m data")
        self.stocks: dict[str, pd.DataFrame] = {}
        self.daily: dict[str, pd.DataFrame] = {}

    def day_bars(self, symbol: str) -> pd.DataFrame:
        bars = self.m5.get(symbol)
        if bars is None or bars.empty:
            return pd.DataFrame()
        return bars[[session_date(t) == self.target for t in bars.index]]

    def stock_frames(self, names: list[str]) -> None:
        """5m (with 1m volume) and daily frames for the names not fetched yet."""
        todo = [n for n in names if n not in self.stocks]
        if not todo:
            return
        tickers = {raw: data.yahoo_stock(raw) for raw in todo}
        log.info("fetching %d stocks", len(todo))
        sm5 = data.fetch_many(tickers.values(), "30d", "5m")
        sm1 = data.fetch_many(tickers.values(), "7d", "1m")
        sd1 = data.fetch_many(tickers.values(), "90d", "1d")
        for raw, ysym in tickers.items():
            df5 = sm5.get(ysym)
            if df5 is None or df5.empty:
                continue
            self.stocks[raw] = data.apply_1m_volume(df5, sm1.get(ysym))
            self.daily[raw] = sd1.get(ysym)


def market_path(tape: Tape, or_minutes: int) -> list[dict]:
    """The session's pick, bar by bar, under one opening-range length."""
    node = tape.uni["nifty"]
    vix = tape.uni["vix"]
    out: list[dict] = []
    with opening_range(or_minutes):
        for ts in tape.nifty_5[[session_date(t) == tape.target for t in tape.nifty_5.index]].index:
            sector_5 = {n: _slice(tape.m5.get(sym), ts) for n, sym in tape.index_map.items()}
            sector_d = {n: tape.d1.get(sym) for n, sym in tape.index_map.items()}
            md = run_market(_slice(tape.nifty_5, ts), tape.d1.get(node),
                            _slice(tape.m5.get(vix), ts), tape.d1.get(vix),
                            sector_5, sector_d)
            out.append({"time": to_ist(ts).strftime("%H:%M"), "dir": md.dir_label,
                        "conf": md.dir_conf, "pick": md.pick_sector, "side": md.pick_side,
                        "action": md.action, "reason": md.pick_reason})
    return out


def dominant_pick(path: list[dict]) -> tuple[str, int]:
    """The sector the session spent most of its decided bars in (after 09:45)."""
    seen = [(r["pick"], r["side"]) for r in path
            if r["time"] > "09:45" and r["pick"] not in (None, "WAIT") and r["side"] in (1, -1)]
    if not seen:
        return "WAIT", 0
    counts: dict[tuple[str, int], int] = {}
    for key in seen:
        counts[key] = counts.get(key, 0) + 1
    return max(counts.items(), key=lambda kv: kv[1])[0]


def first_pick(path: list[dict]) -> str:
    for r in path:
        if r["pick"] not in (None, "WAIT") and r["side"] in (1, -1):
            return f"{r['time']} {r['pick']} {'long' if r['side'] == 1 else 'short'}"
    return "never"


def outcome(bars: pd.DataFrame, at: datetime, side: str, price: float, sl: float) -> dict:
    """What the entry would have done: stopped out first, else its R at the close.

    The stop is the fire candle's own EMA9 stop, the same one the alert carries.
    A bar that touches both the stop and a better price is scored as the stop —
    the conservative reading, and the one that keeps an optimistic tape from
    flattering a shorter opening range.
    """
    risk = abs(price - sl)
    if not risk or not np.isfinite(risk):
        return {"r_close": None, "mfe": None, "stopped": None, "stop_at": None}
    after = bars[bars.index > at]
    mfe, stopped, stop_at = 0.0, False, None
    for ts, bar in after.iterrows():
        if side == "SELL":
            if bar["High"] >= sl:
                stopped, stop_at = True, to_ist(ts).strftime("%H:%M")
                break
            mfe = max(mfe, (price - bar["Low"]) / risk)
        else:
            if bar["Low"] <= sl:
                stopped, stop_at = True, to_ist(ts).strftime("%H:%M")
                break
            mfe = max(mfe, (bar["High"] - price) / risk)
    if stopped:
        return {"r_close": -1.0, "mfe": mfe, "stopped": True, "stop_at": stop_at}
    if after.empty:
        return {"r_close": None, "mfe": None, "stopped": False, "stop_at": None}
    close = float(after["Close"].iloc[-1])
    r_close = (close - price) / risk if side == "BUY" else (price - close) / risk
    return {"r_close": r_close, "mfe": mfe, "stopped": False, "stop_at": None}


def live_entries(tape: Tape, path: list[dict], sector: str, side: int) -> list[dict]:
    """Fires that happened on a candle whose pick was live, one per name."""
    if sector not in tape.uni["sectors"] or side not in (1, -1):
        return []
    names = list(tape.uni["sectors"][sector]["stocks"])
    tape.stock_frames(names)
    want = "BUY" if side == 1 else "SELL"
    live = {r["time"] for r in path if r["pick"] == sector and r["side"] == side}
    out: list[dict] = []
    for raw in names:
        frame = tape.stocks.get(raw)
        if frame is None or frame.empty:
            continue
        try:
            fires = scan_stock(raw, frame, tape.daily.get(raw), keep_all=True, as_of=tape.target)
        except Exception:
            log.exception("scan failed for %s", raw)
            continue
        hit = next((s for s in fires if s.side == want and s.time in live), None)
        if hit is None:
            continue
        when = datetime.combine(tape.target, datetime.strptime(hit.time, "%H:%M").time(),
                                tzinfo=frame.index[0].tzinfo)
        res = outcome(frame, when, want, float(hit.price), float(hit.sl))
        out.append({"raw": raw, "time": hit.time, "side": want, "price": float(hit.price),
                    "sl": float(hit.sl), "sector": sector, **res})
    return sorted(out, key=lambda e: (e["time"], e["raw"]))


def compare_day(target: date, ors=DEFAULT_ORS) -> dict:
    """Replay one session once per opening range and collect what each produced."""
    tape = Tape(target)
    results = []
    for value in ors:
        path = market_path(tape, value)
        sector, side = dominant_pick(path)
        entries = live_entries(tape, path, sector, side)
        r = [e["r_close"] for e in entries if e["r_close"] is not None]
        results.append({
            "or": int(value),
            "first_pick": first_pick(path),
            "dominant": f"{sector} {'long' if side == 1 else 'short'}" if sector != "WAIT" else "none",
            "wait_bars": sum(1 for r2 in path if r2["time"] > "09:45" and r2["pick"] in (None, "WAIT")),
            "entries": entries,
            "net_r": float(sum(r)) if r else 0.0,
            "stops": sum(1 for e in entries if e["stopped"]),
        })
        log.info("%s OR %dm: first pick %s, %d entries, net %.1fR",
                 target, value, results[-1]["first_pick"], len(entries), results[-1]["net_r"])
    # A day where the ranges agree is the common case, and it means something the
    # table cannot show on its own: on that tape the opening range was not the
    # binding constraint. Said out loud, identical columns read as a result
    # instead of as a bug (18 Nov's first report had four of five days identical).
    same = len({(r["first_pick"], r["dominant"], r["wait_bars"],
                 tuple((e["raw"], e["time"]) for e in r["entries"])) for r in results}) == 1
    return {"date": str(target), "results": results, "identical": same}


def _names(entries: list[dict]) -> str:
    if not entries:
        return "—"
    return ", ".join(f"{e['raw']}@{e['time']}"
                     + (f" ({e['r_close']:+.1f}R)" if e["r_close"] is not None else "")
                     for e in entries)


def render(day: dict) -> str:
    """One markdown table per session, plus the totals across sessions."""
    results = day["results"]
    head = " | ".join(f"OR {r['or']} min" for r in results)
    rows = [
        f"### {day['date']}",
        "",
        f"| | {head} |",
        "|---|---|" + "---|" * (len(results) - 1),
    ]
    for label, key in (("First pick", "first_pick"), ("Sector held", "dominant"),
                       ("WAIT bars after 09:45", "wait_bars"), ("Entries", None),
                       ("Net R at the close", "net_r"), ("Stopped out", "stops")):
        cells = []
        for r in results:
            if key is None:
                cells.append(f"{len(r['entries'])} — {_names(r['entries'])}")
            elif key == "net_r":
                cells.append(f"{r['net_r']:+.1f}R" if r["entries"] else "—")
            else:
                cells.append(str(r[key]))
        rows.append(f"| {label} | " + " | ".join(cells) + " |")
    if day.get("identical"):
        rows += ["", "_Every opening range produced the same pick path and the same entries on_"
                     " _this session: the range was not what decided it._"]
    return "\n".join(rows)


def render_totals(days: list[dict]) -> str:
    if not days:
        return "_No sessions replayed._"
    ors = [r["or"] for r in days[0]["results"]]
    lines = ["### Across the replayed sessions", "",
             "| | " + " | ".join(f"OR {o} min" for o in ors) + " |",
             "|---|---|" + "---|" * (len(ors) - 1)]
    for idx, o in enumerate(ors):
        entries = [e for d in days for e in d["results"][idx]["entries"]]
        r = [e["r_close"] for e in entries if e["r_close"] is not None]
        stops = sum(1 for e in entries if e["stopped"])
        wins = sum(1 for v in r if v > 0)
        avg = sum(r) / len(r) if r else 0.0
        cell = [f"{len(entries)}", f"{sum(r):+.1f}R", f"{avg:+.2f}R",
                f"{wins}/{len(r)}", f"{stops}"]
        lines.append(f"| OR {o} min (sessions {len(days)}) | " + " | ".join(cell) + " |")
    lines[2:4] = ["| OR | entries | net R | average R | winners | stops |",
                  "|---|---|---|---|---|---|"]
    return "\n".join(lines)


def render_report(days: list[dict], ors=DEFAULT_ORS) -> str:
    head = [
        "# Opening range: " + " vs ".join(f"{o} min" for o in ors),
        "",
        "Same tape, same market engine, same stock engine — only the length of the",
        "opening range changes. One entry per name per session, at the fire's own",
        "EMA9 stop; a bar that touches the stop is scored as the stop. The entry cap,",
        "the OI gate and the 1m projections are not replayed: this measures the two",
        "opening ranges, not what the bots would have sent.",
        "",
        "",
    ]
    body = [render(d) + "\n" for d in days]
    return "\n".join(head) + "\n".join(body) + "\n" + render_totals(days) + "\n"
