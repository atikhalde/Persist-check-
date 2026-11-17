"""The knobs a run is using have to be visible — in the log and in the repo.

Yesterday's failure mode: repository variables were set, and there was no way to
tell from a run whether they had arrived. `vars.X` is empty for a variable that
was created as a *secret* or in an environment the job does not attach, the
workflow silently supplies its default, and the day looks exactly like a day
where the knob had no effect. So:

* every pick knob is audited in `knobs.AUDITED`, forwarded by **both**
  workflows with the same default, and logged by **both** loops;
* each workflow echoes `toJSON(vars)` before it starts, which is the one fact
  the process cannot know for itself.
"""

import inspect
import os
import re
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from nse_scanner import knobs, pipeline, scan1m

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ("live-scan.yml", "live-scan-1m.yml")

#: Read at run time from the runner or the operator's shell, never from a
#: repository variable (or meaningless outside a replay).
RUNTIME_ONLY = {
    "GITHUB_OUTPUT", "DRY_RUN", "LOG_LEVEL",
    "BACKFILL", "OI_FILE", "OI_SOURCE", "YF_PERIOD_5M", "YF_PERIOD_1D",
}


def workflow_text(name):
    return (ROOT / ".github" / "workflows" / name).read_text()


def forwarded(text):
    """``{NAME: default}`` for the ``vars.X || 'default'`` entries in a workflow."""
    pairs = re.findall(r"^\s+([A-Z0-9_]+): \$\{\{\s*vars\.\1\s*\|\|\s*'([^']*)'\s*\}\}",
                       text, re.M)
    return dict(pairs)


class KnobForwardingTests(TestCase):
    """A knob that no workflow forwards is a knob you cannot set."""

    def test_every_pick_knob_is_forwarded_by_both_workflows_with_its_default(self):
        for name in WORKFLOWS:
            got = forwarded(workflow_text(name))
            for knob, default in knobs.AUDITED:
                with self.subTest(workflow=name, knob=knob):
                    self.assertIn(knob, got, f"{name} does not forward {knob}")
                    self.assertEqual(got[knob], default,
                                     f"{name} defaults {knob} to {got[knob]}, "
                                     f"knobs.AUDITED to {default}")

    def test_every_variable_the_code_reads_reaches_a_workflow(self):
        known: set[str] = set()
        for name in WORKFLOWS:
            known |= set(re.findall(r"^\s+([A-Z0-9_]+):", workflow_text(name), re.M))
        read: dict[str, set[str]] = {}
        for f in (ROOT / "nse_scanner").glob("*.py"):
            for var in re.findall(r'environ\.get\(\s*"([A-Z0-9_]+)"', f.read_text()):
                read.setdefault(var, set()).add(f.name)
        missing = {v: files for v, files in read.items()
                   if v not in known and v not in RUNTIME_ONLY}
        self.assertEqual(missing, {}, "a knob set in the repository reaches nothing")

    def test_both_workflows_print_the_repository_variables(self):
        for name in WORKFLOWS:
            with self.subTest(workflow=name):
                self.assertIn("toJSON(vars)", workflow_text(name))

    def test_both_loops_log_the_knobs_they_are_running_with(self):
        for fn in (pipeline.live_loop, scan1m.live_loop_1m):
            with self.subTest(loop=fn.__name__):
                self.assertIn("describe()", inspect.getsource(fn))


class KnobReportTests(TestCase):
    def test_the_recommended_set_is_named_on_the_line(self):
        with patch.dict(os.environ, {"PICK_CONF_SCALED_FLOOR": "1",
                                     "SECTOR_FOLLOW_ON_WEAK": "0",
                                     "DIVERGENCE_SHORT": "0"}, clear=True):
            in_force, changed = knobs.describe()
        self.assertIn("PICK_CONF_SCALED_FLOOR=1", in_force)
        self.assertIn("SECTOR_FOLLOW_ON_WEAK=0", in_force)
        self.assertIn("DIVERGENCE_SHORT=0", in_force)
        # ... and only the one that differs from its default shows as a change.
        self.assertEqual(changed, "pick knobs differing from default: PICK_CONF_SCALED_FLOOR=1")

    def test_a_default_run_says_so_instead_of_naming_everything(self):
        with patch.dict(os.environ, {}, clear=True):
            in_force, changed = knobs.describe()
        self.assertIn("DIVERGENCE_SHORT=0", in_force)          # printed anyway
        self.assertTrue(changed.startswith("pick knobs differing from default: none"))
        self.assertEqual(knobs.differing(), {})

    def test_an_empty_variable_is_a_default_not_a_value(self):
        # `vars.X` renders as "" when the variable does not exist; that must not
        # become a value the code has never seen (it is how a knob "arrives" as
        # nothing and the old behaviour silently stays).
        with patch.dict(os.environ, {"PICK_CONF_SCALED_FLOOR": "", "PICK_RS_MARGIN": "  "},
                        clear=True):
            self.assertEqual(knobs.effective("PICK_CONF_SCALED_FLOOR", "0"), "0")
            self.assertEqual(knobs.differing(), {})

    def test_the_defaults_reproduce_the_old_behaviour(self):
        # Everything new ships off: the floor is flat, the weak follow and the
        # divergence short are closed, and the pick is unchanged.
        from nse_scanner.market import (conf_scaled_floor, divergence_short, follow_on_weak,
                                        pick_min_persist)
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(conf_scaled_floor())
            self.assertFalse(follow_on_weak())
            self.assertFalse(divergence_short())
            self.assertEqual(pick_min_persist(), float(dict(knobs.AUDITED)["PICK_MIN_PERSIST"]))
