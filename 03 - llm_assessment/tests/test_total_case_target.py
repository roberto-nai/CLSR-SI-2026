"""Exercise total-case configuration and propagation through both planners."""
import unittest
from unittest.mock import patch

import test_pipeline as fixture_module
from curia_assessment import pipeline


class TotalCaseTargetTests(unittest.TestCase):
    def test_total_target_validation(self):
        fixture = fixture_module.PipelineTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        for value in (True, False, 0, -1, 1.5, "50", []):
            with self.subTest(value=value):
                fixture.config["total_cases"] = value
                fixture._write_config()
                with self.assertRaisesRegex(ValueError, "total_cases"):
                    pipeline.load_config(fixture.config_path)
        for overrides in ({"cases_per_year": None}, {"cases_per_year": -1}, {"case_ids": ["C-1/20"]}):
            with self.subTest(overrides=overrides):
                fixture.config.update(cases_per_year=2, case_ids=None, total_cases=50)
                fixture.config.update(overrides)
                fixture._write_config()
                with self.assertRaisesRegex(ValueError, "total_cases"):
                    pipeline.load_config(fixture.config_path)

    def test_both_planners_keep_explicit_total_separate_from_nominal_annual_quotas(self):
        for mode in ("rule", "case"):
            with self.subTest(mode=mode):
                fixture = fixture_module.PipelineTests()
                fixture.setUp()
                try:
                    fixture.config.update(assessment_mode=mode, conditions=["A"], cases_per_year=1,
                                          year_end=2022, total_cases=1)
                    fixture._write_config()
                    with patch.object(pipeline, "send_request") as send:
                        manifest = pipeline.prepare(fixture.config_path)
                    send.assert_not_called()
                    self.assertEqual(manifest["sampled_cases"], 1)
                    self.assertEqual(sum(row["additional_cases"] for row in manifest["case_sampling_by_year"]), 0)
                    report = (fixture.output / "case_extraction_report.txt").read_text()
                    self.assertIn("Annual quota reference: 3 x 1 = 3", report)
                    self.assertIn("Configured total target: 1", report)
                    self.assertIn("Selected cases: 1", report)
                    self.assertIn("Overall shortfall against target: 0", report)
                finally:
                    fixture.doCleanups()


if __name__ == "__main__":
    unittest.main()
