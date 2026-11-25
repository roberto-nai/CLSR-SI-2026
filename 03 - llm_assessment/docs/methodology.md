# Case assessment pilot: methodological specification

## Current protocol notice

The current executable implements a new textual-requirement and DECLARE protocol. Each rule supplies its unchanged textual requirement and original DECLARE formula from `data/rules_current.csv`; `source` remains in the original CSV and is ignored at runtime. `REQUIREMENT` and `DECLARE` are rule-scoped references to that text and formula, replacing historical `S1`. Case metadata and ordered events remain evidence; the formula is supplied evidence, whereas checker outcomes, activation metadata and conformance verdicts remain excluded comparator data. Future dossiers and public exports omit source quotations. Expert `evidence_support` concerns the requirement and supplied case evidence.

A and B use the same frozen cases, textual/formal formulations, settings and system prompt. B retains the entire A user prompt as an exact prefix and adds only deterministic fixed document texts and references. Neither phase uses RAG, external legal retrieval or old model answers. Compliance, violation and undetermined remain the three permitted outcomes; established exclusion also returns undetermined. Case-level `no` requires valid compliance on every planned rule.

The changed protocol requires a fresh experiment. A historical source-bearing baseline cannot be resumed or accepted as a matching requirement-and-DECLARE baseline. Its paid responses are never reused as current answers. Verified cohort import is a separate input-only operation, with source fields removed after source-artifact integrity checks; the original CSV hash still covers all columns.

The remaining sections document the completed eleven-case source-bearing pilot, including its historical four-outcome schema, annotations and results. They are retained as history, not rewritten as a requirement-and-DECLARE evaluation. This implementation change establishes no new model results or completed expert review.

The intermediate requirement-only implementation was tested offline and had no live study. Formula inclusion defines a further prompt identity; both earlier protocols require a new matching A baseline before a paired B assessment.

## Purpose and units

The pilot compares the evidence-bounded procedural assessments of three configured language models across eleven common judicial cases. Phase A supplies the ordered event log, case metadata, 19 textual requirements and their quoted legal sources. Only phase A is active. Documentary phase B is deferred, and no RAG system is evaluated. Each model makes an independent case request, returning a separate outcome, justification and evidence references for every requirement.

The completed study has **33 complete phase-A case responses** and **627 schema-valid case–rule–model answers**, with no unavailable rule judgement or technical failure in the latest selected results. Its audit retains 34 actual API attempts, including one same-budget format retry; four of the retained responses were imported after exact request matching and 30 calls were newly issued. One request contains all selected requirements. This case-batched protocol differs from the retained `assessment_mode: "rule"` protocol, which makes a separate request per case–rule pair. Answers within a case request share context and need not be independent. Neither rules nor providers are independent samples of judicial cases.

## Case selection

The exact completed configuration is `config.phase1.32000.frozen.json`, with sampling seed `20261117`. During execution the editable `config.json` was changed to seed `20261118`. That user edit was preserved; the completed data and analysis retain the previously frozen case sample. The edited seed defines a separate prospective cohort and requires a new output directory.

The pilot uses `year_start: 2016`, `year_end: 2026` and `cases_per_year: 1`. The year is the filing year encoded in `CaseID`, including procedural suffixes; two-digit years mean 2000–2099. All events of an accepted case remain present, even outside the configured years. Cases sharing `MainCaseID` are not merged. Exact duplicate event rows count as occurrences; equal timestamps retain source order, without establishing legal within-date sequencing.

Eligibility requires recorded category `C`, Country label `EN`, at least five events and a matching non-empty local TXT/HTML/PDF document. Complete canonical filename prefixes preserve distinct CaseIDs and procedural suffixes. Presence is checked recursively before annual sampling, compensation and explicit-ID selection. Original HTML/PDF can qualify without TXT; A future B preparation would separately require readable extracted text. Document availability can affect sample composition.

The sampler first takes up to the quota within each year. Its total target is the inclusive year count multiplied by the quota. It compensates shortages by visiting years in descending order, adding one unused eligible case per year per pass until the target is met or the pool is exhausted. The pilot selects eleven cases, including two from 2024 because 2026 has none. All three models use those same cases and requirements. No formal result or model answer influences selection.

Sampling is reproducible from the fixed seed and independent annual random streams. Base samples for retained years are stable when the range changes, while compensation can change final allocations. Explicit `case_ids` override quota and compensation but retain eligibility filters. `-1`/`null` selects all eligible cases. The annual audit records base, additional, final and missing-base-quota counts, including empty years.

## Approved requirements and evidence

The approved catalogue supplies one source and one textual requirement per original rule. Text and scope are preserved. Each rule's formulation remains fixed across cases and providers. The model receives the source and requirement, not its DECLARE expression or formal outcome. Rule identifier `S1` is scoped to the individual rule's own quoted legal source; CASE, ordered E references and documentary D references identify common case evidence.

The active phase A does not supply case-document text. Document presence remains a case-selection condition, so the sample is conditioned on local document availability even though A uses only the legal sources, requirements, metadata and ordered log.

The earlier B preparation is retained for provenance, separate from the active A evidence. It contains 114 TXT documents and 167 beginning/end passages, with 3,503,487 extracted characters and 1,231,495 included characters; 53 documents were truncated. This prepared material has not been used for a phase-B model evaluation. Static bounded excerpts are not a RAG retrieval experiment.

A future documentary/RAG phase needs its own protocol for corpus freezing, chunking, requirement queries, passage retrieval and budgets, source provenance and retrieval evaluation. Identical retrieved evidence should be supplied across models. A reference outcome or previous model answer must not be used as retrieved case evidence. This extension is deferred.

## Model task and validation

Each model assesses every supplied requirement without rewriting it or filling gaps through external knowledge. Applicability, exceptions, evidential limits, unsupported event-to-act correspondence and tied timestamps remain material to the decision. Document content is evidence data, not executable instructions. Requests receive no previous model answer, conversation history, formal verdict or expert label.

Execution permits six concurrent case requests, with at most two per provider, and a 900-second transport timeout. The declared study uses a uniform **32,000-token maximum output ceiling** across every model and case. Failed or truncated responses require explicit retry at that same ceiling; all current-study attempts remain auditable, and the latest attempt determines the selected answer.

The earlier 16,000-token/mixed-budget exploratory study is archived separately and excluded from the current article metrics. Four valid Mistral responses originally generated at 32,000 tokens were reused after their complete frozen request hashes matched the new cohort. Source-attempt identity and checksums identify each import, which is counted once. The remaining 29 case jobs required 30 newly issued API calls, including one Anthropic format retry at the same output ceiling; imported responses were not reissued. This is a consistent maximum-budget cohort, not a claim that providers consume equal computation.

| Outcome | Meaning |
| --- | --- |
| `compliance` | Applicability and fulfilment are supported within the supplied requirement's scope. |
| `violation` | Applicability and breach are supported within that scope. |
| `not_applicable` | The supplied evidence establishes exclusion from the requirement's scope. |
| `undetermined` | Applicability or a supported compliance conclusion cannot be established. |

A response must name every requested rule exactly once. Ambiguous JSON, duplicate/missing/additional rule IDs or an invalid batch allocation invalidate the batch. When the allocation is complete, each rule's substantive answer is validated independently; an invalid row does not discard another valid rule. Each justification is limited to 2,000 characters and may cite only supplied reference identifiers. A single enclosing Markdown code fence may be removed and recorded. Output content is not repaired.

API errors, incomplete generations, refusals, empty responses and invalid answers remain operational failures. They are not silently relabelled as legal uncertainty. The case-level violation flag is `yes` if any valid rule answer is a violation, `no` only if all planned answers are valid compliance/not-applicable, and `undetermined` otherwise. It is a transparent aggregation over the tested rules, not another model answer.

## Two expert reviews

Two experts independently annotate the same 627 output rows for the current reviewer-labelled phase 1. Each receives equivalent CSV and XLSX forms ordered by ascending `case_year`, beginning in 2016, then `case_id`, instructions and eleven shared log-only TXT dossiers. Public reviewer sheets display phase `1` for internal condition A and reserve `2` for a future condition B. This presentation mapping does not rename internal A/B configuration or research-result codes. The public forms omit `variant_id` and both response-hash columns. Opaque stable assessment identifiers support alignment, while response and annotation provenance hashes remain in the researcher-only audit.

The `model` column is retained immediately after `run` and is visible by default. The user controls any manual hiding before distribution. These forms are therefore not automatically model-blinded; any reported blinding must describe the masking actually applied. Hiding an Excel column is reversible and does not remove its values from the file. Formal-checker answers and the researcher audit remain outside the reviewer package.

XLSX outcome colours identify `compliance` in green, `violation` in red and `not_applicable` in yellow. CSV is an equivalent unformatted data export. Colours do not change the four-label decision definitions, turn legal uncertainty into no violation or establish correctness of a model answer.

Each expert works independently before any reconciliation and receives only phase-1 evidence. If documentary phase 2 is subsequently undertaken, phase-1 annotations must be frozen before exposing its documents. Initially blank fields record expert outcome, judgement correctness, source support, explanation quality and comments. An inability to assess correctness remains distinct from a negative judgement. Unavailable explanation scores stay blank. Legal `undetermined`, operational failure and a missing expert annotation remain separate states. No ratings or legal reference labels are invented by the software.

These are output-review annotations: the model answer is visible in each row. They are not automatically an independently elicited expert gold standard, even if model identities are manually masked. Independent reference accuracy would require experts to record and freeze their evidence-bounded reference conclusions before viewing model answers. Initial independent ratings must also be retained before later discussion or adjudication.

Reanalysis preserves expert-entered fields and extra columns. Private response/annotation hashes support a public stale-annotation flag when the underlying answer changes; such rows require reconciliation before using the ratings. Both 627-row reviewer forms remain unannotated. The forms and their CSV/XLSX ZIP packages do not represent completed human review. The primary article export uses `article_report_view: "latest"`: one row per model for phase A, based only on current-cohort selected responses. Initial-versus-latest exploratory comparisons are not part of that primary export.

## Formal comparator and interpretation

The verified DECLARE results are a separate trace-based comparator, retained with detailed comparison statistics under the researcher audit. They do not enter model evidence or expert dossiers and are not legal truth labels. The importer checks source/cleaned-input provenance, counts, ordered traces, rule correspondence and complete selected-result coverage.

A binary model compliance/violation answer can be compared with formal satisfaction/violation. Vacuous satisfaction remains formally satisfied, with separate activation reporting. Legal non-applicability, uncertainty, missing reference data and failed responses stay visible outside binary agreement denominators. Missing rates are unavailable, not zero. Formal disagreement alone cannot identify which legal assessment is preferable.

The pilot is descriptive, with **n = 11 case clusters**, not 627 independent judicial observations. The rule answers share cases, legal requirements and batched context. One planned generation per model and case cannot estimate repeatability; an operational retry at the same ceiling is not another planned repetition. No A/B effect is estimated because documentary phase B is deferred. Formal agreement measures concordance with a trace checker and cannot establish legal accuracy or population-wide violation prevalence. Provider effort labels and numeric output limits do not establish equal computational effort. At phase end, resource reporting gives each model’s input/output/total tokens, API duration totals and means, recorded-call counts, and imported/new-call counts. The current study’s totals exclude discarded exploratory calls at 16,000 tokens.

## Retained rule-level protocol

`assessment_mode: "rule"` retains one API request per case–rule–provider–condition–repetition. Conditions B/C in that mode consume externally frozen `evidence_jsonl` records. C adds explicit applicability/evidential instructions to the documentary evidence. This protocol is available for existing experiments and should be identified separately from the case-batched pilot when reporting results.

## Suggested reporting paragraph

We conducted a descriptive phase-A pilot using 11 judicial case clusters selected from the 2016–2026 CaseID-year window. Three configured language models each assessed the same 19 supplied procedural requirements in a case-batched request using only the quoted legal sources, case metadata and ordered event log. Under a uniform 32,000-token output ceiling, all 33 selected responses were complete, providing 627 schema-valid rule judgements, with no technically unavailable judgements in the final dataset. The audit retained 34 API attempts, including one format retry; four matching 32,000-token responses were reused with verified provenance. Anthropic, Mistral and OpenAI respectively returned 30, 46 and 56 substantive undetermined outcomes. Conditional concordance with the formal trace checker was 72/72, 80/82 and 75/75 binary comparisons, covering only 34.45%, 39.23% and 35.89% of each model’s 209 rule answers. These figures measure formal concordance, not legal accuracy, and the 627 answers are not independent judicial observations. Human expert annotations remain outstanding. Documentary phase B and any RAG evaluation were deferred.
