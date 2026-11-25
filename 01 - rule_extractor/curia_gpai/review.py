"""Independent review sheets and descriptive summaries, without an LLM judge."""

from __future__ import annotations

import csv
import io
import json
import random
from collections import defaultdict
from pathlib import Path

from .core import collect_records, read_json, sha256, utc_now, write_json

BASE_FIELDS = ["review_id", "family", "source", "requirement", "declare"]
DISPLAY_FIELDS = BASE_FIELDS + ["model", "run"]
RATERS = {
    "lawyer_1": ["source_grounding", "normative_fidelity"],
    "lawyer_2": ["source_grounding", "normative_fidelity"],
    "technical_reviewer": ["declare_correct"],
}
REVIEW_FILES = {
    "lawyer_1": "domain_expert_law_1.csv",
    "lawyer_2": "domain_expert_law_2.csv",
    "technical_reviewer": "domain_expert_cscience.csv",
}


def safe_cell(value) -> str:
    """Prevent formula execution when an LLM output is opened in Excel."""
    text = "" if value is None else str(value)
    if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r")):
        return "'" + text
    return text


def write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Use UTF-8 with a BOM and semicolons for compatibility with regional Excel settings.
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, delimiter=";", extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: safe_cell(row.get(key, "")) for key in fields})


def dataset_hash(records: list[dict], statuses: list[dict]) -> str:
    return sha256(json.dumps({"records": records, "statuses": statuses},
                             sort_keys=True, ensure_ascii=False).encode("utf-8"))


def export_reviews(experiment: Path, destination: Path, allow_incomplete: bool = False) -> dict:
    plan, records, statuses = collect_records(experiment)
    unfinished = [s for s in statuses if s["status"] in ("not_run", "interrupted", "error")]
    if unfinished and not allow_incomplete:
        raise ValueError("Some jobs have not run, were interrupted or failed. Complete them or use --allow-incomplete.")
    if destination.exists():
        raise ValueError("The review folder already exists. Choose a different name to preserve existing judgements.")
    proposed = [r for r in records if r["reviewable"]]
    if not proposed:
        raise ValueError("There are no reviewable proposals. Check the original outputs and job outcomes.")
    random.Random(plan["blinding_seed"]).shuffle(proposed)
    destination.mkdir(parents=True)
    coordinator = destination / "coordinator"
    coordinator.mkdir()
    base_rows, mapping = [], []
    returned_models = {status["job_id"]: status.get("model_returned") for status in statuses}
    for number, record in enumerate(proposed, 1):
        identifier = f"V{number:04d}"
        model_returned = returned_models.get(record["job_id"])
        displayed_model = model_returned or record["model"]
        row = {
            "review_id": identifier, "family": record["family"],
            "source": record["source_reference"] + "\n\n" + record["source_quote"],
            "requirement": record["requirement"], "declare": record["declare"],
            "model": displayed_model,
            "run": record["repetition"],
        }
        base_rows.append(row)
        mapping.append({"review_id": identifier, "record_key": record["record_key"],
                        "provider_id": record["provider_id"], "model": record["model"],
                        "model_returned": model_returned,
                        "repetition": record["repetition"], "slot_id": record["slot_id"],
                        "family": record["family"], "format_issues": record["format_issues"]})
        if "activity_vocabulary_valid" in record:
            mapping[-1].update({
                "activity_vocabulary_valid": record["activity_vocabulary_valid"],
                "activity_validation_status": record["activity_validation_status"],
                "activity_validation_issues": record["activity_validation_issues"],
            })
    for rater, criteria in RATERS.items():
        write_csv(destination / REVIEW_FILES[rater], DISPLAY_FIELDS + criteria + ["optional_notes"], base_rows)
    write_csv(coordinator / "private_mapping.csv", list(mapping[0]), mapping)
    write_json(coordinator / "review_baseline.json", base_rows)
    write_json(coordinator / "private_mapping.json", mapping)
    write_json(coordinator / "all_slots.json", records)
    write_csv(coordinator / "run_status.csv", list(statuses[0]), statuses)
    excluded = [r for r in records if not r["reviewable"]]
    write_csv(coordinator / "non_reviewable_slots.csv",
              ["record_key", "provider_id", "model", "repetition", "slot_id", "family", "state", "run_status", "parse_status"], excluded)
    manifest = {
        "created_at_utc": utc_now(), "dataset_sha256": dataset_hash(records, statuses),
        "n_requested_slots": len(records), "n_reviewable_proposals": len(proposed),
        "n_other_slots": len(excluded), "allow_incomplete": allow_incomplete,
        "criteria": RATERS, "sheet_files": REVIEW_FILES, "ratings": ["YES", "NO"],
        "notes": "Send each reviewer only their own sheet, the source and the instructions. Keep coordinator/ private."
    }
    write_json(destination / "review_manifest.json", manifest)
    vocabulary_path = experiment / "frozen" / "activity_vocabulary.json"
    if vocabulary_path.is_file():
        activities = read_json(vocabulary_path)["activities"]
        vocabulary_text = (
            "ALLOWED ACTIVITY LABELS\n\n"
            "Use each label exactly, including capitalisation, spaces and punctuation.\n"
            "The quotation marks below delimit JSON strings and are not part of the labels.\n\n"
            + "\n".join(json.dumps(label, ensure_ascii=False) for label in activities) + "\n"
        )
        (destination / "ACTIVITY_VOCABULARY.txt").write_text(vocabulary_text, encoding="utf-8")
        notation_instructions = (
            'The dialect is Template("Exact activity label") or\n'
            'Template("First exact activity label", "Second exact activity label").\n'
            "Arguments must match the labels in ACTIVITY_VOCABULARY.txt exactly.\n"
            "A separate mechanical check records vocabulary compliance; it is not an extra human rating.\n"
        )
    else:
        notation_instructions = (
            "The dialect is Template(activity_a, activity_b), as defined in prompts/declare_templates.json.\n"
        )
    instructions = (
        "VALIDATION INSTRUCTIONS\n\n"
        "Complete your own sheet independently before discussing your judgements.\n"
        "Enter YES or NO in the rating fields; leave pending judgements blank.\n"
        "Notes are optional. Preserve the column headings, IDs and original text.\n"
        "Assess the original output without correcting proposals in the review sheets.\n\n"
        "LAWYERS\n"
        "source_grounding: is the cited source verifiable and does it support the proposed requirement?\n"
        "normative_fidelity: does the sentence preserve the provision's meaning, including essential conditions and exceptions?\n"
        "YES means that the criterion is met on the supplied evidence; NO means that it is not met.\n"
        "Rate the two criteria separately. Check excerpts against the common PDF when necessary.\n\n"
        "TECHNICAL REVIEWER\n"
        "declare_correct: does the DECLARE formula correctly formalise the stated requirement\n"
        "within its explicit scope? Provide one YES/NO judgement without separate scores.\n"
        + notation_instructions +
        "This is not the Declare4Py .decl serialisation.\n\n"
        "Save the CSV as UTF-8 with semicolon delimiters; comma and tab delimiters are also accepted on import.\n"
        "The model column is visible in each sheet; this means model identity is not blinded.\n"
        "The run column is the repetition number for that model (1, 2, 3, etc.).\n"
        "The coordinator folder contains the private record mapping; keep it private.\n"
    )
    (destination / "INSTRUCTIONS.txt").write_text(instructions, encoding="utf-8")
    return manifest


def read_sheet(path: Path, rater: str, expected_rows: list[dict]) -> dict[str, dict]:
    text = path.read_text(encoding="utf-8-sig")
    header_line = text.splitlines()[0] if text.splitlines() else ""
    delimiter = max((";", ",", "\t"), key=header_line.count)
    reader = csv.DictReader(io.StringIO(text, newline=""), delimiter=delimiter)
    required = set(BASE_FIELDS + RATERS[rater])
    if not required.issubset(reader.fieldnames or []):
        raise ValueError(f"Missing or modified column headings in {path.name}.")
    expected = {r["review_id"]: r for r in expected_rows}
    loaded = {}
    for row in reader:
        if not any(value for value in row.values()):
            continue
        identifier = row.get("review_id", "").strip()
        if identifier not in expected or identifier in loaded:
            raise ValueError(f"Unknown or duplicate ID in {path.name}: {identifier!r}.")
        for field in BASE_FIELDS:
            actual = (row.get(field) or "").replace("\r\n", "\n")
            original = safe_cell(expected[identifier][field]).replace("\r\n", "\n")
            if actual != original:
                raise ValueError(f"Original text modified in {path.name}, {identifier}, {field}.")
        for field in ("model", "run"):
            if field in expected[identifier]:
                actual = (row.get(field) or "").replace("\r\n", "\n")
                original = safe_cell(expected[identifier][field]).replace("\r\n", "\n")
                if actual != original:
                    raise ValueError(f"{field.capitalize()} identity modified in {path.name}, {identifier}.")
        votes = {}
        for criterion in RATERS[rater]:
            value = (row.get(criterion) or "").strip().upper()
            aliases = {"YES": 1, "1": 1, "NO": 0, "0": 0, "": None}
            if value not in aliases:
                raise ValueError(f"Invalid judgement in {path.name}, {identifier}, {criterion}: use YES or NO.")
            votes[criterion] = aliases[value]
        loaded[identifier] = votes
    if set(loaded) != set(expected):
        raise ValueError(f"Missing rows in {path.name}. Preserve every ID and leave pending judgements blank.")
    return loaded


def _fraction(values: list[int]) -> dict:
    return {"positive": sum(values), "rated": len(values),
            "rate": round(sum(values) / len(values), 6) if values else None}


def summarise(experiment: Path, reviews: Path, destination: Path) -> dict:
    plan, records, statuses = collect_records(experiment)
    manifest = read_json(reviews / "review_manifest.json")
    if manifest["dataset_sha256"] != dataset_hash(records, statuses):
        raise ValueError("The experimental outputs changed after export. Create new sheets without overwriting existing annotations.")
    if destination.exists():
        raise ValueError("The summary folder already exists. Choose a new name to preserve the existing version.")
    coordinator = reviews / "coordinator"
    baseline = read_json(coordinator / "review_baseline.json")
    mapping = read_json(coordinator / "private_mapping.json")
    ratings = {r: read_sheet(reviews / REVIEW_FILES[r], r, baseline) for r in RATERS}
    id_for_key = {m["record_key"]: m["review_id"] for m in mapping}
    detailed = []
    for record in records:
        identifier = id_for_key.get(record["record_key"])
        votes = {}
        for rater, criteria in RATERS.items():
            for criterion in criteria:
                votes[f"{rater}_{criterion}"] = ratings[rater][identifier][criterion] if identifier else None
        complete = identifier is not None and all(v is not None for v in votes.values())
        human_accepted = int(all(v == 1 for v in votes.values())) if complete else None
        vocabulary_valid = record.get("activity_vocabulary_valid")
        accepted = int(human_accepted == 1 and vocabulary_valid is not False) if complete else None
        detailed.append({
            "record_key": record["record_key"], "review_id": identifier or "",
            "provider_id": record["provider_id"], "model": record["model"],
            "repetition": record["repetition"], "family": record["family"],
            "state": record["state"], "run_status": record["run_status"],
            "reviewable": int(record["reviewable"]), **votes,
            "activity_vocabulary_valid": vocabulary_valid,
            "activity_validation_status": record.get("activity_validation_status", "not_checked"),
            "activity_validation_issues": record.get("activity_validation_issues", []),
            "complete_review": int(complete),
            "human_accepted_without_changes": human_accepted,
            "accepted_without_changes": accepted,
        })
    vote_fields = [f"{r}_{c}" for r, cs in RATERS.items() for c in cs]

    def group_summary(rows: list[dict], labels: dict) -> dict:
        out = {**labels, "requested_slots": len(rows),
               "reviewable_proposals": sum(r["reviewable"] for r in rows),
               "abstentions": sum(r["state"] == "abstained" and r["run_status"] == "ok" for r in rows),
               "complete_reviews": sum(r["complete_review"] for r in rows),
               "activity_vocabulary_checked": sum(
                   r["state"] == "proposed" and r["activity_vocabulary_valid"] is not None for r in rows),
               "activity_vocabulary_passed": sum(
                   r["state"] == "proposed" and r["activity_vocabulary_valid"] is True for r in rows),
               "activity_vocabulary_failed": sum(
                   r["state"] == "proposed" and r["activity_vocabulary_valid"] is False for r in rows)}
        for field in vote_fields:
            stats = _fraction([r[field] for r in rows if r[field] is not None])
            for suffix, value in stats.items():
                out[f"{field}_{suffix}"] = value
        accepted = [r["accepted_without_changes"] for r in rows if r["accepted_without_changes"] is not None]
        stats = _fraction(accepted)
        out.update({f"accepted_{key}": value for key, value in stats.items()})
        # Yield per requested slot is final only when all reviews are complete
        # and every job in the group has finished without a transport error.
        finished = all(r["run_status"] not in ("not_run", "interrupted", "error") for r in rows)
        all_rated = out["complete_reviews"] == out["reviewable_proposals"]
        out["accepted_per_requested_slot"] = round(sum(accepted) / len(rows), 6) if finished and all_rated and rows else None
        return out

    per_model_groups = defaultdict(list)
    per_run_groups = defaultdict(list)
    per_family_groups = defaultdict(list)
    for row in detailed:
        per_model_groups[(row["provider_id"], row["model"])].append(row)
        per_run_groups[(row["provider_id"], row["model"], row["repetition"])].append(row)
        per_family_groups[(row["provider_id"], row["model"], row["family"])].append(row)
    per_model = [group_summary(v, dict(zip(("provider_id", "model"), k))) for k, v in per_model_groups.items()]
    per_run = [group_summary(v, dict(zip(("provider_id", "model", "repetition"), k))) for k, v in per_run_groups.items()]
    per_family = [group_summary(v, dict(zip(("provider_id", "model", "family"), k))) for k, v in per_family_groups.items()]
    agreement = []
    for criterion in RATERS["lawyer_1"]:
        pairs = [(r[f"lawyer_1_{criterion}"], r[f"lawyer_2_{criterion}"]) for r in detailed
                 if r[f"lawyer_1_{criterion}"] is not None and r[f"lawyer_2_{criterion}"] is not None]
        n = len(pairs)
        observed = sum(a == b for a, b in pairs) / n if n else None
        p1 = sum(a for a, _ in pairs) / n if n else None
        p2 = sum(b for _, b in pairs) / n if n else None
        chance = p1*p2 + (1-p1)*(1-p2) if n else None
        kappa = (observed - chance)/(1-chance) if n and chance < 1 else None
        agreement.append({"criterion": criterion, "paired_ratings": n,
                          "agreement_rate": observed, "cohen_kappa": kappa})
    result = {
        "created_at_utc": utc_now(), "repetitions": plan["repetitions"],
        "requested_slots": len(records), "reviewable_proposals": len(mapping),
        "complete_reviews": sum(r["complete_review"] for r in detailed),
        "per_model": per_model, "per_run": per_run, "per_family": per_family,
        "legal_agreement": agreement, "job_status": statuses,
        "interpretation": [
            "Rates use non-missing ratings; each denominator is reported.",
            "Accepted requires both legal criteria from both lawyers, the technical criterion and compliance with the fixed activity vocabulary when required by the plan.",
            "The five human ratings are preserved separately from the mechanical activity-vocabulary check.",
            "Blanks are pending ratings, never automatic NO votes.",
            "Abstentions/missing outputs/transport failures are not legal or technical judgements.",
            "Replicas from one legal source are not independent legal documents.",
            "Slot IDs are positions, not an alignment of legal rules across runs.",
            "No statistical significance, distributional precision or semantic stability is inferred."
        ]
    }
    destination.mkdir(parents=True)
    write_json(destination / "summary.json", result)
    for filename, rows in (("per_model.csv", per_model), ("per_run.csv", per_run),
                           ("per_family.csv", per_family), ("legal_agreement.csv", agreement),
                           ("ratings_joined.csv", detailed)):
        write_csv(destination / filename, list(rows[0]) if rows else [], rows)
    return result
