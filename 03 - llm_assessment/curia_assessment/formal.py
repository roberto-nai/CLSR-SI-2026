"""Compute or import a trace-matched formal reference without using it in prompts."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import zipfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .inputs import cases_from_rows, normalised_timestamp, read_csv_text, sha256_file, trace_hash, write_csv

_IGNORED_REFERENCE_DIRECTORIES = {"__MACOSX", "_old"}


def _validate_case(case: dict[str, Any]) -> None:
    events = case["events"]
    if not events or len(events) != case["event_count"]:
        raise ValueError("Dossier event counts are inconsistent.")
    order = [(normalised_timestamp(event["timestamp"]), event["source_row"]) for event in events]
    if order != sorted(order) or len({event["source_row"] for event in events}) != len(events):
        raise ValueError("Dossier events must retain chronological/source-row order.")
    if case["trace_sha256"] != trace_hash(case["case_id"], events):
        raise ValueError("A dossier trace changed after its hash was calculated.")


def _base_rules(variants: list[dict[str, Any]]) -> dict[str, str]:
    rules: dict[str, str] = {}
    for variant in variants:
        identifier, constraint = variant["base_rule_id"], variant["declare"]
        if identifier in rules and rules[identifier] != constraint:
            raise ValueError(f"Textual variants of {identifier} contain different DECLARE constraints.")
        rules[identifier] = constraint
    if not rules:
        raise ValueError("A formal reference requires at least one retained rule.")
    return rules


def _boolean(value: Any, description: str) -> bool:
    if isinstance(value, bool):
        return value
    if str(value).lower() in {"true", "1"}:
        return True
    if str(value).lower() in {"false", "0"}:
        return False
    raise ValueError(f"Invalid Boolean value for {description}: {value!r}.")


def _count(value: Any, description: str) -> int | None:
    if value is None or value == "":
        return None
    text = str(value)
    if not re.fullmatch(r"[0-9]+", text):
        raise ValueError(f"Invalid count for {description}: {value!r}.")
    return int(text)


def _normalise_row(row: dict[str, Any], case: dict[str, Any]) -> dict[str, Any]:
    """Retain the binary formal state separately from activation and legal labels."""
    case_id = row.get("CaseID", row.get("case_id"))
    rule_id = row.get("rule_id", row.get("base_rule_id"))
    state = _count(row.get("formal_satisfied"), "formal_satisfied")
    if state not in (0, 1):
        raise ValueError("Formal satisfaction must be encoded as 0 or 1.")
    outcome = row.get("formal_outcome")
    activation = row.get("activation_status")
    vacuous = _boolean(row.get("vacuously_satisfied"), "vacuously_satisfied")
    if outcome not in {"satisfied", "violated", "vacuously_satisfied"}:
        raise ValueError(f"Unknown formal outcome: {outcome!r}.")
    if activation not in {"active", "inactive", "not_applicable"}:
        raise ValueError(f"Unknown activation status: {activation!r}.")
    if state != int(outcome != "violated") or vacuous != (outcome == "vacuously_satisfied"):
        raise ValueError("Formal outcome, state and vacuity flag do not agree.")
    if vacuous != (activation == "inactive"):
        raise ValueError("Operational inactivity and vacuous satisfaction do not agree.")
    activation_count = _count(row.get("activation_count"), "activation_count")
    fulfilment_count = _count(row.get("fulfilment_count"), "fulfilment_count")
    violation_count = _count(row.get("violation_count"), "violation_count")
    if activation == "not_applicable":
        if any(value is not None for value in (activation_count, fulfilment_count, violation_count)):
            raise ValueError("Original-rule unary activation counts must remain unavailable.")
    else:
        if any(value is None for value in (activation_count, fulfilment_count, violation_count)):
            raise ValueError("Event-triggered activation counts are missing.")
        if activation_count != fulfilment_count + violation_count:
            raise ValueError("Activation, fulfilment and violation counts are inconsistent.")
        if (activation == "active") != (activation_count > 0):
            raise ValueError("Activation status disagrees with the activation count.")
        if state != int(violation_count == 0):
            raise ValueError("Formal state disagrees with component violation counts.")
    if row.get("event_count") not in (None, "") and int(row["event_count"]) != case["event_count"]:
        raise ValueError(f"Formal event count differs from the supplied trace for {case_id}.")
    return {
        "case_id": case_id,
        "CaseID": case_id,
        "base_rule_id": rule_id,
        "rule_id": rule_id,
        "constraint": row.get("constraint"),
        "template": row.get("template"),
        "formal_satisfied": state,
        "formal_outcome": outcome,
        "activation_status": activation,
        "activation_count": activation_count,
        "fulfilment_count": fulfilment_count,
        "violation_count": violation_count,
        "vacuously_satisfied": vacuous,
        "component_count": _count(row.get("component_count"), "component_count"),
        "event_count": case["event_count"],
        "has_timestamp_ties": case["has_timestamp_ties"],
        "trace_sha256": case["trace_sha256"],
    }


class _ReferenceFiles:
    """Read named results safely from a directory or ZIP without extracting it."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.archive: zipfile.ZipFile | None = None

    def __enter__(self) -> "_ReferenceFiles":
        if self.path.is_file():
            if not zipfile.is_zipfile(self.path):
                raise ValueError("Formal results must be a directory or a ZIP archive.")
            self.archive = zipfile.ZipFile(self.path)
        elif not self.path.is_dir():
            raise ValueError("The formal-results path does not exist.")
        return self

    def __exit__(self, *args: Any) -> None:
        if self.archive is not None:
            self.archive.close()

    def _resolve(self, name: str) -> str | Path:
        if Path(name).name != name or name in {"", ".", ".."}:
            raise ValueError("A reference member must be identified by a safe basename.")
        if self.archive is not None:
            matches = [item.filename for item in self.archive.infolist()
                       if not item.is_dir() and Path(item.filename).name == name
                       and not _IGNORED_REFERENCE_DIRECTORIES.intersection(Path(item.filename).parts[:-1])]
        else:
            matches = [path for path in self.path.rglob(name) if path.is_file()
                       and not _IGNORED_REFERENCE_DIRECTORIES.intersection(path.relative_to(self.path).parts[:-1])]
        if len(matches) != 1:
            raise ValueError(f"Expected one {name!r} in the formal results; found {len(matches)}.")
        return matches[0]

    def read(self, name: str) -> bytes:
        resolved = self._resolve(name)
        return self.archive.read(str(resolved)) if self.archive is not None else Path(resolved).read_bytes()

    def json(self, name: str) -> dict[str, Any]:
        value = json.loads(self.read(name).decode("utf-8-sig"))
        if not isinstance(value, dict):
            raise ValueError(f"{name} must contain a JSON object.")
        return value

    @contextmanager
    def csv_rows(self, name: str, required: set[str]) -> Iterator[csv.DictReader]:
        resolved = self._resolve(name)
        binary = self.archive.open(str(resolved)) if self.archive is not None else Path(resolved).open("rb")
        with binary:
            with io.TextIOWrapper(binary, encoding="utf-8-sig", newline="") as stream:
                first_line = stream.readline()
                possibilities = []
                for delimiter in (",", ";", "\t"):
                    columns = next(csv.reader([first_line], delimiter=delimiter), [])
                    if required.issubset(columns):
                        possibilities.append((delimiter, columns))
                if len(possibilities) != 1:
                    raise ValueError(f"Cannot identify the delimiter/columns of {name}.")
                delimiter, columns = possibilities[0]
                if len(columns) != len(set(columns)):
                    raise ValueError(f"Duplicate columns in {name}.")
                yield csv.DictReader(stream, fieldnames=columns, delimiter=delimiter)


def import_formal_results(
    results_path: str | Path,
    cases: list[dict[str, Any]],
    variants: list[dict[str, Any]],
    event_log_path: str | Path,
    min_events: int = 2,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Import an existing run only after checking its inputs, traces and rule IDs.

    The run manifest selects the clean CSV by basename. Other clean logs in the
    archive are ignored. The supplied log must exactly match either the recorded
    source or the independently verified archived clean CSV. Whole-file hashes,
    preprocessing, trace order and the original rule catalogue must match.
    The assessment may use a different event-count threshold if every selected
    trace is covered by the independently validated reference.
    """
    if isinstance(min_events, bool) or not isinstance(min_events, int) or (min_events != -1 and min_events < 1):
        raise ValueError("min_events must be a positive integer or -1.")
    expected_rules = _base_rules(variants)
    requested_cases = {case["case_id"]: case for case in cases}
    if not requested_cases or len(requested_cases) != len(cases):
        raise ValueError("The requested case list must be non-empty and unique.")
    for case in cases:
        _validate_case(case)
        if min_events != -1 and case["event_count"] < min_events:
            raise ValueError(f"The requested dossier {case['case_id']} has fewer than {min_events} recorded events.")
    actual_input_hash = sha256_file(event_log_path)
    with _ReferenceFiles(results_path) as files:
        manifest = files.json("run_manifest.json")
        cleaning = files.json("cleaning_manifest.json")
        source = manifest.get("input", {})
        settings = manifest.get("configuration", {})
        engine = manifest.get("engine", {})
        raw_hash = source.get("source_sha256")
        cleaning_source = cleaning.get("input", {})
        if (not isinstance(raw_hash, str) or re.fullmatch(r"[0-9a-f]{64}", raw_hash) is None
                or cleaning_source.get("source_sha256") != raw_hash):
            raise ValueError("Formal results were not produced from the supplied event-log bytes.")
        reference_min_events = source.get("minimum_recorded_events")
        recorded_minimums = (reference_min_events, settings.get("min_events"),
                             cleaning.get("input", {}).get("minimum_recorded_events"))
        if any(isinstance(value, bool) or not isinstance(value, int)
               or (value != -1 and value < 1) or value != reference_min_events for value in recorded_minimums):
            raise ValueError("The formal reference has inconsistent minimum event counts.")
        if source.get("deduplicate_exact") is not False or settings.get("deduplicate_exact") is not False:
            raise ValueError("The formal reference must retain exact duplicate event records.")
        if source.get("tie_break") != "source" or settings.get("tie_break") != "source":
            raise ValueError("The formal reference must use source-row ordering for timestamp ties.")
        if engine.get("tie_break_policy") != "source":
            raise ValueError("The engine ordering convention is inconsistent with the run configuration.")
        if engine.get("state_encoding") != {"satisfied": 1, "not_satisfied": 0}:
            raise ValueError("The imported engine state orientation is not the verified convention.")
        clean_settings = manifest.get("cleaning", {})
        clean_name = Path(clean_settings.get("clean_event_log", "")).name
        if not clean_name:
            raise ValueError("The run manifest does not identify the cleaned event log.")
        if Path(cleaning.get("cleaning", {}).get("clean_event_log", "")).name != clean_name:
            raise ValueError("The run and cleaning manifests identify different cleaned logs.")
        clean_bytes = files.read(clean_name)
        clean_hash = hashlib.sha256(clean_bytes).hexdigest()
        recorded_hashes = (
            clean_settings.get("clean_event_log_sha256"),
            cleaning.get("cleaning", {}).get("clean_event_log_sha256"),
            manifest.get("checker_input_sha256"),
        )
        if any(value != clean_hash for value in recorded_hashes):
            raise ValueError("The cleaned event-log hash does not match its provenance.")
        if actual_input_hash == raw_hash:
            input_kind = "source"
        elif actual_input_hash == clean_hash:
            input_kind = "cleaned"
        else:
            raise ValueError("Formal results were not produced from the supplied event-log bytes.")
        clean_columns, clean_rows = read_csv_text(clean_bytes.decode("utf-8-sig"), {"CaseID", "Activity", "Timestamp"})
        clean_cases, _, clean_summary = cases_from_rows(clean_columns, clean_rows, min_events=1, category=None)
        clean_lookup = {case["case_id"]: case for case in clean_cases}
        for name in ("source_events", "source_cases", "selected_events", "selected_cases"):
            value = source.get(name)
            cleaning_value = cleaning_source.get(name)
            if (any(isinstance(count, bool) or not isinstance(count, int) or count < 0
                    for count in (value, cleaning_value)) or cleaning_value != value):
                raise ValueError(f"The formal reference has inconsistent {name}.")
        if (source["source_events"] < source["selected_events"]
                or source["source_cases"] < source["selected_cases"]
                or source["source_events"] < source["source_cases"]):
            raise ValueError("The formal reference has inconsistent source and selected counts.")
        if len(clean_rows) != source["selected_events"] or len(clean_cases) != source["selected_cases"]:
            raise ValueError("Cleaned log counts differ from the manifest.")
        if reference_min_events != -1 and any(case["event_count"] < reference_min_events for case in clean_cases):
            raise ValueError("The cleaned log contains cases below the reference minimum event count.")
        if input_kind == "source":
            raw_columns, raw_rows = read_csv_text(Path(event_log_path).read_text(encoding="utf-8-sig"),
                                                  {"CaseID", "Activity", "Timestamp"})
            eligible, _, raw_summary = cases_from_rows(raw_columns, raw_rows,
                                                     min_events=reference_min_events, category=None)
            eligible_lookup = {case["case_id"]: case for case in eligible}
            for name in ("source_events", "source_cases", "selected_events", "selected_cases"):
                if source[name] != raw_summary[name]:
                    raise ValueError(f"The formal reference has inconsistent {name}.")
            if set(eligible_lookup) != set(clean_lookup):
                raise ValueError("The cleaned log contains different eligible case identifiers.")
            for case_id, original in eligible_lookup.items():
                if original["trace_sha256"] != clean_lookup[case_id]["trace_sha256"]:
                    raise ValueError(f"The cleaned log changed the ordered trace for {case_id}.")
        for case_id, case in requested_cases.items():
            if case_id not in clean_lookup:
                raise ValueError(f"The formal reference does not cover requested case {case_id}; "
                                 "supply a reference covering the selected cases or compute a new reference.")
            if case["trace_sha256"] != clean_lookup[case_id]["trace_sha256"]:
                raise ValueError(f"The requested dossier trace differs from the reference for {case_id}.")
        catalogue = {}
        with files.csv_rows("rule_catalogue.csv", {"rule_id", "constraint"}) as reader:
            for row in reader:
                if None in row or any(value is None for value in row.values()):
                    raise ValueError("Malformed row in the formal rule catalogue.")
                identifier = row["rule_id"]
                if identifier in catalogue:
                    raise ValueError(f"Duplicate formal catalogue identifier: {identifier}.")
                catalogue[identifier] = row["constraint"]
        for identifier, constraint in expected_rules.items():
            if catalogue.get(identifier) != constraint:
                raise ValueError(f"The formal reference contains a different constraint for {identifier}.")
        if engine.get("original_rules") != len(catalogue) or engine.get("cases") != len(clean_cases):
            raise ValueError("Engine counts disagree with the cleaned log and original catalogue.")
        formal_rows = []
        seen = set()
        required = {
            "CaseID", "rule_id", "constraint", "template", "formal_satisfied",
            "formal_outcome", "activation_status", "vacuously_satisfied",
        }
        with files.csv_rows("case_rule_results.csv", required) as reader:
            for row in reader:
                if None in row or any(value is None for value in row.values()):
                    raise ValueError("Malformed row in the formal case--rule results.")
                key = (row["CaseID"], row["rule_id"])
                if key in seen:
                    raise ValueError(f"Duplicate formal case--rule result: {key}.")
                seen.add(key)
                if row["CaseID"] not in clean_lookup or row["rule_id"] not in catalogue:
                    raise ValueError("Formal results refer to an unknown case or rule.")
                if row["constraint"] != catalogue[row["rule_id"]]:
                    raise ValueError("A formal result contains a changed DECLARE constraint.")
                if row["CaseID"] in requested_cases and row["rule_id"] in expected_rules:
                    formal_rows.append(_normalise_row(row, requested_cases[row["CaseID"]]))
        if len(seen) != len(clean_cases) * len(catalogue):
            raise ValueError("The archived formal case--rule table is incomplete.")
        expected_pairs = {(case_id, rule_id) for case_id in requested_cases for rule_id in expected_rules}
        if {(row["case_id"], row["base_rule_id"]) for row in formal_rows} != expected_pairs:
            raise ValueError("Some requested case--rule evaluations are missing.")
        case_order = {case_id: position for position, case_id in enumerate(requested_cases)}
        rule_order = {rule_id: position for position, rule_id in enumerate(expected_rules)}
        formal_rows.sort(key=lambda row: (case_order[row["case_id"]], rule_order[row["base_rule_id"]]))
        provenance = {
            "origin": "imported",
            "results_path": str(Path(results_path)),
            "source_event_log_sha256": actual_input_hash,
            "event_log_input_kind": input_kind,
            "reference_source_event_log_sha256": raw_hash,
            "clean_event_log_name": clean_name,
            "clean_event_log_sha256": clean_hash,
            "reference_cases": len(clean_cases),
            "reference_rules": len(catalogue),
            "reference_pairs": len(seen),
            "reference_min_events": reference_min_events,
            "selection_min_events": min_events,
            "selected_cases": len(requested_cases),
            "selected_base_rules": len(expected_rules),
            "selected_pairs": len(formal_rows),
            "ordered_traces_verified": True,
            "catalogue_verified": True,
            "state_encoding": engine["state_encoding"],
            "tie_break": "source",
            "deduplicate_exact": False,
            "package_versions": manifest.get("package_versions", {}),
            "formal_results_are_legal_ground_truth": False,
        }
    return formal_rows, provenance


def compute_formal_results(
    cases: list[dict[str, Any]], variants: list[dict[str, Any]], output_dir: str | Path,
) -> list[dict[str, Any]]:
    """Run the bundled verified checker on exactly the selected dossier traces."""
    import pandas as pd
    from curia_conformance.engine import run_conformance
    from curia_conformance.model import load_rules, primitive_model_text

    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    base_rules = _base_rules(variants)
    rules_path = directory / "formal_rules.decl"
    with rules_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, delimiter="\t")
        writer.writerow(["ID", "DECLARE"])
        writer.writerows(base_rules.items())
    rules = load_rules(rules_path)
    (directory / "resolved_engine_model.decl").write_text(primitive_model_text(rules), encoding="utf-8")
    event_rows = []
    case_lookup = {}
    for case in cases:
        _validate_case(case)
        if case["case_id"] in case_lookup:
            raise ValueError("Duplicate case identifiers in the formal input.")
        case_lookup[case["case_id"]] = case
        for event in case["events"]:
            event_rows.append({
                "CaseID": case["case_id"],
                "Activity": event["activity"],
                "Timestamp": pd.Timestamp(normalised_timestamp(event["timestamp"])),
                "_source_row": event["source_row"],
            })
    pairs, components, metadata = run_conformance(pd.DataFrame(event_rows), rules, tie_break="source")
    records = json.loads(pairs.to_json(orient="records"))
    rows = [_normalise_row(row, case_lookup[row["CaseID"]]) for row in records]
    case_order = {case_id: position for position, case_id in enumerate(case_lookup)}
    rule_order = {rule_id: position for position, rule_id in enumerate(base_rules)}
    rows.sort(key=lambda row: (case_order[row["case_id"]], rule_order[row["base_rule_id"]]))
    write_csv(directory / "formal_results.csv", rows)
    components.to_csv(directory / "formal_components.csv", index=False)
    observed = {event["activity"] for case in cases for event in case["events"]}
    metadata["activities_absent_from_selected_traces"] = sorted(
        {activity for rule in rules for activity in rule.activities} - observed
    )
    metadata["origin"] = "computed"
    metadata["formal_results_are_legal_ground_truth"] = False
    (directory / "formal_manifest.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
                                                   encoding="utf-8")
    return rows

