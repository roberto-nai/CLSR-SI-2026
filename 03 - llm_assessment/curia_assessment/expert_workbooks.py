"""Create the editable Excel counterparts of the expert CSV review sheets."""

from __future__ import annotations

import csv
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


def _runtime() -> tuple[Path, Path]:
    """Use the configured runtime, or the installed Codex spreadsheet runtime."""
    dependencies = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node"
    modules = Path(os.environ.get("CURIA_SPREADSHEET_NODE_MODULES", str(dependencies / "node_modules"))).expanduser()
    executable = os.environ.get("CURIA_SPREADSHEET_NODE")
    node = Path(executable).expanduser() if executable else dependencies / "bin/node"
    if not node.is_file() and not executable:
        node = Path(shutil.which("node") or "/nonexistent/node")
    if not node.is_file() or not (modules / "@oai/artifact-tool").is_dir():
        raise RuntimeError(
            "Excel expert exports require Node.js and @oai/artifact-tool. "
            "Set CURIA_SPREADSHEET_NODE and CURIA_SPREADSHEET_NODE_MODULES to the installed runtime."
        )
    return node, modules


def write_expert_workbooks(review_dir: str | Path, *, preview_dir: str | Path | None = None) -> list[Path]:
    """Export both CSVs without changing their contents, order or annotation values.

    Workbooks are staged together so a failed export leaves existing files intact.
    Optional previews are for visual verification and remain outside reviewer packs.
    """
    review_dir = Path(review_dir).resolve()
    state_path = review_dir.parent / "_audit/expert_review_state/workbooks.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    for number in (1, 2):
        workbook_path = review_dir / f"expert_{number}.xlsx"
        if workbook_path.exists() and state.get(workbook_path.name) != hashlib.sha256(workbook_path.read_bytes()).hexdigest():
            raise ValueError(
                f"Existing Excel review has been edited: {workbook_path}. "
                "Keep that workbook and export its reviewed sheet to the corresponding CSV before regenerating; "
                "then move the edited workbook to a separate archive."
            )
    files = []
    for number in (1, 2):
        path = review_dir / f"expert_{number}.csv"
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            fields = reader.fieldnames or []
            rows = list(reader)
        if (not fields or len(fields) != len(set(fields))
                or any(None in row or any(value is None for value in row.values()) for row in rows)):
            raise ValueError(f"Malformed expert CSV: {path}.")
        if any(row.get("source_support", "").strip() for row in rows):
            raise ValueError(f"Legacy source_support annotations exist in {path}. Existing files were preserved; archive and explicitly review that column before using evidence_support. Old ratings are not translated automatically.")
        if "declare" not in fields or any(not row["declare"].strip() for row in rows):
            raise ValueError(f"Expert CSV requires each original DECLARE formula in declare: {path}. Missing formulas are not invented; existing files were preserved.")
        # Historical quotations may be read for compatibility, but never enter
        # a newly authored workbook. An empty old rating is not a new rating.
        removed = {"source", "legal_source", "quoted_source", "source_quote", "legal_basis", "source_support"}
        current_fields = [field for field in fields if field not in removed]
        if "source_support" in fields and "evidence_support" not in current_fields:
            current_fields.append("evidence_support")
        rows = [{field: row.get(field, "") for field in current_fields} for row in rows]
        fields = current_fields
        files.append({"csv": str(path), "name": path.stem, "fields": fields, "row_count": len(rows), "rows": rows})
    node, modules = _runtime()
    builder = Path(__file__).with_name("expert_workbooks.mjs")
    with tempfile.TemporaryDirectory(prefix="curia-expert-xlsx-") as temporary:
        staging = Path(temporary)
        (staging / "node_modules").symlink_to(modules, target_is_directory=True)
        shutil.copyfile(builder, staging / builder.name)
        for file in files:
            # Artifact Tool interprets a leading '=' as a formula. Escape it
            # in the temporary CSV so evidence remains literal spreadsheet text.
            literal_csv = staging / f"{file['name']}.csv"
            with literal_csv.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.writer(stream)
                matrix = [file["fields"], *[[row[field] for field in file["fields"]] for row in file.pop("rows")]]
                writer.writerows([["'" + value if value.startswith("=") else value for value in row]
                                  for row in matrix])
            file["csv"] = str(literal_csv)
        manifest = {"files": files, "output_dir": str(staging),
                    "preview_dir": str(Path(preview_dir).resolve()) if preview_dir is not None else None}
        manifest_path = staging / "manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        completed = subprocess.run(
            [str(node), str(staging / builder.name), str(manifest_path)],
            capture_output=True, text=True, timeout=240, check=False,
        )
        if completed.returncode:
            raise RuntimeError(f"Expert XLSX export failed: {completed.stderr[-3000:] or completed.stdout[-3000:]}")
        # Do not replace either workbook unless both staged exports exist.
        for file in files:
            if not (staging / f"{file['name']}.xlsx").is_file():
                raise RuntimeError(f"Excel exporter did not produce {file['name']}.xlsx.")
        destinations = []
        for file in files:
            destination = review_dir / f"{file['name']}.xlsx"
            pending = destination.with_name(destination.name + ".tmp")
            shutil.copyfile(staging / destination.name, pending)
            pending.replace(destination)
            destinations.append(destination)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        pending_state = state_path.with_name(state_path.name + ".tmp")
        pending_state.write_text(json.dumps({path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                                             for path in destinations}, indent=2) + "\n", encoding="utf-8")
        pending_state.replace(state_path)
    return destinations
