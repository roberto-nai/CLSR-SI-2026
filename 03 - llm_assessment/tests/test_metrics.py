"""Check provider token semantics and complete-attempt resource accounting."""

from __future__ import annotations

import copy
import unittest

from curia_assessment.metrics import TOKEN_FIELDS, normalise_usage, summarise_resource_usage


def job(provider="openai", *, model="test-model", condition="A", run=1):
    return {"provider": provider, "model": model, "condition": condition, "run": run}


class UsageNormalisationTests(unittest.TestCase):
    def test_openai_cache_and_reasoning_are_details_not_additional_tokens(self):
        usage = {
            "input_tokens": 100, "output_tokens": 60, "total_tokens": 160,
            "input_tokens_details": {"cached_tokens": 40, "cache_write_tokens": 20,
                                     "cache_write_tokens_details": {"ttl_12h_tokens": 20}},
            "output_tokens_details": {"reasoning_tokens": 50},
        }
        original = copy.deepcopy(usage)
        self.assertEqual(normalise_usage("openai", usage), {
            "input_tokens": 100, "output_tokens": 60, "total_tokens": 160,
            "cached_input_tokens": 40, "cache_creation_input_tokens": 20, "reasoning_tokens": 50,
        })
        self.assertEqual(usage, original)

    def test_anthropic_adds_distinct_input_buckets_once(self):
        usage = {
            "input_tokens": 100, "cache_creation_input_tokens": 30, "cache_read_input_tokens": 20,
            "cache_creation": {"ephemeral_5m_input_tokens": 30, "ephemeral_1h_input_tokens": 0},
            "output_tokens": 40, "output_tokens_details": {"thinking_tokens": 10},
        }
        self.assertEqual(normalise_usage("anthropic", usage), {
            "input_tokens": 150, "output_tokens": 40, "total_tokens": 190,
            "cached_input_tokens": 20, "cache_creation_input_tokens": 30, "reasoning_tokens": 10,
        })

    def test_mistral_cached_prompt_and_reasoning_remain_subsets(self):
        usage = {
            "prompt_tokens": 100, "completion_tokens": 30, "total_tokens": 130,
            "prompt_tokens_details": {"cached_tokens": 64},
            "completion_tokens_details": {"reasoning_tokens": 20},
        }
        self.assertEqual(normalise_usage("mistral", usage), {
            "input_tokens": 100, "output_tokens": 30, "total_tokens": 130,
            "cached_input_tokens": 64, "cache_creation_input_tokens": None, "reasoning_tokens": 20,
        })

    def test_unknown_usage_and_zero_usage_remain_distinct(self):
        for usage in (None, [], "unavailable", {}, {"unrelated": 100}):
            with self.subTest(usage=usage):
                self.assertEqual(normalise_usage("openai", usage), dict.fromkeys(TOKEN_FIELDS))
        zeros = normalise_usage("openai", {
            "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0},
        })
        self.assertEqual(zeros["total_tokens"], 0)
        self.assertEqual(zeros["cached_input_tokens"], 0)
        self.assertIsNone(zeros["cache_creation_input_tokens"])
        self.assertEqual(normalise_usage("unknown", {"input_tokens": 1}), dict.fromkeys(TOKEN_FIELDS))

    def test_invalid_token_counts_are_not_coerced_or_fabricated(self):
        for value in (True, False, -1, 1.0, float("inf"), float("nan"), "2", [], {}):
            with self.subTest(value=value):
                result = normalise_usage("openai", {
                    "input_tokens": value, "output_tokens": value, "total_tokens": value,
                    "input_tokens_details": {"cached_tokens": value, "cache_write_tokens": value},
                    "output_tokens_details": {"reasoning_tokens": value},
                })
                self.assertEqual(result, dict.fromkeys(TOKEN_FIELDS))

    def test_total_can_be_derived_only_from_full_known_input_and_output(self):
        result = normalise_usage("mistral", {"prompt_tokens": 7, "completion_tokens": 3})
        self.assertEqual(result["total_tokens"], 10)
        incomplete = normalise_usage("anthropic", {"input_tokens": 7, "output_tokens": 3})
        self.assertIsNone(incomplete["input_tokens"])
        self.assertIsNone(incomplete["total_tokens"])
        self.assertEqual(incomplete["output_tokens"], 3)
        self.assertIsNone(incomplete["cached_input_tokens"])


class ResourceSummaryTests(unittest.TestCase):
    def test_retries_and_invalid_outputs_count_with_partial_measurement_coverage(self):
        planned = [job()]
        ledger = [
            {**job(), "job_id": "same-job", "attempt": 1, "status": "invalid_output",
             "is_latest_attempt": False, "elapsed_seconds": 1.2,
             "usage": {"input_tokens": 10, "output_tokens": 4,
                       "input_tokens_details": {"cached_tokens": 0}}},
            {**job(), "job_id": "same-job", "attempt": 2, "status": "ok",
             "is_latest_attempt": True, "elapsed_seconds": 3.4,
             "usage": {"input_tokens": 20, "output_tokens": 8,
                       "input_tokens_details": {"cached_tokens": 5}}},
            {**job(), "job_id": "other-job", "attempt": 1, "status": "interrupted"},
        ]
        original = copy.deepcopy((planned, ledger))
        result = summarise_resource_usage(planned, ledger)[0]
        self.assertEqual(result["n_attempts"], 3)
        self.assertEqual(result["elapsed_seconds_total"], 4.6)
        self.assertEqual(result["elapsed_seconds_mean"], 2.3)
        self.assertEqual(result["n_attempts_with_elapsed_seconds"], 2)
        self.assertEqual(result["input_tokens_total"], 30)
        self.assertEqual(result["output_tokens_total"], 12)
        self.assertEqual(result["total_tokens_total"], 42)
        self.assertEqual(result["n_attempts_with_total_tokens"], 2)
        self.assertEqual(result["cached_input_tokens_total"], 5)
        self.assertIsNone(result["reasoning_tokens_total"])
        self.assertEqual(result["n_attempts_with_reasoning_tokens"], 0)
        self.assertEqual((planned, ledger), original)

    def test_groups_keep_condition_repetition_and_planned_models_without_attempts(self):
        planned = [job(), job("anthropic"), job(condition="B"), job(run=2)]
        ledger = [{**job(), "elapsed_seconds": 0, "usage": {"input_tokens": 0, "output_tokens": 0}}]
        rows = summarise_resource_usage(planned, ledger)
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0]["n_attempts"], 1)
        self.assertEqual(rows[0]["elapsed_seconds_total"], 0)
        self.assertEqual(rows[0]["total_tokens_total"], 0)
        self.assertEqual(rows[0]["n_attempts_with_total_tokens"], 1)
        for row in rows[1:]:
            self.assertEqual(row["n_attempts"], 0)
            self.assertEqual(row["n_attempts_with_elapsed_seconds"], 0)
            self.assertIsNone(row["elapsed_seconds_total"])
            self.assertIsNone(row["elapsed_seconds_mean"])
            for field in TOKEN_FIELDS:
                self.assertIsNone(row[field + "_total"])
                self.assertEqual(row["n_attempts_with_" + field], 0)

    def test_elapsed_measurements_reject_invalid_values_but_keep_reported_zero(self):
        times = (None, True, False, -1, float("inf"), float("nan"), "2", 0)
        ledger = [{**job(), "elapsed_seconds": value} for value in times]
        row = summarise_resource_usage([job()], ledger)[0]
        self.assertEqual(row["n_attempts"], len(times))
        self.assertEqual(row["n_attempts_with_elapsed_seconds"], 1)
        self.assertEqual(row["elapsed_seconds_total"], 0)
        self.assertEqual(row["elapsed_seconds_mean"], 0)
        self.assertIsNone(row["input_tokens_total"])


if __name__ == "__main__":
    unittest.main()
