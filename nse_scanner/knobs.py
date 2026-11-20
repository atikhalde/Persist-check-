"""Which knobs a run is actually using — printed once, so a run can be audited.

A repository variable that lands in the wrong place (a **secret** instead of a
variable, an environment the job does not attach, the other repository) resolves
to an empty string in ``${{ vars.X }}``, and the workflow's ``vars.X || '0'``
then quietly restores the *old* behaviour. Nothing in the pick says so: the day
looks like the knob had no effect, which is indistinguishable from the knob
never having arrived.

Two things make that visible instead of inferable:

* ``describe()`` — both loops log the resolved value of every pick knob, and
  then the ones that differ from their default. A knob whose recommendation
  differs from its default shows up there exactly when the variable landed.
* the workflow's *Repository variables in force* step echoes ``toJSON(vars)``,
  which names what is set **in the repository** — the one thing the process
  cannot know, because the workflow materialises the defaults.
"""

from __future__ import annotations

import os

#: Knobs whose value changes what is picked or taken, with the value the workflow
#: uses when the variable is unset (``vars.X || '<default>'``). Kept in the order
#: they appear in the README table, so the log and the docs read the same way.
AUDITED: tuple[tuple[str, str], ...] = (
    ("PICK_CONF_SCALED_FLOOR", "0"),
    ("PICK_LOWCONF_MIN_PERSIST", "65"),
    ("PICK_MIN_PERSIST", "50"),
    ("PICK_RS_MARGIN", "0.05"),
    ("PICK_STICKY_MARGIN", "5"),
    ("SECTOR_MAX_LAG_BARS", "2"),
    ("RANK_BY_SECTOR_LEAD", "1"),
    ("SECTOR_FOLLOW", "1"),
    ("SECTOR_FOLLOW_ON_WEAK", "0"),
    ("SECTOR_FOLLOW_WEAK_MIN_PERSIST", "85"),
    ("SECTOR_FOLLOW_MIN_PERSIST", "75"),
    ("SECTOR_FOLLOW_MAX", "2"),
    ("SECTOR_EXTRA_MAX", "1"),
    ("TRIPLE_PICK", "1"),
    ("ENTRY_MIN_RANK", "40"),
    ("ENTRY_RANK_BAND", "0.75"),
    ("TRIPLE_MIN_PERSIST", "85"),
    ("NAME_RANK", "combined"),
    ("EOD_REPORT", "1"),
    ("EOD_AUDIT", "1"),
    ("EOD_MAX_CHARS", "3800"),
    ("AUDIT_DIR", "reports"),
    ("AUDIT_BOARD", "1"),
    ("AUDIT_BOARD_EVERY_MIN", "5"),
    ("AUDIT_BOARD_FROM", "09:20"),
    ("AUDIT_BOARD_TO", "15:15"),
    ("AUDIT_BOARD_PERIOD", "1d"),
    ("COST_BPS", "5"),
    ("COST_MIN", "20"),
    ("TRADE_CAPITAL", "100000"),
    ("DIVERGENCE_SHORT", "0"),
    ("DIVERGE_MIN_PERSIST", "65"),
    ("OI_FILTER", "annotate"),
    ("MAX_ENTRIES_PER_PICK", "0"),
    ("SECTOR_FOLLOW_EXTRAS", "1"),
    ("DUAL_PICK", "1"),
    ("SECTOR_TOP_N", "5"),
    ("SCAN5M_REQUIRE_TARGET", "1"),
    ("SCAN5M_LEADER_CATCHUP", "1"),
    ("LATE_LEADER_ENTRY", "1"),
    ("SCAN5M_LEADER_MAX_MINUTES", "40"),
    ("SCAN5M_LEADER_MAX_R", "2.0"),
    ("PICK_STICKY_LOG", "0"),
    ("MIN_TARGET_R", "0.5"),
    ("MIN_TARGET_PCT", "0.10"),
    ("TARGET_FALLBACK", "1"),
    ("PICK_CATCHUP", "1"),
    ("SCAN1M_EARLY_FROM", "09:30"),
    ("EXIT_EMA9_BUFFER_PCT", "0.10"),
    ("EXIT_EMA9_BUFFER_R", "0.25"),
    ("BREAKEVEN_AFTER_T1", "1"),
    ("MARKET_CONF_DELTA", "10"),
)


def effective(name: str, default: str) -> str:
    """The value the code will act on — an empty variable is an absent variable.

    GitHub renders an unset variable as the empty string, so ``""`` has to fall
    back exactly the way the workflow's ``|| default`` does; anything else would
    make the log disagree with the code that reads the same variable.
    """
    raw = os.environ.get(name)
    return default if raw is None or not raw.strip() else raw.strip()


def resolved() -> list[tuple[str, str, bool]]:
    """``(name, value, differs_from_default)`` for every audited knob."""
    return [(name, effective(name, default), effective(name, default) != default)
            for name, default in AUDITED]


def differing() -> dict[str, str]:
    """The audited knobs this run disagrees with, in AUDITED order."""
    return {name: value for name, value, differs in resolved() if differs}


def describe() -> list[str]:
    """The two lines both loops log (and ``doctor`` prints)."""
    values = resolved()
    changed = [f"{name}={value}" for name, value, differs in values if differs]
    return [
        "pick knobs in force: " + " · ".join(f"{name}={value}" for name, value, _ in values),
        ("pick knobs differing from default: " + " · ".join(changed) if changed else
         "pick knobs differing from default: none — every pick knob is at its "
         "workflow default (a knob whose recommendation *is* the default cannot "
         "show a difference here; the run's variables step shows what is set)"),
    ]
