#!/usr/bin/env python3
"""Run reproducible formal conformance, activation analysis and rule summaries."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import logging
import platform
import sys
from datetime import UTC, datetime
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pandas as pd

from curia_conformance.engine import run_conformance
from curia_conformance.io import (
    file_sha256,
    load_and_select_log,
    load_endpoint_events,
    save_clean_log,
)
from curia_conformance.model import (
    load_rules,
    primitive_model_text,
    propose_reduced_rules,
    rule_table_text,
)
from curia_conformance.reporting import diagnose_catalogue, summarise_results

ROOT = Path(__file__).resolve().parent
DATA_INPUT = ROOT / "data_input"
RESULTS = ROOT / "results"
ENDPOINT_EVENTS_CONFIG = ROOT / "endpoint_events.json"


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Analyse formal DECLARE conformance on recorded CURIA traces.",
    )
    parser.add_argument("--log", type=Path, default=DATA_INPUT / "curia_log_en_C_2016-2026.csv")
    parser.add_argument("--model", type=Path, default=DATA_INPUT / "regole_c.decl")
    parser.add_argument("--output-dir", type=Path, default=RESULTS)
    parser.add_argument("--separator", default=";", help="Input and output CSV separator (default: ';').")
    parser.add_argument("--min-events", type=int, default=2, help="Minimum recorded events per case (default: 2).")
    parser.add_argument(
        "--endopoint-events", dest="endpoint_events", nargs="?", const=None,
        default=ENDPOINT_EVENTS_CONFIG,
        type=lambda value: None if value.lower() in {"none", ""} else Path(value),
        metavar="JSON|none",
        help="Filter cases by allowed first and last activities (default: endpoint_events.json); use 'none' or omit the value to disable.",
    )
    parser.add_argument("--tie-break", choices=["source", "alphabetical"], default="source", help="Order equal timestamps by source record or the legacy alphabetical convention.")
    parser.add_argument("--deduplicate-exact", action="store_true", help="Remove exactly identical source records before counting events; repeats are retained by default.")
    parser.add_argument("--compare-tie-orders", action="store_true", help="Also compare source and alphabetical tie conventions on the same selected cases.")
    parser.add_argument("--no-figure", action="store_true", help="Do not produce the outcome chart.")
    parser.add_argument("--clean-log", type=Path, help="Clean CSV path; by default, the output directory contains <source_stem>_clean.csv.")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--clean-only", action="store_true", help="Only write retained/excluded event logs, the case register and exclusion report.")
    modes.add_argument("--propose-rules", action="store_true", default=False, help="Also write a reduced DECLARE proposal in the output directory, using explicit template implications; disabled by default.")
    parser.add_argument("--rule-ids", help="Comma-separated rule identifiers selected before the analysis; all rules are included by default.")
    return parser.parse_args()


def format_decimal(value: object) -> str:
    """Display at most three decimal places, without trailing zeros or -0."""
    formatted = f"{value:.3f}".rstrip("0").rstrip(".")
    return "0" if formatted == "-0" else formatted


def write_table(frame: pd.DataFrame, path: Path, separator: str, *, aggregate: bool = False) -> None:
    """Write tables, limiting aggregate decimals and leaving undefined values blank."""
    frame.to_csv(
        path, sep=separator, index=False, encoding="utf-8", na_rep="",
        float_format=format_decimal if aggregate else None,
    )


def print_section(title: str) -> None:
    """Separate terminal sections with a consistent, visible heading."""
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}", flush=True)


def archive_results(output_dir: Path) -> Path:
    """Archive the completed output directory in the project root."""
    directory = output_dir.resolve()
    archive_path = ROOT / "results.zip"
    archive_path.unlink(missing_ok=True)
    with ZipFile(archive_path, "w", compression=ZIP_DEFLATED) as archive:
        archive.write(directory, arcname=directory.name)
        for path in sorted(directory.rglob("*")):
            if path.resolve() != archive_path.resolve():
                archive.write(path, arcname=Path(directory.name) / path.relative_to(directory))
    print(f"Results archive: {archive_path.relative_to(ROOT)}", flush=True)
    return archive_path


def draw_outcomes(pairs: pd.DataFrame, output_dir: Path) -> None:
    """Show one share per original rule, including the direct unary evaluations."""
    logging.getLogger().setLevel(logging.WARNING)
    for name in ("matplotlib", "matplotlib.font_manager", "matplotlib.backends.backend_pdf"):
        logging.getLogger(name).setLevel(logging.WARNING)
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import PercentFormatter

    order = pairs["rule_id"].drop_duplicates().tolist()
    counts = pd.crosstab(pairs["rule_id"], pairs["formal_outcome"]).reindex(order, fill_value=0)
    kinds = ["satisfied", "vacuously_satisfied", "violated"]
    for kind in kinds:
        if kind not in counts:
            counts[kind] = 0
    shares = counts[kinds].div(counts[kinds].sum(axis=1), axis=0)
    labels = {
        "satisfied": "Satisfied without vacuity",
        "vacuously_satisfied": "Vacuously satisfied",
        "violated": "Formally violated",
    }
    colours = {"satisfied": "#237A6C", "vacuously_satisfied": "#C5CDD5", "violated": "#C96A4A"}
    with plt.rc_context({"font.family": "Times New Roman", "pdf.fonttype": 42}):
        figure, axis = plt.subplots(figsize=(9.0, max(4.0, len(order) * 0.28 + 1.8)))
        left = pd.Series(0.0, index=shares.index)
        for kind in kinds:
            axis.barh(order, shares[kind], left=left, label=labels[kind], color=colours[kind], height=0.72)
            for row, (start, share) in enumerate(zip(left, shares[kind], strict=True)):
                if share > 0:
                    # Keep small edge labels inside the full bar.
                    position = min(max(start + share / 2, 0.015), 0.985)
                    axis.text(
                        position, row, f"{share:.0%}", ha="center", va="center",
                        fontsize=7,
                        color="white" if kind == "satisfied" and share >= 0.03 else "#222222",
                    )
            left = left + shares[kind]
        axis.invert_yaxis()
        axis.set_xlim(0, 1)
        axis.xaxis.set_major_formatter(PercentFormatter(1))
        axis.set_xlabel("Share of selected cases", fontweight="bold")
        axis.set_ylabel("Original rule identifier", fontweight="bold")
        axis.set_title("Formal conformance by rule", loc="left", pad=14, fontweight="bold")
        axis.legend(loc="upper center", bbox_to_anchor=(0.5, -0.09), ncol=3, frameon=False, fontsize=8)
        axis.grid(axis="x", linewidth=0.5, alpha=0.2)
        axis.set_axisbelow(True)
        for side in ["top", "right"]:
            axis.spines[side].set_visible(False)
        figure.tight_layout()
        figure.savefig(output_dir / "formal_outcomes.png", dpi=220, bbox_inches="tight")
        figure.savefig(output_dir / "formal_outcomes.pdf", bbox_inches="tight")
        plt.close(figure)


def compare_tie_orders(
    events: pd.DataFrame,
    rules: list,
    pairs: pd.DataFrame,
    selected_convention: str,
) -> tuple[pd.DataFrame, dict]:
    """Compare two stated conventions, without claiming exhaustive order invariance."""
    other = "alphabetical" if selected_convention == "source" else "source"
    keys = ["CaseID", "Timestamp"]
    if other == "alphabetical":
        keys.append("Activity")
    keys.append("_source_row")
    reordered = events.sort_values(keys, kind="mergesort").reset_index(drop=True)
    if reordered["_source_row"].tolist() == events["_source_row"].tolist():
        alternative = pairs
    else:
        alternative, _, _ = run_conformance(reordered, rules, tie_break=other)
    selected_columns = ["CaseID", "rule_id", "formal_satisfied", "formal_outcome"]
    comparison = pairs[selected_columns].merge(
        alternative[selected_columns], on=["CaseID", "rule_id"], validate="one_to_one",
        suffixes=("_selected", "_alternative"),
    )
    changed = comparison.loc[
        comparison["formal_satisfied_selected"] != comparison["formal_satisfied_alternative"]
    ].copy()
    changed["selected_tie_convention"] = selected_convention
    changed["alternative_tie_convention"] = other
    metadata = {
        "selected_convention": selected_convention,
        "alternative_convention": other,
        "compared_pairs": len(comparison),
        "changed_formal_outcomes": len(changed),
        "cases_with_changed_outcomes": int(changed["CaseID"].nunique()),
        "interpretation": "Agreement between these two conventions does not establish invariance under every possible within-timestamp ordering.",
    }
    return changed, metadata


def main() -> int:
    args = parse_arguments()
    logging.getLogger().setLevel(logging.WARNING)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / ".gitkeep").touch(exist_ok=True)

    print("*** PROGRAM START ***")

    print_section("Input selection")
    print(f"Source log: {args.log.resolve().relative_to(ROOT, walk_up=True)}")
    if not args.clean_only:
        print(f"Rule model: {args.model.resolve().relative_to(ROOT, walk_up=True)}")
    print("Case identifiers: all values")

    print_section("Cleaning")
    print("Applied filters:")
    print(f"  Minimum recorded events per case: {args.min_events}")
    endpoint_events = load_endpoint_events(args.endpoint_events) if args.endpoint_events is not None else None
    if args.endpoint_events is None:
        print("  Endpoint activities: disabled")
    else:
        print("  Endpoint activities: enabled (allowed initial and final activities)")
        print(f"    Configuration: {args.endpoint_events.resolve().relative_to(ROOT, walk_up=True)}")
    print(f"  Exact duplicate removal before case filters: {'enabled' if args.deduplicate_exact else 'disabled'}")
    print("Loading the log and applying cleaning filters...", flush=True)
    events, case_register, input_metadata = load_and_select_log(
        args.log, separator=args.separator, min_events=args.min_events,
        tie_break=args.tie_break,
        deduplicate_exact=args.deduplicate_exact,
        endpoint_events=endpoint_events,
    )
    input_metadata["endpoint_events_configuration"] = (
        {"path": str(args.endpoint_events), "sha256": file_sha256(args.endpoint_events)}
        if args.endpoint_events is not None else None
    )
    clean_path = args.clean_log or args.output_dir / f"{args.log.stem}_clean.csv"
    cleaning_metadata = save_clean_log(
        args.log, clean_path, events, case_register, input_metadata,
        args.output_dir / "case_exclusions.txt", separator=args.separator,
    )
    write_table(case_register, args.output_dir / "case_selection.csv", args.separator)
    (args.output_dir / "cleaning_manifest.json").write_text(
        json.dumps({"input": input_metadata, "cleaning": cleaning_metadata}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"Cases before cleaning: {input_metadata['source_cases']:,}", flush=True)
    if args.deduplicate_exact:
        print(f"Exact duplicate records removed: {input_metadata['exact_duplicate_records']:,}", flush=True)
    print(f"Cases removed by minimum event count: {cleaning_metadata['removed_min_events_case_count']:,}", flush=True)
    print(f"Cases removed by endpoint activities: {cleaning_metadata['removed_endpoint_case_count']:,}", flush=True)
    print(f"Total cases removed: {cleaning_metadata['removed_case_count']:,}", flush=True)
    print(f"Cases remaining: {input_metadata['selected_cases']:,}", flush=True)
    print(f"Clean CSV: {clean_path.resolve().relative_to(ROOT, walk_up=True)}", flush=True)
    print(f"Excluded CSV: {Path(cleaning_metadata['excluded_event_log']).resolve().relative_to(ROOT, walk_up=True)}", flush=True)
    if args.clean_only:
        print_section("Output files")
        print(f"Case identifiers and exclusion counts: {(args.output_dir / 'case_exclusions.txt').resolve().relative_to(ROOT, walk_up=True)}")
        print(f"Results saved in: {args.output_dir.resolve().relative_to(ROOT, walk_up=True)}")
        archive_results(args.output_dir)
        return 0

    print_section("Formal conformance")
    # The analysis consumes the saved clean file, making it reusable downstream.
    events, _, clean_metadata = load_and_select_log(
        clean_path, separator=args.separator, min_events=1,
        tie_break=args.tie_break, deduplicate_exact=False,
        endpoint_events=endpoint_events,
    )
    if len(events) != input_metadata["selected_events"] or events["CaseID"].nunique() != input_metadata["selected_cases"]:
        raise ValueError("Reloading the clean event log changed its selected case or event count.")
    all_rules = load_rules(args.model)
    if args.rule_ids is None:
        rules = all_rules
    else:
        requested = [value.strip() for value in args.rule_ids.split(",")]
        if not all(requested) or len(requested) != len(set(requested)):
            raise ValueError("Selected rule identifiers must be non-empty and distinct.")
        unknown_ids = set(requested) - {rule.rule_id for rule in all_rules}
        if unknown_ids:
            raise ValueError("Unknown selected rule identifiers: " + ", ".join(sorted(unknown_ids)))
        rules = [rule for rule in all_rules if rule.rule_id in requested]
    vocabulary = set(input_metadata["activity_vocabulary"])
    unknown = sorted({activity for rule in rules for activity in rule.activities} - vocabulary)
    if unknown:
        raise ValueError("Rule activities absent from the source-log vocabulary: " + ", ".join(unknown))
    model_diagnostics = diagnose_catalogue(rules)
    print(f"Selected {input_metadata['selected_cases']:,} cases, {len(events):,} events and {len(rules)} original rules.", flush=True)
    print("Running Declare4Py and preserving template-specific activation information...", flush=True)
    pairs, components, engine_metadata = run_conformance(events, rules, tie_break=args.tie_break)

    print_section("Aggregation and export")
    print("Calculating summaries and writing result tables...", flush=True)
    case_fields = case_register[["CaseID", "event_count", "has_timestamp_ties", "identical_record_excess"]]
    pairs = pairs.merge(case_fields, on="CaseID", how="left", validate="many_to_one", sort=False)
    tables = summarise_results(pairs)
    outputs: list[str] = [
        str(clean_path), Path(cleaning_metadata["excluded_event_log"]).name,
        "case_exclusions.txt", "cleaning_manifest.json",
    ]

    def save(name: str, frame: pd.DataFrame, *, aggregate: bool = False) -> None:
        write_table(frame, args.output_dir / name, args.separator, aggregate=aggregate)
        outputs.append(name)

    save("case_selection.csv", case_register)
    save("case_rule_results.csv", pairs)
    save("component_results.csv", components)
    save("model_diagnostics.csv", model_diagnostics)
    catalogue = pd.DataFrame([{
        "rule_id": rule.rule_id, "template": rule.template,
        "constraint": rule.constraint,
        "activities": json.dumps(list(rule.activities), ensure_ascii=False),
        "legal_applicability": "not_assessed",
    } for rule in rules])
    save("rule_catalogue.csv", catalogue)
    selected_rule_ids = {rule.rule_id for rule in rules}
    save("rule_selection.csv", pd.DataFrame([{
        "rule_id": rule.rule_id, "constraint": rule.constraint,
        "selected": rule.rule_id in selected_rule_ids,
        "selection_basis": "All supplied rules" if args.rule_ids is None else "Explicit rule identifiers supplied before checking",
    } for rule in all_rules]))
    (args.output_dir / "resolved_engine_model.decl").write_text(primitive_model_text(rules), encoding="utf-8")
    outputs.append("resolved_engine_model.decl")
    rule_proposal = None
    if args.propose_rules:
        proposed_rules, omitted_rules = propose_reduced_rules(rules)
        proposal_path = args.output_dir / f"{args.model.stem}_{len(proposed_rules)}_proposed.decl"
        if proposal_path.resolve() == args.model.resolve():
            raise ValueError("The proposal output path must differ from the input model; choose another output directory.")
        comments = ["# Proposed reduction using explicit template implications under a shared applicability scope."]
        comments.extend(f"# Proposed omission: {rule_id} is implied by retained rule {witness}." for rule_id, witness in omitted_rules.items())
        proposal_path.write_text("\n".join(comments) + "\n" + rule_table_text(proposed_rules), encoding="utf-8")
        outputs.append(proposal_path.name)
        rule_proposal = {
            "output_file": proposal_path.name,
            "retained_rule_count": len(proposed_rules),
            "omitted_rules": omitted_rules,
            "sha256": file_sha256(proposal_path),
        }
        print(f"Rule proposal ({len(proposed_rules)} rules): {proposal_path.resolve().relative_to(ROOT, walk_up=True)}", flush=True)
    names = {
        "satisfaction_matrix": "formal_satisfaction_matrix.csv",
        "violation_matrix": "formal_violation_matrix.csv",
    }
    for key, table in tables.items():
        save(names.get(key, key + ".csv"), table, aggregate=True)

    tie_metadata = None
    if args.compare_tie_orders:
        print("Comparing source and alphabetical ordering of timestamp ties...", flush=True)
        differences, tie_metadata = compare_tie_orders(events, rules, pairs, args.tie_break)
        save("ordering_sensitivity.csv", differences)
    if not args.no_figure:
        print("Drawing formal outcome charts...", flush=True)
        draw_outcomes(pairs, args.output_dir)
        outputs.extend(["formal_outcomes.png", "formal_outcomes.pdf"])

    versions = {}
    for name in ["Declare4Py", "pm4py", "pandas", "numpy", "matplotlib"]:
        versions[name] = importlib.metadata.version(name)
    manifest = {
        "created_utc": datetime.now(UTC).isoformat(),
        "phase": "Formal conformance and activation analysis",
        "python": platform.python_version(),
        "platform": platform.platform(),
        "package_versions": versions,
        "configuration": {key: str(value) if isinstance(value, Path) else value for key, value in vars(args).items()},
        "input": input_metadata,
        "cleaning": cleaning_metadata,
        "checker_input_sha256": clean_metadata["source_sha256"],
        "rule_source_sha256": file_sha256(args.model),
        "engine": engine_metadata,
        "original_rule_count": len(rules),
        "supplied_rule_count": len(all_rules),
        "case_rule_pairs": len(pairs),
        "catalogue_diagnostic_count": len(model_diagnostics),
        "rule_proposal": rule_proposal,
        "tie_order_comparison": tie_metadata,
        "legal_interpretation": "Formal outcomes are evaluated on the recorded traces. Legal applicability and documentary evidence are not assessed by this stage.",
        "output_files": outputs + ["run_manifest.json"],
    }
    (args.output_dir / "run_manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    print_section("Global formal summaries")
    summary = tables["global_summary"].iloc[0]
    count_columns = ("n_pairs", "n_formally_satisfied", "n_formally_violated", "n_vacuously_satisfied")
    score_columns = ("standard_conformance_score", "positive_evidence_score", "vacuity_excluded_score", "event_triggered_conditional_score")
    label_width = max(map(len, count_columns + score_columns))
    print("Counts:")
    for column in count_columns:
        print(f"  {column:<{label_width}} : {int(summary[column]):,}")
    print("\nScores:")
    for column in score_columns:
        value = summary[column]
        print(f"  {column:<{label_width}} : {'unavailable' if pd.isna(value) else format_decimal(value)}")
    if not model_diagnostics.empty or tie_metadata is not None:
        print_section("Diagnostics")
    if not model_diagnostics.empty:
        print(f"Catalogue diagnostics: {len(model_diagnostics)}")
        print("  See model_diagnostics.csv before interpreting all-rule aggregates.")
    if tie_metadata is not None:
        print(f"Timestamp ordering: {tie_metadata['changed_formal_outcomes']:,} pair outcomes differ between the two stated conventions.")
    print_section("Output files")
    print(f"Results saved in: {args.output_dir.resolve().relative_to(ROOT, walk_up=True)}")
    archive_results(args.output_dir)
    print()
    print("*** PROGRAM END ***")
    print()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, FileNotFoundError) as exc:
        message = (
            f"{exc.strerror}: {Path(exc.filename).resolve().relative_to(ROOT, walk_up=True)}"
            if isinstance(exc, FileNotFoundError) and exc.filename else str(exc)
        )
        print(f"Error: {message}", file=sys.stderr)
        raise SystemExit(2) from exc
