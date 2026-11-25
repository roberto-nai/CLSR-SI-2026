# Verification record

## Textual-requirement and DECLARE protocol migration — 24 November 2026

The current implementation supplies the exact CSV `requirement` and `DECLARE` formula, with rule-scoped `REQUIREMENT` and `DECLARE` references. Model and expert evidence excludes `source`, `legal_source`, checker verdicts and activation metadata while the original CSV is preserved. Targeted integration tests exercise literal formula preservation in both phases, exact A-to-B prefix preservation, source exclusion, archive recovery and frozen-input safeguards. All 457 automated tests passed in the coordinated offline suite. A separate temporary preparation verified 50 real cohort cases, 950 case–rule pairs, 150 planned case requests and literal original formulas in rule/expert exports, with no model calls. All 50 case prompt pairs also preserve the exact phase-A system and user-prefix contract in phase B. No live model request or existing-study regeneration was performed.

The preceding requirement-only implementation passed 97 targeted integration tests offline but had no live model run. Those checks do not establish any results for the revised formula-bearing protocol.

All completed source-bearing studies retain their original prompts, inputs, configuration and responses. Their archived executable snapshots must be used for their original analysis and verification. A changed prompt identity is rejected rather than silently resumed or treated as a matching A baseline. The formula-bearing protocol has a new identity as well. Verified cohort import reuses inputs only, never historical model answers. The remainder is the historical validation record.

## Completed uniform 32,000-token phase-A cohort — 22 November 2026

All **290 automated tests passed**. The final selected dataset contains **33 complete case responses and 627 schema-valid rule judgements** for eleven cases and three models, with zero selected technical failures or unavailable rule judgements. Every observed request ceiling in this cohort is 32,000 tokens. No phase-B API call was made.

The exact completed settings are in `config.phase1.32000.frozen.json`, including sampling seed `20261117`; outputs are under `outputs/phase1_2016_2026_1_per_year_32000_20261122`. The user changed editable `config.json` to seed `20261118` during execution. That edit was preserved. Reanalysis/resume of this completed cohort uses the frozen configuration; a cohort sampled with the edited seed requires a new output directory.

The audit retains 34 API attempts: four exact-request-hash-matched Mistral responses reused at 32,000 tokens and 30 newly issued calls. One Anthropic formatting failure required a same-budget retry. Its original attempt remains in resource accounting, while the valid latest answer supplies the selected result. Earlier 16,000-token/mixed-budget exploratory results are archived separately and excluded from these article/resource metrics.

### Final model outcomes

Each model has 209 valid rule answers. Counts below use the latest selected response; legal uncertainty remains distinct from a technical failure.

| Model | Compliance | Violation | Not applicable | Undetermined | Case flags yes / no / undetermined | Formal agreement | Comparable coverage |
| --- | ---: | ---: | ---: | ---: | --- | --- | ---: |
| Anthropic | 70 | 2 | 107 | 30 | 2 / 0 / 9 | 72/72 | 34.45% |
| Mistral | 75 | 7 | 81 | 46 | 7 / 2 / 2 | 80/82 | 39.23% |
| OpenAI | 71 | 4 | 78 | 56 | 4 / 1 / 6 | 75/75 | 35.89% |

Formal agreement is conditional on valid binary comparisons: 72, 82 and 75 of the respective 209 rule answers. The 100% rates for Anthropic and OpenAI therefore do not describe all 209 answers or establish legal accuracy. This is a descriptive **n = 11 case-cluster** pilot; the 627 answers share cases and batched contexts and are not independent judicial observations. Failure, unavailable output and legal uncertainty must never be converted to “no violation”.

### Phase-A token and API-time totals

`model_resources.csv` was checked against all retained current-cohort attempts. Measurements include imported responses once and the same-budget format retry, with complete measurement coverage. API durations are summed request round trips, not wall-clock phase duration.

| Model | Input tokens | Output tokens | Total tokens | API seconds total | API seconds mean | Calls | Reused / new |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| Anthropic | 65,725 | 20,624 | 86,349 | 120.527 | 10.043917 | 12 | 0 / 12 |
| Mistral | 40,818 | 109,288 | 150,106 | 1,706.936 | 155.176 | 11 | 4 / 7 |
| OpenAI | 40,391 | 62,001 | 102,392 | 1,118.253 | 101.659364 | 11 | 0 / 11 |

The primary `article_results.csv` contains exactly three latest-answer model rows, with no initial-attempt comparison table. Both expert CSVs contain 627 A-only rows with all human annotation fields blank. Separate `expert_1_review.zip` and `expert_2_review.zip` packages were created and verified to omit model/provider identifiers and formal-checker answers. No B dossier is included. These checks establish a complete machine-generated dataset and review material, not completed human validation.

The earlier records below describe historical implementations and configurations.

## Required document presence — 21 November 2026

All **221 automated tests passed** using `.venv/bin/python -m unittest discover -s tests`. Checks include recursive exact CaseID filename matching, non-empty TXT/HTML/PDF presence, original-only downloads, document exclusions before annual sampling and compensation, explicit identifier eligibility, frozen presence-index changes and strict cleaned-input formal-reference validation.

Both real configurations were prepared and resumed successfully with the current cleaned log and formal reference containing 112,252 case–rule outcomes. Every selected case’s matching non-empty files were checked, and the British English TXT extraction reports retained identical bytes on resume. No model API requests were made. The configured source contains 5,908 cases and 30,922 events.

| Configuration | CaseID years | Eligible before documents | Eligible after documents | Excluded without documents | Selected cases | Pairs | Planned assessments |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `config.json` | 2016–2026 | 305 | 305 | 0 | 55 | 1,045 | 3,135 |
| `config.test.json` | 2017–2026 | 269 | 269 | 0 | 50 | 150 | 450 |

The main allocation is six cases in each year from 2016 through 2024, one in 2025 and none in 2026. The test allocation is six each in 2017–2023, seven in 2024, one in 2025 and none in 2026. Both meet their total targets after compensation. These counts describe the verified document-inventory snapshot; ongoing downloads can change which cases qualify. The reports are `outputs/direct_EN_yearly_min5/case_extraction_report.txt` and `outputs/direct_EN_yearly_test/case_extraction_report.txt`.

## Earlier quota compensation and TXT report — 21 November 2026

This record predates mandatory document presence and the main configuration’s change to 2016–2026. Its counts describe that earlier implementation.

All **194 automated tests passed** using `.venv/bin/python -m unittest discover -s tests`. Checks include annual sampling and descending-year compensation, pool exhaustion, unique selections, explicit/all-case overrides, TXT report contents and frozen-artifact verification.

Both supplied configurations were prepared and resumed in their configured output directories with the real inputs and verified formal reference. The TXT reports were inspected, and resuming retained identical report bytes. Formal import passed; no model API calls were made and no request directories were created.

| Configuration | Eligible cases | Base sample | Additional cases | Selected cases | Rules | Pairs | Planned assessments |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `config.json` | 269 | 41 | 9 | 50 | 19 | 950 | 2,850 |
| `config.test.json` | 269 | 41 | 9 | 50 | 3 | 150 | 450 |

Both use 2017–2026 inclusive, `cases_per_year: 5`, `country: "EN"` and `min_events: 5`. The final allocation is six cases in each year from 2017 through 2023, seven in 2024, one in 2025 and none in 2026. Nine additional cases compensate the missing base quotas, reaching the total target of 50 with no overall shortfall. The reports are `outputs/direct_EN_yearly_min5/case_extraction_report.txt` and `outputs/direct_EN_yearly_test/case_extraction_report.txt`.

## Initial annual sampling verification — 21 November 2026

This record predates quota compensation and the TXT extraction report. The counts below describe the initial annual sampling implementation. Current allocation and output definitions are in [the configuration guide](../README.md) and [outputs.md](outputs.md).

All **190 automated tests passed** using `.venv/bin/python -m unittest discover -s tests`. Annual checks cover strict configuration validation, inclusive year boundaries, CaseID year parsing and suffixes, reproducible independent samples per year, shortages and empty years, preserved complete traces, explicit identifiers, obsolete `case_count` rejection and common case–rule pairs across providers.

Both supplied configurations were prepared and resumed in temporary output directories using the real inputs and verified formal reference. Import coverage and frozen-experiment reuse passed without any model API request. Existing experiment outputs were preserved.

| Configuration | CaseID years | Cases per year | Eligible cases | Selected cases | Rules | Pairs | Planned assessments |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `config.json` | 2017–2026 | 5 | 269 | 41 | 19 | 779 | 2,337 |
| `config.test.json` | 2017–2026 | 5 | 269 | 41 | 3 | 123 | 369 |

Both configurations use `country: "EN"`, `min_events: 5`, three models, condition A and one repetition. Each samples five cases in 2017–2024, one in 2025 and none in 2026; the annual audit records missing quotas of four and five respectively. New runs use `outputs/direct_EN_yearly_min5` and `outputs/direct_EN_yearly_test`. See [the configuration guide](../README.md) and [output definitions](outputs.md).

## Historical verification — 19 November 2026

Historical verification on 19 November 2026 for version 0.4.0, before annual case sampling replaced the total case count. Verification itself made no live model API requests. Counts and output paths below describe those preserved experiments. The current annual configuration and audit are documented in [../README.md](../README.md) and [outputs.md](outputs.md).

### Automated checks

All 169 automated tests passed in the environment containing the optional formal engine. Checks cover approved-workbook conversion, exact CSV round trips, shared and inline XLSX strings, formula-cell rejection, missing fields, duplicate identifiers, DECLARE consistency, case/rule selection, minimum-event and whole-case country filtering, provider responses, formal comparison, vacuity, retries, resource accounting, project-relative display paths, explicit output cleanup and archive provenance.

Cleanup tests run only against temporary project roots. They verify removal of all generated children, preservation of configuration and credentials outside `outputs`, symbolic-link boundaries, pre-deletion lock checks and invalid-argument rejection before deletion. An integration check exercised `prepare --delete` against the supplied inputs through a temporary project, then standalone `--delete`, without model requests. Existing project outputs remained unchanged during these checks.

Resource tests cover all three provider usage schemas, cache and reasoning details without double counting, unknown versus measured-zero values, incomplete measurement coverage and all-attempt totals including invalid outputs, failures and retries. Legacy archives are normalised in memory without rewriting source response or assessment bytes. Display-path tests cover another working directory, files outside the project and filesystem errors.

The unified `all` command was checked from preparation through model execution, analysis and HTML report generation using the supplied short-test data and 45 simulated responses, 15 per configured provider. Repeating that invocation skipped all 45 completed jobs without repeating any request. CLI tests also cover stage order, credential-file precedence, run controls, provider failures and interruption. Report tests preserve unavailable rates, denominators and partial results, and check HTML escaping and independence from external assets. No live model API request was made during these checks.

CLI timing checks cover all three commands, durations exceeding 24 hours, and closing records on errors or keyboard interruption without changing exit codes. Help output has no timing records.

CLI credential tests verify that the project `.env` takes precedence over inherited environment values for all three providers. Missing file entries can use inherited values; explicitly empty entries suppress inherited credentials. These tests use synthetic credentials and make no model API requests.

Prompt JSON object keys are sorted recursively so saving and reloading dossiers preserves the exact prompt and request hashes. Regression tests cover nested case and event metadata and preparation followed by simulated execution for all three providers and conditions A, B and C. Event and passage list order is preserved.

Integer `-1` and JSON `null` both select all eligible cases or rules. The normalised experiment identity is the same for these equivalent settings. Zero, other negative counts, Booleans, floats and string counts are rejected. Explicit identifier lists retain precedence. Repetitions must remain positive.

Both supplied configurations set `min_events` to `5`. The inclusive threshold counts recorded occurrences, including exact duplicates, before case sampling and retains complete traces. `min_events: -1` disables only this minimum; country and category filters still apply. Unlike case and rule counts, `min_events` does not accept `null`. Reference import validates the archived threshold independently and requires unchanged traces and full result coverage for the selected cases.

The default `country` setting is `"EN"`. It accepts an exact recorded label or JSON `null` for all countries. The string `"none"` is normalised to the same unfiltered setting. Tests check filtering before sampling, complete event retention, missing or conflicting country values, unknown labels, and the same filtered case set across the three model providers.

### Approved input

The supplied approved workbook contains 19 rules, each with one source and one requirement. Every source, requirement and formal expression was preserved exactly in the four-column UTF-8 CSV. All 19 identifiers and expressions match `regole_c.decl`. The original workbook is retained unchanged. Review identifiers and proposal counts are preserved separately and are excluded from the LLM prompt.

The current log contains 5,368 distinct cases and 28,514 events. The compatible formal reference contains 101,992 case–rule outcomes. Hashes, ordered traces, catalogue correspondence and full reference coverage passed import validation.

### Historical prepared configurations

| Configuration | Country filter | Eligible cases | Selected cases | Selected rules | Pairs | Planned model assessments |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| `config.json` | `EN` | 278 | 20 | 19 | 380 | 1,140 |
| `config.test.json` | `EN` | 278 | 5 | 3 | 15 | 45 |

Both configurations use the same three configured models, condition A and one repetition. Every rule has the same approved source and requirement across cases and providers. With `min_events: 5`, the preserved experiment in `outputs/direct_EN_min5` has 1,140 pending rows. Its selection is a new experiment; the earlier successful assessment remains in `outputs/direct_EN_v4` and is not transferred into this sample.

The preserved short experiment in `outputs/direct_EN_test` retains 45 actual API attempts: 40 valid assessments and five invalid outputs. Resource totals were independently recomputed from all 45 original records and saved without repeating model requests. The pre-resource-summary experiment is preserved unchanged in `outputs/archive/direct_EN_test_before_resources_20261119`; that experiment records the verified continuation in `continuation_provenance.json` and retains byte-identical request archives.

| Provider | Recorded attempts | API seconds | Input tokens | Output tokens | Total tokens |
| --- | ---: | ---: | ---: | ---: | ---: |
| OpenAI | 15 | 136.964 | 15,070 | 9,788 | 24,858 |
| Anthropic | 15 | 46.385 | 22,793 | 3,690 | 26,483 |
| Mistral | 15 | 1,004.592 | 15,352 | 29,408 | 44,760 |

The supplied source has 3,408 cases with at least five events across all countries, including 278 recorded `EN` cases and 319 recorded `IT` cases. With `min_events: -1`, all 5,368 source cases remain available before the other filters, including 524 `EN` and 490 `IT` cases. The source was already cleaned with a two-event minimum; no one-event cases can be restored by this setting.

The earlier console-timing update continued the preceding experiment in `outputs/direct_EN_v4`. All 1,140 frozen jobs, pair dossiers and formal results were checked against `outputs/direct_EN_v3` before retaining its successful attempt unchanged. `continuation_provenance.json` records the source manifest, code identities and copied-file hashes. That source experiment remains unchanged; no model request was repeated during that continuation.
