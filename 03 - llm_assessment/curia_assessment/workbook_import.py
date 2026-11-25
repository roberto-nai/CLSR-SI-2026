"""Read approved rule text from an XLSX file without editing the workbook."""

from __future__ import annotations

import csv
import hashlib
import json
import posixpath
import re
import zipfile
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from curia_conformance.model import load_rules


MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
OFFICE_RELATIONSHIPS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_RELATIONSHIPS = "http://schemas.openxmlformats.org/package/2006/relationships"
FIELDS = ["ID", "DECLARE", "source", "requirement"]
HEADER_NAMES = {
    "ID originale": "ID", "ID": "ID", "DECLARE": "DECLARE",
    "source": "source", "requirement": "requirement",
    "review_id con quattro Y": "review_ids", "review_ids": "review_ids",
    "N. proposte": "proposal_count", "proposal_count": "proposal_count",
}


@dataclass(frozen=True)
class Cell:
    """The source cell's text and type, before any catalogue interpretation."""

    text: str
    kind: str
    formula: bool


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _text(element: ET.Element | None) -> str:
    if element is None:
        return ""
    # Rich-text runs contribute their visible text; phonetic runs do not.
    pieces = []
    for child in element:
        if child.tag == f"{{{MAIN}}}t":
            pieces.append(child.text or "")
        elif child.tag == f"{{{MAIN}}}r":
            pieces.extend(node.text or "" for node in child.findall(f"{{{MAIN}}}t"))
    return "".join(pieces)


def _sheet_entries(archive: zipfile.ZipFile) -> list[tuple[str, str]]:
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    targets = {}
    for relationship in relationships.findall(f"{{{PACKAGE_RELATIONSHIPS}}}Relationship"):
        if relationship.get("TargetMode") == "External":
            continue
        target = relationship.get("Target", "")
        resolved = posixpath.normpath(target.lstrip("/") if target.startswith("/") else posixpath.join("xl", target))
        if resolved.startswith("../") or not resolved.startswith("xl/"):
            raise ValueError("A workbook relationship points outside the supported XLSX structure.")
        targets[relationship.get("Id")] = resolved
    entries = []
    for sheet in workbook.findall(f"{{{MAIN}}}sheets/{{{MAIN}}}sheet"):
        name = sheet.get("name")
        relationship_id = sheet.get(f"{{{OFFICE_RELATIONSHIPS}}}id")
        if not name or relationship_id not in targets:
            raise ValueError("A worksheet has no usable name or internal relationship.")
        entries.append((name, targets[relationship_id]))
    if not entries:
        raise ValueError("The workbook contains no worksheets.")
    if len({name for name, _ in entries}) != len(entries):
        raise ValueError("Worksheet names must be unique.")
    return entries


def _read_sheet(archive: zipfile.ZipFile, path: str, shared: list[str]) -> list[tuple[int, dict[str, Cell]]]:
    sheet = ET.fromstring(archive.read(path))
    rows = []
    for row in sheet.findall(f"{{{MAIN}}}sheetData/{{{MAIN}}}row"):
        number = int(row.get("r", "0"))
        if number < 1:
            raise ValueError("Every worksheet row requires a positive source row number.")
        cells = {}
        for element in row.findall(f"{{{MAIN}}}c"):
            reference = element.get("r", "")
            match = re.fullmatch(r"([A-Z]+)([1-9][0-9]*)", reference)
            if not match or int(match.group(2)) != number:
                raise ValueError("A worksheet cell has an invalid source reference.")
            column = match.group(1)
            if column in cells:
                raise ValueError("Duplicate worksheet cell references are not supported.")
            kind = element.get("t", "n")
            value_element = element.find(f"{{{MAIN}}}v")
            value = value_element.text or "" if value_element is not None else ""
            if kind == "s":
                try:
                    index = int(value)
                    if index < 0:
                        raise ValueError("Negative shared-string index")
                    value = shared[index]
                except (ValueError, IndexError) as exc:
                    raise ValueError(f"Invalid shared-string reference at {reference}.") from exc
            elif kind == "inlineStr":
                value = _text(element.find(f"{{{MAIN}}}is"))
            cells[column] = Cell(value, kind, element.find(f"{{{MAIN}}}f") is not None)
        if any(cell.text or cell.formula for cell in cells.values()):
            rows.append((number, cells))
    if len({number for number, _ in rows}) != len(rows):
        raise ValueError("Duplicate worksheet row numbers are not supported.")
    return rows


def _headers(cells: dict[str, Cell]) -> tuple[dict[str, str], dict[str, str]]:
    columns, original = {}, {}
    for column, cell in cells.items():
        if cell.formula:
            raise ValueError("Header cells must contain text, not formulae.")
        field = HEADER_NAMES.get(cell.text)
        if field is not None:
            if field in columns:
                raise ValueError(f"The worksheet contains duplicate columns for {field}.")
            columns[field] = column
            original[field] = cell.text
    return columns, original


def read_approved_rules(path: str | Path, sheet_name: str | None = None) -> tuple[list[dict[str, str]], list[dict[str, Any]], dict[str, Any]]:
    """Recover one supplied source–requirement pair per rule without rewriting it."""
    path = Path(path)
    try:
        with zipfile.ZipFile(path) as archive:
            shared = []
            if "xl/sharedStrings.xml" in archive.namelist():
                root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
                shared = [_text(item) for item in root.findall(f"{{{MAIN}}}si")]
            entries = _sheet_entries(archive)
            if sheet_name is not None:
                entries = [entry for entry in entries if entry[0] == sheet_name]
                if not entries:
                    raise ValueError(f"Worksheet not found: {sheet_name}.")
            candidates = []
            for name, target in entries:
                rows = _read_sheet(archive, target, shared)
                if not rows:
                    continue
                columns, original = _headers(rows[0][1])
                if set(FIELDS).issubset(columns):
                    candidates.append((name, rows, columns, original))
            if len(candidates) != 1:
                raise ValueError("Expected exactly one worksheet with ID, DECLARE, source and requirement columns; specify --sheet if necessary.")
            name, rows, columns, original = candidates[0]
            records, metadata = [], []
            for row_number, cells in rows[1:]:
                selected = {field: cells.get(column, Cell("", "s", False)) for field, column in columns.items()}
                if not any(cell.text or cell.formula for cell in selected.values()):
                    continue
                if any(cell.formula for cell in selected.values()):
                    raise ValueError(f"Worksheet row {row_number} contains a formula in an imported field; supply explicit approved values.")
                record = {}
                for field in FIELDS:
                    cell = selected[field]
                    if cell.kind not in {"s", "inlineStr", "str"} or not cell.text.strip():
                        raise ValueError(f"Worksheet row {row_number}: {field} must contain non-empty text.")
                    record[field] = cell.text
                identifier = record["ID"]
                if identifier != identifier.strip() or any(character.isspace() for character in identifier):
                    raise ValueError(f"Worksheet row {row_number}: rule IDs must not contain whitespace.")
                count_cell = selected.get("proposal_count", Cell("", "s", False))
                proposal_count = None
                if count_cell.text:
                    try:
                        value = Decimal(count_cell.text)
                        if not value.is_finite() or value < 0 or value != value.to_integral_value():
                            raise InvalidOperation
                        proposal_count = int(value)
                    except InvalidOperation as exc:
                        raise ValueError(f"Worksheet row {row_number}: proposal_count must be a non-negative integer.") from exc
                review_cell = selected.get("review_ids", Cell("", "s", False))
                if review_cell.text and review_cell.kind not in {"s", "inlineStr", "str"}:
                    raise ValueError(f"Worksheet row {row_number}: review identifiers must be text.")
                records.append(record)
                metadata.append({
                    "ID": identifier, "review_ids": review_cell.text, "proposal_count": proposal_count,
                    "source_sheet": name, "source_row": row_number,
                })
    except (zipfile.BadZipFile, KeyError, ET.ParseError) as exc:
        raise ValueError("The input is not a readable XLSX workbook with supported worksheet relationships.") from exc
    if not records:
        raise ValueError("The selected worksheet contains no approved rule records.")
    identifiers = [record["ID"] for record in records]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("The worksheet contains duplicate rule identifiers.")
    return records, metadata, {
        "worksheet": name, "header_row": rows[0][0], "row_count": len(records),
        "column_mapping": {original[field]: field for field in original}, "text_modified": False,
    }


def convert_workbook(input_path: str | Path, output_path: str | Path,
                     declare_path: str | Path | None = None, sheet_name: str | None = None) -> dict[str, Any]:
    """Export UTF-8 CSV records and provenance, after all content checks pass."""
    input_path, output_path = Path(input_path).resolve(), Path(output_path).resolve()
    if input_path == output_path:
        raise ValueError("The CSV output must not overwrite the workbook.")
    records, metadata, provenance = read_approved_rules(input_path, sheet_name)
    reference_hash = None
    if declare_path is not None:
        reference_path = Path(declare_path).resolve()
        reference = {rule.rule_id: rule.constraint for rule in load_rules(reference_path)}
        supplied = {record["ID"]: record["DECLARE"] for record in records}
        if supplied != reference:
            mismatches = sorted(identifier for identifier in set(supplied) | set(reference)
                                if supplied.get(identifier) != reference.get(identifier))
            raise ValueError("Workbook rules differ from the DECLARE file: " + ", ".join(mismatches))
        reference_hash = _file_hash(reference_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path = output_path.parent / "approved_rule_metadata.csv"
    manifest_path = output_path.parent / "rules_import_manifest.json"
    if output_path in {metadata_path, manifest_path}:
        raise ValueError("The requested CSV filename conflicts with an import sidecar.")
    with output_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(records)
    with output_path.open(encoding="utf-8", newline="") as stream:
        if list(csv.DictReader(stream)) != records:
            raise ValueError("CSV round-trip verification failed.")
    with metadata_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["ID", "review_ids", "proposal_count", "source_sheet", "source_row"])
        writer.writeheader()
        writer.writerows(metadata)
    manifest = {
        **provenance, "input_file": input_path.name, "input_sha256": _file_hash(input_path),
        "output_file": output_path.name, "output_sha256": _file_hash(output_path),
        "metadata_file": metadata_path.name, "metadata_sha256": _file_hash(metadata_path),
        "encoding": "UTF-8", "delimiter": ",", "output_columns": FIELDS,
        "declare_file_checked": declare_path is not None, "declare_file_sha256": reference_hash,
        "csv_round_trip_verified": True,
        "interpretation": "Supplied approved text is preserved; import performs no new legal validation.",
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest
