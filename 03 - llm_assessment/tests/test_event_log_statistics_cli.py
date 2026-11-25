"""Offline reporting verifies frozen inputs and preserves existing destinations."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import report_event_log_statistics as cli


class EventLogStatisticsCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.study = Path(temporary.name)
        self.audit = self.study / "case_assessment/_audit"
        prepared = self.audit / "prepared"
        prepared.mkdir(parents=True)
        self.pairs = prepared / "pairs.jsonl"
        self.records = [{"case_id": "C-1/20", "events": [
            {"activity": "A", "timestamp": "2020-01-01"}]}]
        self.pairs.write_text(json.dumps(self.records[0]) + "\n", encoding="utf-8")
        self.other = prepared / "selected_rules.csv"
        self.other.write_text("rule_id\nR01\n", encoding="utf-8")
        self.manifest = self.audit / "run_manifest.json"
        self.manifest.write_text(json.dumps({"prepared_sha256": {
            path.relative_to(self.audit).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (self.pairs, self.other)
        }}), encoding="utf-8")

    def test_all_frozen_hashes_are_checked_before_any_report_is_written(self):
        original = self.other.read_bytes()
        self.other.write_bytes(original + b"changed\n")
        with patch.object(cli, "write_event_log_statistics") as writer:
            with self.assertRaisesRegex(ValueError, "SHA-256 mismatch.*selected_rules"):
                cli.export_statistics(self.study)
            writer.assert_not_called()
        self.assertFalse((self.study / "descriptive_statistics").exists())
        self.other.write_bytes(original)
        manifest = json.loads(self.manifest.read_text())
        del manifest["prepared_sha256"]["prepared/pairs.jsonl"]
        self.manifest.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "does not protect"):
            cli.export_statistics(self.study)

    def test_new_report_has_provenance_and_existing_output_is_preserved(self):
        frozen_before = {path: path.read_bytes() for path in (self.pairs, self.other, self.manifest)}
        target = self.study / "descriptive_statistics"
        expected = {"n_cases": 1}

        def write_report(output, records):
            self.assertEqual(records, self.records)
            output.mkdir(parents=True, exist_ok=True)
            (output / "event_log_statistics.json").write_text(json.dumps(expected))
            return expected

        with patch.object(cli, "write_event_log_statistics", side_effect=write_report) as writer:
            self.assertEqual(cli.export_statistics(self.study), expected)
            writer.assert_called_once()
            before = {path: path.read_bytes() for path in target.iterdir()}
            with self.assertRaisesRegex(ValueError, "new or empty"):
                cli.export_statistics(self.study)
            writer.assert_called_once()
        provenance = json.loads((target / "event_log_statistics_provenance.json").read_text())
        self.assertEqual(provenance["pairs_path"], "case_assessment/_audit/prepared/pairs.jsonl")
        self.assertEqual(provenance["pairs_sha256"], hashlib.sha256(self.pairs.read_bytes()).hexdigest())
        self.assertEqual(provenance["manifest_sha256"], hashlib.sha256(self.manifest.read_bytes()).hexdigest())
        self.assertEqual(provenance["verified_prepared_files"], 2)
        self.assertEqual(provenance["model_requests_issued"], 0)
        self.assertEqual(before, {path: path.read_bytes() for path in target.iterdir()})
        self.assertEqual(frozen_before, {path: path.read_bytes() for path in frozen_before})


if __name__ == "__main__":
    unittest.main()
