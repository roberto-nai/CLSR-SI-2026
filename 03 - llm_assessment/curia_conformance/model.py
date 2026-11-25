"""Read unconditional DECLARE rules and resolve their engine components.

Rule identifiers and activity labels are preserved. Boolean equivalents are
resolved into templates supported by Declare4Py's MP-DECLARE analyser. The
component structure also defines this project's operational activation counts;
it is not a claim that every equivalent formulation has identical vacuity.
"""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass
from pathlib import Path


ENGINE_TEMPLATE_NAMES = {
    "Existence": "Existence",
    "Absence": "Absence",
    "Exactly": "Exactly",
    "Init": "Init",
    "End": "End",
    "RespondedExistence": "Responded Existence",
    "Response": "Response",
    "AlternateResponse": "Alternate Response",
    "ChainResponse": "Chain Response",
    "Precedence": "Precedence",
    "AlternatePrecedence": "Alternate Precedence",
    "ChainPrecedence": "Chain Precedence",
    "NotRespondedExistence": "Not Responded Existence",
    "NotResponse": "Not Response",
    "NotPrecedence": "Not Precedence",
    "NotChainResponse": "Not Chain Response",
    "NotChainPrecedence": "Not Chain Precedence",
}

UNARY_TEMPLATES = frozenset({"Existence", "Absence", "Exactly", "Init", "End"})
CARDINALITY_TEMPLATES = frozenset({"Existence", "Absence", "Exactly"})
COMPOSITE_TEMPLATES = frozenset(
    {"CoExistence", "Succession", "AlternateSuccession", "ChainSuccession",
     "NotCoExistence", "NotSuccession", "NotChainSuccession"}
)
_CANONICAL_TEMPLATES = (
    set(ENGINE_TEMPLATE_NAMES) | set(COMPOSITE_TEMPLATES)
    | {"AtLeastOne", "AtMostOne"}
)
_ALIASES = {
    re.sub(r"[\s_-]+", "", name).lower(): name
    for name in _CANONICAL_TEMPLATES
}


@dataclass(frozen=True)
class Primitive:
    """One unconditional constraint passed to the Boolean engine."""

    template: str
    activities: tuple[str, ...]
    cardinality: int | None = None

    @property
    def is_unary(self) -> bool:
        return self.template in UNARY_TEMPLATES

    @property
    def engine_name(self) -> str:
        name = ENGINE_TEMPLATE_NAMES[self.template]
        return f"{name}{self.cardinality}" if self.cardinality is not None else name

    @property
    def engine_constraint(self) -> str:
        suffix = " | |" if self.is_unary else " | | |"
        return f"{self.engine_name}[{', '.join(self.activities)}]{suffix}"

    @property
    def activation_activity(self) -> str | None:
        if self.is_unary:
            return None
        index = 1 if "Precedence" in self.template else 0
        return self.activities[index]


@dataclass(frozen=True)
class Rule:
    """One supplied rule, retaining its identifier and original representation."""

    rule_id: str
    template: str
    activities: tuple[str, ...]
    constraint: str
    components: tuple[Primitive, ...]

    @property
    def is_unary(self) -> bool:
        return len(self.components) == 1 and self.components[0].is_unary


def _resolve_template(name: str) -> tuple[str, int | None]:
    normalised = re.sub(r"[\s_-]+", "", name).lower()
    cardinal = re.fullmatch(r"(existence|absence|exactly)([0-9]*)", normalised)
    if cardinal:
        canonical = _ALIASES[cardinal.group(1)]
        number = int(cardinal.group(2)) if cardinal.group(2) else 1
        if number < 1:
            raise ValueError("DECLARE cardinalities must be positive integers.")
        if canonical == "Existence" and number == 1:
            return "AtLeastOne", 1
        if canonical == "Absence" and number == 2:
            return "AtMostOne", 2
        return canonical, number
    if normalised not in _ALIASES:
        raise ValueError(f"Unsupported DECLARE template: {name!r}.")
    canonical = _ALIASES[normalised]
    cardinality = {"AtLeastOne": 1, "AtMostOne": 2}.get(canonical)
    return canonical, cardinality


def _validate_activity(activity: object) -> str:
    if not isinstance(activity, str) or not activity.strip():
        raise ValueError("Every DECLARE argument must be a non-empty activity label.")
    if activity != activity.strip():
        raise ValueError(
            f"Activity label has leading or trailing whitespace: {activity!r}. "
            "Labels are not silently normalised."
        )
    if any(character in activity for character in ("[", "]", "|", ",", "\n", "\r", "\t")):
        raise ValueError(
            f"Activity label cannot be represented unambiguously in the supported "
            f"Declare4Py serialisation: {activity!r}."
        )
    return activity


def _components(
    template: str, activities: tuple[str, ...], cardinality: int | None
) -> tuple[Primitive, ...]:
    if template == "AtLeastOne":
        return (Primitive("Existence", activities, 1),)
    if template == "AtMostOne":
        return (Primitive("Absence", activities, 2),)
    if template in ENGINE_TEMPLATE_NAMES:
        return (Primitive(template, activities, cardinality),)

    if template == "CoExistence":
        return (
            Primitive("RespondedExistence", activities),
            Primitive("RespondedExistence", tuple(reversed(activities))),
        )
    if template == "NotCoExistence":
        return (
            Primitive("NotRespondedExistence", activities),
            Primitive("NotRespondedExistence", tuple(reversed(activities))),
        )
    if template in {"Succession", "AlternateSuccession", "ChainSuccession"}:
        prefix = template.removesuffix("Succession")
        return (
            Primitive(f"{prefix}Response", activities),
            Primitive(f"{prefix}Precedence", activities),
        )
    if template == "NotSuccession":
        return (Primitive("NotResponse", activities),)
    if template == "NotChainSuccession":
        return (Primitive("NotChainResponse", activities),)
    raise ValueError(f"No engine resolution is defined for {template}.")


def _make_rule(rule_id: str, representation: str, notation: str) -> Rule:
    if not rule_id or rule_id != rule_id.strip() or any(c.isspace() for c in rule_id):
        raise ValueError(f"Rule identifier must be non-empty and contain no whitespace: {rule_id!r}.")
    source = representation.strip()
    if notation == "quoted":
        match = re.fullmatch(r"([A-Za-z][A-Za-z0-9 _-]*)\((.*)\)", source)
        if not match:
            raise ValueError(f"Rule {rule_id}: invalid quoted DECLARE representation.")
        try:
            arguments = json.loads(f"[{match.group(2)}]")
        except json.JSONDecodeError as exc:
            raise ValueError(f"Rule {rule_id}: arguments must be double-quoted JSON strings.") from exc
    else:
        pieces = source.split("|")
        if any(piece.strip() for piece in pieces[1:]):
            raise ValueError(
                f"Rule {rule_id}: data or time conditions are not supported. "
                "Only unconditional DECLARE constraints can be evaluated."
            )
        match = re.fullmatch(r"([A-Za-z][A-Za-z0-9 _-]*)\[(.*)\]", pieces[0].strip())
        if not match:
            raise ValueError(f"Rule {rule_id}: invalid native DECLARE representation.")
        arguments = [item.strip() for item in match.group(2).split(",")]

    template, cardinality = _resolve_template(match.group(1).strip())
    activities = tuple(_validate_activity(argument) for argument in arguments)
    unary = template in UNARY_TEMPLATES or template in {"AtLeastOne", "AtMostOne"}
    expected = 1 if unary else 2
    if len(activities) != expected:
        raise ValueError(f"Rule {rule_id}: {template} requires {expected} activity argument(s).")
    if not unary and activities[0] == activities[1]:
        raise ValueError(
            f"Rule {rule_id}: identical arguments in a binary constraint are unsupported "
            "by this verified engine configuration."
        )
    components = _components(template, activities, cardinality)
    return Rule(rule_id, template, activities, source, components)


def load_rules(path: str | Path) -> list[Rule]:
    """Read the supplied ID/DECLARE TSV or a native unconditional .decl file.

    TSV identifiers are retained. Native constraints receive R01, R02, and so
    forth in source order. Native activity declarations are accepted, but data
    bindings, attribute definitions and non-empty conditions are rejected.
    """
    source_path = Path(path)
    content = source_path.read_text(encoding="utf-8-sig")
    meaningful = [line for line in content.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    if not meaningful:
        raise ValueError("The rule file is empty.")
    rules: list[Rule] = []
    if "\t" in meaningful[0]:
        table = csv.DictReader(io.StringIO("\n".join(meaningful)), delimiter="\t")
        if table.fieldnames != ["ID", "DECLARE"]:
            raise ValueError("The rule table must contain exactly the columns ID and DECLARE.")
        for row_number, row in enumerate(table, start=2):
            if None in row or row.get("ID") is None or row.get("DECLARE") is None:
                raise ValueError(f"Malformed rule-table row {row_number}.")
            if not row["ID"] and not row["DECLARE"]:
                continue
            rules.append(_make_rule(row["ID"], row["DECLARE"], "quoted"))
    else:
        for line_number, line in enumerate(content.splitlines(), start=1):
            text = line.strip()
            if not text or text.startswith("#"):
                continue
            if text.startswith("activity "):
                _validate_activity(text[len("activity "):])
                continue
            if text.startswith("bind ") or ":" in text.split("[", 1)[0]:
                raise ValueError(f"Line {line_number}: data declarations are unsupported.")
            rule_id = f"R{len(rules) + 1:02d}"
            try:
                rules.append(_make_rule(rule_id, text, "native"))
            except ValueError as exc:
                raise ValueError(f"Line {line_number}: {exc}") from exc
    if not rules:
        raise ValueError("The file contains no supported DECLARE constraints.")
    identifiers = [rule.rule_id for rule in rules]
    if len(identifiers) != len(set(identifiers)):
        repeated = sorted({identifier for identifier in identifiers if identifiers.count(identifier) > 1})
        raise ValueError(f"Duplicate rule identifiers: {', '.join(repeated)}.")
    return rules


def primitive_model_text(rules: list[Rule]) -> str:
    """Return the resolved engine model, with original-rule mapping comments."""
    lines = ["# Resolved unconditional model for Declare4Py."]
    for rule in rules:
        for index, primitive in enumerate(rule.components, start=1):
            lines.append(f"# {rule.rule_id} component {index}: {rule.constraint}")
            lines.append(primitive.engine_constraint)
    return "\n".join(lines) + "\n"
