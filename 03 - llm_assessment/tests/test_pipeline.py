"""Exercise planning, explicit retries and reporting without network requests."""

from __future__ import annotations

import contextlib
import csv
import io
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from curia_assessment import pipeline
from curia_assessment.paths import display_path


class PipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.output = self.root / "outputs"
        self.case_texts = self.root / "case-texts"
        self.config_path = self.root / "config.json"
        self.config = {
            "rules_csv": "rules.csv",
            "event_log_csv": "events.csv",
            "formal_results": "reference.zip",
            "case_texts_dir": "case-texts",
            "output_dir": "outputs",
            "min_events": 2,
            "country": None,
            "year_start": 2020,
            "year_end": 2020,
            "cases_per_year": 2,
            "rule_count": 2,
            "providers": [{
                "id": "test_openai", "provider": "openai",
                "model": "test-pinned-2026-11-17",
            }],
        }
        self._write_config()
        self._write_csv(self.root / "rules.csv", [
            {"ID": "R01", "DECLARE": "Response[A, B]",
             "source": "Article 1: A shall be followed by B.",
             "requirement": "Every A shall be followed by B."},
            {"ID": "R02", "DECLARE": "AtLeastOne[B]",
             "source": "[V0002]\nArticle 2: B is required.\n\n[V0003]\nArticle 3: Each case requires B.",
             "requirement": "[V0002]\nB must occur in the case.\n\n[V0003]\nAt least one B must be recorded."},
        ])
        self._write_csv(self.root / "events.csv", [
            {"CaseID": "C-1/20", "Activity": "A", "Timestamp": "2020-01-01", "Case_Category": "C"},
            {"CaseID": "C-1/20", "Activity": "B", "Timestamp": "2020-01-02", "Case_Category": "C"},
            {"CaseID": "C-2/20", "Activity": "B", "Timestamp": "2020-01-01", "Case_Category": "C"},
            {"CaseID": "C-2/20", "Activity": "A", "Timestamp": "2020-01-02", "Case_Category": "C"},
        ])
        # The identity still hashes this input; the importer alone is mocked.
        (self.root / "reference.zip").write_bytes(b"Synthetic formal reference fixture.\n")
        importer = patch.object(pipeline, "import_formal_results", side_effect=self._formal_reference)
        self.importer = importer.start()
        self.addCleanup(importer.stop)

    def _write_csv(self, path: Path, rows: list[dict]) -> None:
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]), delimiter=";")
            writer.writeheader()
            writer.writerows(rows)
        if path.name == "events.csv":
            self.case_texts.mkdir(exist_ok=True)
            for case_id in {row["CaseID"] for row in rows}:
                document = self._document_path(case_id)
                if not document.exists():
                    document.write_text("Synthetic case document.\n", encoding="utf-8")

    def _document_path(self, case_id: str, suffix: str = ".txt") -> Path:
        prefix = re.sub(r"[^A-Za-z0-9._()-]+", "_", case_id).strip("._")
        return self.case_texts / f"{prefix}_01_01{suffix}"

    def _write_config(self) -> None:
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")

    @staticmethod
    def _formal_reference(path, cases, variants, event_log_path, min_events=2):
        rules = {variant["base_rule_id"]: variant["declare"] for variant in variants}
        rows = []
        for case in cases:
            for rule_id, constraint in rules.items():
                violated = case["case_id"] == "C-2/20" and rule_id == "R01"
                unary = rule_id == "R02"
                rows.append({
                    "case_id": case["case_id"], "CaseID": case["case_id"],
                    "base_rule_id": rule_id, "rule_id": rule_id,
                    "constraint": constraint, "formal_satisfied": int(not violated),
                    "formal_outcome": "violated" if violated else "satisfied",
                    "activation_status": "not_applicable" if unary else "active",
                    "activation_count": None if unary else 1,
                    "fulfilment_count": None if unary else int(not violated),
                    "violation_count": None if unary else int(violated),
                    "vacuously_satisfied": False, "component_count": 1,
                    "trace_sha256": case["trace_sha256"],
                    "event_count": case["event_count"],
                })
        return rows, {"origin": "synthetic_test_fixture", "selected_pairs": len(rows)}

    @staticmethod
    def _response(outcome="compliance", status="ok") -> dict:
        return {
            "status": status, "attempted": True,
            "text": json.dumps({"outcome": outcome, "evidence_refs": ["REQUIREMENT", "DECLARE", "E0001"],
                                "justification": "The supplied requirement and recorded events support this judgement."}),
            "http_status": 200 if status == "ok" else 500,
            "model_returned": "test-pinned-2026-11-17",
            "error": None if status == "ok" else {"kind": "provider_error", "detail": "Synthetic server error."},
            "raw_response": {"fixture": True},
        }

    def _run(self, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return pipeline.run(self.config_path, **kwargs)

    def test_case_texts_directory_is_required_and_validated(self) -> None:
        self.config["case_texts_dir"] = "../case-texts"
        self._write_config()
        config, root = pipeline.load_config(self.config_path)
        self.assertEqual(pipeline.resolve(root, config["case_texts_dir"]), (self.root.parent / "case-texts").resolve())
        del self.config["case_texts_dir"]
        self._write_config()
        defaults, _ = pipeline.load_config(self.config_path)
        self.assertEqual(defaults["case_texts_dir"], "../00 - event_log/data_text")
        for value in (None, "", 42, [], True):
            with self.subTest(value=value):
                self.config["case_texts_dir"] = value
                self._write_config()
                with self.assertRaisesRegex(ValueError, "case_texts_dir"):
                    pipeline.load_config(self.config_path)

    def test_document_filter_applies_before_annual_quotas_and_compensation(self) -> None:
        case_ids = ["C-1/19", "C-1/20", "C-2/20", "C-3/20", "C-4/20", "C-5/20"]
        rows = [
            {"CaseID": case_id, "Activity": activity, "Timestamp": timestamp, "Case_Category": "C"}
            for case_id in case_ids
            for activity, timestamp in (("A", "2020-01-01"), ("B", "2020-01-02"))
        ]
        self._write_csv(self.root / "events.csv", rows)
        self._document_path("C-1/19").write_bytes(b"")
        self._document_path("C-5/20").unlink()
        self._document_path("C-5/20", ".csv").write_text("Unsupported case file.\n", encoding="utf-8")
        self.config.update(year_start=2019, year_end=2020, cases_per_year=2)
        self._write_config()
        manifest = pipeline.prepare(self.config_path)
        self.assertEqual(manifest["eligible_cases"], 4)
        self.assertEqual(manifest["sampled_cases"], 4)
        self.assertIn("case_texts_dir", manifest["input_sha256"])
        self.assertEqual(manifest["case_sampling_by_year"], [
            {"year": 2019, "eligible_cases": 0, "requested_cases": 2, "base_sampled_cases": 0,
             "additional_cases": 0, "sampled_cases": 0, "missing_cases": 2},
            {"year": 2020, "eligible_cases": 4, "requested_cases": 2, "base_sampled_cases": 2,
             "additional_cases": 2, "sampled_cases": 4, "missing_cases": 0},
        ])
        selected = self.importer.call_args.args[1]
        self.assertEqual({case["case_id"] for case in selected}, {"C-1/20", "C-2/20", "C-3/20", "C-4/20"})
        for case in selected:
            self.assertEqual(case["document_file_count"], 1)
            self.assertEqual(case["document_files"], [self._document_path(case["case_id"]).name])
            for filename in case["document_files"]:
                self.assertFalse(Path(filename).is_absolute())
                self.assertGreater((self.case_texts / filename).stat().st_size, 0)
        summary = pipeline.read_json(self.output / "prepared/cleaning/selection_summary.json")
        self.assertIs(summary["require_case_documents"], True)
        self.assertEqual(summary["eligible_cases_before_document_filter"], 6)
        self.assertEqual(summary["eligible_cases_after_document_filter"], 4)
        self.assertEqual(summary["excluded_document_cases"], 2)
        with (self.output / "prepared/cleaning/case_selection.csv").open(encoding="utf-8-sig") as stream:
            register = {row["case_id"]: row for row in csv.DictReader(stream)}
        for case_id in ("C-1/19", "C-5/20"):
            self.assertEqual(register[case_id]["document_file_count"], "0")
            self.assertEqual(register[case_id]["selection_status"], "excluded")
            self.assertIn("document", register[case_id]["exclusion_reason"].lower())
        report = (self.output / "case_extraction_report.txt").read_text(encoding="utf-8")
        self.assertIn("Document availability: at least one non-empty .txt, .html or .pdf file per selected case", report)
        self.assertIn("case-texts", report)
        self.assertIn("Cases excluded without documents: 2", report)
        self.assertIn("Selected cases: 4", report)

    def test_explicit_cases_without_documents_are_rejected(self) -> None:
        self._document_path("C-1/20").unlink()
        self.config["case_ids"] = ["C-1/20"]
        self._write_config()
        with patch.object(pipeline, "send_request") as send:
            with self.assertRaisesRegex(ValueError, "Unknown or excluded"):
                pipeline.prepare(self.config_path)
            send.assert_not_called()
        self.importer.assert_not_called()

    def test_selected_case_proof_lists_supported_nonempty_document_files(self) -> None:
        self._document_path("C-1/20", ".html").write_text("<html>Case document</html>", encoding="utf-8")
        self._document_path("C-1/20", ".pdf").write_bytes(b"%PDF-1.7\nSynthetic PDF fixture.\n")
        self._document_path("C-1/20", ".csv").write_text("Ignored data file.\n", encoding="utf-8")
        (self.case_texts / "C-1_20_02_02.txt").write_bytes(b"")
        self.config["case_ids"] = ["C-1/20"]
        self._write_config()
        pipeline.prepare(self.config_path)
        selected = self.importer.call_args.args[1][0]
        expected_files = ["C-1_20_01_01.html", "C-1_20_01_01.pdf", "C-1_20_01_01.txt"]
        self.assertEqual(selected["document_files"], expected_files)
        self.assertEqual(selected["document_file_count"], 3)
        pairs = pipeline.read_jsonl(self.output / "prepared/pairs.jsonl")
        self.assertTrue(all(pair["document_files"] == expected_files and pair["document_file_count"] == 3 for pair in pairs))

    def test_missing_document_directory_stops_before_preparation_or_requests(self) -> None:
        self.config["case_texts_dir"] = "missing-case-documents"
        self._write_config()
        with patch.object(pipeline, "send_request") as send:
            with self.assertRaises((ValueError, OSError)):
                self._run()
            send.assert_not_called()
        self.importer.assert_not_called()
        self.assertFalse((self.output / "run_manifest.json").exists())

    def test_document_presence_changes_block_frozen_experiment_resume(self) -> None:
        manifest = pipeline.prepare(self.config_path)
        self.assertIn("case_texts_dir", manifest["input_sha256"])
        document = self._document_path("C-1/20")
        original = document.read_bytes()
        document.unlink()
        with patch.object(pipeline, "send_request") as send:
            with self.assertRaisesRegex(ValueError, "input files: case_texts_dir"):
                self._run()
            send.assert_not_called()
        document.write_bytes(original)
        self._document_path("C-3/20").write_text("New case document.\n", encoding="utf-8")
        with patch.object(pipeline, "send_request") as send:
            with self.assertRaisesRegex(ValueError, "input files: case_texts_dir"):
                self._run()
            send.assert_not_called()
        self.assertEqual(self.importer.call_count, 1)

    def test_document_contents_and_ignored_files_do_not_change_presence_identity(self) -> None:
        original = pipeline.prepare(self.config_path)
        report_path = self.output / "case_extraction_report.txt"
        report_bytes = report_path.read_bytes()
        self._document_path("C-1/20").write_text("Revised non-empty document content.\n", encoding="utf-8")
        (self.case_texts / "README.md").write_text("Ignored corpus metadata.\n", encoding="utf-8")
        self._document_path("C-3/20").write_bytes(b"")
        resumed = pipeline.prepare(self.config_path)
        self.assertEqual(original["experiment_sha256"], resumed["experiment_sha256"])
        self.assertEqual(original["input_sha256"]["case_texts_dir"], resumed["input_sha256"]["case_texts_dir"])
        self.assertEqual(report_path.read_bytes(), report_bytes)
        self.assertEqual(self.importer.call_count, 1)

    def test_minimum_events_filters_before_sampling_and_explicit_case_selection(self) -> None:
        rows = [
            {"CaseID": case_id, "Activity": f"Activity {index}",
             "Timestamp": f"2020-01-{index:02d}", "Case_Category": "C", "Country": "EN"}
            for case_id, count in (("C-3/20", 4), ("C-5/20", 5), ("C-6/20", 6))
            for index in range(1, count + 1)
        ]
        self._write_csv(self.root / "events.csv", rows)
        self.config.update(min_events=5, country="EN", cases_per_year=1, rule_count=1)
        self._write_config()
        manifest = pipeline.prepare(self.config_path)
        self.assertEqual(manifest["eligible_cases"], 2)
        self.assertEqual(manifest["sampled_cases"], 1)
        selected = self.importer.call_args.args[1]
        self.assertEqual(len(selected), 1)
        self.assertGreaterEqual(selected[0]["event_count"], 5)
        self.assertEqual(self.importer.call_args.args[4], 5)
        summary = pipeline.read_json(self.output / "prepared/cleaning/selection_summary.json")
        self.assertEqual(summary["minimum_recorded_events"], 5)
        with (self.output / "prepared/cleaning/case_selection.csv").open(encoding="utf-8-sig") as stream:
            audit = {row["case_id"]: row for row in csv.DictReader(stream)}
        self.assertEqual(audit["C-3/20"]["selection_status"], "excluded")
        self.assertIn("Fewer than 5 recorded events", audit["C-3/20"]["exclusion_reason"])

        self.config.update(output_dir="explicit_case", case_ids=["C-3/20"])
        self._write_config()
        self.importer.reset_mock()
        with self.assertRaisesRegex(ValueError, "Unknown or excluded"):
            pipeline.prepare(self.config_path)
        self.importer.assert_not_called()

    def test_prepare_run_and_analyse_keep_pair_units_and_denominators(self) -> None:
        manifest = pipeline.prepare(self.config_path)
        self.assertEqual(manifest["sampled_cases"], 2)
        self.assertEqual(manifest["catalogue_base_rules"], 2)
        self.assertEqual(manifest["catalogue_textual_variants"], 3)
        self.assertEqual(manifest["selected_pairs"], 4)
        self.assertEqual(manifest["planned_assessments"], 4)
        self.assertEqual(self.importer.call_count, 1)
        pending = pipeline.analyse(self.config_path)["model_summary"][0]
        self.assertEqual(pending["n_pending_or_not_run"], 4)
        self.assertIsNone(pending["agreement_rate"])
        stats = pipeline.read_json(self.output / "event_log_statistics.json")
        self.assertEqual((stats["n_cases"], stats["n_events"], stats["n_variants"]), (2, 4, 2))
        self.assertEqual(stats["median_case_duration_days"], 1)
        self.assertEqual(stats["n_documents"], 2)
        self.assertEqual(stats["mean_documents_per_case"], 1)

        with patch.object(pipeline, "send_request", return_value=self._response()) as send:
            counts = self._run()
        self.assertEqual(send.call_count, 4)
        self.assertEqual(counts["successful"], 4)
        summary = pipeline.analyse(self.config_path)["model_summary"][0]
        self.assertEqual(summary["n_cases"], 2)
        self.assertEqual(summary["n_case_rule_pairs"], 4)
        self.assertEqual(summary["n_comparable_binary_decisions"], 4)
        self.assertEqual(summary["n_agreements"], 3)
        self.assertEqual(summary["n_disagreements"], 1)
        self.assertEqual(summary["agreement_rate"], 0.75)
        self.assertTrue((self.output / "comparison_by_pair.csv").is_file())
        self.assertTrue((self.output / "summary_by_activation.csv").is_file())

    def test_resume_does_not_repeat_completed_calls(self) -> None:
        pipeline.prepare(self.config_path)
        with patch.object(pipeline, "send_request", return_value=self._response()) as send:
            first = self._run(max_requests=1)
            resumed = self._run()
            completed = self._run()
        self.assertEqual(first["processed"], 1)
        self.assertEqual(resumed["processed"], 3)
        self.assertEqual(completed["processed"], 0)
        self.assertEqual(completed["skipped_existing"], 4)
        self.assertEqual(send.call_count, 4)
        self.assertEqual(len(list((self.output / "requests").glob("*/attempt_*"))), 4)

    def test_preparation_saves_unknown_resource_totals_before_any_attempt(self) -> None:
        pipeline.prepare(self.config_path)
        summaries = pipeline.analyse(self.config_path)
        usage = summaries["usage_summary"][0]
        self.assertEqual(usage["n_attempts"], 0)
        self.assertEqual(usage["n_attempts_with_elapsed_seconds"], 0)
        self.assertIsNone(usage["elapsed_seconds_total"])
        self.assertIsNone(usage["elapsed_seconds_mean"])
        self.assertIsNone(usage["total_tokens_total"])
        self.assertEqual(usage["n_attempts_with_total_tokens"], 0)
        self.assertEqual(summaries["model_summary"][0]["n_attempts"], 0)
        with (self.output / "usage_by_model.csv").open(encoding="utf-8-sig") as stream:
            row = next(csv.DictReader(stream))
        self.assertEqual(row["total_tokens_total"], "")
        self.assertEqual(row["n_attempts"], "0")
        with (self.output / "attempts.csv").open(encoding="utf-8-sig") as stream:
            headers = csv.DictReader(stream).fieldnames
        self.assertIn("elapsed_seconds", headers)
        self.assertIn("input_tokens", headers)
        self.assertFalse((self.output / "requests").exists())

    def test_legacy_usage_is_normalised_without_rewriting_archived_assessments(self) -> None:
        pipeline.prepare(self.config_path)
        response = self._response()
        response.update(elapsed_seconds=12.5, usage={
            "input_tokens": 10, "output_tokens": 3, "total_tokens": 13,
            "input_tokens_details": {"cached_tokens": 2},
            "output_tokens_details": {"reasoning_tokens": 1},
        })
        with patch.object(pipeline, "send_request", return_value=response):
            self._run(max_requests=1)
        path = next((self.output / "requests").glob("*/attempt_001/assessment.json"))
        legacy = pipeline.read_json(path)
        for field in pipeline.TOKEN_FIELDS:
            legacy.pop(field)
        pipeline.write_json(path, legacy)
        original_assessment = path.read_bytes()
        response_path = path.with_name("response.json")
        original_response = response_path.read_bytes()

        summaries = pipeline.analyse(self.config_path)
        usage = summaries["usage_summary"][0]
        self.assertEqual(usage["n_attempts"], 1)
        self.assertEqual(usage["elapsed_seconds_total"], 12.5)
        self.assertEqual(usage["elapsed_seconds_mean"], 12.5)
        self.assertEqual(usage["input_tokens_total"], 10)
        self.assertEqual(usage["output_tokens_total"], 3)
        self.assertEqual(usage["total_tokens_total"], 13)
        self.assertEqual(usage["cached_input_tokens_total"], 2)
        self.assertEqual(usage["reasoning_tokens_total"], 1)
        self.assertEqual(usage["n_attempts_with_total_tokens"], 1)
        self.assertEqual(summaries["model_summary"][0]["total_tokens_total"], 13)
        self.assertEqual(path.read_bytes(), original_assessment)
        self.assertEqual(response_path.read_bytes(), original_response)
        with (self.output / "attempts.csv").open(encoding="utf-8-sig") as stream:
            row = next(csv.DictReader(stream))
        self.assertEqual(row["input_tokens"], "10")
        self.assertEqual(row["total_tokens"], "13")
        self.assertEqual(json.loads(row["usage"]), response["usage"])

    def test_resource_totals_include_failed_and_invalid_attempts_before_retries(self) -> None:
        pipeline.prepare(self.config_path)

        def response_with_usage(index, *, outcome="compliance", status="ok", missing_usage=False):
            response = self._response(outcome, status)
            response.update(elapsed_seconds=float(index), usage=None if missing_usage else {
                "input_tokens": 10 * index, "output_tokens": 2 * index, "total_tokens": 12 * index,
            })
            return response

        initial = [response_with_usage(1), response_with_usage(2, outcome="unrecognised_label"),
                   response_with_usage(3, status="api_error", missing_usage=True),
                   response_with_usage(4, outcome="undetermined")]
        with patch.object(pipeline, "send_request", side_effect=initial):
            self._run()
        first = pipeline.analyse(self.config_path)["usage_summary"][0]
        self.assertEqual(first["n_attempts"], 4)
        self.assertEqual(first["elapsed_seconds_total"], 10.0)
        self.assertEqual(first["total_tokens_total"], 84)
        self.assertEqual(first["n_attempts_with_total_tokens"], 3)

        with patch.object(pipeline, "send_request", side_effect=[response_with_usage(5), response_with_usage(6)]):
            self._run(retry_failed=True)
        summaries = pipeline.analyse(self.config_path)
        usage = summaries["usage_summary"][0]
        self.assertEqual(usage["n_attempts"], 6)
        self.assertEqual(usage["elapsed_seconds_total"], 21.0)
        self.assertEqual(usage["elapsed_seconds_mean"], 3.5)
        self.assertEqual(usage["n_attempts_with_elapsed_seconds"], 6)
        self.assertEqual(usage["input_tokens_total"], 180)
        self.assertEqual(usage["output_tokens_total"], 36)
        self.assertEqual(usage["total_tokens_total"], 216)
        self.assertEqual(usage["n_attempts_with_total_tokens"], 5)
        latest = summaries["model_summary"][0]
        self.assertEqual(latest["n_valid_outputs"], 4)
        self.assertEqual(latest["n_invalid_outputs"], 0)
        self.assertEqual(latest["n_attempts"], 6)
        self.assertEqual(latest["total_tokens_total"], 216)
        self.assertNotIn("n_attempts", summaries["rule_summary"][0])
        with (self.output / "attempts.csv").open(encoding="utf-8-sig") as stream:
            ledger = list(csv.DictReader(stream))
        self.assertEqual(len(ledger), 6)
        self.assertEqual(sum(row["status"] == "invalid_output" for row in ledger), 1)
        self.assertEqual(sum(row["status"] == "api_error" for row in ledger), 1)
        self.assertEqual(sum(row["total_tokens"] == "" for row in ledger), 1)

    def test_prepared_requests_with_metadata_match_runtime_for_all_providers_and_conditions(self) -> None:
        with (self.root / "events.csv").open(encoding="utf-8", newline="") as stream:
            events = list(csv.DictReader(stream, delimiter=";"))
        for event in events:
            event.update(Country="EN", Area_of_Law="Procedural law", Activity_Party="Applicant",
                         ECLI="ECLI:EU:C:2020:1", Date_Quality="exact")
        self._write_csv(self.root / "events.csv", events)
        self.config.update(case_ids=["C-1/20"], rule_ids=["R01"],
                           conditions=["A", "B", "C"], evidence_jsonl="evidence.jsonl")
        self.config["providers"].extend([
            {"id": "test_anthropic", "provider": "anthropic", "model": "test-pinned-2026-11-17"},
            {"id": "test_mistral", "provider": "mistral", "model": "test-pinned-2026-11-17"},
        ])
        self._write_config()
        pipeline.write_jsonl(self.root / "evidence.jsonl", [{
            "case_id": "C-1/20", "variant_id": "R01", "passages": [{
                "passage_id": "D1", "source_reference": "Frozen case document, paragraph 1",
                "text": "The case document records B.",
            }],
        }])
        manifest = pipeline.prepare(self.config_path)
        jobs = pipeline.read_jsonl(self.output / "prepared" / "planned_assessments.jsonl")
        self.assertEqual(manifest["planned_assessments"], 9)
        self.assertEqual({(job["provider"], job["condition"]) for job in jobs}, {
            (provider, condition)
            for provider in ("openai", "anthropic", "mistral") for condition in ("A", "B", "C")
        })
        with patch.object(pipeline, "send_request", return_value=self._response()) as send:
            result = self._run()
        self.assertEqual(result["successful"], 9)
        self.assertEqual(send.call_count, 9)
        for job, call in zip(jobs, send.call_args_list, strict=True):
            self.assertEqual(pipeline.digest(call.args[0]), job["request_sha256"])

    def test_case_and_rule_counts_select_a_shared_cartesian_product(self) -> None:
        self.config.update(cases_per_year=1, rule_count=1)
        self.config['providers'].extend([
            {'id': 'test_anthropic', 'provider': 'anthropic', 'model': 'test-pinned-2026-11-17'},
            {'id': 'test_mistral', 'provider': 'mistral', 'model': 'test-pinned-2026-11-17'},
        ])
        self._write_config()
        manifest = pipeline.prepare(self.config_path)
        self.assertEqual(manifest['sampled_cases'], 1)
        self.assertEqual(manifest['selected_base_rules'], 1)
        self.assertEqual(manifest['selected_pairs'], 1)
        self.assertEqual(manifest['planned_assessments'], 3)
        jobs = pipeline.read_jsonl(self.output / 'prepared' / 'planned_assessments.jsonl')
        self.assertEqual(len({job['case_id'] for job in jobs}), 1)
        self.assertEqual(len({job['variant_id'] for job in jobs}), 1)
        self.assertEqual(len({job['prompt_sha256'] for job in jobs}), 1)
        self.assertEqual({job['run'] for job in jobs}, {1})

    def test_annual_sampling_preserves_traces_and_shares_pairs_across_providers(self) -> None:
        case_ids = [
            "C-1/16", "C-1/17", "C-2/17 P", "Avis 3/17",
            "C-1/18", "C-2/18 PPU", "C-1/20", "C-1/21",
        ]
        rows = [
            {"CaseID": case_id, "Activity": activity, "Timestamp": timestamp,
             "Case_Category": "C", "Country": "EN"}
            for case_id in case_ids
            for activity, timestamp in (("A", "2015-01-01"), ("B", "2022-01-01"), ("A", "2027-01-01"))
        ]
        self._write_csv(self.root / "events.csv", rows)
        self.config.update(year_start=2017, year_end=2020, cases_per_year=2, country="EN")
        self.config["providers"].extend([
            {"id": "test_anthropic", "provider": "anthropic", "model": "test-pinned-2026-11-17"},
            {"id": "test_mistral", "provider": "mistral", "model": "test-pinned-2026-11-17"},
        ])
        self._write_config()
        manifest = pipeline.prepare(self.config_path)
        expected_audit = [
            {"year": 2017, "eligible_cases": 3, "requested_cases": 2, "base_sampled_cases": 2,
             "additional_cases": 1, "sampled_cases": 3, "missing_cases": 0},
            {"year": 2018, "eligible_cases": 2, "requested_cases": 2, "base_sampled_cases": 2,
             "additional_cases": 0, "sampled_cases": 2, "missing_cases": 0},
            {"year": 2019, "eligible_cases": 0, "requested_cases": 2, "base_sampled_cases": 0,
             "additional_cases": 0, "sampled_cases": 0, "missing_cases": 2},
            {"year": 2020, "eligible_cases": 1, "requested_cases": 2, "base_sampled_cases": 1,
             "additional_cases": 0, "sampled_cases": 1, "missing_cases": 1},
        ]
        self.assertEqual(manifest["case_sampling_by_year"], expected_audit)
        self.assertEqual(manifest["sampled_cases"], 6)
        self.assertEqual(manifest["selected_pairs"], 12)
        self.assertEqual(manifest["planned_assessments"], 36)
        with (self.output / "prepared/case_sampling_by_year.csv").open(encoding="utf-8-sig") as stream:
            exported_audit = [{key: int(value) for key, value in row.items()} for row in csv.DictReader(stream)]
        self.assertEqual(exported_audit, expected_audit)

        selected = self.importer.call_args.args[1]
        selected_ids = {case["case_id"] for case in selected}
        self.assertNotIn("C-1/16", selected_ids)
        self.assertNotIn("C-1/21", selected_ids)
        self.assertIn("C-1/20", selected_ids)
        for case in selected:
            source_trace = [(row["Activity"], row["Timestamp"]) for row in rows if row["CaseID"] == case["case_id"]]
            self.assertEqual([(event["activity"], event["timestamp"]) for event in case["events"]], source_trace)
            self.assertEqual(case["event_count"], 3)
        jobs = pipeline.read_jsonl(self.output / "prepared/planned_assessments.jsonl")
        expected_pairs = {(case_id, rule_id) for case_id in selected_ids for rule_id in ("R01", "R02")}
        for provider in self.config["providers"]:
            provider_jobs = [job for job in jobs if job["provider_id"] == provider["id"]]
            self.assertEqual({(job["case_id"], job["base_rule_id"]) for job in provider_jobs}, expected_pairs)

        report_path = self.output / "case_extraction_report.txt"
        report_bytes = report_path.read_bytes()
        report = report_bytes.decode("utf-8")
        self.assertIn("CASE EXTRACTION REPORT", report)
        self.assertIn("4 x 2 = 8", report)
        self.assertIn("Selected cases: 6", report)
        self.assertIn("Cases selected under annual quotas: 5", report)
        self.assertIn("Additional cases selected to compensate shortfalls: 1", report)
        self.assertIn("Overall shortfall against target: 2", report)
        self.assertIn("Year | Eligible | Quota | Base selected | Additional | Selected | Base shortfall", report)
        self.assertIn("2020 | 1 | 2 | 1 | 0 | 1 | 1", report)
        self.assertIn("2019 | 0 | 2 | 0 | 0 | 0 | 2", report)
        self.assertIn("2018 | 2 | 2 | 2 | 0 | 2 | 0", report)
        self.assertIn("2017 | 3 | 2 | 2 | 1 | 3 | 0", report)
        self.assertIn("Selected CaseIDs (numbered list)", report)
        self.assertEqual(
            [line for line in report.splitlines() if line[:1].isdigit() and ". " in line],
            [f"{number}. {case['case_id']}" for number, case in enumerate(selected, 1)],
        )
        self.assertEqual(manifest["case_extraction_report"], "case_extraction_report.txt")
        self.assertEqual(manifest["prepared_sha256"]["case_extraction_report.txt"], pipeline.sha256_file(report_path))
        resumed = pipeline.prepare(self.config_path)
        self.assertEqual(manifest, resumed)
        self.assertEqual(report_path.read_bytes(), report_bytes)
        self.assertEqual(self.importer.call_count, 1)

    def test_explicit_cases_override_annual_quota_and_audit_actual_selection(self) -> None:
        self.config.update(cases_per_year=1, case_ids=["C-2/20", "C-1/20"])
        self._write_config()
        manifest = pipeline.prepare(self.config_path)
        self.assertEqual(manifest["sampled_cases"], 2)
        self.assertEqual([case["case_id"] for case in self.importer.call_args.args[1]], ["C-2/20", "C-1/20"])
        self.assertEqual(manifest["case_sampling_by_year"], [
            {"year": 2020, "eligible_cases": 2, "requested_cases": None, "base_sampled_cases": 2,
             "additional_cases": 0, "sampled_cases": 2, "missing_cases": None},
        ])
        report = (self.output / "case_extraction_report.txt").read_text(encoding="utf-8")
        self.assertIn("CASE EXTRACTION REPORT", report)
        self.assertIn("Selected CaseIDs (numbered list)", report)
        self.assertIn("case_ids", report)
        self.assertNotIn("1 x 1 = 1", report)
        self.assertIn("Selected cases: 2", report)
        self.assertIn("Additional cases selected to compensate shortfalls: 0", report)
        self.assertIn("1. C-2/20\n2. C-1/20", report)

    def test_modified_or_missing_extraction_report_blocks_resume(self) -> None:
        manifest = pipeline.prepare(self.config_path)
        report_path = self.output / "case_extraction_report.txt"
        self.assertIn("case_extraction_report.txt", manifest["prepared_sha256"])
        original = report_path.read_bytes()
        report_path.write_bytes(original + b"\nChanged extraction report.\n")
        with patch.object(pipeline, "send_request") as send:
            with self.assertRaisesRegex(ValueError, "Prepared evidence or plan changed"):
                self._run()
            send.assert_not_called()
        report_path.unlink()
        with self.assertRaisesRegex(ValueError, "Prepared evidence or plan changed"):
            pipeline.prepare(self.config_path)

    def test_explicit_cases_must_fall_within_configured_year_range(self) -> None:
        with (self.root / "events.csv").open(encoding="utf-8", newline="") as stream:
            rows = list(csv.DictReader(stream, delimiter=";"))
        rows.extend([
            {"CaseID": "C-1/19", "Activity": "A", "Timestamp": "2020-01-01", "Case_Category": "C"},
            {"CaseID": "C-1/19", "Activity": "B", "Timestamp": "2020-01-02", "Case_Category": "C"},
        ])
        self._write_csv(self.root / "events.csv", rows)
        self.config["case_ids"] = ["C-1/19"]
        self._write_config()
        with self.assertRaisesRegex(ValueError, "Unknown or excluded"):
            pipeline.prepare(self.config_path)
        self.importer.assert_not_called()

    def test_annual_sampling_defaults_and_invalid_years_are_checked(self) -> None:
        for name in ("year_start", "year_end", "cases_per_year"):
            del self.config[name]
        self._write_config()
        config, _ = pipeline.load_config(self.config_path)
        self.assertEqual(config["year_start"], 2017)
        self.assertEqual(config["year_end"], 2026)
        self.assertEqual(config["cases_per_year"], 5)
        baseline = dict(self.config)
        for name in ("year_start", "year_end"):
            for value in (None, True, False, 0, -1, 2020.0, "2020", [], {}):
                with self.subTest(name=name, value=value):
                    self.config = {**baseline, name: value}
                    self._write_config()
                    with self.assertRaisesRegex(ValueError, name):
                        pipeline.load_config(self.config_path)
        self.config = {**baseline, "year_start": 2026, "year_end": 2017}
        self._write_config()
        with self.assertRaisesRegex(ValueError, "year_start.*year_end"):
            pipeline.load_config(self.config_path)

    def test_removed_total_case_count_has_a_migration_message(self) -> None:
        self.config["case_count"] = 20
        self._write_config()
        with self.assertRaisesRegex(ValueError, "case_count.*cases_per_year"):
            pipeline.load_config(self.config_path)

    def test_minus_one_and_null_counts_share_the_same_frozen_experiment(self) -> None:
        self.config.update(cases_per_year=-1, rule_count=-1)
        self._write_config()
        first = pipeline.prepare(self.config_path)
        self.assertIsNone(first["configuration"]["cases_per_year"])
        self.assertIsNone(first["configuration"]["rule_count"])
        self.assertEqual(first["sampled_cases"], 2)
        self.assertEqual(first["selected_base_rules"], 2)
        self.assertEqual(first["selected_pairs"], 4)
        self.assertEqual(first["case_sampling_by_year"], [
            {"year": 2020, "eligible_cases": 2, "requested_cases": None, "base_sampled_cases": 2,
             "additional_cases": 0, "sampled_cases": 2, "missing_cases": None},
        ])
        report_path = self.output / "case_extraction_report.txt"
        first_report = report_path.read_bytes()
        report = first_report.decode("utf-8")
        self.assertIn("CASE EXTRACTION REPORT", report)
        self.assertIn("Selected CaseIDs (numbered list)", report)
        self.assertTrue("-1" in report or "null" in report)
        self.assertIn("Selected cases: 2", report)
        self.assertIn("Additional cases selected to compensate shortfalls: 0", report)
        self.assertNotIn(" x ", report)
        jobs_path = self.output / "prepared" / "planned_assessments.jsonl"
        first_jobs = jobs_path.read_bytes()
        self.config.update(cases_per_year=None, rule_count=None)
        self._write_config()
        second = pipeline.prepare(self.config_path)
        self.assertEqual(first["experiment_sha256"], second["experiment_sha256"])
        self.assertEqual(first_jobs, jobs_path.read_bytes())
        self.assertEqual(first_report, report_path.read_bytes())
        self.assertEqual(self.importer.call_count, 1)

    def test_invalid_counts_fail_without_reinterpreting_repetitions(self) -> None:
        baseline = dict(self.config)
        for name in ("cases_per_year", "rule_count"):
            for value in (0, -2, True, False, -1.0, 1.0, "-1"):
                with self.subTest(name=name, value=value):
                    self.config = {**baseline, name: value}
                    self._write_config()
                    with self.assertRaisesRegex(ValueError, name):
                        pipeline.load_config(self.config_path)
        self.config = {**baseline, "repetitions": -1}
        self._write_config()
        with self.assertRaisesRegex(ValueError, "repetitions"):
            pipeline.load_config(self.config_path)

    def test_country_none_and_null_are_equivalent(self) -> None:
        del self.config["country"]
        self._write_config()
        default_config, _ = pipeline.load_config(self.config_path)
        self.assertEqual(default_config["country"], "EN")
        self.config["country"] = "None"
        self._write_config()
        config, _ = pipeline.load_config(self.config_path)
        self.assertIsNone(config["country"])
        self.config["country"] = None
        self._write_config()
        null_config, _ = pipeline.load_config(self.config_path)
        self.assertEqual(config, null_config)
        for value in ("", "  ", True, 1, [], {}):
            with self.subTest(value=value):
                self.config["country"] = value
                self._write_config()
                with self.assertRaisesRegex(ValueError, "country"):
                    pipeline.load_config(self.config_path)

    def test_country_filters_whole_cases_before_shared_pair_selection(self) -> None:
        self._write_csv(self.root / "events.csv", [
            {"CaseID": "C-1/20", "Activity": "A", "Timestamp": "2020-01-01", "Case_Category": "C", "Country": "IT"},
            {"CaseID": "C-1/20", "Activity": "B", "Timestamp": "2020-01-02", "Case_Category": "C", "Country": "IT"},
            {"CaseID": "C-2/20", "Activity": "B", "Timestamp": "2020-01-01", "Case_Category": "C", "Country": "DE"},
            {"CaseID": "C-2/20", "Activity": "A", "Timestamp": "2020-01-02", "Case_Category": "C", "Country": "DE"},
        ])
        self.config.update(country="IT", cases_per_year=-1, rule_count=-1)
        self.config["providers"].extend([
            {"id": "test_anthropic", "provider": "anthropic", "model": "test-pinned-2026-11-17"},
            {"id": "test_mistral", "provider": "mistral", "model": "test-pinned-2026-11-17"},
        ])
        self._write_config()
        manifest = pipeline.prepare(self.config_path)
        self.assertEqual(manifest["country_filter"], "IT")
        self.assertEqual(manifest["eligible_cases"], 1)
        self.assertEqual(manifest["sampled_cases"], 1)
        self.assertEqual(manifest["selected_pairs"], 2)
        self.assertEqual(manifest["planned_assessments"], 6)
        cases = self.importer.call_args.args[1]
        self.assertEqual([case["case_id"] for case in cases], ["C-1/20"])
        self.assertEqual(cases[0]["event_count"], 2)
        self.assertEqual(self.importer.call_args.args[3], (self.root / "events.csv").resolve())
        jobs = pipeline.read_jsonl(self.output / "prepared" / "planned_assessments.jsonl")
        self.assertEqual({job["case_id"] for job in jobs}, {"C-1/20"})
        self.assertEqual(len({job["provider_id"] for job in jobs}), 3)

    def test_decl_and_textual_catalogue_mismatch_stops_preparation(self) -> None:
        self.config['rules_decl'] = 'current.decl'
        self._write_config()
        self._write_csv(self.root / 'rules.csv', [
            {'ID': 'R01', 'DECLARE': 'Response("A", "B")', 'source': 'Article 1', 'requirement': 'A is followed by B.'},
            {'ID': 'R02', 'DECLARE': 'AtLeastOne("B")', 'source': 'Article 2', 'requirement': 'B occurs.'},
        ])
        (self.root / 'current.decl').write_text('ID\tDECLARE\nR01\tResponse("A", "B")\nR02\tAtLeastOne("A")\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'disagree on R02'):
            pipeline.prepare(self.config_path)
        self.importer.assert_not_called()

    def test_explicit_retry_retains_schema_and_api_failures_separately(self) -> None:
        pipeline.prepare(self.config_path)
        responses = [self._response("undetermined"), self._response("unrecognised_label"),
                     self._response(status="api_error"), self._response()]
        with patch.object(pipeline, "send_request", side_effect=responses) as send:
            first = self._run()
        self.assertEqual(send.call_count, 4)
        self.assertEqual(first["failed"], 2)
        summary = pipeline.analyse(self.config_path)["model_summary"][0]
        self.assertEqual(summary["n_undetermined"], 1)
        self.assertEqual(summary["n_invalid_outputs"], 1)
        self.assertEqual(summary["n_errors"], 1)
        self.assertEqual(summary["n_valid_outputs"], 2)

        with patch.object(pipeline, "send_request", return_value=self._response()) as send:
            untouched = self._run()
            self.assertEqual(send.call_count, 0)
            self.assertEqual(untouched["skipped_existing"], 4)
            retried = self._run(retry_failed=True)
        self.assertEqual(send.call_count, 2)
        self.assertEqual(retried["successful"], 2)
        self.assertEqual(retried["skipped_existing"], 2)
        attempts = [json.loads(path.read_text(encoding="utf-8"))
                    for path in (self.output / "requests").glob("*/attempt_*/assessment.json")]
        self.assertEqual(len(attempts), 6)
        self.assertEqual(sum(item["status"] == "invalid_output" for item in attempts), 1)
        self.assertEqual(sum(item["status"] == "api_error" for item in attempts), 1)
        updated = pipeline.analyse(self.config_path)["model_summary"][0]
        self.assertEqual(updated["n_valid_outputs"], 4)
        self.assertEqual(updated["n_undetermined"], 1)

    def test_changed_configuration_cannot_resume_a_prepared_experiment(self) -> None:
        pipeline.prepare(self.config_path)
        self.config["max_output_tokens"] = 8000
        self._write_config()
        with patch.object(pipeline, "send_request") as send:
            with self.assertRaisesRegex(ValueError, "configuration fields: max_output_tokens") as raised:
                self._run()
        send.assert_not_called()
        self.assertIn(str(display_path(self.output)), str(raised.exception))
        self.assertIn("Set output_dir", str(raised.exception))
        with self.assertRaisesRegex(ValueError, "Configuration, inputs or code differ"):
            pipeline.analyse(self.config_path)

    def test_changed_input_is_identified_without_overwriting_preparation(self) -> None:
        pipeline.prepare(self.config_path)
        manifest_path = self.output / "run_manifest.json"
        original = manifest_path.read_bytes()
        events_path = self.root / "events.csv"
        events_path.write_bytes(events_path.read_bytes() + b"\n")
        with patch.object(pipeline, "send_request") as send:
            with self.assertRaisesRegex(ValueError, "input files: event_log_csv"):
                self._run()
        send.assert_not_called()
        self.assertEqual(manifest_path.read_bytes(), original)

    def test_changed_code_is_identified_without_overwriting_preparation(self) -> None:
        pipeline.prepare(self.config_path)
        manifest_path = self.output / "run_manifest.json"
        original = manifest_path.read_bytes()
        with patch.object(pipeline, "code_digest", return_value="changed-code"):
            with self.assertRaisesRegex(ValueError, "Changed: executable code"):
                pipeline.prepare(self.config_path)
        self.assertEqual(manifest_path.read_bytes(), original)

    def test_no_text_failure_remains_distinct_from_legal_uncertainty(self) -> None:
        pipeline.prepare(self.config_path)
        response = self._response(status="error")
        response.update(text="", http_status=200,
                        error={"kind": "empty_output", "code": "no_text", "detail": "No final text was returned"})
        with patch.object(pipeline, "send_request", return_value=response):
            self._run(max_requests=1)
        summary = pipeline.analyse(self.config_path)["model_summary"][0]
        self.assertEqual(summary["n_no_text"], 1)
        self.assertEqual(summary["n_undetermined"], 0)
        self.assertEqual(summary["n_valid_outputs"], 0)
        self.assertEqual(summary["n_pending_or_not_run"], 3)

    def test_changed_prepared_pairs_cannot_be_transmitted(self) -> None:
        pipeline.prepare(self.config_path)
        pairs_path = self.output / "prepared" / "pairs.jsonl"
        pairs_path.write_text(pairs_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        with patch.object(pipeline, "send_request") as send:
            with self.assertRaisesRegex(ValueError, "Prepared evidence or plan changed"):
                self._run()
        send.assert_not_called()

    def test_formal_results_and_document_inventory_do_not_enter_requests(self) -> None:
        pipeline.prepare(self.config_path)
        with patch.object(pipeline, "send_request", return_value=self._response()) as send:
            self._run()
        self.assertEqual(send.call_count, 4)
        for call in send.call_args_list:
            request = call.args[0]
            payload = json.dumps(request["payload"])
            for forbidden in ("formal_satisfied", "formal_outcome",
                              "activation_status", "base_rule_id", "source_review_id",
                              "document_files", "document_file_count", "Synthetic case document.",
                              "C-1_20_01_01.txt", "C-2_20_01_01.txt"):
                self.assertNotIn(forbidden, payload)
            self.assertIn("EVIDENCE DATA BEGIN", payload)
            self.assertNotIn("Article", payload)
            self.assertNotIn("legal_source", payload)
            self.assertIn("REQUIREMENT", payload)
            self.assertIn("DECLARE", payload)
            self.assertTrue("Response[A, B]" in payload or "AtLeastOne[B]" in payload)
            self.assertIn("E0001", payload)

    def test_ignored_source_column_does_not_change_runtime_rules_but_input_bytes_remain_frozen(self) -> None:
        rules_path = self.root / "rules.csv"
        original = pipeline.load_catalogue(rules_path)
        pipeline.prepare(self.config_path)
        with rules_path.open(encoding="utf-8", newline="") as stream:
            rows = list(csv.DictReader(stream, delimiter=";"))
        for row in rows:
            row["source"] = "[V9999]\nSOURCE_ONLY_SENTINEL: incompatible tags are ignored."
        self._write_csv(rules_path, rows)
        self.assertEqual(pipeline.load_catalogue(rules_path), original)
        with patch.object(pipeline, "send_request") as send:
            with self.assertRaisesRegex(ValueError, "rules_csv"):
                pipeline.prepare(self.config_path)
        send.assert_not_called()

    def test_interrupted_attempt_remains_unknown_until_explicit_retry(self) -> None:
        pipeline.prepare(self.config_path)
        jobs = pipeline.read_jsonl(self.output / "prepared" / "planned_assessments.jsonl")
        pairs = {pair["pair_id"]: pair for pair in pipeline.read_jsonl(self.output / "prepared" / "pairs.jsonl")}
        job = jobs[0]
        system, user, _ = pipeline.build_prompts(pairs[job["pair_id"]])
        request = pipeline.build_request(self.config["providers"][0], system, user, 16000)
        directory = self.output / "requests" / job["job_id"] / "attempt_001"
        pipeline.write_json(directory / "request.json", request)

        initial = pipeline.analyse(self.config_path)["model_summary"][0]
        self.assertEqual(initial["n_call_attempt_flags_available"], 3)
        self.assertIsNone(initial["n_calls_attempted"])
        self.assertEqual(initial["n_other_failed_outputs"], 1)
        with patch.object(pipeline, "send_request", return_value=self._response()) as send:
            resumed = self._run()
            self.assertEqual(send.call_count, 3)
            self.assertEqual(resumed["skipped_existing"], 1)
            retried = self._run(retry_failed=True)
        self.assertEqual(send.call_count, 4)
        self.assertEqual(retried["processed"], 1)
        self.assertTrue((directory.parent / "attempt_002" / "assessment.json").is_file())
        self.assertFalse((directory / "assessment.json").exists())
        final = pipeline.analyse(self.config_path)["model_summary"][0]
        self.assertEqual(final["n_valid_outputs"], 4)

    def test_changed_response_or_missing_request_archive_blocks_analysis(self) -> None:
        pipeline.prepare(self.config_path)
        with patch.object(pipeline, "send_request", return_value=self._response()):
            self._run(max_requests=1)
        response_path = next((self.output / "requests").glob("*/attempt_001/response.json"))
        original = response_path.read_bytes()
        response_path.write_bytes(original + b"\n")
        with self.assertRaisesRegex(ValueError, "provider response changed or is missing"):
            pipeline.analyse(self.config_path)
        response_path.write_bytes(original)
        (response_path.parent / "request.json").unlink()
        with self.assertRaisesRegex(ValueError, "assessment has no archived request"):
            pipeline.analyse(self.config_path)

    def test_combined_conditions_keep_frozen_passages_out_of_direct_prompts(self) -> None:
        self.config.update(conditions=["A", "B"], evidence_jsonl="evidence.jsonl", rule_count=None)
        self._write_config()
        evidence = [
            {"case_id": case_id, "variant_id": variant_id, "passages": [{
                "passage_id": "D1", "source_reference": "Frozen case document, paragraph 1",
                "text": "Frozen passage sentinel: the case document records B.",
            }]}
            for case_id in ("C-1/20", "C-2/20")
            for variant_id in ("R01", "R02__V0002", "R02__V0003")
        ]
        pipeline.write_jsonl(self.root / "evidence.jsonl", evidence)
        manifest = pipeline.prepare(self.config_path)
        self.assertEqual(manifest["planned_assessments"], 8)
        example = (self.output / "prepared" / "example_prompt.txt").read_text(encoding="utf-8")
        self.assertNotIn("Frozen passage sentinel", example)
        with patch.object(pipeline, "send_request", return_value=self._response()) as send:
            self._run()
        self.assertEqual(send.call_count, 8)
        payloads = [json.dumps(call.args[0]["payload"]) for call in send.call_args_list]
        self.assertEqual(sum("Frozen passage sentinel" in payload for payload in payloads), 4)


if __name__ == "__main__":
    unittest.main()
