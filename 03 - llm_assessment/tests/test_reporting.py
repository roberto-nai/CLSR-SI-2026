"""Check presentation of uncertainty, missingness and formal-agreement rates."""

from __future__ import annotations

import tempfile
import re
import unittest
from html.parser import HTMLParser
from pathlib import Path

from curia_assessment.reporting import format_model_summary, write_results_report
from curia_assessment.comparison import compare_rows, summarise_comparisons


def summary(**changes):
    row = {
        "provider": "openai", "model": "test-model", "condition": "A", "run": 1,
        "n_planned_assessments": 10, "n_valid_outputs": 6, "n_pending_or_not_run": 2,
        "n_binary_decisions": 3, "n_undetermined": 2, "n_not_applicable": 1,
        "n_comparable_binary_decisions": 3, "n_agreements": 2,
        "agreement_rate": 2 / 3, "valid_output_coverage": 0.6,
    }
    return {**row, **changes}


def resource_summary(**changes):
    row = summary(n_planned_assessments=2, n_valid_outputs=1, n_pending_or_not_run=0,
                  n_binary_decisions=0, n_undetermined=1, n_not_applicable=0,
                  n_comparable_binary_decisions=0, n_agreements=0,
                  agreement_rate=None, valid_output_coverage=0.5)
    row.update({
        "n_attempts": 3,
        "elapsed_seconds_total": 3661.5, "elapsed_seconds_mean": 1830.75,
        "n_attempts_with_elapsed_seconds": 2,
        "input_tokens_total": 3000, "n_attempts_with_input_tokens": 3,
        "output_tokens_total": 150, "n_attempts_with_output_tokens": 2,
        "total_tokens_total": 3150, "n_attempts_with_total_tokens": 2,
        "cached_input_tokens_total": 0, "n_attempts_with_cached_input_tokens": 3,
        "cache_creation_input_tokens_total": None, "n_attempts_with_cache_creation_input_tokens": 0,
        "reasoning_tokens_total": None, "n_attempts_with_reasoning_tokens": 0,
    })
    return {**row, **changes}


class ReportParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []
        self.links = []
        self.text = []
        self.charts = []
        self.current_chart = None

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.links.extend(value for name, value in attrs if name in {"href", "src"})
        values = dict(attrs)
        if values.get("role") == "img" and "status-bar" in values.get("class", "").split():
            self.current_chart = {"label": values.get("aria-label", ""), "segments": []}
            self.charts.append(self.current_chart)
        if tag == "span" and "segment" in values.get("class", "").split() and self.current_chart is not None:
            self.current_chart["segments"].append(values)

    def handle_endtag(self, tag):
        if tag == "div":
            self.current_chart = None

    def handle_data(self, data):
        self.text.append(data)


class ReportingTests(unittest.TestCase):
    def test_terminal_resource_table_retains_all_attempts_and_partial_coverage(self):
        table = format_model_summary([resource_summary()])
        self.assertIn("API time and token usage — all recorded attempts", table)
        self.assertIn("Attempts", table)
        self.assertIn("1 h 1 min 1.500 s [2/3 attempts]", table)
        self.assertIn("1830.750 [2/3 attempts]", table)
        self.assertIn("3,000", table)
        self.assertIn("150 [2/3 attempts]", table)
        self.assertIn("3,150 [2/3 attempts]", table)
        self.assertIn("including failed, invalid and retried requests", table)
        self.assertIn("network time", table)

    def test_html_resource_metrics_keep_unknown_distinct_from_measured_zero(self):
        measured = resource_summary(
            elapsed_seconds_total=0.0, elapsed_seconds_mean=0.0,
            n_attempts_with_elapsed_seconds=3,
            input_tokens_total=0, output_tokens_total=0, total_tokens_total=0,
            n_attempts_with_output_tokens=3, n_attempts_with_total_tokens=3,
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = write_results_report(Path(temporary), {"model_summary": [measured]})
            html = path.read_text(encoding="utf-8")
        section = html.split("<h2>API time and token usage</h2>", 1)[1].split("</section>", 1)[0]
        self.assertIn("0 h 0 min 0.000 s", section)
        self.assertIn("<td>0.000</td>", section)
        self.assertIn("<td>0</td>", section)
        self.assertEqual(section.count("<td>n/a</td>"), 2)
        self.assertIn("Cache creation", section)
        self.assertIn("Reasoning", section)
        self.assertIn("must not be added again", section)
        self.assertIn('href="usage_by_model.csv"', html)

    def test_html_uses_explicit_resource_summary_and_escapes_its_labels(self):
        usage = resource_summary(model='<img src="bad">', n_attempts=4,
                                 n_attempts_with_input_tokens=3)
        with tempfile.TemporaryDirectory() as temporary:
            path = write_results_report(Path(temporary), {
                "model_summary": [summary()], "usage_summary": [usage],
            })
            html = path.read_text(encoding="utf-8")
        section = html.split("<h2>API time and token usage</h2>", 1)[1].split("</section>", 1)[0]
        self.assertIn("3,000 [3/4 attempts]", section)
        self.assertIn("&lt;img", section)
        parser = ReportParser()
        parser.feed(html)
        self.assertNotIn("img", parser.tags)

    def test_terminal_keeps_groups_and_distinguishes_unavailable_from_zero(self):
        rows = [
            summary(model="uncertain-model", n_binary_decisions=0, n_comparable_binary_decisions=0,
                    n_agreements=0, agreement_rate=None, n_undetermined=5, n_not_applicable=1),
            summary(model="zero-agreement-model", condition="B", run=2,
                    n_agreements=0, agreement_rate=0.0),
        ]
        table = format_model_summary(rows)
        self.assertIn("uncertain-model", table)
        self.assertIn("zero-agreement-model", table)
        self.assertIn("n/a (0/0)", table)
        self.assertIn("0.0% (0/3)", table)
        self.assertIn("60.0%", table)
        self.assertIn("Pending", table)
        self.assertIn("Failed / missing", table)
        self.assertIn("Undetermined", table)
        self.assertNotIn("100.0%", table)
        self.assertIn("comparable binary decisions only", table)

    def test_html_retains_partial_statuses_denominators_and_local_downloads(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "experiment"
            path = write_results_report(output, {"model_summary": [summary()]})
            self.assertEqual(path, output / "results.html")
            self.assertFalse((output / "results.html.tmp").exists())
            html = path.read_text(encoding="utf-8")
        parser = ReportParser()
        parser.feed(html)
        text = " ".join(parser.text)
        self.assertIn("Pending assessments remain", text)
        self.assertIn("66.7% (2/3)", text)
        self.assertIn("60.0%", text)
        self.assertIn("Agreement: 2", text)
        self.assertIn("Disagreement: 1", text)
        self.assertIn("Undetermined: 3", text)
        self.assertNotIn("Valid binary:", text)
        self.assertNotIn("Not applicable:", text)
        self.assertIn("Failed / missing: 2", text)
        self.assertIn("Pending: 2", text)
        self.assertIn("not legal accuracy", text)
        self.assertNotIn("script", parser.tags)
        self.assertTrue(all(not link.startswith(("http:", "https:", "//")) for link in parser.links))
        self.assertIn("comparison_by_pair.csv", parser.links)
        self.assertIn("summary_by_model.csv", parser.links)

    def chart(self, row):
        with tempfile.TemporaryDirectory() as temporary:
            path = write_results_report(Path(temporary), {"model_summary": [row]})
            html = path.read_text(encoding="utf-8")
        parser = ReportParser()
        parser.feed(html)
        self.assertEqual(len(parser.charts), 1)
        return html, parser.charts[0]

    def test_chart_has_exactly_three_categories_and_uses_valid_outputs_as_its_denominator(self):
        row = summary(n_binary_decisions=4, n_not_applicable=1, n_undetermined=1,
                      n_comparison_agreements=2, n_comparison_disagreements=1,
                      n_comparison_undetermined=3, n_missing_formal_results=1)
        html, chart = self.chart(row)
        counts = re.findall(r"(Agreement|Disagreement|Undetermined):\s*(\d+)", chart["label"])
        self.assertEqual(counts, [("Agreement", "2"), ("Disagreement", "1"), ("Undetermined", "3")])
        self.assertEqual(sum(int(value) for _, value in counts), row["n_valid_outputs"])
        self.assertNotRegex(chart["label"], r"Valid binary|Not applicable|Pending|Failed")
        self.assertEqual(len(chart["segments"]), 3)
        widths = [float(re.search(r"width:\s*([0-9.]+)%", segment["style"]).group(1))
                  for segment in chart["segments"]]
        self.assertAlmostEqual(sum(widths), 100, places=3)
        for segment, count, expected_width in zip(chart["segments"], (2, 1, 3), (100 / 3, 100 / 6, 50)):
            self.assertIn(f"{count} of 6", segment["title"])
            self.assertAlmostEqual(float(re.search(r"width:\s*([0-9.]+)%", segment["style"]).group(1)),
                                   expected_width, places=3)
        self.assertIn("66.7% (2/3)", html)
        self.assertIn("Failed / missing: 2", html)
        self.assertIn("Pending: 2", html)

    def test_old_summary_dicts_compute_comparison_undetermined_including_missing_reference(self):
        row = summary(n_binary_decisions=4, n_undetermined=1, n_not_applicable=1,
                      n_missing_formal_results=1)
        self.assertFalse(any(key.startswith("n_comparison_") for key in row))
        _, chart = self.chart(row)
        self.assertEqual(re.findall(r"(Agreement|Disagreement|Undetermined):\s*(\d+)", chart["label"]),
                         [("Agreement", "2"), ("Disagreement", "1"), ("Undetermined", "3")])

    def test_zero_valid_outputs_leave_all_comparison_counts_zero_and_operations_separate(self):
        row = summary(n_planned_assessments=4, n_valid_outputs=0, n_pending_or_not_run=2,
                      n_binary_decisions=0, n_undetermined=0, n_not_applicable=0,
                      n_comparable_binary_decisions=0, n_agreements=0, agreement_rate=None,
                      valid_output_coverage=0)
        html, chart = self.chart(row)
        self.assertEqual(re.findall(r"(Agreement|Disagreement|Undetermined):\s*(\d+)", chart["label"]),
                         [("Agreement", "0"), ("Disagreement", "0"), ("Undetermined", "0")])
        for segment in chart["segments"]:
            self.assertEqual(float(re.search(r"width:\s*([0-9.]+)%", segment["style"]).group(1)), 0)
        self.assertIn("n/a (0/0)", html)
        self.assertIn("Failed / missing: 2", html)
        self.assertIn("Pending: 2", html)
        self.assertNotIn("100.0%", html)

    def test_chart_rejects_removed_label_and_separates_missing_formal_and_pending(self):
        predictions = [{"provider": "test", "model": "test-model", "condition": "B", "run": 1,
                        "case_id": f"C-{number}/20", "base_rule_id": "R01", "variant_id": "R01",
                        "status": status, "outcome": outcome}
                       for number, (status, outcome) in enumerate([
                           ("ok", "compliance"), ("ok", "violation"), ("ok", "not_applicable"),
                           ("ok", "undetermined"), ("ok", "compliance"), ("pending", None), ("api_error", None)], 1)]
        formal = [{"case_id": row["case_id"], "base_rule_id": "R01", "formal_satisfied": 1,
                   "formal_outcome": "satisfied", "activation_status": "active", "vacuously_satisfied": False}
                  for row in predictions if row["case_id"] != "C-5/20"]
        joined = compare_rows(predictions, formal)
        row = summarise_comparisons(joined)["model_summary"][0]
        html, chart = self.chart(row)
        self.assertEqual(re.findall(r"(Agreement|Disagreement|Undetermined):\s*(\d+)", chart["label"]),
                         [("Agreement", "1"), ("Disagreement", "1"), ("Undetermined", "2")])
        self.assertIn("50.0% (1/2)", html)
        self.assertIn("Pending: 1", html)
        self.assertIn("Failed / missing: 2", html)
        self.assertNotIn("n_not_applicable", row)
        self.assertEqual(joined[2]["llm_output_status"], "invalid_outcome")
        self.assertIsNone(joined[2]["comparison_category"])
        self.assertEqual(row["n_undetermined"], 1)

    def test_html_escapes_data_and_keeps_null_rates_unavailable(self):
        attack = '<script>alert("model")</script>'
        row = summary(provider=attack, model='" onmouseover="bad', condition="<A>",
                      n_binary_decisions=0, n_comparable_binary_decisions=0,
                      n_agreements=0, agreement_rate=None)
        with tempfile.TemporaryDirectory() as temporary:
            path = write_results_report(Path(temporary) / "<experiment>", {"model_summary": [row]})
            html = path.read_text(encoding="utf-8")
        parser = ReportParser()
        parser.feed(html)
        self.assertNotIn("script", parser.tags)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("&lt;experiment&gt;", html)
        self.assertIn("&lt;A&gt;", html)
        self.assertIn("n/a (0/0)", html)
        self.assertNotIn(attack, html)

    def test_empty_summaries_show_no_invented_rate_or_completed_experiment(self):
        self.assertIn("No model assessment summaries", format_model_summary([]))
        with tempfile.TemporaryDirectory() as temporary:
            path = write_results_report(Path(temporary), {"model_summary": []})
            html = path.read_text(encoding="utf-8")
        self.assertIn("No model assessment summaries are available", html)
        self.assertNotIn("All planned assessments have recorded outcomes", html)
        self.assertNotIn("100.0%", html)


if __name__ == "__main__":
    unittest.main()
