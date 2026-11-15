"""The 1m workflow must stay the same loop as the 5m one.

Both files are read as YAML and compared on the things that decide whether the
scan actually runs: the guard, the job budget, the handover contract, the state
cache, the concurrency group, the rule inputs and the secrets. If someone edits
one workflow and forgets the other, this test says so.
"""
from pathlib import Path
from unittest import TestCase

import yaml

ROOT = Path(__file__).resolve().parent.parent
FIVE = yaml.safe_load((ROOT / ".github/workflows/live-scan.yml").read_text(encoding="utf-8"))
ONE = yaml.safe_load((ROOT / ".github/workflows/live-scan-1m.yml").read_text(encoding="utf-8"))


def steps(doc, job):
    return {s.get("name"): s for s in doc["jobs"][job]["steps"] if s.get("name")}


def scan_step(doc):
    return doc["jobs"]["scan"]["steps"][[s.get("id") for s in doc["jobs"]["scan"]["steps"]].index("scan")]


class WorkflowParityTests(TestCase):
    def test_same_trigger_shape(self):
        for doc in (FIVE, ONE):
            dispatch = doc[True]["workflow_dispatch"]["inputs"]
            self.assertEqual(sorted(dispatch), ["dry_run", "handoff"])
            self.assertTrue(doc[True]["schedule"])
            self.assertEqual(doc["permissions"], {"contents": "read", "actions": "write"})

    def test_same_guard_and_budget(self):
        self.assertEqual(FIVE["jobs"]["guard"]["timeout-minutes"],
                         ONE["jobs"]["guard"]["timeout-minutes"])
        self.assertEqual(FIVE["jobs"]["scan"]["timeout-minutes"],
                         ONE["jobs"]["scan"]["timeout-minutes"])
        for doc in (FIVE, ONE):
            self.assertEqual(doc["jobs"]["scan"]["needs"], "guard")
            text = yaml.safe_dump(doc["jobs"]["guard"])
            self.assertIn("1533", text)                     # 15:33 IST stop
            self.assertIn("830", text)                      # 08:30 IST start
            self.assertIn("DRY RUN", text)                  # dry runs sit beside the loop

    def test_same_loop_command_and_budget(self):
        self.assertIn("--max-minutes 170", scan_step(FIVE)["run"])
        self.assertIn("--exit-at-close", scan_step(FIVE)["run"])
        self.assertIn("--max-minutes 170", scan_step(ONE)["run"])
        self.assertIn("--exit-at-close", scan_step(ONE)["run"])

    def test_own_but_equivalent_state_and_concurrency(self):
        five, one = FIVE["jobs"]["scan"], ONE["jobs"]["scan"]
        self.assertNotEqual(five["concurrency"]["group"], one["concurrency"]["group"])
        self.assertFalse(five["concurrency"]["cancel-in-progress"])
        self.assertFalse(one["concurrency"]["cancel-in-progress"])
        self.assertIn("state/scan1m_state.json", str(one))
        self.assertIn("nse-1m-scan-state-", str(one))
        self.assertNotIn("state/scan_state.json", str(one))   # never the 5m ledger

    def test_guard_watches_its_own_workflow_file(self):
        self.assertIn("live-scan.yml", str(FIVE["jobs"]["guard"]))
        self.assertIn("live-scan-1m.yml", str(ONE["jobs"]["guard"]))

    def test_same_handover_and_validation_steps(self):
        for doc, wf in ((FIVE, "live-scan.yml"), (ONE, "live-scan-1m.yml")):
            names = steps(doc, "scan")
            handover = [s for n, s in names.items() if "Hand over" in n][0]
            self.assertIn(f"gh workflow run {wf}", handover["run"])
            self.assertIn("-f handoff=true", handover["run"])
            validate = [s for n, s in names.items() if n.startswith("Validate")][0]
            self.assertIn("load(", validate["run"])           # schema check, not json.load
            self.assertIn("always()", validate["if"])

    def test_same_rule_inputs(self):
        five, one = scan_step(FIVE)["env"], scan_step(ONE)["env"]
        for key in ("SECTOR_FOLLOW", "SF_ALIGNED_SECTORS", "OI_FILTER",
                    "MAX_ALERT_LAG_SECONDS", "BAR_SETTLE_SECONDS", "DRY_RUN", "LOG_LEVEL"):
            self.assertIn(key, five, f"5m workflow lost {key}")
            self.assertIn(key, one, f"1m workflow lost {key} (a rule input)")

    def test_one_bot_three_on_the_second_loop(self):
        one = scan_step(ONE)["env"]
        self.assertIn("TELEGRAM_BOT_TOKEN_3", one)
        self.assertIn("TELEGRAM_CHAT_ID_3", one)
        self.assertNotIn("TELEGRAM_BOT_TOKEN", one)     # never bot 1
        five = scan_step(FIVE)["env"]
        self.assertNotIn("TELEGRAM_BOT_TOKEN_3", five)  # bot 1 never gets bot 3's token
