"""Verify frozen document passages, format fallbacks and explicit excerpt bounds."""

from __future__ import annotations

import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from curia_assessment.text_evidence import build_document_evidence


class DocumentEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.cases = [{"case_id": "C-1/20"}]

    def write(self, relative: str, content: str | bytes) -> bytes:
        path = self.directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        data = content.encode("utf-8") if isinstance(content, str) else content
        path.write_bytes(data)
        return data

    def build(self, *, cases=None, document_limit=None, case_limit=None, **options):
        return build_document_evidence(
            cases if cases is not None else self.cases, self.directory,
            max_document_characters=document_limit, max_case_characters=case_limit,
            **options,
        )

    def pdf_extract(self, texts):
        def extract(*args, **kwargs):
            text = texts[kwargs["input"]]
            return subprocess.CompletedProcess([], 0, text.encode("utf-8"), b"")
        return patch("curia_assessment.text_evidence.subprocess.run", side_effect=extract)

    def sidecars(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return Path(temporary.name)

    def test_txt_is_preferred_to_originals_without_duplicating_document_evidence(self) -> None:
        content = self.write("C-1_20_01_01.txt", "The supplied document text.")
        self.write("C-1_20_01_01.html", "<p>Original HTML text.</p>")
        self.write("C-1_20_01_01.pdf", b"An original PDF that must not be parsed.")
        evidence, audit = self.build()
        self.assertEqual(len(evidence["C-1/20"]), 1)
        self.assertEqual(evidence["C-1/20"][0]["text"], "The supplied document text.")
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["source_file"], "C-1_20_01_01.txt")
        self.assertEqual(audit[0]["source_sha256"], hashlib.sha256(content).hexdigest())
        self.assertTrue(audit[0]["included"])
        self.assertFalse(audit[0]["truncated"])
        self.assertEqual(audit[0]["omitted_characters"], 0)

    def test_html_fallback_uses_visible_text_and_decodes_entities(self) -> None:
        self.write("C-1_20_01_01.html", """<html><head><title>Hidden title</title>
            <style>Hidden styles</style></head><body><p>First &amp; second.</p>
            <script>Hidden instructions</script><p>A <b>recorded</b> act.</p></body></html>""")
        evidence, audit = self.build()
        text = "\n".join(passage["text"] for passage in evidence["C-1/20"])
        self.assertIn("First & second.", text)
        self.assertIn("A recorded act.", text)
        self.assertNotIn("Hidden", text)
        self.assertEqual(audit[0]["source_format"], "html")

    def test_empty_or_unreadable_txt_is_audited_before_original_fallback(self) -> None:
        for content, expected_status in ((b" \n\t", "empty"), (b"\xff\xfe\xff", "unreadable")):
            with self.subTest(content=content):
                self.write("C-1_20_01_01.txt", content)
                self.write("C-1_20_01_01.html", "<p>Readable original.</p>")
                evidence, audit = self.build()
                self.assertEqual(evidence["C-1/20"][0]["text"], "Readable original.")
                self.assertEqual([row["status"] for row in audit], [expected_status, "included"])
                self.assertFalse(audit[0]["included"])
                self.assertTrue(audit[0]["error"])
                self.assertEqual(audit[0]["source_sha256"], hashlib.sha256(content).hexdigest())
                self.assertTrue(audit[1]["included"])

    def test_pdf_fallback_extracts_the_same_frozen_bytes_that_are_hashed(self) -> None:
        content = self.write("C-1_20_01_01.pdf", b"%PDF-1.4 example source bytes")
        result = subprocess.CompletedProcess([], 0, b"Extracted PDF document.\n", b"")
        with patch("curia_assessment.text_evidence.shutil.which", return_value="/tools/pdftotext"), \
                patch("curia_assessment.text_evidence.subprocess.run", return_value=result) as extract:
            evidence, audit = self.build()
        self.assertEqual(evidence["C-1/20"][0]["text"], "Extracted PDF document.")
        self.assertEqual(extract.call_args.kwargs["input"], content)
        self.assertEqual(extract.call_args.args[0][-2:], ["-", "-"])
        self.assertEqual(audit[0]["source_sha256"], hashlib.sha256(content).hexdigest())

    def test_missing_pdf_extractor_or_unreadable_documents_fail_before_model_execution(self) -> None:
        self.write("C-1_20_01_01.pdf", b"%PDF-1.4 source")
        with patch("curia_assessment.text_evidence.shutil.which", return_value=None):
            with self.assertRaisesRegex(ValueError, "C-1/20.*pdftotext"):
                self.build()
        (self.directory / "C-1_20_01_01.pdf").unlink()
        self.write("C-1_20_01_01.txt", " \n\t")
        with self.assertRaisesRegex(ValueError, "No readable document text.*C-1/20"):
            self.build()

    def test_case_budget_is_shared_across_all_documents_with_explicit_head_tail_ranges(self) -> None:
        first = "abcdefghij" + "X" * 20 + "qrstuvwxyz"
        self.write("C-1_20_01_03.txt", first)
        self.write("C-1_20_02_03.txt", "0123456789")
        self.write("C-1_20_03_03.txt", "A" * 40)
        evidence, audit = self.build(document_limit=30, case_limit=50)
        passages = evidence["C-1/20"]
        self.assertEqual([row["included_characters"] for row in audit], [20, 10, 20])
        self.assertEqual([row["omitted_characters"] for row in audit], [20, 0, 20])
        self.assertEqual(sum(len(passage["text"]) for passage in passages), 50)
        self.assertTrue(all(row["included"] for row in audit))
        self.assertEqual(passages[0]["text"], "abcdefghij")
        self.assertEqual(passages[1]["text"], "qrstuvwxyz")
        self.assertIn("characters 1–10 of 40", passages[0]["source_reference"])
        self.assertIn("characters 31–40 of 40", passages[1]["source_reference"])
        self.assertIn("20 document characters omitted", passages[0]["source_reference"])
        self.assertEqual([p["passage_id"] for p in passages], [f"D{index:04d}" for index in range(1, 6)])
        self.assertIn("max_document_characters", audit[0]["truncation_reason"])
        self.assertIn("max_case_characters", audit[0]["truncation_reason"])

    def test_passages_never_exceed_twelve_thousand_characters_and_none_limits_include_full_text(self) -> None:
        text = "A" * 26000
        self.write("C-1_20_01_01.txt", text)
        evidence, audit = self.build()
        passages = evidence["C-1/20"]
        self.assertEqual([len(passage["text"]) for passage in passages], [12000, 12000, 2000])
        self.assertEqual("".join(passage["text"] for passage in passages), text)
        self.assertFalse(audit[0]["truncated"])
        bounded, bounded_audit = self.build(document_limit=24000, case_limit=120000)
        self.assertEqual([len(p["text"]) for p in bounded["C-1/20"]], [12000, 12000])
        self.assertEqual(bounded_audit[0]["included_characters"], 24000)
        self.assertEqual(bounded_audit[0]["omitted_characters"], 2000)

    def test_budget_too_small_to_represent_every_document_is_an_explicit_error(self) -> None:
        self.write("C-1_20_01_02.txt", "First.")
        self.write("C-1_20_02_02.txt", "Second.")
        with self.assertRaisesRegex(ValueError, "max_case_characters.*every|per readable document"):
            self.build(case_limit=1)

    def test_case_and_document_order_are_deterministic_and_other_cases_do_not_leak(self) -> None:
        self.write("z/C-1_20_02_02.txt", "Second document.")
        self.write("a/C-1_20_01_02.txt", "First document.")
        self.write("C-10_20_01_01.txt", "Different case.")
        self.write("C-1_20_P_01_01.txt", "Appeal case.")
        cases = self.cases + [{"case_id": "C-10/20"}]
        evidence, audit = self.build(cases=cases)
        self.assertEqual((evidence, audit), self.build(cases=list(reversed(cases))))
        self.assertEqual([p["text"] for p in evidence["C-1/20"]], ["First document.", "Second document."])
        self.assertEqual([p["text"] for p in evidence["C-10/20"]], ["Different case."])

    def test_used_document_changes_change_the_frozen_hash_and_text(self) -> None:
        self.write("C-1_20_01_01.txt", "First version.")
        original, first_audit = self.build()
        self.write("C-1_20_01_01.txt", "Second version.")
        changed, second_audit = self.build()
        self.assertNotEqual(first_audit[0]["source_sha256"], second_audit[0]["source_sha256"])
        self.assertNotEqual(original, changed)

    def test_invalid_limits_or_case_identifiers_are_rejected(self) -> None:
        for name in ("document_limit", "case_limit"):
            for value in (0, -1, True, False, 10.0, "10"):
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    self.build(**{name: value})
        for cases in ([], [{"case_id": ""}], [{"case_id": 1}], self.cases * 2):
            with self.subTest(cases=cases), self.assertRaises(ValueError):
                self.build(cases=cases)

    def test_full_text_exact_duplicates_are_collapsed_with_all_source_associations(self) -> None:
        text = "Complete source text.\n" * 1800
        first = self.write("a/C-1_20_01_03.txt", text)
        second = self.write("z/C-1_20_02_03.html", "<pre>" + text + "</pre>")
        self.write("C-1_20_03_03.txt", "A different source.")
        evidence, audit = self.build(deduplicate_exact_text=True)
        passages = evidence["C-1/20"]
        duplicate, canonical = audit[-1], audit[1]
        self.assertEqual(duplicate["status"], "duplicate")
        self.assertEqual(duplicate["duplicate_of"], "a/C-1_20_01_03.txt")
        self.assertEqual(canonical["source_file"], "a/C-1_20_01_03.txt")
        self.assertFalse(duplicate["included"])
        self.assertFalse(duplicate["truncated"])
        self.assertEqual(canonical["associated_source_files"], ["a/C-1_20_01_03.txt", "z/C-1_20_02_03.html"])
        self.assertEqual(canonical["associated_passage_ids"], duplicate["associated_passage_ids"])
        associated = [p for p in passages if p["passage_id"] in duplicate["associated_passage_ids"]]
        self.assertEqual("".join(p["text"] for p in associated), text.strip())
        self.assertTrue(all("a/C-1_20_01_03.txt" in p["source_reference"] and "z/C-1_20_02_03.html" in p["source_reference"] for p in associated))
        self.assertEqual(canonical["extracted_text_sha256"], duplicate["extracted_text_sha256"])
        self.assertEqual(canonical["source_sha256"], hashlib.sha256(first).hexdigest())
        self.assertEqual(duplicate["source_sha256"], hashlib.sha256(second).hexdigest())
        self.assertEqual((self.directory / canonical["source_file"]).read_bytes(), first)
        self.assertEqual((self.directory / duplicate["source_file"]).read_bytes(), second)
        self.assertEqual((evidence, audit), self.build(deduplicate_exact_text=True))

    def test_deduplication_is_exact_within_each_case_and_precedes_character_budget(self) -> None:
        self.write("C-1_20_01_03.txt", "One complete document.")
        self.write("C-1_20_02_03.txt", "One complete document.")
        self.write("C-1_20_03_03.txt", "One  complete document.")
        self.write("C-2_20_01_01.txt", "One complete document.")
        cases = self.cases + [{"case_id": "C-2/20"}]
        evidence, audit = self.build(cases=cases, deduplicate_exact_text=True, case_limit=10)
        first = [row for row in audit if row["case_id"] == "C-1/20"]
        self.assertEqual([row["status"] for row in first], ["truncated", "duplicate", "truncated"])
        self.assertEqual([row["included_characters"] for row in first], [5, 0, 5])
        self.assertTrue(evidence["C-2/20"])
        self.assertIsNone(audit[-1]["duplicate_of"])
        legacy, _ = self.build(cases=cases)
        self.assertEqual(len(legacy["C-1/20"]), 3)

    def test_identifier_only_extraction_is_excluded_but_short_substantive_text_is_retained(self) -> None:
        identifiers = ("C-1/20", "C-708/20-1", "Case C‑1/20 P\n1", "ECLI:EU:C:2020:1", "62020CJ0001", "C-1/20\nECLI:EU:C:2020:1\nPage 1 of 1")
        for text in identifiers:
            with self.subTest(text=text):
                self.write("C-1_20_01_01.txt", text)
                with self.assertRaisesRegex(ValueError, "Only document identifiers"):
                    self.build(exclude_identifier_only=True)
                legacy, _ = self.build()
                self.assertEqual(legacy["C-1/20"][0]["text"], text)
        self.write("C-1_20_01_01.txt", "C-1/20: Application dismissed.")
        evidence, audit = self.build(exclude_identifier_only=True)
        self.assertIn("Application dismissed", evidence["C-1/20"][0]["text"])
        self.assertEqual(audit[0]["status"], "included")

    def test_enhanced_full_text_preserves_whitespace_only_chunks_without_omissions(self) -> None:
        text = "A" + " " * 24000 + "B"
        self.write("C-1_20_01_01.txt", text)
        evidence, audit = self.build(deduplicate_exact_text=True)
        passages = evidence["C-1/20"]
        self.assertEqual("".join(passage["text"] for passage in passages), text)
        self.assertTrue(all(passage["text"].strip() for passage in passages))
        self.assertEqual(audit[0]["included_characters"], len(text))
        self.assertEqual(audit[0]["omitted_characters"], 0)
        self.assertFalse(audit[0]["truncated"])
        self.assertEqual(audit[0]["truncation_reason"], "")
        legacy, legacy_audit = self.build()
        self.assertLess(sum(len(passage["text"]) for passage in legacy["C-1/20"]), len(text))
        self.assertTrue(legacy_audit[0]["truncated"])

    def test_identifier_stub_is_audited_before_falling_back_to_substantive_original(self) -> None:
        self.write("C-1_20_01_01.txt", "ECLI:EU:C:2020:1")
        self.write("C-1_20_01_01.html", "<p>The application was dismissed.</p>")
        evidence, audit = self.build(exclude_identifier_only=True)
        self.assertEqual([row["status"] for row in audit], ["identifier_only", "included"])
        self.assertEqual(evidence["C-1/20"][0]["text"], "The application was dismissed.")

    def test_pdf_ocr_override_freezes_both_hashes_and_never_modifies_original_corpus(self) -> None:
        original = self.write("nested/C-1_20_01_01_unconverted.pdf", b"%PDF-original")
        sidecars = self.sidecars()
        sidecar = sidecars / "nested/C-1_20_01_01_unconverted.txt"
        sidecar.parent.mkdir()
        ocr = b"A complete OCR transcription of the original ruling."
        sidecar.write_bytes(ocr)
        with self.pdf_extract({original: "ECLI:EU:C:2020:1"}):
            evidence, audit = self.build(ocr_sidecars_dir=sidecars, exclude_identifier_only=True)
        row = audit[0]
        self.assertEqual(evidence["C-1/20"][0]["text"], ocr.decode())
        self.assertIn("OCR transcription: nested/C-1_20_01_01_unconverted.txt", evidence["C-1/20"][0]["source_reference"])
        self.assertEqual(row["source_sha256"], hashlib.sha256(original).hexdigest())
        self.assertEqual(row["ocr_source_sha256"], hashlib.sha256(ocr).hexdigest())
        self.assertEqual(row["ocr_source_path"], str(sidecar.resolve()))
        self.assertEqual(row["original_extraction_status"], "identifier_only")
        self.assertEqual(row["extraction_method"], "ocr_sidecar")
        self.assertFalse(row["truncated"])
        self.assertEqual((self.directory / row["source_file"]).read_bytes(), original)
        self.assertEqual(sidecar.read_bytes(), ocr)

    def test_ocr_sidecar_is_used_only_for_empty_or_identifier_only_pdf_extraction(self) -> None:
        original = self.write("C-1_20_01_01.pdf", b"%PDF-original")
        sidecars = self.sidecars()
        (sidecars / "C-1_20_01_01.txt").write_text("A replacement OCR text.", encoding="utf-8")
        for extracted, expected in (("", "A replacement OCR text."), ("Substantive original.", "Substantive original.")):
            with self.subTest(extracted=extracted), self.pdf_extract({original: extracted}):
                evidence, audit = self.build(ocr_sidecars_dir=sidecars, exclude_identifier_only=True)
                self.assertEqual(evidence["C-1/20"][0]["text"], expected)
                self.assertEqual("ocr_source_file" in audit[0], not bool(extracted))
        self.write("C-1_20_01_01.txt", "A native TXT document.")
        evidence, audit = self.build(ocr_sidecars_dir=sidecars, exclude_identifier_only=True)
        self.assertEqual(evidence["C-1/20"][0]["text"], "A native TXT document.")
        self.assertNotIn("ocr_source_file", audit[0])

    def test_invalid_ocr_does_not_promote_an_identifier_stub_to_substantive_evidence(self) -> None:
        original = self.write("C-1_20_01_01.pdf", b"%PDF-original")
        sidecars = self.sidecars()
        sidecar = sidecars / "C-1_20_01_01.txt"
        for content in (None, b"", b"ECLI:EU:C:2020:1", b"\xff"):
            with self.subTest(content=content):
                if content is None:
                    sidecar.unlink(missing_ok=True)
                else:
                    sidecar.write_bytes(content)
                with self.pdf_extract({original: "C-1/20"}):
                    with self.assertRaisesRegex(ValueError, "No readable document text.*C-1/20"):
                        self.build(ocr_sidecars_dir=sidecars, exclude_identifier_only=True)

    def test_ocr_deduplication_retains_all_original_and_transcription_associations(self) -> None:
        first = self.write("C-1_20_01_02.pdf", b"%PDF-first")
        second = self.write("C-1_20_02_02.pdf", b"%PDF-second")
        sidecars = self.sidecars()
        for name in ("C-1_20_01_02.txt", "C-1_20_02_02.txt"):
            (sidecars / name).write_text("Identical complete transcription.", encoding="utf-8")
        with self.pdf_extract({first: "C-1/20", second: "ECLI:EU:C:2020:1"}):
            evidence, audit = self.build(ocr_sidecars_dir=sidecars, exclude_identifier_only=True, deduplicate_exact_text=True)
        self.assertEqual(len(evidence["C-1/20"]), 1)
        reference = evidence["C-1/20"][0]["source_reference"]
        for row in audit:
            self.assertIn(row["source_file"], reference)
            self.assertIn(row["ocr_source_file"], reference)
            self.assertTrue(row["ocr_source_sha256"])
        self.assertEqual(audit[1]["status"], "duplicate")

    def test_invalid_evidence_options_and_overlapping_ocr_directory_are_rejected(self) -> None:
        self.write("C-1_20_01_01.txt", "Native text.")
        for option in ("deduplicate_exact_text", "exclude_identifier_only"):
            for value in (None, 0, 1, "true"):
                with self.subTest(option=option, value=value), self.assertRaisesRegex(ValueError, option):
                    self.build(**{option: value})
        for path in ("", False, self.directory / "missing", self.directory, self.directory.parent):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "ocr_sidecars_dir"):
                self.build(ocr_sidecars_dir=path)
        outside = self.sidecars()
        target = self.sidecars() / "elsewhere.txt"
        target.write_text("A document.", encoding="utf-8")
        (outside / "C-1_20_01_01.txt").symlink_to(target)
        (self.directory / "C-1_20_01_01.txt").unlink()
        original = self.write("C-1_20_01_01.pdf", b"%PDF-original")
        with self.pdf_extract({original: ""}), self.assertRaisesRegex(ValueError, "remain within ocr_sidecars_dir"):
            self.build(ocr_sidecars_dir=outside)


if __name__ == "__main__":
    unittest.main()
