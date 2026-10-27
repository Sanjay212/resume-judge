"""Offline tests for judge.py: no API key or network needed."""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "judge"))
import judge  # noqa: E402

EX = ROOT / "examples"
load = lambda name: json.loads((EX / name).read_text())


class TestItems(unittest.TestCase):
    def test_items_follow_fixed_section_order(self):
        items = judge.extract_items(load("posting.json"))
        self.assertEqual([i["section"] for i in items][:4], ["Requirements"] * 4)
        self.assertEqual(items[-1]["section"], "Preferred")
        self.assertEqual(len(items), 8)

    def test_about_text_used_only_when_a_section_is_missing(self):
        p = {"sections": {"2_requirements": ["Five years of product work in payments."],
                          "5_about_the_job": "You will own the payouts roadmap from discovery through launch. "
                                             "This is a remote contract role with a great salary."}}
        items = judge.extract_items(p)
        self.assertEqual([i["section"] for i in items], ["Requirements", "Role duties (from About the job)"])
        self.assertEqual(len(items), 2)   # the contract/remote sentence is skipped


class TestOfflineRescore(unittest.TestCase):
    def test_saved_links_reproduce_the_report(self):
        rep = judge.report(load("posting.json"),
                           {"before": load("resume_before.json"), "after": load("resume_after.json")},
                           load("links.json"))
        before, after = rep["versions"]["before"]["score"], rep["versions"]["after"]["score"]
        self.assertGreater(after["match_pct"], before["match_pct"])
        self.assertEqual(len(after["gaps"]), 0)

    def test_links_fail_loudly_when_items_change(self):
        posting = load("posting.json")
        posting["sections"]["2_requirements"].append("A new requirement added after scoring.")
        with self.assertRaises(Exception):
            judge.report(posting, {"after": load("resume_after.json")}, load("links.json"))


class TestRules(unittest.TestCase):
    def resume(self, bullets, summary=""):
        return {"summary": summary, "jobs": [["Acme | PM | 2020 - 2024", bullets]], "skills": "", "edu": []}

    def test_years_are_not_numbers_but_small_counts_are(self):
        self.assertEqual(judge._numbers("From 2019 to 2024 we cut 3 steps and 40% of rework"), ["3", "40%"])

    def test_only_single_use_repeats_fail(self):
        r = self.resume(["Cut rework 40% with a new queue."], summary="Cut rework 40%.")
        self.assertTrue(judge.rules(r)["passes"])
        out = judge.rules(r, single_use=["40%"])
        self.assertEqual(out["repeated_single_use"], {"40%": 2})
        self.assertFalse(out["passes"])

    def test_reverse_chronological_order(self):
        r = {"summary": "", "skills": "", "edu": [],
             "jobs": [["Old Co | PM | 2010 - 2012", ["Did a thing."]], ["New Co | PM | 2020 - 2024", ["Did another."]]]}
        self.assertTrue(judge.rules(r)["out_of_order"])

    def test_near_duplicate_bullets(self):
        r = self.resume(["Built a reconciliation tool that cut manual matching.",
                         "Built a reconciliation tool which cut manual matching."])
        self.assertEqual(len(judge.rules(r)["duplicate_bullets"]), 1)

    def test_word_budget(self):
        r = self.resume(["word " * 701])
        self.assertTrue(judge.rules(r)["over_budget"])


if __name__ == "__main__":
    unittest.main()
