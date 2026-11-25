# Approved rule catalogue and provenance

## Current textual-requirement and DECLARE runtime

The original four-column `data/rules_current.csv` is preserved byte-for-byte. Runtime uses `ID`, `requirement` and the original `DECLARE` formula as rule evidence, with a separate formal comparator; it ignores `source`, including any alternative-block tags in that column. Requirement blocks alone determine formulation identifiers. Runtime selections, frozen case–rule inputs, prompts and future exports omit source quotations and use `requirement_review_id` for formulation provenance. The raw CSV file is still hashed as a whole, so any byte change changes the audited input identity.

Both phases supply the selected requirement wording and exact CSV formula. Rule payloads contain `rule_id`, `requirement` and `declare`. `REQUIREMENT` and `DECLARE` identify those two items within the assessed rule; there is no legal-source evidence block or `S1` reference. B preserves A's full prompt as a prefix and appends only fixed case texts. Source removal is prospective: the conversion and experiment descriptions below retain the original workbook/CSV provenance and source-bearing pilot. Old source-bearing A requests cannot match the new prompt identity; verified cohort input import does not import their answers.

Checker verdicts and activation metadata remain excluded. The intermediate requirement-only version had no live run; its prompt identity is also distinct from the current requirement-and-DECLARE protocol.

The pilot descriptions below retain their historical source-bearing evidence boundary; the conversion still preserves all original CSV fields.

The historical experiment uses the configured clean event log, `regole_c.decl`, the approved Excel workbook and a local document-presence index. The case-batched pilot uses `data/curia_log_en_C_2016-2026_clean.csv` with an independent CaseID-year filter of 2016–2026. Earlier configurations can retain their own year filters. The log and DECLARE file are retained unchanged in `data/`. The workbook is retained byte-for-byte as `data/_old/approved_rules.xlsx`.

## Excel conversion

`data/rules_current.csv` is a UTF-8 CSV with exactly four columns: `ID`, `DECLARE`, `source`, and `requirement`. The workbook's `ID originale` heading maps to `ID`; the other three field names remain unchanged. Each of the 19 workbook rows becomes one CSV record.

Every source passage, requirement and DECLARE expression is preserved exactly, including punctuation, Unicode text and embedded newlines. CSV quoting escapes delimiters and quotation marks without changing the decoded field values. The CSV round trip is checked before the conversion is accepted.

The workbook supplies one source and one requirement for each rule. Earlier alternative formulations and their random selection are superseded by this approved input. In the experiment, each rule has the same source and requirement across all cases and models.

`data/_old/approved_rule_metadata.csv` preserves the supplied review identifiers and proposal counts, together with the worksheet and row of origin. These metadata do not enter the LLM prompt. They describe the submitted workbook and are not recomputed expert judgements.

`data/_old/rules_import_manifest.json` records the workbook and output hashes, worksheet, field mapping and conversion checks. All 19 rule identifiers and formal expressions agree with `data/regole_c.decl`. The converter rejects missing mandatory fields, duplicate identifiers, formula cells in imported data and mismatches with the supplied DECLARE file.

## Repeat the conversion

```bash
python convert_rules.py --input data/_old/approved_rules.xlsx --output data/rules_current.csv --declare data/regole_c.decl
```

The converter uses only the Python standard library. It reads the workbook without modifying it. If a workbook contains more than one matching sheet, select the intended one with `--sheet`.

## Formal reference

`data/declare_results_current.zip` supplies the formal results for all 19 current rules. The approved Excel contains the same formal expressions. Compatibility is verified against the configured log, selected traces and reference provenance during preparation; a filename alone does not establish compatibility. Reference discovery ignores archived `_old` and `__MACOSX` directories so they cannot shadow the active reference.

The current cleaned log contains 5,908 cases and 30,922 events; its verified reference contains 112,252 case–rule outcomes. The reference uses a minimum of two recorded events, source-row ordering for equal timestamps, no case-identifier prefix restriction and no removal of exact duplicate records. The importer checks input hashes, ordered traces, rule expressions and complete reference coverage before model requests.

The configured event log may be either the archived raw source or the exact cleaned log consumed by the formal checker. Cleaned input is accepted only when its bytes match the archived cleaned-log SHA-256 and that hash agrees across the run manifest, cleaning sidecar and checker-input record. The importer also checks manifest/sidecar count consistency, cleaned-log counts, event thresholds, selected trace hashes and complete rule-result coverage. A different file with matching case identifiers alone is rejected.

The assessment configurations apply an independent `min_events` selection threshold, initially `5`, before sampling cases. Cases with at least that many recorded occurrences qualify; repeats count, and accepted traces are retained in full. The supplied reference covers this stricter subset. The importer revalidates the full reference using its own recorded two-event threshold and then verifies selected trace and result coverage.

Set `min_events` to `-1` to disable the assessment's event-count minimum while retaining its other selection filters. This includes only cases available in the supplied source file, which has already lost cases with fewer than two events during earlier cleaning. If another source or reference leaves a selected case uncovered, import fails before model requests; formal recomputation must be configured explicitly.

## Document-presence mapping

`case_texts_dir` is a required existing directory, defaulting to `../00 - event_log/data_text` relative to the configuration file. Preparation searches it recursively for non-empty `.txt`, `.html` and `.pdf` files following the download naming convention `<sanitised CaseID>_<sequence>_<total>.<extension>`. The slash and other unsafe characters in a case identifier become underscores, using the downloader’s sanitisation rule. For example, `C-48/24` corresponds to `C-48_24_01_01.html` and `C-48_24_01_01.txt`. Complete identifiers, including procedural suffixes, are matched; another case with the same prefix is not evidence of availability.

At least one matching non-empty file is sufficient. Original HTML/PDF files qualify even when text conversion failed or no TXT exists; downloaded originals ending in `_unconverted.html` or `_unconverted.pdf` also qualify, while `_unconverted.txt` does not. Empty files, directories and unrelated index/report files do not qualify. This presence check precedes annual sampling, compensation and explicit `case_ids` selection, so a case with no qualifying file cannot enter any of those selections.

Selected cases record `document_file_count` and `document_files`. The cleaning register records matching-file counts and missing-document exclusions, while its summary records eligible counts before and after the document filter and `excluded_document_cases`. A digest of the document-presence index is frozen in experiment identity so inventory changes that affect eligibility require a new experiment. This establishes local document availability. Case mode can additionally extract and freeze B evidence, but the active pilot is A-only and excludes document contents. Earlier B preparation is archived for provenance, not presented as an evaluated phase. Legacy rule mode uses its separately supplied frozen evidence interface.

## Case-mode documentary evidence

The deferred documentary path in `assessment_mode: "case"` can build B evidence directly from local files; it is not active in the current A-only pilot. It prefers TXT, then HTML and PDF, once per document stem; unreadable/empty alternatives can fall back to the original. The same document text is shared across all requirements and models for that case. It does not use outcome-aware retrieval or a model-generated passage selection.

The pilot caps included extracted characters at 24,000 per document and 120,000 per case. Documents share the case budget, and bounded texts use beginning/end excerpts. Frozen passage labels identify exact character ranges, total source-text lengths and omitted counts. `_audit/prepared/document_selection.csv` records source SHA-256, format, extraction status, extracted/included/omitted characters, passage count and truncation reasons. Selected document bytes are hashed in experiment identity in addition to the document-presence index.

The pilot extraction snapshot contains 114 TXT documents and 167 passages, with 3,503,487 extracted characters, 1,231,495 included characters and 53 truncated documents. These are bounded excerpts, so missing information cannot be equated with absence from the complete judicial record. This documentary snapshot is unused by the current phase-A model and expert-review packets. A future RAG variant would require a separate retrieval protocol and evaluation.
