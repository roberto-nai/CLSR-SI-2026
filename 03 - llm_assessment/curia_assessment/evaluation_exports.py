"""Export case decisions, traceable rule assessments and independent expert reviews."""

from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping

from .inputs import case_year
from .prompts import _metadata_copy
from .expert_workbooks import write_expert_workbooks

_OUTCOMES = {"compliance", "violation", "undetermined"}
_PENDING = {"pending", "planned", "not_run", "dry_run", "skipped"}
_ANNOTATIONS = ("expert_outcome", "judgement_correct", "evidence_support", "explanation_quality", "comments")
_QUOTED_SOURCE_FIELDS = ("source", "legal_source", "quoted_source", "source_quote", "legal_basis")
_RULE_FIELDS = (
    "assessment_id", "job_id", "case_id", "case_year", "phase", "provider_id", "provider", "model", "run",
    "rule_id", "variant_id", "status", "outcome", "violation", "declare", "requirement", "justification",
    "evidence_refs", "document_files", "document_evidence", "dossier_path", "error",
)
_CASE_FIELDS = (
    "case_id", "case_year", "phase", "provider_id", "provider", "model", "run", "status", "violation",
    "n_rules", "n_completed", "n_violated", "n_undetermined", "n_compliance", "n_pending", "n_failed",
    "violated_rule_ids", "explanations", "dossier_path",
)
_EXPERT_FIELDS = (
    "assessment_id", "case_id", "case_year", "phase", "run", "model", "rule_id", "status", "outcome",
    "declare", "requirement", "justification", "evidence_refs", "document_evidence", "dossier_path",
    "annotation_status", *_ANNOTATIONS,
)
_REMOVED_EXPERT_FIELDS = {"variant_id", "response_sha256", "annotation_response_sha256", "source_support", *_QUOTED_SOURCE_FIELDS}
_RESPONSE_FIELDS = ("status", "outcome", "declare", "requirement", "justification", "evidence_refs")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _valid(row: Mapping[str, Any]) -> bool:
    return row["status"] == "ok" and row["outcome"] in _OUTCOMES


def _violation(row: Mapping[str, Any]) -> str:
    if not _valid(row) or row["outcome"] == "undetermined":
        return "undetermined"
    return "yes" if row["outcome"] == "violation" else "no"


def _atomic_csv(path: Path, rows: list[dict[str, Any]], fields: Iterable[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(fields))
        writer.writeheader()
        for row in rows:
            writer.writerow({key: "" if value is None else value for key, value in row.items()})
    temporary.replace(path)


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _dossier_path(case_id: str, phase: str) -> str:
    # Case/model identifiers cannot introduce paths or reveal the model to experts.
    phase_label = re.sub(r"[^A-Za-z0-9_-]", "_", phase)
    return f"expert_review/dossiers/case_{_hash(case_id)[:20]}_phase_{phase_label}.txt"


def _expert_phase(phase: str) -> str:
    """Present numbered review phases while keeping scientific condition codes."""
    return {"A": "1", "B": "2"}.get(phase, phase)


def _passages(pair: Mapping[str, Any], phase: str) -> list[dict[str, Any]]:
    return list(pair.get("passages") or []) if phase != "A" else []


def _document_evidence(pair: Mapping[str, Any], phase: str) -> list[dict[str, Any]]:
    return [{"passage_id": item.get("passage_id", ""), "source_reference": item.get("source_reference", "")}
            for item in _passages(pair, phase)]


def _dossier(case_id: str, phase: str, pairs: list[dict[str, Any]]) -> str:
    case = pairs[0]
    lines = ["CASE EVIDENCE DOSSIER", "", f"CaseID: {case_id}", f"CaseID year: {case.get('case_year', case_year(case_id))}",
             f"Phase: {_expert_phase(phase)}", "", "Case metadata", _json(_metadata_copy(case.get("metadata", {}))), "", "Ordered event log"]
    for index, event in enumerate(case.get("events", []), 1):
        lines.append(f"{event.get('ref', f'E{index:04d}')} | {event.get('timestamp', '')} | {event.get('activity', '')}")
        if event.get("metadata"):
            lines.append("  Metadata: " + _json(_metadata_copy(event["metadata"])))
    lines.extend(["", "Supplied procedural requirements and DECLARE formulas"])
    for pair in sorted(pairs, key=lambda item: (item.get("base_rule_id", ""), item.get("variant_id", ""))):
        lines.extend(["", f"Rule: {pair['base_rule_id']} | Variant: {pair['variant_id']}",
                      "DECLARE (scoped to this rule):", pair["declare"],
                      "REQUIREMENT (scoped to this rule):", pair["requirement"]])
    if phase == "A":
        lines.extend(["", "Phase 1 contains the supplied textual requirements, original DECLARE formulas and ordered event log.",
                      "No additional case document passages were supplied."])
    else:
        lines.extend(["", "Case document evidence supplied to the model"])
        grouped: dict[str, tuple[dict[str, Any], list[str]]] = {}
        for pair in pairs:
            for passage in _passages(pair, phase):
                key = _json(passage)
                if key not in grouped:
                    grouped[key] = (passage, [])
                grouped[key][1].append(pair["variant_id"])
        for passage, rules in grouped.values():
            lines.extend(["", f"{passage.get('passage_id', '')} | {passage.get('source_reference', '')}",
                          "Available for requirements: " + ", ".join(sorted(set(rules))), passage.get("text", "")])
        if not grouped:
            lines.append("No additional document passages are recorded for this phase.")
    return "\n".join(lines) + "\n"


def _expert_rows(
    path: Path, fresh: list[dict[str, Any]], state_path: Path,
) -> tuple[list[dict[str, Any]], list[str], dict[str, Any]]:
    """Preserve ratings, migrating response identities into private audit state."""
    existing: dict[str, dict[str, str]] = {}
    extras: list[str] = []
    previous = {}
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            raise ValueError(f"Cannot read private expert review state: {state_path}. Existing annotations were preserved.") from exc
        if (not isinstance(state, dict) or state.get("schema_version") != 1
                or not isinstance(state.get("assessments"), dict)
                or any(not isinstance(identifier, str) or not isinstance(item, dict)
                       or any(not isinstance(item.get(field, ""), str)
                              for field in ("response_sha256", "annotation_response_sha256", "annotation_status"))
                       for identifier, item in state["assessments"].items())):
            raise ValueError(f"Malformed private expert review state: {state_path}. Existing annotations were preserved.")
        previous = state["assessments"]
    if path.exists():
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if reader.fieldnames is None or "assessment_id" not in reader.fieldnames:
                raise ValueError(f"Expert review file has no assessment_id column: {path}.")
            if len(reader.fieldnames) != len(set(reader.fieldnames)):
                raise ValueError(f"Expert review file has duplicate column names: {path}. Existing annotations were preserved.")
            extras = [field for field in reader.fieldnames
                      if field not in _EXPERT_FIELDS and field not in _REMOVED_EXPERT_FIELDS]
            for row in reader:
                identifier = row.get("assessment_id", "")
                if not identifier or identifier in existing or None in row or any(value is None for value in row.values()):
                    raise ValueError(f"Expert review file has a missing, duplicate or malformed assessment row: {path}.")
                if row.get("expert_outcome", "") and row["expert_outcome"] not in _OUTCOMES:
                    raise ValueError(f"Unsupported expert_outcome in {path}; use compliance, violation or undetermined. Existing annotations were preserved.")
                if row.get("source_support", "").strip():
                    raise ValueError(f"Legacy source_support annotations exist in {path}. Existing files were preserved; archive and explicitly review that column before using evidence_support. Old ratings are not translated automatically.")
                existing[identifier] = row
        unknown = set(existing) - {row["assessment_id"] for row in fresh}
        if unknown:
            raise ValueError(f"Expert review file contains assessments outside this experiment: {path}. Existing annotations were preserved.")
    merged = []
    identities = {}
    for row in fresh:
        updated = {field: row.get(field, "") for field in _EXPERT_FIELDS}
        old = existing.get(row["assessment_id"], {})
        prior = previous.get(row["assessment_id"], {})
        for field in (*_ANNOTATIONS, *extras):
            updated[field] = old.get(field, "")
        annotated = any(str(updated[field]).strip() for field in (*_ANNOTATIONS, *extras))
        # Legacy hashes migrate unchanged. New CSVs retain enough displayed
        # response fields to recover an identity if private state is unavailable.
        displayed_fields = (*(field for field in _RESPONSE_FIELDS if field in old),
                            *(field for field in _QUOTED_SOURCE_FIELDS if field in old))
        displayed_hash = (old.get("response_sha256")
                          or (_hash({field: old[field] for field in displayed_fields})
                              if all(field in old for field in _RESPONSE_FIELDS if field != "declare") else "")
                          or prior.get("response_sha256", ""))
        annotated_hash = old.get("annotation_response_sha256") or prior.get("annotation_response_sha256", "")
        if annotated:
            was_changed = (prior.get("annotation_status") == "response_changed_since_annotation"
                           or bool(annotated_hash and displayed_hash and annotated_hash != displayed_hash))
            if old.get("annotation_status") == "annotated" and was_changed:
                annotated_hash = displayed_hash
            annotated_hash = annotated_hash or displayed_hash
        else:
            annotated_hash = ""
        updated["annotation_status"] = ("response_changed_since_annotation" if annotated and annotated_hash != row["response_sha256"]
                                        else "annotated" if annotated else "unreviewed")
        identities[row["assessment_id"]] = {
            "response_sha256": row["response_sha256"], "annotation_response_sha256": annotated_hash,
            "annotation_status": updated["annotation_status"],
        }
        merged.append(updated)
    return merged, [*_EXPERT_FIELDS, *extras], {"schema_version": 1, "assessments": identities}


def _case_comparison(cases: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """Keep one case per row with faithful explanations and full-detail links."""
    views = sorted({(row["phase"], row["provider_id"], row["run"]) for row in cases})
    multiple_runs = len({run for _, _, run in views}) > 1
    labels = {view: f"{view[0]}_{view[1]}" + (f"_run_{view[2]}" if multiple_runs else "") for view in views}
    if len(set(labels.values())) != len(labels):
        raise ValueError("Phase/provider/run labels collide in the case comparison export.")
    suffixes = ("violation", "status", "violated_rule_ids", "explanations")
    fields = ["case_id", "case_year", *[f"{labels[view]}_{suffix}" for view in views for suffix in suffixes]]
    grouped: dict[str, dict[str, Any]] = {}
    for row in cases:
        comparison = grouped.setdefault(row["case_id"], {"case_id": row["case_id"], "case_year": row["case_year"]})
        label = labels[(row["phase"], row["provider_id"], row["run"])]
        for field in suffixes[:-1]:
            comparison[f"{label}_{field}"] = row[field]
        reference = (
            f"See rule_assessments.csv: case_id={row['case_id']}; phase={row['phase']}; "
            f"provider_id={row['provider_id']}; run={row['run']}. "
            f"{row['n_completed']}/{row['n_rules']} completed requirement assessments; "
            "the file preserves each original DECLARE formula, textual requirement, explanation and evidence references."
        )
        explanations = "\n".join(
            f"{item['rule_id']}: {item['outcome'] or item['status']} — "
            f"{item['justification'] or 'No model explanation is available.'}"
            for item in json.loads(row["explanations"])
        )
        text = explanations + "\n\n" + reference
        if len(text) > 24000:
            suffix = ("\n[Excerpt truncated; complete explanations, formulas and requirements in rule_assessments.csv.]\n"
                      + reference)
            text = explanations[:max(0, 24000 - len(suffix))] + suffix
        comparison[f"{label}_explanations"] = text
    return list(grouped.values()), fields


_INSTRUCTIONS = """INDEPENDENT EXPERT REVIEW

Each expert reviews their own CSV independently. expert_1.csv and expert_2.csv contain the same assessment identifiers and evidence. The model name is included immediately after run; hide that column before independent rating if model blinding is desired, leaving its underlying values unchanged. Internal provider identifiers and formal-checker results are omitted.

One row is one case–requirement–model–phase assessment. Rows are ordered by case_year, starting with the earliest year, and then by case_id so the same case remains consecutive. assessment_id is an opaque stable identifier. Phase 1 uses the supplied textual requirements, original DECLARE formulas and ordered event log. Phase 2 uses the same evidence and additionally supplies the recorded case document passages. A dossier is shared across all model assessments of the same case and phase. Evidence reference REQUIREMENT refers to the current row's textual requirement, DECLARE to its original formula, CASE to case metadata, E0001 etc. to the ordered events, and D-prefixed references to the documentary evidence as recorded. REQUIREMENT and DECLARE are scoped to their rule. Documentary reference identifiers are scoped to their requirement when dossier sections differ.

Open dossier_path relative to this expert_review directory. The CSV also contains the exact textual requirement, original DECLARE formula, model outcome, justification and evidence references for the assessed row. Judge the requirement and formula as supplied using only the case evidence supplied for that phase; do not use later or externally retrieved material. Dossiers contain all selected requirements and formulas for a case. Assess the rule identified in the current CSV row. A formula states the requested constraint, not a formal-checker verdict. If its relationship to the textual requirement or the case evidence is unresolved, explain that limitation rather than silently rewriting either input.

Leave machine-generated columns unchanged. Enter ratings only in these initially blank columns:
- expert_outcome: compliance | violation | undetermined. This is your independent judgement of the requirement on the available evidence. Use undetermined when the requirement is outside the case's scope or the evidence does not establish a compliance or violation judgement; explain the reason in comments.
- judgement_correct: yes | no | undetermined. Does the model's stated outcome agree with your expert judgement? Use undetermined when correctness cannot be assessed.
- evidence_support: supported | partly_supported | unsupported | not_assessable. Assess whether the supplied textual requirement, original DECLARE formula and case evidence support the model's outcome and explanation.
- explanation_quality: 0 | 1 | 2 | 3. 0 = missing, contradictory or unsupported explanation; 1 = substantial omissions or unsupported reasoning; 2 = mostly adequate with minor omissions; 3 = clear, adequate and supported by the supplied evidence. Leave blank if no assessable model response is available.
- comments: free text explaining your ratings, uncertainty or evidence concerns.

pending and failed statuses are operational states, not legal outcomes. Do not convert missing responses into violations or treat them as legal undetermined answers. The three permitted legal outcomes remain distinct. No expert judgement or score is pre-filled.

Use either the CSV or the XLSX for your review, rather than editing both copies separately. The XLSX has filters, frozen headers and editable rating fields. Its outcome cells are green for compliance, red for violation and yellow for undetermined. Return your completed workbook or CSV to the researcher.

Reanalysis preserves ratings and added columns from the CSV. Historical source_support ratings require explicit review and stop regeneration if populated; they are not translated into evidence_support. Before regenerating exports from an annotated XLSX, the researcher must save its reviewed sheet to the corresponding CSV and archive the edited workbook separately. An edited XLSX is never overwritten automatically. Response identities and annotation history are retained privately in _audit/expert_review_state; technical hash columns are not part of the expert CSV. annotation_status=response_changed_since_annotation means the response changed after annotation; reconcile that row before analysis. After reviewing the displayed response and retaining or revising the ratings, set annotation_status to annotated to acknowledge that review. Other machine-generated fields must remain unchanged. Save a separate copy before removing rows or changing assessment_id values. Unknown or duplicate identifiers stop regeneration without overwriting either expert CSV.

Case-level violation in case_results.csv is a transparent aggregation, not another model judgement: yes if any completed valid requirement assessment is a violation; no only if every planned assessment is valid compliance; otherwise undetermined. Counts, pending/error states and individual rule explanations remain available for inspection.
"""


def write_evaluation_outputs(
    output: str | Path, *, config: Mapping[str, Any], pairs: Mapping[str, dict[str, Any]] | Iterable[dict[str, Any]],
    jobs: Iterable[dict[str, Any]], assessments: Iterable[dict[str, Any]],
) -> dict[str, str]:
    """Write auditable CSVs and two independently editable expert packs.

    ``assessments`` contains the latest available result for each planned job;
    omitted jobs remain pending. No formal-checker verdict is consumed here.
    Existing expert annotations are never replaced by blank templates.
    """
    output = Path(output)
    catalogue = list(pairs.values()) if isinstance(pairs, Mapping) else list(pairs)
    pair_index = {pair["pair_id"]: pair for pair in catalogue}
    if len(pair_index) != len(catalogue):
        raise ValueError("Evaluation exports require unique pair identifiers.")
    if any(not isinstance(pair.get("declare"), str) or not pair["declare"].strip() for pair in catalogue):
        raise ValueError("Evaluation exports require each original DECLARE formula in pair.declare; missing formulas are not invented.")
    planned = list(jobs)
    job_ids = {job["job_id"] for job in planned}
    if len(job_ids) != len(planned):
        raise ValueError("Evaluation exports require unique job identifiers.")
    results: dict[str, dict[str, Any]] = {}
    for result in assessments:
        identifier = result["job_id"]
        if identifier not in job_ids or identifier in results:
            raise ValueError("Evaluation results contain an unknown or duplicate job identifier.")
        outcome = result.get("outcome")
        if ((outcome is not None and outcome != "" and (not isinstance(outcome, str) or outcome not in _OUTCOMES))
                or result.get("status") == "ok" and outcome not in _OUTCOMES):
            raise ValueError("Evaluation exports require compliance, violation or undetermined; an unsupported model outcome must remain a failed assessment, not a binary decision.")
        results[identifier] = result
    rules = []
    dossier_pairs: dict[tuple[str, str], dict[str, dict[str, Any]]] = defaultdict(dict)
    for job in planned:
        pair = pair_index[job["pair_id"]]
        phase = job["condition"]
        result = results.get(job["job_id"], {})
        row = {
            "assessment_id": "assessment_" + _hash(job["job_id"])[:24], "job_id": job["job_id"],
            "case_id": job["case_id"], "case_year": pair.get("case_year", case_year(job["case_id"])), "phase": phase,
            "provider_id": job["provider_id"], "provider": job["provider"], "model": job["model"], "run": job.get("run", 1),
            "rule_id": job["base_rule_id"], "variant_id": job["variant_id"],
            "status": result.get("status", "pending"), "outcome": result.get("outcome") or "",
            "declare": pair["declare"], "requirement": pair["requirement"],
            "justification": result.get("justification") or "", "evidence_refs": _json(result.get("evidence_refs") or []),
            "document_files": _json(pair.get("document_files") or []), "document_evidence": _json(_document_evidence(pair, phase)),
            "dossier_path": _dossier_path(job["case_id"], phase), "error": _json(result.get("error")) if result.get("error") else "",
        }
        row["violation"] = _violation(row)
        rules.append(row)
        dossier_pairs[(job["case_id"], phase)][pair["pair_id"]] = pair
    rules.sort(key=lambda row: (row["case_year"], row["case_id"], row["phase"], row["provider_id"], row["run"], row["rule_id"], row["variant_id"]))
    cases = []
    grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in rules:
        grouped[(row["case_id"], row["provider_id"], row["phase"], row["run"])].append(row)
    for rows in grouped.values():
        first = rows[0]
        valid = [row for row in rows if _valid(row)]
        outcomes = Counter(row["outcome"] for row in valid)
        pending = sum(row["status"] in _PENDING for row in rows)
        failed = len(rows) - len(valid) - pending
        violation = ("yes" if outcomes["violation"] else "no" if outcomes["compliance"] == len(rows) else "undetermined")
        status = "completed" if len(valid) == len(rows) else "pending" if pending == len(rows) else "failed" if failed == len(rows) else "partial"
        cases.append({
            **{key: first[key] for key in ("case_id", "case_year", "phase", "provider_id", "provider", "model", "run", "dossier_path")},
            "status": status, "violation": violation, "n_rules": len({row["rule_id"] for row in rows}), "n_completed": len(valid),
            "n_violated": outcomes["violation"], "n_undetermined": outcomes["undetermined"], "n_compliance": outcomes["compliance"],
            "n_pending": pending, "n_failed": failed,
            "violated_rule_ids": _json(sorted({row["rule_id"] for row in valid if row["outcome"] == "violation"})),
            "explanations": _json([
                {**{key: row[key] for key in ("assessment_id", "rule_id", "variant_id", "status", "outcome", "declare", "requirement", "justification")},
                 "evidence_refs": json.loads(row["evidence_refs"])}
                for row in rows]),
        })
    fresh_experts = []
    for row in sorted(rules, key=lambda item: (int(item["case_year"]), item["case_id"], item["phase"], item["run"],
                                             item["model"], item["rule_id"], item["assessment_id"])):
        fresh = {field: row.get(field, "") for field in _EXPERT_FIELDS}
        fresh["phase"] = _expert_phase(row["phase"])
        fresh["dossier_path"] = row["dossier_path"].removeprefix("expert_review/")
        fresh["response_sha256"] = _hash({field: row[field] for field in _RESPONSE_FIELDS})
        fresh_experts.append(fresh)
    # Preflight both expert files before writing any output or modifying annotations.
    expert_files = [(output / "expert_review" / f"expert_{number}.csv") for number in (1, 2)]
    state_files = [(output / "_audit/expert_review_state" / f"expert_{number}.json") for number in (1, 2)]
    expert_data = [_expert_rows(path, fresh_experts, state_path)
                   for path, state_path in zip(expert_files, state_files, strict=True)]
    comparison_rows, comparison_fields = _case_comparison(cases)
    _atomic_csv(output / "case_results.csv", cases, _CASE_FIELDS)
    _atomic_csv(output / "case_comparison.csv", comparison_rows, comparison_fields)
    _atomic_csv(output / "rule_assessments.csv", rules, _RULE_FIELDS)
    for (case_id, phase), selected in dossier_pairs.items():
        _atomic_text(output / _dossier_path(case_id, phase), _dossier(case_id, phase, list(selected.values())))
    for path, state_path, (rows, fields, state) in zip(expert_files, state_files, expert_data, strict=True):
        _atomic_csv(path, rows, fields)
        _atomic_text(state_path, _json(state) + "\n")
    _atomic_text(output / "expert_review/instructions.txt", _INSTRUCTIONS)
    write_expert_workbooks(output / "expert_review")
    return {"case_results": "case_results.csv", "case_comparison": "case_comparison.csv",
            "rule_assessments": "rule_assessments.csv", "expert_review": "expert_review"}
