"""Reject accidental truncation settings and unsupported paired-run modes."""

import unittest
import hashlib

import test_pipeline as fixture_module
from curia_assessment.pipeline import load_config
from curia_assessment.case_pipeline import _document_sources


class Phase2ConfigTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture_module.PipelineTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_full_text_is_explicit_and_boolean_options_are_strict(self):
        self.fixture.config.update(assessment_mode="case", conditions=["B"],
                                   baseline_phase1_dir="old-output", max_document_characters=None,
                                   max_case_characters=None, deduplicate_exact_text=True,
                                   exclude_identifier_only=True, ocr_sidecars_dir="ocr")
        self.fixture._write_config()
        config, _ = load_config(self.fixture.config_path)
        self.assertIsNone(config["max_document_characters"])
        self.assertIsNone(config["max_case_characters"])
        for name, value in (("deduplicate_exact_text", "true"), ("exclude_identifier_only", 1),
                            ("max_document_characters", 0), ("max_case_characters", True)):
            with self.subTest(name=name):
                original = self.fixture.config[name]
                self.fixture.config[name] = value
                self.fixture._write_config()
                with self.assertRaisesRegex(ValueError, name):
                    load_config(self.fixture.config_path)
                self.fixture.config[name] = original

    def test_baseline_cannot_be_combined_with_a_or_rule_mode(self):
        for mode, conditions in (("case", ["A"]), ("case", ["A", "B"]), ("rule", ["B"])):
            with self.subTest(mode=mode, conditions=conditions):
                self.fixture.config.update(assessment_mode=mode, conditions=conditions,
                                           baseline_phase1_dir="old-output")
                self.fixture._write_config()
                with self.assertRaisesRegex(ValueError, "baseline_phase1_dir"):
                    load_config(self.fixture.config_path)

    def test_missing_ocr_is_frozen_as_absence_and_later_addition_changes_identity(self):
        root = self.fixture.root
        ocr = root / "ocr"
        ocr.mkdir()
        original = self.fixture._document_path("C-1/20")
        row = {"source_file": original.name, "source_sha256": hashlib.sha256(original.read_bytes()).hexdigest(),
               "ocr_source_file": "missing.txt", "ocr_source_sha256": None, "ocr_status": "missing"}
        config = {"case_texts_dir": "case-texts", "ocr_sidecars_dir": "ocr"}
        expected = _document_sources(config, root, [row], expected=True)
        self.assertEqual(expected, _document_sources(config, root, [row]))
        (ocr / "missing.txt").write_text("Newly available document text.", encoding="utf-8")
        self.assertNotEqual(expected, _document_sources(config, root, [row]))
