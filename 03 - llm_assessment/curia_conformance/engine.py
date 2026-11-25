"""Run Declare4Py and preserve formal outcomes and activation metadata.

Declare4Py supplies every primitive Boolean result. Original-rule satisfaction
is the conjunction of its primitive results with vacuity enabled. Operational
inactivity is defined only for event-triggered rules and means that the total
number of component obligations is zero. Unary constraints remain assessable
without an event trigger. Neither inactivity nor satisfaction establishes legal
applicability or legal compliance.
"""

from __future__ import annotations

import importlib.metadata
import math
from dataclasses import asdict
from typing import Any

import pandas as pd

from .model import Primitive, Rule


_METRICS = ("num_activations", "num_fulfillments", "num_violations", "num_pendings", "state")
_VERIFIED_ENGINE_VERSION: str | None = None


def _imports() -> tuple[Any, ...]:
    try:
        from Declare4Py.D4PyEventLog import D4PyEventLog
        from Declare4Py.ProcessModels.DeclareModel import DeclareModel
        from Declare4Py.ProcessMiningTasks.ConformanceChecking.MPDeclareAnalyzer import MPDeclareAnalyzer
        from Declare4Py.Utils.Declare.Checkers import TemplateConstraintChecker
        from pm4py.objects.conversion.log import converter
        from pm4py.util import constants
    except ImportError as exc:
        raise RuntimeError("Install the pinned experiment requirements before running conformance.") from exc
    return D4PyEventLog, DeclareModel, MPDeclareAnalyzer, TemplateConstraintChecker, converter, constants


def _nullable_count(value: object, description: str) -> int | None:
    if value is None or pd.isna(value):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid engine count for {description}: {value!r}.") from exc
    if not math.isfinite(numeric) or numeric < 0 or not numeric.is_integer():
        raise ValueError(f"Invalid engine count for {description}: {value!r}.")
    return int(numeric)


def _binary_state(value: object, description: str) -> int:
    if value is None or pd.isna(value):
        raise ValueError(f"Missing engine state for {description}.")
    if value not in (0, 1):
        raise ValueError(f"Expected a binary engine state for {description}, received {value!r}.")
    return int(value)


def _event_log(frame: pd.DataFrame) -> tuple[Any, list[str]]:
    D4PyEventLog, _, _, _, converter, constants = _imports()
    renamed = frame[["CaseID", "Activity", "Timestamp"]].rename(
        columns={"CaseID": "case:concept:name", "Activity": "concept:name", "Timestamp": "time:timestamp"}
    )
    event_log = converter.apply(
        renamed,
        parameters={converter.Variants.TO_EVENT_LOG.value.Parameters.CASE_ID_KEY: "case:concept:name"},
        variant=converter.Variants.TO_EVENT_LOG,
    )
    properties = dict(event_log.properties)
    properties[constants.PARAMETER_CONSTANT_ACTIVITY_KEY] = "concept:name"
    properties[constants.PARAMETER_CONSTANT_TIMESTAMP_KEY] = "time:timestamp"
    properties[constants.PARAMETER_CONSTANT_CASEID_KEY] = "case:concept:name"
    event_log.properties = properties
    case_ids = []
    for trace in event_log:
        case_id = (getattr(trace, "attributes", {}) or {}).get("concept:name")
        if not isinstance(case_id, str) or not case_id:
            raise ValueError("The converted event log contains a missing or invalid case identifier.")
        case_ids.append(case_id)
    if len(case_ids) != len(set(case_ids)) or set(case_ids) != set(frame["CaseID"]):
        raise ValueError("Case identifiers changed during conversion to the engine event log.")
    return D4PyEventLog(case_name="case:concept:name", log=event_log), case_ids


def _primitive_model(primitives: list[Primitive]) -> Any:
    _, DeclareModel, _, TemplateConstraintChecker, _, _ = _imports()
    text = "\n".join(primitive.engine_constraint for primitive in primitives)
    model = DeclareModel().parse_from_string(text)
    if len(model.constraints) != len(primitives):
        raise ValueError("Declare4Py did not parse every resolved primitive constraint.")
    for primitive, constraint in zip(primitives, model.constraints, strict=True):
        template = constraint["template"]
        if template.templ_str != primitive.engine_name.rstrip("0123456789"):
            raise ValueError("The parsed engine template differs from the resolved primitive.")
        if tuple(constraint["activities"]) != primitive.activities:
            raise ValueError("Declare4Py changed an activity label while parsing the model.")
        if primitive.cardinality is not None and constraint.get("n") != primitive.cardinality:
            raise ValueError("Declare4Py changed a constraint cardinality while parsing the model.")
        checker_name = f"mp{template.templ_str.replace(' ', '')}"
        if not callable(getattr(TemplateConstraintChecker, checker_name, None)):
            raise ValueError(f"The installed Declare4Py version has no checker for {template.templ_str}.")
        expected = 2 if primitive.is_unary else 3
        conditions = list(constraint.get("condition", []))
        if any(condition.strip() for condition in conditions):
            raise ValueError("An unexpected data or time condition entered the engine model.")
        constraint["condition"] = [""] * expected
    model.serialized_constraints = []
    model.set_constraints()
    if len(model.serialized_constraints) != len(primitives):
        raise ValueError("Engine serialisation does not align with the resolved primitives.")
    return model


def _run_primitives(frame: pd.DataFrame, primitives: list[Primitive]) -> tuple[list[str], dict[bool, dict[str, pd.DataFrame]]]:
    _, _, MPDeclareAnalyzer, _, _, _ = _imports()
    event_log, case_ids = _event_log(frame)
    model = _primitive_model(primitives)
    expected = (len(case_ids), len(primitives))
    runs: dict[bool, dict[str, pd.DataFrame]] = {}
    for vacuity in (True, False):
        browser = MPDeclareAnalyzer(event_log, model, vacuity).run()
        matrices = {}
        for metric in _METRICS:
            matrix = browser.get_metric(metric=metric)
            if not isinstance(matrix, pd.DataFrame):
                matrix = pd.DataFrame(matrix)
            if matrix.shape != expected:
                raise ValueError(f"Engine metric {metric} has shape {matrix.shape}; expected {expected}.")
            matrices[metric] = matrix
        runs[vacuity] = matrices
    return case_ids, runs


def _verify_engine_contract(version: str) -> None:
    """Check state orientation, unary metadata and vacuity on small known traces."""
    global _VERIFIED_ENGINE_VERSION
    if _VERIFIED_ENGINE_VERSION == version:
        return
    rows = []
    examples = {"satisfied": ("A", "B"), "violated": ("B", "A"), "inactive": ("X",)}
    for case_id, activities in examples.items():
        for position, activity in enumerate(activities):
            rows.append({"CaseID": case_id, "Activity": activity,
                         "Timestamp": pd.Timestamp("2000-01-01") + pd.Timedelta(minutes=position)})
    primitives = [Primitive("Response", ("A", "B")), Primitive("Existence", ("A",), 1)]
    cases, runs = _run_primitives(pd.DataFrame(rows), primitives)
    expected_response = {"satisfied": (1, 1, 1), "violated": (0, 0, 1), "inactive": (1, 0, 0)}
    for row, case_id in enumerate(cases):
        expected_true, expected_false, expected_activations = expected_response[case_id]
        actual_true = _binary_state(runs[True]["state"].iat[row, 0], "contract Response state")
        actual_false = _binary_state(runs[False]["state"].iat[row, 0], "contract Response state")
        actual_count = _nullable_count(runs[True]["num_activations"].iat[row, 0], "contract Response activations")
        unary_state = _binary_state(runs[True]["state"].iat[row, 1], "contract Existence state")
        unary_count = _nullable_count(runs[True]["num_activations"].iat[row, 1], "contract Existence activations")
        if (actual_true, actual_false, actual_count) != (expected_true, expected_false, expected_activations):
            raise RuntimeError("Declare4Py failed the known-answer state and vacuity checks.")
        if unary_state != int(case_id != "inactive") or unary_count is not None:
            raise RuntimeError("Declare4Py failed the known-answer unary-metadata check.")
    _VERIFIED_ENGINE_VERSION = version


def run_conformance(
    event_table: pd.DataFrame, rules: list[Rule], tie_break: str = "source"
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    """Evaluate each selected case against each original rule.

    ``source`` orders tied events by their retained source-row number.
    ``alphabetical`` reproduces alphabetical activity ordering, with the source
    row as a final tie-breaker. ``activity`` is retained as an alias. Either
    policy is an ordering convention, not evidence
    of the chronology of events sharing the same timestamp.
    """
    required = {"CaseID", "Activity", "Timestamp", "_source_row"}
    if not required.issubset(event_table.columns):
        raise ValueError(f"Missing engine input columns: {sorted(required - set(event_table.columns))}.")
    if event_table.empty or not rules:
        raise ValueError("Conformance requires at least one selected case and one rule.")
    if tie_break == "activity":
        tie_break = "alphabetical"
    if tie_break not in {"source", "alphabetical"}:
        raise ValueError("tie_break must be 'source' or 'alphabetical'.")
    if event_table[list(required)].isna().any().any():
        raise ValueError("Required engine input fields contain missing values.")
    for column in ("CaseID", "Activity"):
        if not event_table[column].map(lambda value: isinstance(value, str) and bool(value.strip())).all():
            raise ValueError(f"Engine input column {column} must contain non-empty strings.")
    if event_table.duplicated(["CaseID", "_source_row"]).any():
        raise ValueError("Source-row numbers must be unique within each case.")
    frame = event_table.copy()
    frame["Timestamp"] = pd.to_datetime(frame["Timestamp"], errors="raise")
    sort_columns = ["CaseID", "Timestamp"]
    if tie_break == "alphabetical":
        sort_columns.append("Activity")
    sort_columns.append("_source_row")
    frame = frame.sort_values(sort_columns, kind="mergesort").reset_index(drop=True)

    version = importlib.metadata.version("Declare4Py")
    _verify_engine_contract(version)
    primitives = [component for rule in rules for component in rule.components]
    case_ids, runs = _run_primitives(frame, primitives)
    component_rows: list[dict[str, Any]] = []
    pair_rows: list[dict[str, Any]] = []

    for trace_index, case_id in enumerate(case_ids):
        primitive_index = 0
        for rule in rules:
            results = []
            for component_index, primitive in enumerate(rule.components, start=1):
                description = f"{case_id}, {rule.rule_id}, component {component_index}"
                counts: dict[str, int | None] = {}
                for engine_metric, output_name in (
                    ("num_activations", "activation_count"),
                    ("num_fulfillments", "fulfilment_count"),
                    ("num_violations", "violation_count"),
                    ("num_pendings", "pending_count"),
                ):
                    with_vacuity = _nullable_count(runs[True][engine_metric].iat[trace_index, primitive_index], description)
                    without_vacuity = _nullable_count(runs[False][engine_metric].iat[trace_index, primitive_index], description)
                    if with_vacuity != without_vacuity:
                        raise ValueError(f"Engine counts changed with the vacuity setting for {description}.")
                    counts[output_name] = with_vacuity
                standard_state = _binary_state(runs[True]["state"].iat[trace_index, primitive_index], description)
                strict_state = _binary_state(runs[False]["state"].iat[trace_index, primitive_index], description)
                if strict_state > standard_state:
                    raise ValueError(f"Inconsistent engine vacuity states for {description}.")
                if primitive.is_unary:
                    if standard_state != strict_state:
                        raise ValueError(f"A unary engine state changed with the vacuity setting for {description}.")
                else:
                    if any(counts[name] is None for name in ("activation_count", "fulfilment_count", "violation_count")):
                        raise ValueError(f"Missing event-triggered engine counts for {description}.")
                    if counts["pending_count"] not in (None, 0):
                        raise ValueError(f"Pending obligations in a completed-trace evaluation for {description}.")
                    if counts["activation_count"] != counts["fulfilment_count"] + counts["violation_count"]:
                        raise ValueError(f"Inconsistent completed-trace obligation counts for {description}.")
                    if standard_state != int(counts["violation_count"] == 0):
                        raise ValueError(f"Inconsistent engine state and violation count for {description}.")
                    if counts["activation_count"] > 0 and standard_state != strict_state:
                        raise ValueError(f"An activated engine state changed with vacuity for {description}.")
                result = {
                    "CaseID": case_id,
                    "rule_id": rule.rule_id,
                    "component_id": f"{rule.rule_id}__C{component_index}",
                    "component_index": component_index,
                    "primitive_template": primitive.template,
                    "primitive_constraint": primitive.engine_constraint,
                    "activation_activity": primitive.activation_activity,
                    **counts,
                    "state_with_vacuity": standard_state,
                    "state_without_vacuity": strict_state,
                }
                component_rows.append(result)
                results.append(result)
                primitive_index += 1

            formal_satisfied = int(all(result["state_with_vacuity"] == 1 for result in results))
            if rule.is_unary:
                activation_status = "not_applicable"
                activation_count = fulfilment_count = violation_count = None
                vacuous = False
            else:
                activation_count = sum(result["activation_count"] for result in results)
                fulfilment_count = sum(result["fulfilment_count"] for result in results)
                violation_count = sum(result["violation_count"] for result in results)
                activation_status = "active" if activation_count else "inactive"
                vacuous = bool(formal_satisfied and activation_count == 0)
            outcome = "violated" if not formal_satisfied else "vacuously_satisfied" if vacuous else "satisfied"
            pair_rows.append({
                "CaseID": case_id,
                "rule_id": rule.rule_id,
                "constraint": rule.constraint,
                "template": rule.template,
                "formal_satisfied": formal_satisfied,
                "formal_outcome": outcome,
                "activation_status": activation_status,
                "activation_count": activation_count,
                "fulfilment_count": fulfilment_count,
                "violation_count": violation_count,
                "vacuously_satisfied": vacuous,
                "component_count": len(results),
            })

    pairs = pd.DataFrame.from_records(pair_rows)
    components = pd.DataFrame.from_records(component_rows)
    for column in ("activation_count", "fulfilment_count", "violation_count"):
        pairs[column] = pairs[column].astype("Int64")
        components[column] = components[column].astype("Int64")
    components["pending_count"] = components["pending_count"].astype("Int64")
    if len(pairs) != len(case_ids) * len(rules) or pairs.duplicated(["CaseID", "rule_id"]).any():
        raise ValueError("Original-rule results do not form a complete case-by-rule table.")
    metadata = {
        "engine": "Declare4Py MPDeclareAnalyzer",
        "declare4py_version": version,
        "pm4py_version": importlib.metadata.version("pm4py"),
        "pandas_version": pd.__version__,
        "known_answer_contract_checks_passed": True,
        "state_encoding": {"satisfied": 1, "not_satisfied": 0},
        "cases": len(case_ids),
        "original_rules": len(rules),
        "engine_components": len(primitives),
        "case_order": case_ids,
        "tie_break_policy": tie_break,
        "ordering_columns": sort_columns,
        "formal_satisfaction": "Conjunction of original-rule component states with consider_vacuity=True.",
        "activation_convention": (
            "For event-triggered rules, activation, fulfilment and violation counts are sums "
            "of engine component obligations, not counts of distinct events. Zero total "
            "activation denotes operational inactivity; satisfaction with zero activation "
            "is reported as vacuously_satisfied under this project convention."
        ),
        "unary_convention": (
            "Unary constraints are evaluated on every selected trace. Activation status is "
            "not_applicable and original-rule activation, fulfilment and violation counts "
            "are null; their formal state is retained."
        ),
        "negative_succession_convention": (
            "Unconditional NotSuccession(A,B) uses NotResponse(A,B); NotChainSuccession(A,B) "
            "uses NotChainResponse(A,B). A is the activation activity."
        ),
        "legal_interpretation": "Formal outcomes do not determine legal applicability or legal compliance.",
        "component_mapping": [
            {"rule_id": rule.rule_id, "template": rule.template, "constraint": rule.constraint,
             "components": [{"component_id": f"{rule.rule_id}__C{index}", **asdict(component),
                             "engine_constraint": component.engine_constraint,
                             "activation_activity": component.activation_activity}
                            for index, component in enumerate(rule.components, start=1)]}
            for rule in rules
        ],
    }
    return pairs, components, metadata
