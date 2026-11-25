"""Verify the Excel review forms retain the CSV evidence and outcome colours."""

from __future__ import annotations

import csv
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from curia_assessment.expert_workbooks import _runtime, write_expert_workbooks

NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


class ExpertWorkbookTests(unittest.TestCase):
    def test_runtime_error_explains_configuration(self):
        with patch.dict("os.environ", {"CURIA_SPREADSHEET_NODE": "/missing/node",
                                       "CURIA_SPREADSHEET_NODE_MODULES": "/missing/modules"}):
            with self.assertRaisesRegex(RuntimeError, "CURIA_SPREADSHEET_NODE"):
                _runtime()

    def test_excel_matches_csv_has_editable_fields_and_dynamic_colours(self):
        try:
            _runtime()
        except RuntimeError as exc:
            self.skipTest(str(exc))
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            fields = ["assessment_id", "case_id", "case_year", "phase", "run", "model", "rule_id",
                      "status", "outcome", "declare", "requirement", "justification", "evidence_refs",
                      "document_evidence", "dossier_path", "annotation_status", "expert_outcome",
                      "judgement_correct", "evidence_support", "explanation_quality", "comments"]
            outcomes = ["compliance", "violation", "undetermined"]
            rows = [{**dict.fromkeys(fields, ""), "assessment_id": f"assessment_{i}", "case_id": "C-1/16",
                     "case_year": "2016", "phase": "1", "run": "1", "model": "test-model", "rule_id": "R01",
                     "status": "ok", "outcome": outcome, "declare": 'Response( "Act A", "Act B" )',
                     "requirement": '=Literal requirement, "quotation"\nSecond paragraph.', "justification": "Complete evidence explanation.",
                     "evidence_refs": '["REQUIREMENT","DECLARE","E0001"]',
                     "document_evidence": json.dumps([{"passage_id": f"D{j:04d}", "source_reference": "Complete original file provenance " * 8}
                                                      for j in range(50)]) if i == 0 else "[]",
                     "annotation_status": "unreviewed", "comments": "'An existing expert note."}
                    for i, outcome in enumerate(outcomes)]
            for number in (1, 2):
                with (directory / f"expert_{number}.csv").open("w", encoding="utf-8-sig", newline="") as stream:
                    writer = csv.DictWriter(stream, fieldnames=fields)
                    writer.writeheader()
                    writer.writerows(rows)
            original = [(directory / f"expert_{number}.csv").read_bytes() for number in (1, 2)]
            paths = write_expert_workbooks(directory)
            self.assertEqual(len(paths), 2)
            self.assertEqual(original, [(directory / f"expert_{number}.csv").read_bytes() for number in (1, 2)])
            for path in paths:
                with ZipFile(path) as archive:
                    self.assertIsNone(archive.testzip())
                    sheet = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
                    styles = ET.fromstring(archive.read("xl/styles.xml"))
                    shared = ET.fromstring(archive.read("xl/sharedStrings.xml"))
                    strings = ["".join(item.itertext()) for item in shared]
                    matrix = []
                    for row in sheet.findall("s:sheetData/s:row", NS):
                        cells = {}
                        for cell in row:
                            self.assertIsNone(cell.find("s:f", NS), "CSV text must remain literal, even when starting with =.")
                            value = cell.find("s:v", NS)
                            if cell.get("t") == "s":
                                text = strings[int(value.text)]
                            elif cell.get("t") == "inlineStr":
                                text = "".join(cell.find("s:is", NS).itertext())
                            else:
                                text = value.text if value is not None else ""
                            cells[cell.get("r")] = text
                        matrix.append(cells)
                    def column(index):
                        return chr(65 + index)  # This 21-column fixture fits A:U.
                    self.assertEqual([matrix[0][f"{column(i)}1"] for i in range(len(fields))], fields)
                    for row_number, expected in enumerate(rows, 2):
                        self.assertEqual([matrix[row_number - 1].get(f"{column(i)}{row_number}", "")
                                          for i in range(len(fields))], [expected[field] for field in fields])
                    pane = sheet.find("s:sheetViews/s:sheetView/s:pane", NS)
                    self.assertEqual((pane.get("xSplit"), pane.get("ySplit")), ("2", "1"))
                    phase_cell = sheet.find("s:sheetData/s:row/s:c[@r='D2']", NS)
                    self.assertNotEqual(phase_cell.get("t"), "s")
                    self.assertEqual(phase_cell.find("s:v", NS).text, "1")
                    formatting = sheet.findall("s:conditionalFormatting", NS)
                    self.assertEqual({item.get("sqref") for item in formatting}, {"I2:I4", "Q2:Q4"})
                    dxfs = styles.find("s:dxfs", NS)
                    wanted = {"compliance": "FFC6EFCE", "violation": "FFFFC7CE", "undetermined": "FFFFEB9C"}
                    for item in formatting:
                        self.assertEqual(len(item), 3)
                        for rule in item:
                            formula = rule.find("s:formula", NS).text
                            outcome = formula.split('"')[1]
                            fill = dxfs[int(rule.get("dxfId"))].find("s:fill/s:patternFill/s:bgColor", NS)
                            self.assertEqual(fill.get("rgb"), wanted[outcome])
                    self.assertEqual(len(sheet.findall("s:dataValidations/s:dataValidation", NS)), 4)
                    expert_validation = sheet.find("s:dataValidations/s:dataValidation[@sqref='Q2:Q4']/s:formula1", NS)
                    self.assertEqual(expert_validation.text, '"compliance,violation,undetermined"')
                    evidence_validation = sheet.find("s:dataValidations/s:dataValidation[@sqref='S2:S4']/s:formula1", NS)
                    self.assertEqual(evidence_validation.text, '"supported,partly_supported,unsupported,not_assessable"')
                    self.assertNotIn("not_applicable", archive.read("xl/worksheets/sheet1.xml").decode())
                    # The full index is retained, but only the substantive text
                    # columns influence wrapped row heights.
                    xfs = styles.find("s:cellXfs", NS)
                    index_cell = sheet.find("s:sheetData/s:row/s:c[@r='N2']", NS)
                    requirement_cell = sheet.find("s:sheetData/s:row/s:c[@r='K2']", NS)
                    index_alignment = xfs[int(index_cell.get("s"))].find("s:alignment", NS)
                    requirement_alignment = xfs[int(requirement_cell.get("s"))].find("s:alignment", NS)
                    self.assertIn(index_alignment.get("wrapText"), (None, "0", "false"))
                    self.assertIn(requirement_alignment.get("wrapText"), ("1", "true"))
                    data_rows = sheet.findall("s:sheetData/s:row", NS)[1:]
                    self.assertEqual(len({row.get("ht") for row in data_rows}), 1)
            # Never replace a workbook that the user or an expert has modified.
            paths[0].write_bytes(paths[0].read_bytes() + b"edited")
            modified = paths[0].read_bytes()
            with self.assertRaisesRegex(ValueError, "Existing Excel review has been edited"):
                write_expert_workbooks(directory)
            self.assertEqual(paths[0].read_bytes(), modified)

    def test_workbook_rejects_unsupported_model_or_expert_labels_without_writing_files(self):
        try:
            _runtime()
        except RuntimeError as exc:
            self.skipTest(str(exc))
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            fields = ["outcome", "expert_outcome", "declare"]
            for invalid_field in ("outcome", "expert_outcome"):
                with self.subTest(invalid_field=invalid_field):
                    for number in (1, 2):
                        with (directory / f"expert_{number}.csv").open("w", encoding="utf-8-sig", newline="") as stream:
                            writer = csv.DictWriter(stream, fieldnames=fields)
                            writer.writeheader()
                            writer.writerow({"outcome": "compliance", "expert_outcome": "", "declare": 'AtLeastOne("A")', invalid_field: "not_applicable"})
                    original = {path: path.read_bytes() for path in directory.glob("*.csv")}
                    with self.assertRaisesRegex(RuntimeError, f"Unsupported {invalid_field}"):
                        write_expert_workbooks(directory)
                    self.assertEqual({path: path.read_bytes() for path in directory.glob("*.csv")}, original)
                    self.assertFalse(list(directory.glob("*.xlsx")))

    def test_legacy_quotes_are_excluded_from_new_workbooks_without_modifying_csvs(self):
        try:
            _runtime()
        except RuntimeError as exc:
            self.skipTest(str(exc))
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "expert_review"
            directory.mkdir()
            fields = ["assessment_id", "outcome", "requirement", "source", "legal_source", "quoted_source",
                      "source_quote", "legal_basis", "source_support", "comments"]
            fields.append("declare")
            row = {field: "LEGACY_QUOTATION_SENTINEL" for field in fields}
            row.update(assessment_id="assessment_fixture", outcome="compliance", requirement="Supplied requirement.",
                       source_support="", comments="Preserve this independent comment.", declare='AtLeastOne("A")')
            for number in (1, 2):
                with (directory / f"expert_{number}.csv").open("w", encoding="utf-8-sig", newline="") as stream:
                    writer = csv.DictWriter(stream, fieldnames=fields)
                    writer.writeheader()
                    writer.writerow(row)
            before = {file: file.read_bytes() for file in directory.glob("*.csv")}
            for file in write_expert_workbooks(directory):
                with ZipFile(file) as archive:
                    shared = ET.fromstring(archive.read("xl/sharedStrings.xml"))
                    strings = ["".join(item.itertext()) for item in shared]
                    sheet = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
                    texts = []
                    for cell in sheet.findall("s:sheetData/s:row/s:c", NS):
                        if cell.get("t") == "s":
                            texts.append(strings[int(cell.find("s:v", NS).text)])
                        elif cell.get("t") == "inlineStr":
                            texts.append("".join(cell.find("s:is", NS).itertext()))
                        else:
                            value = cell.find("s:v", NS)
                            texts.append(value.text if value is not None else "")
                    self.assertNotIn("LEGACY_QUOTATION_SENTINEL", texts)
                    self.assertIn("Preserve this independent comment.", texts)
                    self.assertIn("evidence_support", texts)
                    self.assertFalse(set(fields[3:9]) & set(texts))
                    validation = sheet.find("s:dataValidations/s:dataValidation/s:formula1", NS)
                    self.assertEqual(validation.text, '"supported,partly_supported,unsupported,not_assessable"')
            self.assertEqual({file: file.read_bytes() for file in directory.glob("*.csv")}, before)

    def test_legacy_support_annotations_require_explicit_review_before_workbook_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "expert_review"
            directory.mkdir()
            fields = ["assessment_id", "outcome", "source_support", "declare"]
            for number in (1, 2):
                with (directory / f"expert_{number}.csv").open("w", encoding="utf-8-sig", newline="") as stream:
                    writer = csv.DictWriter(stream, fieldnames=fields)
                    writer.writeheader()
                    writer.writerow({"assessment_id": "assessment_fixture", "outcome": "compliance",
                                     "source_support": "supported" if number == 2 else "", "declare": 'AtLeastOne("A")'})
            before = {file: file.read_bytes() for file in directory.glob("*.csv")}
            with self.assertRaisesRegex(ValueError, "Legacy source_support annotations.*not translated automatically"):
                write_expert_workbooks(directory)
            self.assertEqual({file: file.read_bytes() for file in directory.glob("*.csv")}, before)
            self.assertFalse(list(directory.glob("*.xlsx")))

    def test_workbook_requires_original_formulas_without_fabricating_unknown_values(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "expert_review"
            directory.mkdir()
            for formula in (None, "", " "):
                with self.subTest(formula=formula):
                    fields = ["outcome", "requirement"] + (["declare"] if formula is not None else [])
                    for number in (1, 2):
                        with (directory / f"expert_{number}.csv").open("w", encoding="utf-8-sig", newline="") as stream:
                            writer = csv.DictWriter(stream, fieldnames=fields)
                            writer.writeheader()
                            writer.writerow({"outcome": "compliance", "requirement": "Supplied requirement.",
                                             **({"declare": formula} if formula is not None else {})})
                    before = {file: file.read_bytes() for file in directory.glob("*.csv")}
                    with self.assertRaisesRegex(ValueError, "original DECLARE formula.*not invented"):
                        write_expert_workbooks(directory)
                    self.assertEqual({file: file.read_bytes() for file in directory.glob("*.csv")}, before)
                    self.assertFalse(list(directory.glob("*.xlsx")))


if __name__ == "__main__":
    unittest.main()
