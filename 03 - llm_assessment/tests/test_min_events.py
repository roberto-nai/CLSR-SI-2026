"""Check event-count eligibility boundaries and the no-threshold sentinel."""

from __future__ import annotations

import copy
import csv
import json
import tempfile
import unittest
from pathlib import Path

from curia_assessment.inputs import cases_from_rows, load_cases, select_cases
from curia_assessment.pipeline import load_config


def case_rows(case_id: str, count: int, *, category: str = "C", country: str = "EN") -> list[dict[str, str]]:
    return [
        {"CaseID": case_id, "Activity": f"Activity {index}",
         "Timestamp": f"2020-01-{index:02d}", "Case_Category": category, "Country": country}
        for index in range(1, count + 1)
    ]


class MinimumEventInputTests(unittest.TestCase):
    def test_threshold_is_inclusive_and_preserves_complete_ordered_traces(self):
        rows = case_rows("C-four", 4) + list(reversed(case_rows("C-five", 5)))
        rows += list(reversed(case_rows("C-six", 6)))
        original = copy.deepcopy(rows)
        cases, register, summary = cases_from_rows(list(rows[0]), rows, min_events=5)
        self.assertEqual([case["case_id"] for case in cases], ["C-five", "C-six"])
        for case, count in zip(cases, (5, 6), strict=True):
            self.assertEqual(case["event_count"], count)
            self.assertEqual([event["activity"] for event in case["events"]],
                             [f"Activity {index}" for index in range(1, count + 1)])
            self.assertEqual([event["ref"] for event in case["events"]],
                             [f"E{index:04d}" for index in range(1, count + 1)])
        self.assertEqual(rows, original)
        excluded = next(row for row in register if row["case_id"] == "C-four")
        self.assertEqual(excluded["selection_status"], "excluded")
        self.assertIn("Fewer than 5 recorded events", excluded["exclusion_reason"])
        self.assertEqual(summary["minimum_recorded_events"], 5)
        self.assertEqual(summary["selected_events"], 11)
        self.assertEqual(summary["excluded_cases"], 1)
        selected = select_cases(cases, count=1)
        self.assertGreaterEqual(selected[0]["event_count"], 5)
        with self.assertRaisesRegex(ValueError, "Unknown or excluded"):
            select_cases(cases, count=1, case_ids=["C-four"])

    def test_minus_one_keeps_single_event_cases_without_relaxing_other_filters(self):
        rows = case_rows("C-one", 1) + case_rows("C-two", 2)
        rows += case_rows("T-one", 1, category="T")
        rows += case_rows("C-DE", 1, country="DE")
        cases, register, summary = cases_from_rows(list(rows[0]), rows, min_events=-1, country="EN")
        self.assertEqual([case["case_id"] for case in cases], ["C-one", "C-two"])
        self.assertEqual([case["event_count"] for case in cases], [1, 2])
        self.assertEqual(summary["minimum_recorded_events"], -1)
        self.assertEqual(summary["selected_events"], 3)
        audit = {row["case_id"]: row for row in register}
        self.assertEqual(audit["C-one"]["selection_status"], "retained")
        self.assertIn("case category", audit["T-one"]["exclusion_reason"])
        self.assertIn("does not match EN", audit["C-DE"]["exclusion_reason"])
        self.assertTrue(all("Fewer than" not in row["exclusion_reason"] for row in register))
        all_cases, _, _ = cases_from_rows(list(rows[0]), rows, min_events=-1, category=None)
        self.assertEqual(len(all_cases), 4)

    def test_threshold_counts_recorded_occurrences_including_identical_events(self):
        row = case_rows("C-repeat", 1)[0]
        rows = [dict(row) for _ in range(5)]
        cases, register, summary = cases_from_rows(list(row), rows, min_events=5)
        self.assertEqual(cases[0]["event_count"], 5)
        self.assertEqual(len(cases[0]["events"]), 5)
        self.assertEqual(cases[0]["identical_record_excess"], 4)
        self.assertEqual(register[0]["distinct_activity_count"], 1)
        self.assertEqual(summary["selected_events"], 5)
        self.assertEqual(cases_from_rows(list(row), rows, min_events=6)[0], [])

    def test_invalid_threshold_values_are_rejected(self):
        rows = case_rows("C-one", 1)
        for value in (0, -2, True, False, -1.0, 5.0, "-1", "5", None, [], {}):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "min_events"):
                cases_from_rows(list(rows[0]), rows, min_events=value)

    def test_minus_one_exports_all_events_and_records_the_disabled_threshold(self):
        rows = case_rows("C-one", 1) + case_rows("C-two", 2)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "events.csv"
            with path.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=list(rows[0]), delimiter=";")
                writer.writeheader()
                writer.writerows(rows)
            cases, register, summary = load_cases(path, min_events=-1, output_dir=root / "prepared")
            self.assertEqual(len(cases), 2)
            self.assertEqual(summary["minimum_recorded_events"], -1)
            self.assertEqual(summary["excluded_cases"], 0)
            self.assertTrue(all(row["selection_status"] == "retained" for row in register))
            with Path(summary["clean_event_log"]).open(encoding="utf-8", newline="") as stream:
                self.assertEqual(list(csv.DictReader(stream, delimiter=";")), rows)
            explanation = (root / "prepared/case_exclusions.txt").read_text(encoding="utf-8")
            self.assertIn("Minimum recorded events: -1", explanation)
            self.assertIn("Cases removed: 0", explanation)


class MinimumEventConfigTests(unittest.TestCase):
    def test_default_five_and_explicit_positive_or_disabled_thresholds(self):
        config = {
            "rules_csv": "rules.csv", "event_log_csv": "events.csv", "formal_results": "reference.zip",
            "providers": [{"id": "test", "provider": "openai", "model": "test-pinned-2026-11-17"}],
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            self.assertEqual(load_config(path)[0]["min_events"], 5)
            for value in (1, 5, 8, -1):
                with self.subTest(value=value):
                    path.write_text(json.dumps({**config, "min_events": value}), encoding="utf-8")
                    self.assertEqual(load_config(path)[0]["min_events"], value)

    def test_invalid_threshold_config_values_are_rejected(self):
        config = {
            "rules_csv": "rules.csv", "event_log_csv": "events.csv", "formal_results": "reference.zip",
            "providers": [{"id": "test", "provider": "openai", "model": "test-pinned-2026-11-17"}],
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            for value in (0, -2, True, False, -1.0, 5.0, "-1", "5", None, [], {}):
                with self.subTest(value=value):
                    path.write_text(json.dumps({**config, "min_events": value}), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, "min_events"):
                        load_config(path)


if __name__ == "__main__":
    unittest.main()
