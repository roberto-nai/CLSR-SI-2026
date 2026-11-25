"""Verify faithful rule imports using the supplied workbook and small XML fixtures."""

from __future__ import annotations

import csv
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

from curia_assessment.inputs import load_catalogue
from curia_assessment.workbook_import import convert_workbook, read_approved_rules


class WorkbookImportTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def _workbook(self, rows=None, *, shared=False, formula=False, duplicate=False, missing=False):
        """Create a minimal test fixture, not a user-facing workbook."""
        rows = rows or [["R01", 'Response("A", "B")', 'Article 1\n\nA, then "B"; qualification: è.', "A is followed by B."]]
        if duplicate:
            rows = rows + [list(rows[0])]
        headings = ["ID originale", "DECLARE", "source", "requirement"]
        if missing:
            headings[-1] = "Other field"
        matrix = [headings] + rows
        strings, xml_rows = [], []
        for number, values in enumerate(matrix, 1):
            cells = []
            for position, value in enumerate(values):
                reference = f"{chr(65 + position)}{number}"
                if formula and number == 2 and position == 2:
                    cell = f'<c r="{reference}" t="str"><f>"cached value"</f><v>{escape(value)}</v></c>'
                elif shared:
                    index = len(strings)
                    strings.append(value)
                    cell = f'<c r="{reference}" t="s"><v>{index}</v></c>'
                else:
                    cell = f'<c r="{reference}" t="inlineStr"><is><t xml:space="preserve">{escape(value)}</t></is></c>'
                cells.append(cell)
            xml_rows.append(f'<row r="{number}">' + "".join(cells) + "</row>")
        path = self.directory / "fixture.xlsx"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("xl/workbook.xml", '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Approved" sheetId="1" r:id="rId1"/></sheets></workbook>')
            archive.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"/></Relationships>')
            archive.writestr("xl/worksheets/sheet1.xml", '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>' + "".join(xml_rows) + '</sheetData></worksheet>')
            if shared:
                archive.writestr("xl/sharedStrings.xml", '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">' + "".join('<si><t xml:space="preserve">' + escape(value) + '</t></si>' for value in strings) + '</sst>')
        return path

    def test_inline_and_shared_strings_preserve_multiline_text(self) -> None:
        for shared in (False, True):
            with self.subTest(shared=shared):
                path = self._workbook(shared=shared)
                records, metadata, provenance = read_approved_rules(path)
                self.assertEqual(records[0]["source"], 'Article 1\n\nA, then "B"; qualification: è.')
                self.assertEqual(metadata[0]["source_row"], 2)
                self.assertEqual(provenance["column_mapping"]["ID originale"], "ID")
                self.assertFalse(provenance["text_modified"])

    def test_csv_round_trip_and_script_catalogue_use_one_formulation(self) -> None:
        path = self._workbook(shared=True)
        output = self.directory / "rules.csv"
        manifest = convert_workbook(path, output)
        source_records, _, _ = read_approved_rules(path)
        with output.open(encoding="utf-8", newline="") as stream:
            self.assertEqual(list(csv.DictReader(stream)), source_records)
        variants = load_catalogue(output)
        self.assertEqual(len(variants), 1)
        self.assertEqual(variants[0]["variant_id"], "R01")
        self.assertTrue(manifest["csv_round_trip_verified"])
        self.assertTrue((self.directory / "approved_rule_metadata.csv").is_file())

    def test_formula_cells_are_rejected_even_with_cached_text(self) -> None:
        with self.assertRaisesRegex(ValueError, "contains a formula"):
            read_approved_rules(self._workbook(formula=True))

    def test_missing_columns_and_empty_required_text_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "exactly one worksheet"):
            read_approved_rules(self._workbook(missing=True))
        with self.assertRaisesRegex(ValueError, "requirement must contain"):
            read_approved_rules(self._workbook(rows=[["R01", 'AtLeastOne("A")', "Article 1", ""]]))

    def test_duplicate_ids_and_unknown_sheets_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate rule"):
            read_approved_rules(self._workbook(duplicate=True))
        with self.assertRaisesRegex(ValueError, "Worksheet not found"):
            read_approved_rules(self._workbook(), "Missing")

    def test_decl_mismatch_stops_before_writing_output(self) -> None:
        path = self._workbook()
        model = self.directory / "rules.decl"
        model.write_text('ID\tDECLARE\nR01\tResponse("A", "C")\n', encoding="utf-8")
        output = self.directory / "rules.csv"
        with self.assertRaisesRegex(ValueError, "differ from the DECLARE"):
            convert_workbook(path, output, model)
        self.assertFalse(output.exists())

    def test_supplied_workbook_has_nineteen_exactly_preserved_rules(self) -> None:
        project = Path(__file__).resolve().parent.parent
        path = project / "data" / "_old" / "approved_rules.xlsx"
        records, metadata, provenance = read_approved_rules(path, "Foglio1")
        self.assertEqual(len(records), 19)
        self.assertEqual([record["ID"] for record in records], [f"R{number:02d}" for number in range(1, 20)])
        self.assertEqual(sum(row["proposal_count"] for row in metadata), 46)
        output = self.directory / "rules.csv"
        manifest = convert_workbook(path, output, project / "data" / "regole_c.decl", "Foglio1")
        with output.open(encoding="utf-8", newline="") as stream:
            self.assertEqual(list(csv.DictReader(stream)), records)
        self.assertEqual(len(load_catalogue(output)), 19)
        self.assertTrue(manifest["declare_file_checked"])
        self.assertEqual(json.loads((self.directory / "rules_import_manifest.json").read_text())["row_count"], 19)
        self.assertFalse(provenance["text_modified"])


if __name__ == "__main__":
    unittest.main()
