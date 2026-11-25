"""Offline tests: no real keys, API calls or synthetic legislation in the study data."""

import contextlib
import csv
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from curia_gpai.cli import main
from curia_gpai.core import (PROJECT_ROOT, CONTENT_FIELDS, collect_records, create_plan,
                            make_slots, parse_output, read_json, run_plan, sha256, verify_plan, write_json)
from curia_gpai.review import BASE_FIELDS, DISPLAY_FIELDS, RATERS, REVIEW_FILES, export_reviews, summarise


def proposal_output(slots):
    return json.dumps({"rules": [
        {"slot_id": s["slot_id"], "source_reference": "Synthetic fixture, section 1",
         "source_quote": "Synthetic test-only passage; not a legal source.",
         "requirement": "An example activity occurs.", "declare": 'AtLeastOne("Example activity")'}
        for s in slots
    ]})


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        config = read_json(PROJECT_ROOT / "config.json")
        source = ("SYNTHETIC TEST FIXTURE. NOT NORMATIVE MATERIAL.\n" * 80).encode()
        (self.root / "source.txt").write_bytes(source)
        write_json(self.root / "source_manifest.json", {"text_sha256": sha256(source), "test_fixture": True})
        config["source_file"] = "source.txt"
        config["source_manifest"] = "source_manifest.json"
        (self.root / "event_log.csv").write_text("CaseID;Activity\nfixture-1;Example activity\n", encoding="utf-8-sig")
        config["event_log_file"] = "event_log.csv"
        config["activity_column"] = "Activity"
        for key in ("system_prompt_file", "task_prompt_file", "templates_file"):
            config[key] = str(PROJECT_ROOT / config[key])
        write_json(self.root / "config.json", config)
        self.experiment = self.root / "study"
        self.plan = create_plan(self.root / "config.json", self.experiment, repetitions=2)
        self.env = self.root / "empty.env"
        self.env.write_text(
            "OPENAI_API_KEY=dotenv-openai-test-only\n"
            "ANTHROPIC_API_KEY=dotenv-anthropic-test-only\n"
            "MISTRAL_API_KEY=dotenv-mistral-test-only\n",
            encoding="utf-8",
        )
        self.keys = patch.dict(os.environ, {"OPENAI_API_KEY": "offline-test-only",
                                          "ANTHROPIC_API_KEY": "offline-test-only", "MISTRAL_API_KEY": "offline-test-only"})
        self.keys.start()
        self.addCleanup(self.keys.stop)

    def response(self, request, timeout):
        text = proposal_output(self.plan["slots"])
        usage = (
            {"prompt_tokens": 25, "completion_tokens": 75, "total_tokens": 100}
            if request["provider"] == "mistral"
            else {"input_tokens": 25, "output_tokens": 75, "total_tokens": 100}
        )
        return {"status": "ok", "text": text, "raw_response": {"test_fixture": True, "text": text},
                "model_returned": request["payload"]["model"], "attempted": True,
                "usage": usage,
                "elapsed_seconds": 0.001, "request_id": "test-only"}

    def execute(self, side_effect=None, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()), patch("curia_gpai.core.call_model", side_effect=side_effect or self.response) as mocked:
            result = run_plan(self.experiment, self.env, **kwargs)
        return result, mocked.call_count

    def fill_sheets(self, reviews, vote="YES"):
        for rater, criteria in RATERS.items():
            path = reviews / REVIEW_FILES[rater]
            with path.open(encoding="utf-8-sig", newline="") as stream:
                reader = csv.DictReader(stream, delimiter=";")
                fields, rows = reader.fieldnames, list(reader)
            for row in rows:
                for criterion in criteria:
                    row[criterion] = vote
            with path.open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=fields, delimiter=";")
                writer.writeheader()
                writer.writerows(rows)

    def test_full_pipeline_and_resume_without_duplicates(self):
        results, count = self.execute()
        self.assertEqual(count, 6)
        self.assertEqual(len(results), 6)
        with (self.experiment / "metrics.csv").open(encoding="utf-8-sig", newline="") as stream:
            metrics = list(csv.DictReader(stream, delimiter=";"))
        self.assertEqual(len(metrics), 6)
        self.assertEqual({row["job_id"] for row in metrics}, {job["job_id"] for job in self.plan["jobs"]})
        self.assertTrue(all(row["input_tokens"] == "25" for row in metrics))
        self.assertTrue(all(row["output_tokens"] == "75" for row in metrics))
        self.assertTrue(all(row["total_tokens"] == "100" for row in metrics))
        self.assertTrue(all(row["elapsed_seconds"] == "0.001" for row in metrics))
        self.assertTrue(all(row["slot_count"] == "12" for row in metrics))
        self.assertTrue(all(row["proposal_count"] == "12" for row in metrics))
        self.assertTrue(all(row["complete_proposal_count"] == "12" for row in metrics))
        self.assertTrue(all(row["partial_proposal_count"] == "0" for row in metrics))
        self.assertTrue(all(row["abstention_count"] == "0" for row in metrics))
        self.assertTrue(all(row["other_slot_count"] == "0" for row in metrics))
        self.assertEqual(self.execute()[1], 0)
        _, records, statuses = collect_records(self.experiment)
        self.assertEqual(len(records), 72)
        self.assertTrue(all(r["reviewable"] for r in records))
        # Some test formulas deliberately use the wrong family. The runner must
        # preserve them and leave the assessment to the technical reviewer.
        reviews = self.experiment / "reviews"
        manifest = export_reviews(self.experiment, reviews)
        self.assertEqual(manifest["n_reviewable_proposals"], 72)
        self.assertEqual(set(manifest["sheet_files"].values()), {
            "domain_expert_law_1.csv", "domain_expert_law_2.csv", "domain_expert_cscience.csv"
        })
        self.assertEqual({path.name for path in reviews.glob("*.csv")}, set(REVIEW_FILES.values()))
        with (reviews / REVIEW_FILES["lawyer_1"]).open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream, delimiter=";")
            self.assertEqual(set(reader.fieldnames), set(DISPLAY_FIELDS + RATERS["lawyer_1"] + ["optional_notes"]))
            rows = list(reader)
            self.assertEqual({row["model"] for row in rows}, {job["model"] for job in self.plan["jobs"]})
            self.assertEqual({int(row["run"]) for row in rows}, {1, 2})
        baseline = read_json(reviews / "coordinator/review_baseline.json")
        self.assertTrue(all(row["model"] for row in baseline))
        self.assertEqual({row["run"] for row in baseline}, {1, 2})
        # Blank cells are not negative judgements.
        unfilled = summarise(self.experiment, reviews, self.experiment / "summary_empty")
        self.assertEqual(unfilled["complete_reviews"], 0)
        self.assertIsNone(unfilled["per_model"][0]["accepted_rate"])
        self.fill_sheets(reviews)
        summary = summarise(self.experiment, reviews, self.experiment / "summary_filled")
        self.assertEqual(summary["complete_reviews"], 72)
        self.assertTrue(all(m["accepted_rate"] == 1 for m in summary["per_model"]))
        self.assertTrue(all(m["agreement_rate"] == 1 for m in summary["legal_agreement"]))
        self.assertTrue(all(m["cohen_kappa"] is None for m in summary["legal_agreement"]))

    def test_dotenv_overrides_process_api_keys(self):
        def verify_key(request, timeout):
            self.assertEqual(os.environ["OPENAI_API_KEY"], "dotenv-openai-test-only")
            return self.response(request, timeout)

        with contextlib.redirect_stdout(io.StringIO()), patch(
            "curia_gpai.core.call_model", side_effect=verify_key
        ) as mocked:
            run_plan(self.experiment, self.env, selected_job="openai__r01")
        self.assertEqual(mocked.call_count, 1)

    def test_missing_dotenv_key_does_not_fall_back_to_process_environment(self):
        self.env.write_text("", encoding="utf-8")
        with patch("curia_gpai.core.call_model") as mocked:
            with self.assertRaisesRegex(ValueError, "Missing OPENAI_API_KEY"):
                run_plan(self.experiment, self.env, selected_job="openai__r01")
            mocked.assert_not_called()

    def test_metrics_track_abstentions_per_run(self):
        response = self.response(
            read_json(self.experiment / "frozen/requests/openai.json"), 300
        )
        document = json.loads(response["text"])
        for item in document["rules"][:4]:
            for field in CONTENT_FIELDS:
                item[field] = None
        document["rules"][4]["declare"] = None
        response["text"] = json.dumps(document)
        with contextlib.redirect_stdout(io.StringIO()), patch(
            "curia_gpai.core.call_model", return_value=response
        ):
            run_plan(self.experiment, self.env, selected_job="openai__r01")
        with (self.experiment / "metrics.csv").open(encoding="utf-8-sig", newline="") as stream:
            metrics = list(csv.DictReader(stream, delimiter=";"))
        row = next(item for item in metrics if item["job_id"] == "openai__r01")
        self.assertEqual(row["slot_count"], "12")
        self.assertEqual(row["proposal_count"], "8")
        self.assertEqual(row["complete_proposal_count"], "7")
        self.assertEqual(row["partial_proposal_count"], "1")
        self.assertEqual(row["abstention_count"], "4")
        self.assertEqual(row["other_slot_count"], "0")
        self.assertEqual(row["abstention_rate"], "0.333333")

    def test_three_repetitions_freeze_nine_jobs(self):
        plan = create_plan(self.root / "config.json", self.root / "three", repetitions=3)
        self.assertEqual(len(plan["jobs"]), 9)
        self.assertEqual(len(plan["slots"]), 12)
        self.assertNotIn("repetition", read_json(self.experiment / "frozen/requests/openai.json")["payload"])

    def test_vocabulary_is_frozen_and_case_rows_are_not_sent(self):
        vocabulary = read_json(self.experiment / "frozen/activity_vocabulary.json")
        self.assertEqual(vocabulary["activities"], ["Example activity"])
        self.assertEqual(vocabulary["row_count"], 1)
        self.assertEqual(vocabulary["source_sha256"], sha256((self.root / "event_log.csv").read_bytes()))
        prompt = (self.experiment / "frozen/user_prompt.txt").read_text(encoding="utf-8")
        self.assertIn('"Example activity"', prompt)
        self.assertNotIn("fixture-1", prompt)
        (self.root / "event_log.csv").write_text("CaseID;Activity\nfixture-2;Changed activity\n", encoding="utf-8")
        verify_plan(self.experiment)
        self.assertEqual(read_json(self.experiment / "frozen/activity_vocabulary.json")["activities"], ["Example activity"])

    def test_invalid_activity_cannot_be_accepted_despite_all_positive_human_votes(self):
        def response_with_unknown_activity(request, timeout):
            result = self.response(request, timeout)
            result["text"] = result["text"].replace("Example activity", "Unobserved activity")
            return result
        self.execute(side_effect=response_with_unknown_activity)
        _, records, _ = collect_records(self.experiment)
        self.assertTrue(all(record["reviewable"] for record in records))
        self.assertTrue(all(record["activity_vocabulary_valid"] is False for record in records))
        self.assertTrue(all("Unobserved activity" in record["declare"] for record in records))
        reviews = self.experiment / "reviews"
        export_reviews(self.experiment, reviews)
        self.fill_sheets(reviews)
        summary = summarise(self.experiment, reviews, self.experiment / "summary")
        self.assertEqual(summary["complete_reviews"], 72)
        self.assertTrue(all(model["accepted_rate"] == 0 for model in summary["per_model"]))
        with (self.experiment / "summary/ratings_joined.csv").open(encoding="utf-8-sig", newline="") as stream:
            joined = list(csv.DictReader(stream, delimiter=";"))
        self.assertTrue(all(row["human_accepted_without_changes"] == "1" for row in joined))
        self.assertTrue(all(row["accepted_without_changes"] == "0" for row in joined))

    def test_archived_api_error_can_be_diagnosed_without_calls_or_writes(self):
        def archived_error(request, timeout):
            return {"status": "error", "text": "", "http_status": 404, "request_id": "request-test",
                    "error": {"kind": "http_error", "detail": "HTTP 404; no automatic retry"},
                    "raw_response": {"error": {"message": "Requested test model is not available.", "type": "invalid_request_error", "code": "model_not_found", "param": "model"}}}
        self.execute(side_effect=archived_error, selected_job="openai__r01")
        before = {str(path.relative_to(self.experiment)): sha256(path.read_bytes()) for path in self.experiment.rglob("*") if path.is_file()}
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch("curia_gpai.core.call_model") as call:
            exit_code = main(["diagnose", "--experiment", str(self.experiment), "--job", "openai__r01"])
        after = {str(path.relative_to(self.experiment)): sha256(path.read_bytes()) for path in self.experiment.rglob("*") if path.is_file()}
        self.assertEqual(exit_code, 0)
        self.assertIn("model_not_found", output.getvalue())
        self.assertIn("Requested test model is not available.", output.getvalue())
        self.assertEqual(before, after)
        call.assert_not_called()

    def test_legacy_review_data_keep_their_original_record_shape(self):
        # This synthetic fixture represents an older immutable plan. Production
        # plans must never be migrated or edited in place.
        plan = read_json(self.experiment / "plan.json")
        plan["schema_version"] = 1
        plan.pop("activity_vocabulary_file")
        plan.pop("activity_count")
        plan["frozen_sha256"].pop("frozen/activity_vocabulary.json")
        (self.experiment / "frozen/activity_vocabulary.json").unlink()
        write_json(self.experiment / "plan.json", plan)
        (self.experiment / "plan.sha256").write_text(sha256((self.experiment / "plan.json").read_bytes()) + "\n", encoding="ascii")
        self.execute()
        _, records, statuses = collect_records(self.experiment)
        self.assertTrue(all("activity_vocabulary_valid" not in row for row in records))
        self.assertTrue(all("error_detail" not in row for row in statuses))
        reviews = self.experiment / "legacy_reviews"
        export_reviews(self.experiment, reviews)
        self.fill_sheets(reviews)
        # Old code hashes prevent new calls but do not prevent read-only review.
        plan["code_sha256"]["curia_gpai/core.py"] = "0" * 64
        write_json(self.experiment / "plan.json", plan)
        (self.experiment / "plan.sha256").write_text(sha256((self.experiment / "plan.json").read_bytes()) + "\n", encoding="ascii")
        with self.assertRaisesRegex(ValueError, "code has changed"):
            self.execute()
        summary = summarise(self.experiment, reviews, self.experiment / "legacy_summary")
        self.assertEqual(summary["complete_reviews"], 72)
        self.assertTrue(all(model["accepted_rate"] == 1 for model in summary["per_model"]))

    def test_frozen_source_and_config_cannot_change(self):
        (self.experiment / "frozen/source.txt").write_text("modified", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Frozen file"):
            self.execute()

    def test_original_source_is_frozen_with_the_text(self):
        original = b"Synthetic original source for a unit test only."
        (self.root / "original.txt").write_bytes(original)
        manifest = read_json(self.root / "source_manifest.json")
        manifest.update({"input_file": "original.txt", "input_sha256": sha256(original)})
        write_json(self.root / "source_manifest.json", manifest)
        destination = self.root / "with_original"
        plan = create_plan(self.root / "config.json", destination, repetitions=2)
        (self.root / "original.txt").write_bytes(b"new source")
        self.assertEqual((destination / plan["original_source_snapshot"]).read_bytes(), original)
        verify_plan(destination)

    def test_original_outputs_cannot_change_unnoticed(self):
        self.execute()
        path = self.experiment / "runs/openai__r01/attempt_001/output.txt"
        path.write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Original output modified"):
            collect_records(self.experiment)

    def test_annotation_text_cannot_be_edited_as_if_original(self):
        self.execute()
        reviews = self.experiment / "reviews"
        export_reviews(self.experiment, reviews)
        path = reviews / REVIEW_FILES["lawyer_1"]
        path.write_text(path.read_text(encoding="utf-8-sig").replace("An example activity occurs.", "A corrected requirement.", 1), encoding="utf-8-sig")
        with self.assertRaisesRegex(ValueError, "Original text modified"):
            summarise(self.experiment, reviews, self.experiment / "summary")

    def test_failure_is_preserved_and_retry_is_explicit(self):
        def fail(request, timeout):
            return {"status": "error", "text": "", "attempted": True, "error": {"kind": "timeout_test"}}
        self.assertEqual(self.execute(side_effect=fail, selected_job="openai__r01")[1], 1)
        self.assertEqual(self.execute(selected_job="openai__r01")[1], 0)
        self.assertEqual(self.execute(selected_job="openai__r01", retry_errors=True)[1], 1)
        first = read_json(self.experiment / "runs/openai__r01/attempt_001/result.json")
        second = read_json(self.experiment / "runs/openai__r01/attempt_002/result.json")
        self.assertEqual(first["status"], "error")
        self.assertEqual(second["status"], "ok")
        with (self.experiment / "metrics.csv").open(encoding="utf-8-sig", newline="") as stream:
            metrics = list(csv.DictReader(stream, delimiter=";"))
        attempts = [row for row in metrics if row["job_id"] == "openai__r01"]
        self.assertEqual([row["attempt"] for row in attempts], ["1", "2"])
        self.assertEqual([row["status"] for row in attempts], ["error", "ok"])
        self.assertEqual(self.execute(selected_job="openai__r01", retry_errors=True)[1], 0)

    def test_refusal_is_not_retried_as_transport_failure(self):
        def refuse(request, timeout):
            return {"status": "refusal", "text": "", "attempted": True}
        self.execute(side_effect=refuse, selected_job="openai__r01")
        self.assertEqual(self.execute(selected_job="openai__r01", retry_errors=True)[1], 0)


class OutputTests(unittest.TestCase):
    def setUp(self):
        self.slots = make_slots({"existence": 1, "relation": 1, "mutual": 0, "negative": 0})

    def test_abstentions_and_missing_are_distinct(self):
        text = json.dumps({"rules": [{"slot_id": "E1", **{k: None for k in CONTENT_FIELDS}}]})
        result = parse_output(text, self.slots)
        self.assertEqual([r["state"] for r in result["rows"]], ["abstained", "missing"])

    def test_invalid_declare_is_preserved_for_human(self):
        text = json.loads(proposal_output(self.slots))
        text["rules"][0]["declare"] = "WRONG SYNTAX AND SEMANTICS"
        result = parse_output(json.dumps(text), self.slots)
        self.assertEqual(result["rows"][0]["declare"], "WRONG SYNTAX AND SEMANTICS")
        self.assertEqual(result["rows"][0]["state"], "proposed")

    def test_duplicate_slots_are_not_silently_deduplicated(self):
        text = json.loads(proposal_output(self.slots))
        text["rules"].append(text["rules"][0].copy())
        result = parse_output(json.dumps(text), self.slots)
        self.assertEqual(result["rows"][0]["state"], "duplicate_slot")
        self.assertEqual(len(result["rows"][0]["original_items"]), 2)

    def test_fences_are_removed_without_repair(self):
        text = proposal_output(self.slots)
        self.assertEqual(parse_output("```json\n" + text + "\n```", self.slots)["parse_status"], "ok")
        self.assertEqual(parse_output(text[:-2], self.slots)["parse_status"], "invalid_json")


if __name__ == "__main__":
    unittest.main()
