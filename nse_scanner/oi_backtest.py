"""Dual-pass backtest: raw scanner entries vs OI-filtered entries.

Self-contained module. Imports from existing scanner but does not modify it.

Usage:
    python -m nse_scanner.oi_backtest --date 2026-11-09 --oi-source stub
    python -m nse_scanner.oi_backtest --date 2026-11-09 --oi-source csv --oi-file data/oi_history.csv

Outputs:
    state/oi_backtest_YYYY-MM-DD.md   (human report)
    state/oi_backtest_YYYY-MM-DD.json (raw data for further analysis)
"""

from __future__ import annotations

import argparse
import csv
import json
from datetime import date
from pathlib import Path
from typing import Optional

from . import data
from .data import sanitize_index_5m
from .market import SECTOR_ORDER, run_market
from .oi_data import CSV_OIReader, StubOIReader
from .oi_filter import should_take_entry
from .session import IST, session_date, to_ist
from .stock import scan_stock
from .trade import build_plan


def _slice(df, ts):
    if df is None or df.empty:
        return df
    return df.loc[:ts]


def _continuation(df5, entry_bar, entry_price: float, side: str, bars_ahead: int = 3):
    """Measure price continuation after the entry bar."""
    if df5 is None or df5.empty:
        return {}
    try:
        pos = df5.index.get_loc(entry_bar)
    except KeyError:
        return {}
    future = df5.iloc[pos + 1 : pos + 1 + bars_ahead]
    if future.empty:
        return {}
    closes = future["Close"].astype(float)
    if side == "BUY":
        cont1 = bool((closes > entry_price).any())
        cont3 = bool((closes.iloc[-1] > entry_price)) if len(closes) else False
        mae = ((entry_price - closes.max()) / entry_price * 100) if len(closes) else 0.0
        mfe = ((closes.max() - entry_price) / entry_price * 100) if len(closes) else 0.0
    else:
        cont1 = bool((closes < entry_price).any())
        cont3 = bool((closes.iloc[-1] < entry_price)) if len(closes) else False
        mae = ((closes.min() - entry_price) / entry_price * 100) if len(closes) else 0.0
        mfe = ((entry_price - closes.min()) / entry_price * 100) if len(closes) else 0.0
    return {
        "continued_1": cont1,
        "continued_3": cont3,
        "mae_pct": round(mae, 2),
        "mfe_pct": round(mfe, 2),
    }


def replay_with_oi(target: date, oi_reader, bars_ahead: int = 3):
    uni = data.load_universe()
    nifty_sym = uni["nifty"]
    vix_sym = uni["vix"]
    index_map = {n: uni["sectors"][n]["index"] for n in SECTOR_ORDER if n in uni["sectors"]}

    idx = [nifty_sym, vix_sym] + list(index_map.values())
    m5 = data.fetch_many(idx, "7d", "5m")
    d1 = data.fetch_many(idx, "90d", "1d")
    nifty_5 = sanitize_index_5m(m5.get(nifty_sym))
    if nifty_5 is None or nifty_5.empty:
        raise RuntimeError("No Nifty 5m data")
    for n, sym in index_map.items():
        if m5.get(sym) is not None:
            m5[sym] = sanitize_index_5m(m5[sym])

    day_bars = nifty_5[[session_date(t) == target for t in nifty_5.index]]
    if day_bars.empty:
        raise RuntimeError(f"No Nifty 5m bars on {target}")

    raw_candidates = []
    oi_filtered = []

    for ts in day_bars.index:
        sector_5 = {n: _slice(m5.get(sym), ts) for n, sym in index_map.items()}
        sector_d = {n: d1.get(sym) for n, sym in index_map.items()}
        md = run_market(
            _slice(nifty_5, ts),
            d1.get(nifty_sym),
            _slice(m5.get(vix_sym), ts),
            d1.get(vix_sym),
            sector_5,
            sector_d,
        )
        bar_time = to_ist(ts)
        bar_label = bar_time.strftime("%H:%M")

        if md.action not in ("SCAN LONG", "SCAN SHORT"):
            continue

        want_buy = md.pick_side == 1
        sec_cfg = uni["sectors"][md.pick_sector]
        stocks = sec_cfg["stocks"]
        tickers = [data.yahoo_stock(s) for s in stocks]
        sm5 = data.fetch_many(tickers, "7d", "5m")
        sm5 = {
            sym: df.loc[df.index <= ts] if df is not None and not df.empty else df
            for sym, df in sm5.items()
        }
        current = [
            sym for sym in tickers
            if sm5.get(sym) is not None and not sm5[sym].empty
            and to_ist(sm5[sym].index[-1]) == ts
        ]
        if not current:
            continue

        sm1 = data.fetch_many(current, "7d", "1m")
        sd1 = data.fetch_many(current, "90d", "1d")

        for raw, ysym in zip(stocks, tickers):
            if ysym not in current:
                continue
            df5 = sm5[ysym]
            fires = scan_stock(raw, df5, sd1.get(ysym), keep_all=True)
            fresh = [
                s for s in fires
                if s.time == bar_label
                and s.side == ("BUY" if want_buy else "SELL")
            ]
            for s in fresh:
                plan = build_plan(s, df5, bar_time, sd1.get(ysym))
                cont = _continuation(df5, bar_time, s.price, s.side, bars_ahead)

                row = {
                    "bar": bar_label,
                    "symbol": raw,
                    "side": s.side,
                    "code": s.code,
                    "price": s.price,
                    "entry": plan.entry if plan else None,
                    "sl": plan.sl if plan else None,
                    **cont,
                }
                raw_candidates.append(row)

                reading = oi_reader.snapshot(raw, bar_time)
                if should_take_entry(s.side, reading):
                    oi_filtered.append({
                        **row,
                        "oi_regime": reading.regime.value if reading else None,
                        "oi_chg_pct": (
                            round(reading.oi_chg_pct, 2)
                            if reading and reading.oi_chg_pct is not None
                            else None
                        ),
                    })

    return raw_candidates, oi_filtered


def _metrics(rows):
    if not rows:
        return {"n": 0}
    n = len(rows)
    cont1 = sum(1 for r in rows if r.get("continued_1"))
    cont3 = sum(1 for r in rows if r.get("continued_3"))
    maes = [r["mae_pct"] for r in rows if r.get("mae_pct") is not None]
    mfes = [r["mfe_pct"] for r in rows if r.get("mfe_pct") is not None]
    return {
        "n": n,
        "continued_1_rate": round(cont1 / n * 100, 1),
        "continued_3_rate": round(cont3 / n * 100, 1),
        "avg_mae_pct": round(sum(maes) / len(maes), 2) if maes else 0.0,
        "avg_mfe_pct": round(sum(mfes) / len(mfes), 2) if mfes else 0.0,
    }


def main(argv=None):
    p = argparse.ArgumentParser(prog="nse_scanner.oi_backtest")
    p.add_argument("--date", required=True, help="YYYY-MM-DD")
    p.add_argument("--oi-source", choices=["csv", "stub"], default="stub")
    p.add_argument("--oi-file", default="data/oi_history.csv")
    p.add_argument("--bars-ahead", type=int, default=3)
    args = p.parse_args(argv)

    target = date.fromisoformat(args.date)
    oi_reader = CSV_OIReader(args.oi_file) if args.oi_source == "csv" else StubOIReader()

    raw, filtered = replay_with_oi(target, oi_reader, bars_ahead=args.bars_ahead)
    raw_m, filt_m = _metrics(raw), _metrics(filtered)

    lines = []
    lines.append(f"# OI x Price Backtest — {target}")
    lines.append("")
    lines.append(f"OI source: {args.oi_source} ({args.oi_file})")
    lines.append(f"Bars ahead for continuation: {args.bars_ahead}")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append("| Metric | Raw entries | OI-filtered |")
    lines.append("|--------|-------------|-------------|")
    lines.append(f"| Total candidates | {raw_m['n']} | {filt_m['n']} |")
    lines.append(f"| Continued after 1 bar | {raw_m.get('continued_1_rate', 0)}% | {filt_m.get('continued_1_rate', 0)}% |")
    lines.append(f"| Continued after 3 bars | {raw_m.get('continued_3_rate', 0)}% | {filt_m.get('continued_3_rate', 0)}% |")
    lines.append(f"| Avg adverse excursion (MAE) | {raw_m.get('avg_mae_pct', 0)}% | {filt_m.get('avg_mae_pct', 0)}% |")
    lines.append(f"| Avg favorable excursion (MFE) | {raw_m.get('avg_mfe_pct', 0)}% | {filt_m.get('avg_mfe_pct', 0)}% |")
    lines.append(f"| Entries suppressed by OI filter | — | {raw_m['n'] - filt_m['n']} |")
    lines.append("")
    lines.append("## Interpretation")
    lines.append("")
    lines.append("- Higher continuation rate in OI-filtered = filter removes reversals.")
    lines.append("- Lower MAE in OI-filtered = less drawdown after entry.")
    lines.append("- Large suppression count = OI threshold may be too strict.")
    lines.append("")
    lines.append("## OI-filtered entries (kept by filter)")
    lines.append("")
    if filtered:
        lines.append("| Bar | Symbol | Side | Code | Price | OI Regime | OI Chg % | Cont 1b | Cont 3b | MAE % | MFE % |")
        lines.append("|-----|--------|------|------|-------|-----------|----------|---------|---------|-------|-------|")
        for r in filtered:
            lines.append(
                f"| {r['bar']} | {r['symbol']} | {r['side']} | {r['code']} | "
                f"{r['price']} | {r.get('oi_regime','-')} | {r.get('oi_chg_pct','-')} | "
                f"{r.get('continued_1')} | {r.get('continued_3')} | {r.get('mae_pct')} | {r.get('mfe_pct')} |"
            )
    else:
        lines.append("(none)")
    lines.append("")
    lines.append("## Raw entries (all candidates, before OI filter)")
    lines.append("")
    if raw:
        lines.append("| Bar | Symbol | Side | Code | Price | Cont 1b | Cont 3b | MAE % | MFE % |")
        lines.append("|-----|--------|------|------|-------|---------|---------|-------|-------|")
        for r in raw:
            lines.append(
                f"| {r['bar']} | {r['symbol']} | {r['side']} | {r['code']} | "
                f"{r['price']} | {r.get('continued_1')} | {r.get('continued_3')} | {r.get('mae_pct')} | {r.get('mfe_pct')} |"
            )
    else:
        lines.append("(none)")

    text = "\n".join(lines)
    print(text)

    out_md = Path("state") / f"oi_backtest_{args.date}.md"
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text(text + "\n", encoding="utf-8")

    out_json = Path("state") / f"oi_backtest_{args.date}.json"
    payload = {
        "date": str(target),
        "oi_source": args.oi_source,
        "raw": raw,
        "filtered": filtered,
        "metrics_raw": raw_m,
        "metrics_filtered": filt_m,
    }
    out_json.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"\nSaved {out_md}")
    print(f"Saved {out_json}")


if __name__ == "__main__":
    main()
