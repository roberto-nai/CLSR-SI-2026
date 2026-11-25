// Editable expert review exports. The JSON manifest and CSVs are supplied by Python.
import fs from "node:fs/promises";
import path from "node:path";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const manifest = JSON.parse(await fs.readFile(process.argv[2], "utf8"));
const colour = {
  compliance: { fill: "#C6EFCE", font: { color: "#006100" } },
  violation: { fill: "#FFC7CE", font: { color: "#9C0006" } },
  undetermined: { fill: "#FFEB9C", font: { color: "#7F6000" } },
};
const widths = {
  assessment_id: 39, case_id: 16, case_year: 12, phase: 9, run: 8, model: 31,
  rule_id: 11, status: 12, outcome: 21, declare: 60, requirement: 65,
  justification: 85, evidence_refs: 30, document_evidence: 28, dossier_path: 52,
  annotation_status: 27, expert_outcome: 23, judgement_correct: 23,
  evidence_support: 25, explanation_quality: 25, comments: 65,
};
const inputs = ["expert_outcome", "judgement_correct", "evidence_support", "explanation_quality", "comments"];
const outcomes = new Set(Object.keys(colour));
const columnName = (index) => {
  let name = "";
  for (let n = index + 1; n; n = Math.floor((n - 1) / 26)) name = String.fromCharCode(65 + (n - 1) % 26) + name;
  return name;
};

for (const file of manifest.files) {
  if (file.fields.some(field => ["source", "legal_source", "quoted_source", "source_quote", "legal_basis", "source_support"].includes(field))) {
    throw new Error("New expert workbooks require textual requirements and evidence_support; historical source columns require explicit migration.");
  }
  if (!file.fields.includes("declare")) throw new Error("New expert workbooks require each original DECLARE formula in declare.");
  const workbook = await Workbook.fromCSV((await fs.readFile(file.csv, "utf8")).replace(/^\uFEFF/, ""), { sheetName: "Expert review" });
  const sheet = workbook.worksheets.getItemAt(0);
  for (const field of ["outcome", "expert_outcome"]) {
    const index = file.fields.indexOf(field);
    if (index < 0 || !file.row_count) continue;
    for (const [value] of sheet.getRangeByIndexes(1, index, file.row_count, 1).values) {
      if (value !== "" && value != null && !outcomes.has(value)) {
        throw new Error(`Unsupported ${field}; use compliance, violation or undetermined.`);
      }
    }
  }
  const n = file.row_count + 1;
  const range = sheet.getRangeByIndexes(0, 0, n, file.fields.length);
  sheet.showGridLines = false;
  sheet.tabColor = "#243746";
  range.format.font = { name: "Arial", size: 11, color: "#172B3A" };
  range.format.verticalAlignment = "top";
  range.format.wrapText = true;
  range.setNumberFormat("@");

  for (let i = 0; i < file.fields.length; i++) {
    const field = file.fields[i];
    const column = sheet.getRangeByIndexes(0, i, n, 1);
    column.format.columnWidth = widths[field] || 30;
    // Keep the full provenance index as literal, single-line text. Its length
    // must not drive row heights; requirements and explanations remain wrapped.
    if (field === "document_evidence") column.format.wrapText = false;
    if (file.row_count && ["case_year", "phase", "run", "explanation_quality"].includes(field)) {
      const data = sheet.getRangeByIndexes(1, i, file.row_count, 1);
      data.values = data.values.map(([value]) => [value === "" || value == null ? null : /^\d+$/.test(String(value)) ? Number(value) : value]);
      data.setNumberFormat("0");
      data.format.horizontalAlignment = "right";
    }
  }
  if (file.row_count) {
    const table = sheet.tables.add(`A1:${columnName(file.fields.length - 1)}${n}`, true, "ExpertReview");
    table.style = "TableStyleLight1";
    table.showFilterButton = true;
    const body = sheet.getRangeByIndexes(1, 0, file.row_count, file.fields.length);
    body.format.autofitRows();
    for (let row = 1; row < n; row++) {
      const fitted = sheet.getRangeByIndexes(row, 0, 1, file.fields.length);
      fitted.format.rowHeight = Math.min(409, fitted.format.rowHeight + 6);
    }
    for (const field of ["outcome", "expert_outcome"]) {
      const index = file.fields.indexOf(field);
      if (index < 0) continue;
      const data = sheet.getRangeByIndexes(1, index, file.row_count, 1);
      for (const [outcome, format] of Object.entries(colour)) {
        data.conditionalFormats.addCustom(`${columnName(index)}2="${outcome}"`, format);
      }
    }
    for (const [field, values] of Object.entries({
      expert_outcome: ["compliance", "violation", "undetermined"],
      judgement_correct: ["yes", "no", "undetermined"],
      evidence_support: ["supported", "partly_supported", "unsupported", "not_assessable"],
      explanation_quality: ["0", "1", "2", "3"],
    })) {
      const index = file.fields.indexOf(field);
      if (index >= 0) sheet.getRangeByIndexes(1, index, file.row_count, 1).dataValidation = { rule: { type: "list", values } };
    }
  }
  const header = sheet.getRangeByIndexes(0, 0, 1, file.fields.length);
  header.format.fill = "#243746";
  header.format.font = { name: "Arial", size: 11, bold: true, color: "#FFFFFF" };
  header.format.horizontalAlignment = "center";
  header.format.verticalAlignment = "center";
  header.format.rowHeight = 34;
  header.format.borders = { insideVertical: { style: "thin", color: "#FFFFFF" } };
  for (const field of inputs) {
    const index = file.fields.indexOf(field);
    if (index >= 0) sheet.getRangeByIndexes(0, index, 1, 1).format.fill = "#456C8C";
  }
  sheet.freezePanes.freezeRows(1);
  sheet.freezePanes.freezeColumns(2);
  workbook.recalculate();
  const check = await workbook.inspect({ kind: "table", range: "'Expert review'!A1:J3", include: "values,formulas", tableMaxRows: 3, tableMaxCols: 10, maxChars: 1600 });
  const errors = await workbook.inspect({ kind: "match", searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#NUM!|#NULL!|#SPILL!|#CALC!", options: { useRegex: true, maxResults: 5 }, maxChars: 1000 });
  if (manifest.preview_dir) {
    await fs.mkdir(manifest.preview_dir, { recursive: true });
    await fs.writeFile(path.join(manifest.preview_dir, `${file.name}.inspection.json`), JSON.stringify({ preview: check.ndjson, errors: errors.ndjson }, null, 2));
    for (const [name, address] of [["outcomes", "B1:I4"], ["evidence", "J1:M3"], ["annotation", "Q1:U3"]]) {
      const preview = await workbook.render({ sheetName: sheet.name, range: address, scale: 1, format: "png" });
      await fs.writeFile(path.join(manifest.preview_dir, `${file.name}.${name}.png`), new Uint8Array(await preview.arrayBuffer()));
    }
    const index = file.fields.indexOf("outcome");
    if (index >= 0 && file.row_count) {
      const values = sheet.getRangeByIndexes(1, index, file.row_count, 1).values;
      for (const outcome of outcomes) {
        const first = values.findIndex(([value]) => value === outcome);
        if (first < 0) continue;
        const row = first + 2;
        const preview = await workbook.render({ sheetName: sheet.name, range: `B${row}:I${row}`, scale: 1, format: "png" });
        await fs.writeFile(path.join(manifest.preview_dir, `${file.name}.${outcome}.png`), new Uint8Array(await preview.arrayBuffer()));
      }
    }
  }
  await (await SpreadsheetFile.exportXlsx(workbook)).save(path.join(manifest.output_dir, `${file.name}.xlsx`));
}
