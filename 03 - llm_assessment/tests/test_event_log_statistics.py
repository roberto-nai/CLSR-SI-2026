"""Check case-level counting and recorded-trace statistics conventions."""

from __future__ import annotations

import copy
import csv
import json
import tempfile
import unittest
from pathlib import Path

from curia_assessment.event_log_statistics import (
    format_event_log_statistics,
    summarise_event_log,
    write_event_log_statistics,
)


def trace(identifier, end, *, start="2020-01-01T00:00:00Z", labels=("A", "B")):
    return {
        "case_id": identifier,
        "events": [
            {"activity": labels[0], "timestamp": start, "source_row": 2},
            {"activity": labels[1], "timestamp": end, "source_row": 3},
        ],
    }


class EventLogStatisticsTests(unittest.TestCase):
    def test_document_formats_and_repeated_pairs_count_one_document_per_stem(self):
        first = trace("C-1/20", "2020-01-02T00:00:00Z")
        first.update(document_files=["nested/one.txt", "nested/one.html", "nested/one_unconverted.pdf",
                                     "two.txt", "two.pdf"], document_file_count=5)
        second = trace("C-2/20", "2020-01-02T00:00:00Z")
        second.update(document_files=["one.txt", "one.txt"])
        stats = summarise_event_log([
            first, {**first, "document_files": list(reversed(first["document_files"]))}, second,
        ])
        self.assertEqual(stats["n_cases_with_document_metadata"], 2)
        self.assertEqual(stats["n_document_files"], 6)
        self.assertEqual(stats["mean_document_files_per_case"], 3)
        self.assertEqual(stats["n_documents"], 3)
        self.assertEqual(stats["mean_documents_per_case"], 1.5)
        self.assertEqual(stats["median_documents_per_case"], 1.5)
        self.assertEqual((stats["min_documents_per_case"], stats["max_documents_per_case"]), (1, 2))
        self.assertIn("Mean associated documents per case: 1.500", format_event_log_statistics(stats))

    def test_missing_document_metadata_is_not_zero_and_later_copies_can_supply_it(self):
        first = trace("C-1/20", "2020-01-02T00:00:00Z")
        second = trace("C-2/20", "2020-01-02T00:00:00Z")
        empty = {**first, "document_files": [], "document_file_count": 0}
        stats = summarise_event_log([empty, second])
        self.assertEqual(stats["n_cases_with_document_metadata"], 1)
        for field in ("n_documents", "mean_documents_per_case", "n_document_files", "median_documents_per_case"):
            self.assertIsNone(stats[field])
        enriched = summarise_event_log([first, second, empty,
                                       {**second, "document_files": ["one.txt"], "document_file_count": 1}])
        self.assertEqual(enriched["n_documents"], 1)
        self.assertEqual(enriched["mean_documents_per_case"], 0.5)
        self.assertEqual(enriched["min_documents_per_case"], 0)
        legacy = {key: value for key, value in stats.items()
                  if not any(part in key for part in ("document",))}
        self.assertIn("Associated distinct documents: unavailable", format_event_log_statistics(legacy))

    def test_conflicting_document_lists_and_incorrect_declared_counts_fail(self):
        record = {**trace("C-1/20", "2020-01-02T00:00:00Z"), "document_files": ["one.txt"]}
        with self.assertRaisesRegex(ValueError, "conflicting document_files"):
            summarise_event_log([record, {**record, "document_files": ["two.txt"]}])
        with self.assertRaisesRegex(ValueError, "document_file_count differs"):
            summarise_event_log([{**record, "document_file_count": 2}])
        with self.assertRaisesRegex(ValueError, "document_file_count differs"):
            summarise_event_log([{**record, "document_files": None, "document_file_count": 2}, record])

    def test_repeated_pairs_do_not_multiply_cases_or_events(self):
        first = trace("C-1/20", "2020-01-02T00:00:00Z")
        second = trace("C-2/20", "2020-01-04T00:00:00Z")
        stats = summarise_event_log([
            {**first, "pair_id": "one"}, {**first, "pair_id": "two"}, second,
        ])
        self.assertEqual(stats["n_cases"], 2)
        self.assertEqual(stats["n_events"], 4)
        self.assertEqual(stats["n_activities"], 2)
        self.assertEqual(stats["n_variants"], 1)
        self.assertEqual(stats["cases_by_year"], {"2020": 2})
        self.assertEqual(stats["mean_case_duration_days"], 2)
        self.assertEqual(stats["median_case_duration_days"], 2)
        self.assertEqual(stats["pm4py_upper_median_case_duration_days"], 3)

    def test_conflicting_duplicate_trace_fails_instead_of_choosing_a_copy(self):
        first = trace("C-1/20", "2020-01-02T00:00:00Z")
        conflict = copy.deepcopy(first)
        conflict["events"][1]["source_row"] = 4
        with self.assertRaisesRegex(ValueError, "conflicting event traces"):
            summarise_event_log([first, conflict])

    def test_tied_timestamps_keep_frozen_order_and_repeated_activities(self):
        first = trace("C-1/20", "2020-01-01T00:00:00Z")
        reverse = trace("C-2/20", "2020-01-01T00:00:00Z", labels=("B", "A"))
        repeat = trace("C-3/20", "2020-01-01T00:00:00Z", labels=("A", "A"))
        stats = summarise_event_log([first, reverse, repeat])
        self.assertEqual(stats["n_cases_with_timestamp_ties"], 3)
        self.assertEqual(stats["n_variants"], 3)
        self.assertEqual(stats["median_case_duration_seconds"], 0)

    def test_offsets_and_naive_timestamps_use_absolute_utc_instants(self):
        first = trace("C-1/20", "2020-01-01T00:00:00+00:00",
                      start="2020-01-01T01:00:00+02:00")
        second = trace("C-2/20", "2020-01-02T00:00:00", start="2020-01-01T00:00:00")
        stats = summarise_event_log([first, second])
        self.assertEqual(stats["min_timestamp"], "2019-12-31T23:00:00+00:00")
        self.assertEqual(stats["max_timestamp"], "2020-01-02T00:00:00+00:00")
        self.assertEqual(stats["mean_case_duration_seconds"], (3600 + 86400) / 2)

    def test_duration_uses_extrema_without_reordering_activity_sequence(self):
        record = trace("C-1/20", "2020-01-01T00:00:00Z", start="2020-01-04T00:00:00Z")
        stats = summarise_event_log([record])
        self.assertEqual(stats["mean_case_duration_days"], 3)
        self.assertEqual(record["events"][0]["activity"], "A")

    def test_missing_and_invalid_timestamps_fail_explicitly(self):
        for invalid in (None, "", "not a timestamp"):
            with self.subTest(timestamp=invalid):
                record = trace("C-1/20", invalid)
                with self.assertRaises(ValueError):
                    summarise_event_log([record])

    def test_empty_input_has_unavailable_dates_and_durations(self):
        stats = summarise_event_log([])
        self.assertEqual(stats["n_cases"], 0)
        self.assertEqual(stats["n_events"], 0)
        self.assertEqual(stats["n_variants"], 0)
        self.assertIsNone(stats["min_timestamp"])
        self.assertIsNone(stats["median_case_duration_seconds"])
        self.assertEqual(stats["cases_by_year"], {})
        self.assertIn("unavailable", format_event_log_statistics(stats))

    def test_writes_consistent_machine_readable_and_text_outputs(self):
        records = [trace("C-1/20", "2020-01-02T00:00:00Z")]
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            stats = write_event_log_statistics(output, iter(records))
            self.assertEqual(json.loads((output / "event_log_statistics.json").read_text()), stats)
            with (output / "event_log_statistics.csv").open(newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["n_cases"], "1")
            self.assertEqual(json.loads(rows[0]["cases_by_year"]), {"2020": 1})
            text = (output / "event_log_statistics.txt").read_text()
            self.assertIn("one day = 86,400 seconds", text)
            self.assertIn("frozen event order", text)
            self.assertIn("upper-central convention", text)
            self.assertFalse(list(output.glob("*.tmp")))


if __name__ == "__main__":
    unittest.main()
