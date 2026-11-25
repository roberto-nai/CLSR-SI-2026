"""Offline checks for exact activity labels and constrained argument notation."""

import csv
import io
import json
import tempfile
import unittest
from pathlib import Path

from curia_gpai.activities import read_activity_vocabulary, validate_declare_activities


class ActivityVocabularyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "events.csv"

    def write_log(self, delimiter, labels):
        with self.path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream, delimiter=delimiter)
            writer.writerow(["CaseID", "Activity", "PrivateField"])
            for number, label in enumerate(labels):
                writer.writerow([f"test-case-{number}", label, "test-only private content"])

    def test_supported_delimiters_preserve_exact_labels_and_no_case_data(self):
        labels = ["Judgment", "Judgment (OJ)", " Judgment", "judgment", 'A, quoted "label"', "Judgment"]
        for delimiter in (",", ";", "\t"):
            with self.subTest(delimiter=delimiter):
                self.write_log(delimiter, labels)
                result = read_activity_vocabulary(self.path)
                self.assertEqual(result["activities"], sorted(set(labels)))
                self.assertEqual(result["row_count"], 6)
                self.assertEqual(result["activity_count"], 5)
                self.assertEqual(result["delimiter"], delimiter)
                self.assertNotIn("test-case-", json.dumps(result))
                self.assertNotIn("test-only private content", json.dumps(result))

    def test_missing_column_empty_log_and_duplicate_headings_are_rejected(self):
        examples = [
            ("CaseID;activity\n1;Judgment\n", "no exact"),
            ("Activity\n", "no activity observations"),
            ("", "empty"),
            ("CaseID;Activity;Activity\n1;Judgment;Order\n", "duplicate"),
        ]
        for contents, message in examples:
            with self.subTest(contents=contents):
                self.path.write_text(contents, encoding="utf-8")
                with self.assertRaisesRegex(ValueError, message):
                    read_activity_vocabulary(self.path)

    def test_missing_blank_and_malformed_rows_are_not_silently_dropped(self):
        examples = [
            "CaseID;Activity\n1;Judgment\n2;\n",
            "CaseID;Activity\n1;Judgment\n2;   \n",
            "CaseID;Activity\n1;Judgment\n2\n",
            "CaseID;Activity\n1;Judgment\n2;Order;extra\n",
            'CaseID;Activity\n1;Judgment\n2;"Unclosed value\n',
        ]
        for contents in examples:
            with self.subTest(contents=contents):
                self.path.write_text(contents, encoding="utf-8")
                with self.assertRaises(ValueError):
                    read_activity_vocabulary(self.path)


class ActivityArgumentTests(unittest.TestCase):
    def setUp(self):
        self.activities = ["Judgment (OJ)", "Order", 'An event, with "quotes"']

    def test_exact_quoted_labels_allow_spaces_parentheses_commas_and_escapes(self):
        formulas = [
            'AtLeastOne("Judgment (OJ)")',
            'Response("Judgment (OJ)", "Order")',
            "Response(" + json.dumps(self.activities[2]) + ', "Order")',
        ]
        for formula in formulas:
            with self.subTest(formula=formula):
                result = validate_declare_activities(formula, self.activities)
                self.assertTrue(result["activity_vocabulary_valid"])
                self.assertEqual(result["activity_validation_status"], "valid")

    def test_case_spacing_snake_case_and_synonyms_do_not_pass_membership(self):
        for label in ("Judgment", "judgment (oj)", "judgment_oj", "Judgment (OJ) ", "A court judgment"):
            with self.subTest(label=label):
                result = validate_declare_activities("AtLeastOne(" + json.dumps(label) + ")", self.activities)
                self.assertFalse(result["activity_vocabulary_valid"])
                self.assertEqual(result["activity_validation_status"], "unknown_activity")

    def test_bare_labels_non_strings_extra_expressions_and_missing_arguments_fail(self):
        for formula in ('AtLeastOne(Order)', 'AtLeastOne(1)', 'AtLeastOne(null)',
                        'Response("Order", "Order", "Order")', 'Response()',
                        'AtLeastOne("Order"); AtLeastOne("Order")', 'No formula'):
            with self.subTest(formula=formula):
                result = validate_declare_activities(formula, self.activities)
                self.assertFalse(result["activity_vocabulary_valid"])
                self.assertEqual(result["activity_validation_status"], "invalid_argument_notation")

    def test_template_semantics_remain_a_human_judgement(self):
        result = validate_declare_activities('UnknownTemplate("Order")', self.activities)
        self.assertTrue(result["activity_vocabulary_valid"])


if __name__ == "__main__":
    unittest.main()
