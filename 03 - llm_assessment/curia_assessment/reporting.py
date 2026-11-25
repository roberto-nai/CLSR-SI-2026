"""Present verified assessment summaries without changing their denominators."""

from __future__ import annotations

from datetime import datetime
from html import escape
import json
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .event_log_statistics import format_event_log_statistics


def write_case_extraction_report(
    output: Path, config: dict[str, Any], cases: list[dict[str, Any]],
    audit: list[dict[str, Any]],
    cleaning: dict[str, Any] | None = None,
) -> Path:
    """Save annual allocation, compensation and every selected case identifier."""
    first, last = config["year_start"], config["year_end"]
    count = config["cases_per_year"]
    explicit = config["case_ids"] is not None
    limited = not explicit and count is not None and count != -1
    nominal_target = (last - first + 1) * count if limited else None
    target = (config.get("total_cases") if config.get("total_cases") is not None else nominal_target) if limited else None
    base_count = sum(row["base_sampled_cases"] for row in audit)
    additional_count = sum(row["additional_cases"] for row in audit)
    lines = [
        "CASE EXTRACTION REPORT",
        "",
        f"CaseID years: {first}–{last} (inclusive)",
        f"Number of years: {last - first + 1}",
        f"Country: {config['country'] if config['country'] is not None else 'all'}",
        f"Case category: {config['case_category'] if config['case_category'] is not None else 'all'}",
        f"Minimum events per case: {config['min_events']}" + (" (no minimum)" if config["min_events"] == -1 else ""),
        f"Sampling seed: {config['sampling_seed']}",
        f"Case document directory: {config['case_texts_dir']}",
        "Document availability: at least one non-empty .txt, .html or .pdf file per selected case",
        "The year is taken from the case number; all events of selected cases are retained.",
        "",
    ]
    if config.get("baseline_phase1_dir"):
        lines.extend([
            "Phase 2 reuses the exact frozen phase-1 case identifiers, event logs and rule formulations.",
            "The annual allocation below records the original phase-1 selection; no new sampling was performed.",
            "",
        ])
    elif config.get("cohort_source_dir"):
        lines.extend([
            "This run reuses the exact frozen cohort case identifiers, event logs and rule formulations.",
            "The annual allocation below records the original selection; no new sampling was performed.",
            "",
        ])
    if limited:
        if config.get("total_cases") is not None:
            lines.extend([
                f"Annual quota reference: {last - first + 1} x {count} = {nominal_target}",
                f"Configured total target: {target} (overrides the annual quota reference)",
            ])
        lines.extend([
            (f"Target sample size: {target}" if config.get("total_cases") is not None
             else f"Target sample size: {last - first + 1} x {count} = {target}"),
            "Selection method: annual quotas, then one additional case per year from year_end to year_start;",
            "repeat until the target is reached or no eligible cases remain.",
        ])
    elif explicit:
        lines.append("Mode: explicit case_ids selection; annual quotas and shortfall compensation do not apply.")
    else:
        lines.append("Mode: all eligible cases (cases_per_year: -1 or null); no quotas or shortfall compensation.")
    lines.extend([
        f"Eligible cases within the year range: {sum(row['eligible_cases'] for row in audit)}",
        *([f"Cases excluded without documents: {cleaning['excluded_document_cases']}"]
          if cleaning is not None and "excluded_document_cases" in cleaning else []),
        f"Cases selected under annual quotas: {base_count}" if limited else f"Cases selected without quotas: {base_count}",
        f"Additional cases selected to compensate shortfalls: {additional_count}",
        f"Selected cases: {len(cases)}",
    ])
    if target is not None:
        lines.append(f"Overall shortfall against target: {max(0, target - len(cases))}")
    lines.extend([
        "",
        "Counts by year",
        "Year | Eligible | Quota | Base selected | Additional | Selected | Base shortfall",
    ])
    for row in reversed(audit):
        lines.append(" | ".join(str(row[key]) if row[key] is not None else "-" for key in (
            "year", "eligible_cases", "requested_cases", "base_sampled_cases", "additional_cases",
            "sampled_cases", "missing_cases")))
    lines.extend([
        "",
        "Base shortfall is the deficit against the initial annual quota; additional cases from other years may compensate for it.",
        "",
        "Selected CaseIDs (numbered list)",
        *[f"{number}. {case['case_id']}" for number, case in enumerate(cases, 1)],
    ])
    path = output / "case_extraction_report.txt"
    output.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path


_COLUMNS = (
    "Provider / model", "Condition", "Run", "Planned", "Valid", "Pending",
    "Failed / missing", "Agreement", "Disagreement", "Undetermined", "Comparable",
    "Conditional agreement", "Valid coverage",
)
_COMPARISON_NOTE = (
    "Agreement and Disagreement compare valid compliance or violation judgements with an available DECLARE "
    "reference. Undetermined covers valid judgements without a binary comparison, including non-applicability, "
    "legal uncertainty and a missing formal reference. These three categories partition valid outputs. "
    "Technical failures, missing outputs and pending assessments are reported separately. "
    "Original legal outcome labels remain in the detailed assessment exports."
)
_USAGE_COLUMNS = (
    "Provider / model", "Condition", "Run", "Attempts", "API time", "Mean (s)",
    "Input tokens", "Output tokens", "Total tokens",
)
_USAGE_DETAIL_COLUMNS = ("Cached input", "Cache creation", "Reasoning")
_USAGE_NOTE = (
    "API time sums measured request round trips, including network time; it is not local CPU time "
    "or total program wall time. All recorded attempts count, including failed, invalid and retried "
    "requests. Known totals show [reported/recorded attempts] when coverage is partial; n/a means "
    "the metric is unavailable. Cached-input, cache-creation and reasoning tokens are already included in the "
    "corresponding input/output totals and must not be added again."
)
_DOWNLOADS = (
    ("summary_by_model.csv", "Summary by model"),
    ("usage_by_model.csv", "API time and token usage by model"),
    ("summary_by_rule.csv", "Summary by rule"),
    ("summary_by_activation.csv", "Summary by activation"),
    ("confusion_matrix.csv", "Confusion matrix"),
    ("comparison_by_pair.csv", "Comparison by case and rule"),
    ("llm_assessments.csv", "All model assessments"),
    ("attempts.csv", "Attempt ledger"),
)


def _count(row: dict[str, Any], name: str) -> int:
    return int(row.get(name, 0))


def _failed(row: dict[str, Any]) -> int:
    return (_count(row, "n_planned_assessments") - _count(row, "n_valid_outputs")
            - _count(row, "n_pending_or_not_run"))


def _comparison_counts(row: dict[str, Any]) -> tuple[int, int, int]:
    """Partition valid outputs, including older summaries without new fields."""
    agreed = _count(row, "n_agreements")
    comparable = _count(row, "n_comparable_binary_decisions")
    disagreed = (int(row["n_disagreements"]) if row.get("n_disagreements") is not None
                else comparable - agreed)
    undetermined = (int(row["n_comparison_undetermined"]) if row.get("n_comparison_undetermined") is not None
                    else _count(row, "n_valid_outputs") - comparable)
    return agreed, disagreed, undetermined


def _percentage(value: Any) -> str:
    return "n/a" if value is None else f"{value:.1%}"


def _cells(row: dict[str, Any]) -> list[str]:
    agreed, disagreed, undetermined = _comparison_counts(row)
    comparable = _count(row, "n_comparable_binary_decisions")
    return [
        f"{row.get('provider', '')} / {row.get('model', '')}",
        str(row.get("condition", "")), str(row.get("run", "")),
        str(_count(row, "n_planned_assessments")), str(_count(row, "n_valid_outputs")),
        str(_count(row, "n_pending_or_not_run")), str(_failed(row)),
        str(agreed), str(disagreed), str(undetermined), str(comparable),
        f"{_percentage(row.get('agreement_rate'))} ({agreed}/{comparable})",
        _percentage(row.get("valid_output_coverage")),
    ]


def _duration(value: Any) -> str:
    if value is None:
        return "n/a"
    hours, remaining = divmod(round(float(value), 3), 3600)
    minutes, seconds = divmod(remaining, 60)
    return f"{int(hours)} h {int(minutes)} min {seconds:.3f} s"


def _with_coverage(row: dict[str, Any], metric: str, value: Any, rendered: str) -> str:
    if value is None:
        return "n/a"
    reported = _count(row, "n_attempts_with_" + metric)
    attempts = _count(row, "n_attempts")
    suffix = f" [{reported}/{attempts} attempts]" if reported < attempts else ""
    return rendered + suffix


def _usage_cells(row: dict[str, Any], *, details: bool = False) -> list[str]:
    elapsed = row.get("elapsed_seconds_total")
    mean = row.get("elapsed_seconds_mean")
    values = [
        f"{row.get('provider', '')} / {row.get('model', '')}",
        str(row.get("condition", "")), str(row.get("run", "")),
        str(_count(row, "n_attempts")),
        _with_coverage(row, "elapsed_seconds", elapsed, _duration(elapsed)),
        _with_coverage(row, "elapsed_seconds", mean, "n/a" if mean is None else f"{mean:.3f}"),
    ]
    metrics = ["input_tokens", "output_tokens", "total_tokens"]
    if details:
        metrics.extend(("cached_input_tokens", "cache_creation_input_tokens", "reasoning_tokens"))
    for metric in metrics:
        value = row.get(metric + "_total")
        values.append(_with_coverage(row, metric, value, "n/a" if value is None else f"{value:,}"))
    return values


def _terminal_table(headers: tuple[str, ...], rows: list[list[str]]) -> list[str]:
    table = [list(headers), *rows]
    widths = [max(len(row[index]) for row in table) for index in range(len(headers))]
    lines = [" | ".join(value.ljust(width) for value, width in zip(table[0], widths, strict=True))]
    lines.append("-+-".join("-" * width for width in widths))
    lines.extend(" | ".join(value.ljust(width) for value, width in zip(row, widths, strict=True))
                 for row in table[1:])
    return lines


def format_model_summary(rows: list[dict[str, Any]]) -> str:
    """Return a terminal table for every provider, model, condition and repetition."""
    if not rows:
        return "No model assessment summaries are available."
    lines = _terminal_table(_COLUMNS, [_cells(row) for row in rows])
    lines.append("Conditional agreement uses comparable binary decisions only; n/a means no denominator.")
    lines.append(_COMPARISON_NOTE)
    usage_rows = [row for row in rows if "n_attempts" in row]
    if usage_rows:
        lines.extend(("", "API time and token usage — all recorded attempts"))
        lines.extend(_terminal_table(_USAGE_COLUMNS, [_usage_cells(row) for row in usage_rows]))
        lines.append(_USAGE_NOTE)
    return "\n".join(lines)


def _status_chart(row: dict[str, Any]) -> str:
    total = _count(row, "n_valid_outputs")
    agreed, disagreed, undetermined = _comparison_counts(row)
    categories = (
        ("agreement", "Agreement", agreed),
        ("disagreement", "Disagreement", disagreed),
        ("undetermined", "Undetermined", undetermined),
    )
    label = f"{row.get('provider', '')} / {row.get('model', '')} · {row.get('condition', '')} · run {row.get('run', '')}"
    segments = []
    counts = []
    for css_class, name, count in categories:
        counts.append(f"{name}: {count}")
        if count and total:
            title = escape(f"{name}: {count} of {total} valid outputs", quote=True)
            segments.append(f'<span class="segment {css_class}" style="width:{100 * count / total:.4f}%" title="{title}"></span>')
    description = escape(f"Comparison among {total} valid outputs; " + "; ".join(counts), quote=True)
    return (
        '<div class="status-row"><div class="status-label">' + escape(label) + '</div>'
        + f'<div class="status-bar" role="img" aria-label="{description}">'
        + "".join(segments) + '</div><div class="status-counts">' + escape(" · ".join(counts))
        + f'</div><div class="operational-counts">Failed / missing: {_failed(row)} · '
        + f'Pending: {_count(row, "n_pending_or_not_run")}</div></div>'
    )


def write_results_report(output_dir: Path, summaries: dict[str, list[dict[str, Any]]]) -> Path:
    """Atomically write a self-contained local HTML report from analysed summaries.

    The comparison partitions valid outputs into Agreement, Disagreement and
    Undetermined. Failed/missing outputs and pending rows remain operational
    counts outside that chart and the conditional agreement denominator.
    """
    output_dir = Path(output_dir)
    rows = summaries.get("model_summary", [])
    total = sum(_count(row, "n_planned_assessments") for row in rows)
    valid = sum(_count(row, "n_valid_outputs") for row in rows)
    pending = sum(_count(row, "n_pending_or_not_run") for row in rows)
    failed = sum(_failed(row) for row in rows)
    state = "Pending assessments remain" if pending else "No pending assessments"
    if not rows:
        state = "No model assessment summaries are available"
    generated = datetime.now(ZoneInfo("Europe/Rome")).strftime("%Y-%m-%d %H:%M:%S %Z")
    headers = "".join(f'<th scope="col">{escape(name)}</th>' for name in _COLUMNS)
    body = "".join('<tr>' + "".join(f'<td>{escape(value)}</td>' for value in _cells(row)) + '</tr>'
                   for row in rows)
    if not rows:
        body = f'<tr><td colspan="{len(_COLUMNS)}">No model assessment summaries are available.</td></tr>'
    charts = "".join(_status_chart(row) for row in rows)
    usage_rows = summaries.get("usage_summary", [row for row in rows if "n_attempts" in row])
    statistics_path = output_dir / "event_log_statistics.json"
    log_section = ""
    if statistics_path.is_file():
        statistics = json.loads(statistics_path.read_text(encoding="utf-8"))
        paragraphs = "".join(f"<p>{escape(line)}</p>"
                             for line in format_event_log_statistics(statistics).splitlines() if line.strip())
        log_section = '<section><h2>Event-log characteristics</h2>' + paragraphs + '</section>'
    usage_section = ""
    if usage_rows:
        usage_headers = "".join(f'<th scope="col">{escape(name)}</th>'
                                for name in (*_USAGE_COLUMNS, *_USAGE_DETAIL_COLUMNS))
        usage_body = "".join('<tr>' + "".join(f'<td>{escape(value)}</td>'
                                             for value in _usage_cells(row, details=True)) + '</tr>'
                             for row in usage_rows)
        usage_section = (
            '<section><h2>API time and token usage</h2><div class="table-wrap"><table>'
            + '<thead><tr>' + usage_headers + '</tr></thead><tbody>' + usage_body
            + '</tbody></table></div><p class="note">' + escape(_USAGE_NOTE) + '</p></section>'
        )
    links = _DOWNLOADS
    if (output_dir / "_audit").is_dir():
        links = (
            ("model_resources.csv", "Tokens and API time by model and phase"),
            ("article_results.csv", "Article results: initial and latest attempts"),
            ("article_summary.txt", "Article-ready methods and results"),
            ("case_comparison.csv", "One row per case"),
            ("case_results.csv", "Case results by model and phase"),
            ("rule_assessments.csv", "Rule judgements and explanations"),
            ("summary_by_model.csv", "Model and phase summary"),
            ("case_extraction_report.txt", "Case extraction report"),
            ("expert_review/expert_1.csv", "Expert 1 review sheet (CSV)"),
            ("expert_review/expert_1.xlsx", "Expert 1 review sheet (Excel)"),
            ("expert_review/expert_2.csv", "Expert 2 review sheet (CSV)"),
            ("expert_review/expert_2.xlsx", "Expert 2 review sheet (Excel)"),
            ("expert_review/instructions.txt", "Expert review instructions"),
        )
        links = tuple((name, label) for name, label in links if (output_dir / name).is_file())
    links = (*links, *((name, label) for name, label in (
        ("event_log_statistics.csv", "Event-log statistics (CSV)"),
        ("event_log_statistics.json", "Event-log statistics (JSON)"),
        ("event_log_statistics.txt", "Event-log statistics and definitions"),
    ) if (output_dir / name).is_file()))
    downloads = "".join(f'<a href="{filename}">{escape(label)}</a>' for filename, label in links)
    html = f'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CURIA assessment results — {escape(output_dir.name)}</title>
<style>
:root {{ color-scheme: light; --ink:#19283a; --muted:#566578; --line:#dce4ec; --agreement:#267b72; --disagreement:#bd4b50; --uncertain:#bd7a11; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:#f3f6fa; color:var(--ink); font:15px/1.55 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
main {{ max-width:1450px; margin:0 auto; padding:42px 28px 56px; }}
.eyebrow {{ margin:0; color:var(--muted); letter-spacing:.12em; font-size:12px; font-weight:700; text-transform:uppercase; }}
h1 {{ margin:8px 0; font-size:34px; line-height:1.2; }}
h2 {{ margin:0 0 18px; font-size:20px; }}
p {{ margin:8px 0; }}
.metadata,.note,.status-counts,.operational-counts {{ color:var(--muted); }}
.state {{ display:inline-block; margin-top:14px; padding:6px 12px; border-radius:6px; background:#e9eef5; font-weight:600; }}
.cards {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:16px; margin:26px 0; }}
.card,section {{ background:white; border:1px solid var(--line); border-radius:12px; }}
.card {{ padding:18px 20px; }}
.card span {{ display:block; color:var(--muted); }}
.card strong {{ display:block; margin-top:4px; font-size:30px; font-variant-numeric:tabular-nums; }}
section {{ padding:24px; margin-top:20px; }}
.table-wrap {{ overflow-x:auto; }}
table {{ width:100%; border-collapse:collapse; text-align:left; font-size:13px; }}
th,td {{ padding:13px 11px; border-bottom:1px solid var(--line); vertical-align:top; }}
th {{ background:#f3f6fa; font-weight:650; white-space:nowrap; }}
td {{ font-variant-numeric:tabular-nums; }}
td:first-child {{ min-width:220px; overflow-wrap:anywhere; }}
.note {{ margin-top:16px; font-size:13px; }}
.status-row + .status-row {{ margin-top:22px; }}
.status-label {{ margin-bottom:6px; font-size:14px; font-weight:600; overflow-wrap:anywhere; }}
.status-bar {{ display:flex; height:20px; overflow:hidden; background:#edf1f6; border-radius:5px; }}
.segment {{ display:block; min-width:0; }}
.agreement {{ background:var(--agreement); }} .disagreement {{ background:var(--disagreement); }}
.undetermined {{ background:var(--uncertain); }}
.status-counts,.operational-counts {{ margin-top:6px; font-size:12px; }}
.downloads {{ display:flex; flex-wrap:wrap; gap:10px; }}
a {{ color:#205d96; text-underline-offset:3px; }}
.downloads a {{ padding:8px 12px; border:1px solid var(--line); border-radius:6px; text-decoration:none; }}
.downloads a:hover {{ background:#f3f6fa; }}
@media (max-width:700px) {{ main {{ padding:26px 14px; }} h1 {{ font-size:28px; }} .cards {{ grid-template-columns:repeat(2,minmax(0,1fr)); gap:10px; }} section {{ padding:18px 14px; }} }}
@media print {{ body {{ background:white; }} main {{ max-width:none; padding:0; }} .card,section {{ break-inside:avoid; }} .downloads {{ display:none; }} }}
</style>
</head>
<body><main>
<header><p class="eyebrow">CURIA · Direct assessment</p>
<h1>Assessment results</h1>
<p class="metadata">Experiment: {escape(output_dir.name)} · Generated {escape(generated)}</p>
<p class="state">{escape(state)}</p></header>
<div class="cards">
<div class="card"><span>Planned assessments</span><strong>{total:,}</strong></div>
<div class="card"><span>Valid outputs</span><strong>{valid:,}</strong></div>
<div class="card"><span>Pending / not run</span><strong>{pending:,}</strong></div>
<div class="card"><span>Failed / missing</span><strong>{failed:,}</strong></div>
</div>
{log_section}
<section><h2>Results by model</h2>
<div class="table-wrap"><table><thead><tr>{headers}</tr></thead><tbody>{body}</tbody></table></div>
<p class="note">Conditional agreement is the number of agreements divided by comparable binary decisions (numerator/denominator shown). Only valid compliance or violation decisions with a formal result enter this denominator. Valid coverage is valid outputs divided by planned assessments. An unavailable rate is shown as n/a, never as zero.</p>
</section>
<section><h2>Comparison with DECLARE</h2>{charts}
<p class="note">{escape(_COMPARISON_NOTE)}</p>
</section>
{usage_section}
<section><h2>Detailed results</h2><div class="downloads">{downloads}</div></section>
<p class="note">These results measure agreement with the formal trace checker, not legal accuracy. Comparison Undetermined is not a violation or a technical failure.</p>
</main></body></html>
'''
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / "results.html"
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.write_text(html, encoding="utf-8")
    temporary.replace(destination)
    return destination
