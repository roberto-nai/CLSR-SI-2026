"""Verify filing-year eligibility and reproducible annual case quotas offline."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from curia_assessment.inputs import case_year, cases_from_rows, validate_year_range
from curia_assessment.pipeline import load_config
from curia_assessment.sampling import select_cases_by_year


def case_rows(
    case_id: str, *, count: int = 2, category: str = "C", country: str = "EN",
) -> list[dict[str, str]]:
    return [
        {"CaseID": case_id, "Activity": f"Activity {index}",
         "Timestamp": f"2020-01-{index:02d}", "Case_Category": category, "Country": country}
        for index in range(1, count + 1)
    ]


def sample_cases(year: int, count: int) -> list[dict]:
    return [
        {"case_id": f"C-{number}/{year % 100:02d}", "case_year": year,
         "events": [{"activity": "Recorded event", "timestamp": "2030-01-01"}]}
        for number in range(1, count + 1)
    ]


class CaseYearTests(unittest.TestCase):
    def test_year_comes_from_case_number_including_recorded_suffixes(self) -> None:
        for identifier, expected in (
            ("C-1/17", 2017), ("C-44/26", 2026), ("C-1/2026", 2026),
            ("C-42/18 P", 2018), ("C-7/19 PPU", 2019), ("T-1/20", 2020),
        ):
            with self.subTest(identifier=identifier):
                self.assertEqual(case_year(identifier), expected)

    def test_missing_or_malformed_recorded_year_is_not_guessed(self) -> None:
        for identifier in ("C-1", "C-1/2", "C-1/201", "C-1/20260", "C-1/0000", "", None, 2020):
            with self.subTest(identifier=identifier), self.assertRaises(ValueError):
                case_year(identifier)

    def test_year_range_requires_inclusive_ordered_integer_bounds(self) -> None:
        validate_year_range(2017, 2026)
        validate_year_range(2020, 2020)
        validate_year_range(1, 9999)
        for start, end in ((2026, 2017), (None, 2026), (2017, None), (True, 2026),
                           (2017, False), (0, 2026), (2017, 10000), (2017.0, 2026),
                           (2017, "2026")):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                validate_year_range(start, end)


class CaseYearEligibilityTests(unittest.TestCase):
    def test_inclusive_filing_year_filter_preserves_complete_traces(self) -> None:
        rows = case_rows("C-1/16") + case_rows("C-1/17")
        rows += case_rows("C-1/26 P") + case_rows("C-1/27")
        rows[2]["Timestamp"] = "2015-01-01"
        rows[3]["Timestamp"] = "2028-01-01"
        original = copy.deepcopy(rows)
        unfiltered, _, _ = cases_from_rows(list(rows[0]), rows)
        cases, register, summary = cases_from_rows(
            list(rows[0]), rows, year_start=2017, year_end=2026,
        )
        self.assertEqual({case["case_id"] for case in cases}, {"C-1/17", "C-1/26 P"})
        self.assertEqual({case["case_year"] for case in cases}, {2017, 2026})
        filing_2017 = next(case for case in cases if case["case_id"] == "C-1/17")
        original_2017 = next(case for case in unfiltered if case["case_id"] == "C-1/17")
        self.assertEqual(filing_2017["events"], original_2017["events"])
        self.assertEqual(filing_2017["trace_sha256"], original_2017["trace_sha256"])
        self.assertEqual(filing_2017["event_count"], 2)
        self.assertEqual(rows, original)
        self.assertEqual(summary["year_start"], 2017)
        self.assertEqual(summary["year_end"], 2026)
        self.assertEqual(summary["excluded_year_cases"], 2)
        audit = {row["case_id"]: row for row in register}
        self.assertEqual(audit["C-1/16"]["case_year"], 2016)
        self.assertEqual(audit["C-1/16"]["selection_status"], "excluded")
        self.assertTrue(audit["C-1/16"]["exclusion_reason"])
        self.assertEqual(audit["C-1/17"]["selection_status"], "retained")

    def test_year_counts_apply_after_other_eligibility_filters(self) -> None:
        rows = case_rows("C-1/16") + case_rows("C-1/17") + case_rows("C-1/18")
        rows += case_rows("C-1/26") + case_rows("C-1/27")
        rows += case_rows("C-2/16", country="DE")
        rows += case_rows("C-3/16", count=1) + case_rows("T-1/16", category="T")
        cases, _, summary = cases_from_rows(
            list(rows[0]), rows, country="EN", year_start=2017, year_end=2026,
        )
        self.assertEqual(len(cases), 3)
        self.assertEqual(summary["eligible_cases_before_year_filter"], 5)
        self.assertEqual(summary["eligible_cases_after_year_filter"], 3)
        self.assertEqual(summary["excluded_year_cases"], 2)
        self.assertEqual(summary["excluded_cases"], 5)

    def test_disabled_year_filter_keeps_existing_non_numeric_case_identifiers(self) -> None:
        rows = case_rows("C-synthetic")
        cases, _, _ = cases_from_rows(list(rows[0]), rows)
        self.assertEqual([case["case_id"] for case in cases], ["C-synthetic"])
        with self.assertRaises(ValueError):
            cases_from_rows(list(rows[0]), rows, year_start=2017, year_end=2026)

    def test_partial_year_bounds_are_rejected(self) -> None:
        rows = case_rows("C-1/20")
        for arguments in ({"year_start": 2017}, {"year_end": 2026}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                cases_from_rows(list(rows[0]), rows, **arguments)


class AnnualSamplingTests(unittest.TestCase):
    def test_explicit_total_fifty_preserves_annual_base_and_compensates_latest_years(self) -> None:
        cases = [case for year in range(2016, 2025) for case in sample_cases(year, 10)]
        cases += sample_cases(2025, 1)
        original = copy.deepcopy(cases)
        selected, audit = select_cases_by_year(
            cases, year_start=2016, year_end=2026, cases_per_year=5, seed=71, total_cases=50,
        )
        self.assertEqual(len(selected), 50)
        self.assertEqual(len({case["case_id"] for case in selected}), 50)
        self.assertEqual(Counter(case["case_year"] for case in selected),
                         {**{year: 5 for year in range(2016, 2021)},
                          **{year: 6 for year in range(2021, 2025)}, 2025: 1})
        self.assertEqual(sum(row["base_sampled_cases"] for row in audit), 46)
        self.assertEqual(sum(row["additional_cases"] for row in audit), 4)
        self.assertEqual({row["year"]: row["additional_cases"] for row in audit if row["additional_cases"]},
                         {2021: 1, 2022: 1, 2023: 1, 2024: 1})
        self.assertEqual((selected, audit), select_cases_by_year(
            list(reversed(cases)), year_start=2016, year_end=2026, cases_per_year=5, seed=71, total_cases=50,
        ))
        base, base_audit = select_cases_by_year(
            cases, year_start=2016, year_end=2026, cases_per_year=5, seed=71, total_cases=46,
        )
        self.assertEqual(len(base), 46)
        self.assertTrue({case["case_id"] for case in base}.issubset({case["case_id"] for case in selected}))
        self.assertEqual(sum(row["additional_cases"] for row in base_audit), 0)
        nominal, nominal_audit = select_cases_by_year(
            cases, year_start=2016, year_end=2026, cases_per_year=5, seed=71,
        )
        self.assertEqual(len(nominal), 55)
        self.assertEqual(sum(row["additional_cases"] for row in nominal_audit), 9)
        self.assertEqual((nominal, nominal_audit), select_cases_by_year(
            cases, year_start=2016, year_end=2026, cases_per_year=5, seed=71, total_cases=None,
        ))
        self.assertEqual(cases, original)

    def test_total_below_annual_base_is_rejected_instead_of_truncating_the_sample(self) -> None:
        cases = sample_cases(2017, 4) + sample_cases(2018, 4)
        with self.assertRaisesRegex(ValueError, "total_cases=3.*annual base sample of 4"):
            select_cases_by_year(cases, year_start=2017, year_end=2018, cases_per_year=2, total_cases=3)

    def test_total_override_still_uses_only_the_eligible_year_pool_when_target_unavailable(self) -> None:
        cases = sample_cases(2017, 3) + sample_cases(2018, 1) + sample_cases(2016, 10)
        selected, audit = select_cases_by_year(
            cases, year_start=2017, year_end=2019, cases_per_year=2, total_cases=8,
        )
        self.assertEqual(len(selected), 4)
        self.assertEqual(Counter(case["case_year"] for case in selected), {2017: 3, 2018: 1})
        self.assertEqual(sum(row["additional_cases"] for row in audit), 1)

    def test_total_override_requires_positive_integer_and_annual_quota_mode(self) -> None:
        cases = sample_cases(2020, 3)
        for value in (0, -1, -2, True, False, 50.0, "50", [], {}):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "total_cases"):
                select_cases_by_year(cases, total_cases=value)
        with self.assertRaisesRegex(ValueError, "total_cases.*explicit case_ids"):
            select_cases_by_year(cases, total_cases=2, case_ids=["C-1/20"])
        for quota in (-1, None):
            with self.subTest(quota=quota), self.assertRaisesRegex(ValueError, "total_cases.*positive cases_per_year"):
                select_cases_by_year(cases, total_cases=2, cases_per_year=quota)

    def test_exhausted_pool_preserves_shortages_and_uses_every_in_range_case(self) -> None:
        cases = sample_cases(2017, 8) + sample_cases(2018, 2)
        cases += sample_cases(2016, 6) + sample_cases(2020, 6)
        original = copy.deepcopy(cases)
        selected, audit = select_cases_by_year(
            cases, year_start=2017, year_end=2019, cases_per_year=5, seed=71,
        )
        self.assertEqual(Counter(case["case_year"] for case in selected), {2017: 8, 2018: 2})
        self.assertEqual(len({case["case_id"] for case in selected}), 10)
        self.assertEqual(audit, [
            {"year": 2017, "eligible_cases": 8, "requested_cases": 5, "sampled_cases": 8,
             "base_sampled_cases": 5, "additional_cases": 3, "missing_cases": 0},
            {"year": 2018, "eligible_cases": 2, "requested_cases": 5, "sampled_cases": 2,
             "base_sampled_cases": 2, "additional_cases": 0, "missing_cases": 3},
            {"year": 2019, "eligible_cases": 0, "requested_cases": 5, "sampled_cases": 0,
             "base_sampled_cases": 0, "additional_cases": 0, "missing_cases": 5},
        ])
        global_deficit = 15 - len(selected)
        self.assertEqual(sum(row["missing_cases"] for row in audit),
                         sum(row["additional_cases"] for row in audit) + global_deficit)
        self.assertEqual(cases, original)

    def test_compensation_repeats_descending_passes_and_skips_exhausted_years(self) -> None:
        cases = sample_cases(2017, 5) + sample_cases(2018, 6) + sample_cases(2019, 7)
        cases += sample_cases(2020, 1)
        selected, audit = select_cases_by_year(
            cases, year_start=2017, year_end=2021, cases_per_year=3, seed=71,
        )
        self.assertEqual(len(selected), 15)
        self.assertEqual(Counter(case["case_year"] for case in selected),
                         {2017: 4, 2018: 5, 2019: 5, 2020: 1})
        # First pass adds 2019, 2018, 2017; the partial second pass adds 2019, 2018.
        self.assertEqual([row["additional_cases"] for row in audit], [1, 2, 2, 0, 0])
        self.assertEqual([row["base_sampled_cases"] for row in audit], [3, 3, 3, 1, 0])
        self.assertEqual(sum(row["missing_cases"] for row in audit), 5)
        self.assertEqual(sum(row["additional_cases"] for row in audit), 5)
        self.assertEqual(len({case["case_id"] for case in selected}), 15)
        self.assertEqual(selected, sorted(selected, key=lambda case: (case["case_year"], case["case_id"])))

    def test_years_exhausted_during_compensation_are_skipped_on_later_passes(self) -> None:
        cases = sample_cases(2017, 8) + sample_cases(2018, 3)
        selected, audit = select_cases_by_year(
            cases, year_start=2017, year_end=2020, cases_per_year=2,
        )
        self.assertEqual(len(selected), 8)
        self.assertEqual(Counter(case["case_year"] for case in selected), {2017: 5, 2018: 3})
        self.assertEqual([row["additional_cases"] for row in audit], [3, 1, 0, 0])

    def test_sampling_without_shortages_is_reproducible_and_year_independent(self) -> None:
        cases = sample_cases(2017, 12) + sample_cases(2018, 12) + sample_cases(2019, 12)
        selected, audit = select_cases_by_year(
            cases, year_start=2017, year_end=2019, cases_per_year=4, seed=32,
        )
        reordered, reordered_audit = select_cases_by_year(
            list(reversed(cases)), year_start=2017, year_end=2019, cases_per_year=4, seed=32,
        )
        single, single_audit = select_cases_by_year(
            cases, year_start=2018, year_end=2018, cases_per_year=4, seed=32,
        )
        self.assertEqual(selected, reordered)
        self.assertEqual(audit, reordered_audit)
        self.assertEqual(single, [case for case in selected if case["case_year"] == 2018])
        self.assertEqual(single_audit, [audit[1]])

    def test_compensation_is_reproducible_and_preserves_original_annual_samples(self) -> None:
        cases = sample_cases(2017, 12) + sample_cases(2018, 12)
        original = copy.deepcopy(cases)
        compensated = select_cases_by_year(
            cases, year_start=2017, year_end=2019, cases_per_year=4, seed=32,
        )
        self.assertEqual(compensated, select_cases_by_year(
            list(reversed(cases)), year_start=2017, year_end=2019, cases_per_year=4, seed=32,
        ))
        selected, audit = compensated
        self.assertEqual(len(selected), 12)
        self.assertEqual([row["additional_cases"] for row in audit], [2, 2, 0])
        for year in (2017, 2018):
            base, _ = select_cases_by_year(
                cases, year_start=year, year_end=year, cases_per_year=4, seed=32,
            )
            self.assertTrue({case["case_id"] for case in base}.issubset(
                {case["case_id"] for case in selected},
            ))
        self.assertEqual(cases, original)

    def test_explicit_cases_override_quota_but_must_respect_year_range(self) -> None:
        cases = sample_cases(2017, 3) + sample_cases(2018, 2) + sample_cases(2016, 1)
        identifiers = ["C-3/17", "C-1/18", "C-1/17"]
        selected, audit = select_cases_by_year(
            cases, year_start=2017, year_end=2018, cases_per_year=1, case_ids=identifiers,
        )
        self.assertEqual([case["case_id"] for case in selected], identifiers)
        self.assertEqual([row["sampled_cases"] for row in audit], [2, 1])
        self.assertTrue(all(row["requested_cases"] is None and row["missing_cases"] is None for row in audit))
        self.assertTrue(all(row["base_sampled_cases"] == row["sampled_cases"]
                            and row["additional_cases"] == 0 for row in audit))
        for invalid in (["C-1/16"], ["C-unknown/17"], [], ["C-1/17", "C-1/17"]):
            with self.subTest(identifiers=invalid), self.assertRaises(ValueError):
                select_cases_by_year(cases, year_start=2017, year_end=2018, case_ids=invalid)

    def test_all_cases_sentinels_have_identical_selection_and_audit(self) -> None:
        cases = sample_cases(2017, 3) + sample_cases(2018, 7)
        selected, audit = select_cases_by_year(cases, year_start=2017, year_end=2019, cases_per_year=-1)
        self.assertEqual((selected, audit), select_cases_by_year(
            cases, year_start=2017, year_end=2019, cases_per_year=None,
        ))
        self.assertEqual(len(selected), 10)
        self.assertEqual([row["sampled_cases"] for row in audit], [3, 7, 0])
        self.assertTrue(all(row["requested_cases"] is None and row["missing_cases"] is None for row in audit))
        self.assertTrue(all(row["base_sampled_cases"] == row["sampled_cases"]
                            and row["additional_cases"] == 0 for row in audit))

    def test_duplicate_identifiers_and_invalid_quota_or_seed_fail(self) -> None:
        cases = sample_cases(2020, 3)
        with self.assertRaises(ValueError):
            select_cases_by_year(cases + [cases[0]])
        for arguments in (
            *({"cases_per_year": value} for value in (0, -2, True, False, 1.0, -1.0, "5", [], {})),
            {"seed": True}, {"seed": "71"}, {"year_start": 2026, "year_end": 2017},
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                select_cases_by_year(cases, **arguments)


class AnnualSamplingConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "config.json"
        self.config = {
            "rules_csv": "rules.csv", "event_log_csv": "events.csv", "formal_results": "reference.zip",
            "providers": [{"id": "test", "provider": "openai", "model": "test-pinned-2026-11-17"}],
        }

    def load(self, **changes):
        self.path.write_text(json.dumps({**self.config, **changes}), encoding="utf-8")
        return load_config(self.path)[0]

    def test_defaults_cover_2017_to_2026_with_five_cases_per_year(self) -> None:
        config = self.load()
        self.assertEqual(config["year_start"], 2017)
        self.assertEqual(config["year_end"], 2026)
        self.assertEqual(config["cases_per_year"], 5)
        self.assertNotIn("case_count", config)

    def test_custom_range_and_all_case_sentinels(self) -> None:
        config = self.load(year_start=2020, year_end=2020, cases_per_year=3)
        self.assertEqual((config["year_start"], config["year_end"], config["cases_per_year"]), (2020, 2020, 3))
        self.assertEqual(self.load(cases_per_year=-1), self.load(cases_per_year=None))

    def test_invalid_year_and_quota_config_values_are_rejected(self) -> None:
        for name in ("year_start", "year_end"):
            for value in (0, -1, True, False, 2017.0, "2017", None, 10000, [], {}):
                with self.subTest(name=name, value=value), self.assertRaisesRegex(ValueError, name):
                    self.load(**{name: value})
        with self.assertRaises(ValueError):
            self.load(year_start=2026, year_end=2017)
        for value in (0, -2, True, False, 5.0, -1.0, "5", [], {}):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "cases_per_year"):
                self.load(cases_per_year=value)

    def test_obsolete_total_case_count_has_an_actionable_migration_error(self) -> None:
        with self.assertRaisesRegex(ValueError, "case_count.*cases_per_year"):
            self.load(case_count=20)


if __name__ == "__main__":
    unittest.main()
