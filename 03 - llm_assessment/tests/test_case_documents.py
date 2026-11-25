"""Check exact CaseID document availability without reading document contents."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from curia_assessment.case_documents import case_document_prefix, index_case_documents


class CaseDocumentPrefixTests(unittest.TestCase):
    def test_prefixes_preserve_distinct_case_numbers_and_procedural_suffixes(self) -> None:
        for identifier, expected in (
            ("C-1/20", "C-1_20"), ("C-10/20", "C-10_20"),
            ("C-1/20 P", "C-1_20_P"), ("C-1/20 PPU", "C-1_20_PPU"),
            ("C-546/12 P-DEP", "C-546_12_P-DEP"),
            ("C-1/20 P(R)", "C-1_20_P(R)"),
            ("._C-1/20  /  P._", "C-1_20_P"),
        ):
            with self.subTest(identifier=identifier):
                self.assertEqual(case_document_prefix(identifier), expected)

    def test_long_prefix_matches_upstream_truncation_and_hashes_original_identifier(self) -> None:
        identifier = "C-" + "1" * 160 + "/20 P"
        expected = identifier[:130] + "_" + hashlib.sha256(identifier.encode()).hexdigest()[:12]
        prefix = case_document_prefix(identifier)
        self.assertEqual(prefix, expected)
        self.assertEqual(len(prefix), 143)
        # The original CaseID, including its suffix, determines the hash.
        self.assertNotEqual(prefix, case_document_prefix(identifier + "PU"))
        boundary = "C-" + "1" * 145 + "/20"
        self.assertEqual(len(case_document_prefix(boundary)), 150)
        self.assertEqual(case_document_prefix(boundary), boundary.replace("/", "_"))


class CaseDocumentIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name) / "documents"
        self.directory.mkdir()

    def write(self, relative: str, content: bytes = b"Downloaded document") -> None:
        path = self.directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def test_full_filename_mapping_keeps_numbers_and_procedural_suffixes_separate(self) -> None:
        for name in (
            "C-1_20_01_02.txt", "C-1_20_02_02.html", "C-10_20_01_01.pdf",
            "C-1_20_P_01_01.txt", "C-1_20_PPU_01_01.html",
        ):
            self.write(name)
        indexed = index_case_documents(self.directory)
        self.assertEqual(indexed, {
            "C-1_20": ["C-1_20_01_02.txt", "C-1_20_02_02.html"],
            "C-10_20": ["C-10_20_01_01.pdf"],
            "C-1_20_P": ["C-1_20_P_01_01.txt"],
            "C-1_20_PPU": ["C-1_20_PPU_01_01.html"],
        })
        self.assertNotIn(case_document_prefix("C-100/20"), indexed)

    def test_nested_paths_and_extension_case_are_supported_and_sorted(self) -> None:
        for name in (
            "z/C-1_20_100_101.PDF", "C-1_20_01_101.txt",
            "a/nested/C-1_20_02_101.HTML", "a/C-1_20_03_101.TXT",
        ):
            self.write(name)
        self.assertEqual(index_case_documents(self.directory), {
            "C-1_20": ["C-1_20_01_101.txt", "a/C-1_20_03_101.TXT",
                       "a/nested/C-1_20_02_101.HTML", "z/C-1_20_100_101.PDF"],
        })

    def test_original_only_and_unconverted_originals_establish_availability(self) -> None:
        self.write("C-1_20_01_01.pdf")
        self.write("C-2_20_01_01_unconverted.html")
        self.write("C-3_20_01_01_unconverted.PDF")
        self.write("C-4_20_01_01_unconverted.txt")
        indexed = index_case_documents(self.directory)
        self.assertEqual(set(indexed), {"C-1_20", "C-2_20", "C-3_20"})
        self.assertEqual(indexed["C-2_20"], ["C-2_20_01_01_unconverted.html"])

    def test_empty_files_directories_temporary_and_unsupported_files_are_ignored(self) -> None:
        self.write("C-1_20_01_01.txt", b"")
        (self.directory / "C-2_20_01_01.pdf").mkdir()
        for name in (
            "download_report_text_C_EN.json", "_curia_scraping_text_C_EN.csv", ".gitkeep",
            "C-3_20_01_01.json", "C-3_20_01_01.csv", "C-3_20_01_01.docx",
            "C-3_20_01_01.txt.tmp", "C-3_20_01_01.pdf.part", "C-3_20_01_01.htm",
            "C-3_20_1_01.txt", "C-3_20_01_1.txt", "C-3_20_01_01_extra.txt",
            "C-3_20.txt", "C-3_20_P.txt",
        ):
            self.write(name)
        self.assertEqual(index_case_documents(self.directory), {})

    def test_nonempty_regular_file_is_required_even_when_report_claims_success(self) -> None:
        self.write("download_report_text_C_EN.json", b'{"documents":[{"CaseID":"C-1/20","status":"downloaded"}]}')
        self.assertEqual(index_case_documents(self.directory), {})
        self.write("C-1_20_01_01.txt")
        self.assertEqual(index_case_documents(self.directory), {"C-1_20": ["C-1_20_01_01.txt"]})

    def test_long_sanitized_case_prefix_is_indexed_without_reinterpretation(self) -> None:
        identifier = "C-" + "1" * 160 + "/20 P(R)"
        prefix = case_document_prefix(identifier)
        name = prefix + "_01_01.txt"
        self.write(name)
        self.assertEqual(index_case_documents(self.directory), {prefix: [name]})

    def test_empty_directory_returns_empty_index(self) -> None:
        self.assertEqual(index_case_documents(self.directory), {})

    def test_missing_path_or_non_directory_has_actionable_error(self) -> None:
        ordinary_file = self.directory / "ordinary-file"
        ordinary_file.write_text("Not a directory", encoding="utf-8")
        for path in (self.directory / "missing", ordinary_file):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "case_texts_dir"):
                index_case_documents(path)


if __name__ == "__main__":
    unittest.main()
