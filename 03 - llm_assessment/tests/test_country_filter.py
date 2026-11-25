"""Check that Country selection filters whole cases without changing traces."""

from __future__ import annotations

import copy
import csv
import json
import tempfile
import unittest
from pathlib import Path

from curia_assessment.inputs import cases_from_rows, load_cases, select_cases


def case_rows(case_id: str, countries: list[str], category: str = "C") -> list[dict[str, str]]:
    return [
        {
            "CaseID": case_id,
            "Activity": f"Activity {index}",
            "Timestamp": f"2020-01-{index:02d}",
            "Case_Category": category,
            "Country": country,
            "Area_of_Law": "Recorded area",
        }
        for index, country in enumerate(countries, 1)
    ]


class CountryFilterTests(unittest.TestCase):
    def test_null_retains_missing_and_conflicting_country_values(self):
        rows = case_rows("C-IT", ["IT", "IT"])
        rows += case_rows("C-empty", ["", ""])
        rows += case_rows("C-mixed", ["IT", "DE"])
        cases, _, summary = cases_from_rows(list(rows[0]), rows, country=None)
        self.assertEqual(len(cases), 3)
        self.assertIsNone(summary["country_filter"])
        self.assertEqual(summary["excluded_country_cases"], 0)
        self.assertEqual(summary["available_countries"], ["DE", "IT"])

    def test_null_does_not_require_country_column(self):
        rows = case_rows("C-1", ["IT", "IT"])
        for row in rows:
            del row["Country"]
        cases, register, summary = cases_from_rows(list(rows[0]), rows)
        self.assertEqual(len(cases), 1)
        self.assertEqual(register[0]["Country"], "")
        self.assertEqual(summary["available_countries"], [])
        with self.assertRaisesRegex(ValueError, "recorded Country column"):
            cases_from_rows(list(rows[0]), rows, country="IT")

    def test_matching_country_keeps_every_event_and_original_metadata(self):
        rows = case_rows("C-IT", ["IT", "", "IT"])
        rows += case_rows("C-DE", ["DE", "DE"])
        original = copy.deepcopy(rows)
        all_cases, _, _ = cases_from_rows(list(rows[0]), rows)
        cases, register, summary = cases_from_rows(list(rows[0]), rows, country="IT")
        self.assertEqual([case["case_id"] for case in cases], ["C-IT"])
        self.assertEqual(cases[0], next(case for case in all_cases if case["case_id"] == "C-IT"))
        self.assertEqual([event["source_row"] for event in cases[0]["events"]], [2, 3, 4])
        self.assertEqual(cases[0]["metadata"]["Country"], "IT")
        self.assertEqual(cases[0]["metadata"]["Area_of_Law"], "Recorded area")
        self.assertEqual(rows, original)
        self.assertEqual(summary["selected_events"], 3)
        self.assertEqual(summary["country_filter"], "IT")
        removed = next(row for row in register if row["case_id"] == "C-DE")
        self.assertEqual(removed["Country"], "DE")
        self.assertIn("does not match IT", removed["exclusion_reason"])

    def test_specific_country_excludes_missing_and_conflicting_cases(self):
        rows = case_rows("C-IT", ["IT", "IT"])
        rows += case_rows("C-empty", ["", ""])
        rows += case_rows("C-space", [" ", " "])
        rows += case_rows("C-mixed", ["IT", "DE"])
        cases, register, summary = cases_from_rows(list(rows[0]), rows, country="IT")
        self.assertEqual([case["case_id"] for case in cases], ["C-IT"])
        records = {row["case_id"]: row for row in register}
        self.assertIn("No non-empty", records["C-empty"]["exclusion_reason"])
        self.assertIn("No non-empty", records["C-space"]["exclusion_reason"])
        self.assertIn("Conflicting", records["C-mixed"]["exclusion_reason"])
        self.assertEqual(json.loads(records["C-mixed"]["Country"]), ["IT", "DE"])
        self.assertEqual(summary["excluded_country_cases"], 3)

    def test_unknown_value_reports_exact_available_labels(self):
        rows = case_rows("C-IT", ["IT", "IT"]) + case_rows("C-DE", ["DE", "DE"])
        for country in ("Italy", "it", " IT "):
            with self.subTest(country=country), self.assertRaisesRegex(
                ValueError, "Unknown recorded Country value .*Available values: DE, IT"
            ):
                cases_from_rows(list(rows[0]), rows, country=country)

    def test_specific_country_must_be_a_non_empty_string(self):
        rows = case_rows("C-IT", ["IT", "IT"])
        for country in ("", " ", True, 12, ["IT"]):
            with self.subTest(country=country), self.assertRaisesRegex(ValueError, "non-empty recorded Country"):
                cases_from_rows(list(rows[0]), rows, country=country)

    def test_country_counts_do_not_double_count_other_exclusions(self):
        rows = case_rows("C-IT", ["IT", "IT"])
        rows += case_rows("C-DE", ["DE", "DE"])
        rows += case_rows("C-short", ["DE"])
        rows += case_rows("T-DE", ["DE", "DE"], category="T")
        cases, _, summary = cases_from_rows(list(rows[0]), rows, country="IT")
        self.assertEqual(len(cases), 1)
        self.assertEqual(summary["source_cases"], 4)
        self.assertEqual(summary["eligible_cases_before_country_filter"], 2)
        self.assertEqual(summary["eligible_cases_after_country_filter"], 1)
        self.assertEqual(summary["excluded_country_cases"], 1)
        self.assertEqual(summary["excluded_cases"], 3)
        self.assertEqual(summary["excluded_one_event_cases"], 1)

    def test_minus_one_and_explicit_ids_use_the_filtered_pool(self):
        rows = case_rows("C-IT1", ["IT", "IT"])
        rows += case_rows("C-DE", ["DE", "DE"])
        rows += case_rows("C-IT2", ["IT", "IT"])
        cases, _, _ = cases_from_rows(list(rows[0]), rows, country="IT")
        selected = select_cases(cases, count=-1)
        self.assertEqual([case["case_id"] for case in selected], ["C-IT1", "C-IT2"])
        with self.assertRaisesRegex(ValueError, "Unknown or excluded"):
            select_cases(cases, count=-1, case_ids=["C-DE"])

    def test_clean_exports_preserve_complete_case_rows_and_explain_exclusions(self):
        rows = case_rows("C-IT", ["IT", "", "IT"])
        rows += case_rows("C-DE", ["DE", "DE"])
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            path = directory / "log.csv"
            with path.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=list(rows[0]), delimiter=";")
                writer.writeheader()
                writer.writerows(rows)
            _, _, summary = load_cases(path, country="IT", output_dir=directory / "prepared")
            with Path(summary["clean_event_log"]).open(encoding="utf-8", newline="") as stream:
                cleaned_rows = list(csv.DictReader(stream, delimiter=";"))
            self.assertEqual(cleaned_rows, rows[:3])
            explanation = (directory / "prepared/case_exclusions.txt").read_text(encoding="utf-8")
            self.assertIn("Recorded Country filter: IT", explanation)
            self.assertIn("Eligible cases before Country filtering: 2", explanation)
            self.assertIn("Eligible cases after Country filtering: 1", explanation)
            self.assertIn("Otherwise eligible cases removed by Country filtering: 1", explanation)
            self.assertTrue(explanation.endswith("C-DE\n"))


if __name__ == "__main__":
    unittest.main()
