# Experimental protocol

Activity-constrained design, version 1.1.0: 9 November 2026. This document describes the experimental protocol and does not report empirical results.

## Task

Three LLMs receive the same English text extracted from `CELEX_32012Q0929(01)_EN_TXT.pdf`, the original 2012 publication of the Rules of Procedure of the Court of Justice. The PDF and text are preserved with SHA-256 hashes. The source reference is https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:32012Q0929(01). This reference identifies the selected edition; it is not a verification of the law currently in force.

Each call requests 12 slots: 3 existence, 3 relation, 3 mutual and 3 negative constraints. The families and templates follow Table 1 in Di Ciccio and Montali (2022), printed page 111, the fourth page of the shared PDF. Quotas apply to distinct prescriptions, not necessarily different templates. An abstention uses null content fields. Omitted slots and abstentions are preserved as distinct outcomes.

The output contains only a source reference and excerpt, a natural-language requirement and a DECLARE formula. Formulas use `Template("Exact activity label")` or `Template("First exact activity label", "Second exact activity label")` notation. Essential applicability conditions are stated in the sentence, and the formula is interpreted within that scope. This does not produce a global model immediately applicable to every event log.

## Fixed activity vocabulary

Every DECLARE argument must exactly match a value in the `Activity` column of `data/curia_log_en.csv`. The supplied semicolon-separated log contains 19 distinct labels and 4,744 events, with no missing activity values. Labels are preserved without trimming, case conversion, translation, synonym substitution or snake_case renaming. New plans freeze the vocabulary and the source log's SHA-256 hash. Only the activity labels enter the prompts; case records, party information, timestamps and observed sequences are not sent to the models.

The legal text supplies normative evidence; the event vocabulary limits which recorded events can be named. A document or publication label must not be substituted for a different procedural act merely because it is related. Requirements that cannot be faithfully represented using the fixed vocabulary require abstention. A trace is the recorded sequence for one case; the prompt does not assume that all legally relevant acts are observed.

A mechanical check records whether each proposed formula has quoted arguments from the frozen vocabulary. Unknown labels and malformed argument notation are preserved as failures of the task restriction. Original proposals remain available to the human reviewers. This check does not add a human assessment criterion and does not establish legal fidelity or the correctness of the chosen DECLARE template.

## Repetitions

The initial configuration uses **3 separate runs for each of the 3 models**: 9 planned calls and up to 108 proposals. The `--repetitions 2` option produces 6 calls and up to 72 proposals. The repetition count is fixed before execution and is not changed in response to the outcomes.

Saeedizade et al. (2026), Section 5, item 3, propose at least three repetitions in CoLLM; Section 3.2 keeps model version, parameters and prompts constant. This is the main reference for choosing three: https://doi.org/10.1177/22104968261479512.

Alvarado Gonzalez et al. (2025), a **preprint** on AI4Math, propose two runs as an economical compromise in “Stability Improvement From Additional Runs”: https://arxiv.org/html/2509.24086v1. Their domain and experimental configuration differ from this study.

Neither source demonstrates that two or three runs are sufficient for every legal extraction task. This comparison is descriptive and confined to one source. Repetitions do not create new independent legal documents. IDs such as E1 and R1 identify slots rather than semantic correspondences between rules from different runs.

## Models and configurations

Initial model IDs: `gpt-5.5-2026-04-23`, `claude-sonnet-5-5`, `mistral-large-4-0`. Actual parameters and requests are archived in the plan. The `medium` reasoning setting does not imply an equal computational budget across the three APIs. Uniform seeds or temperatures unsupported by some services are not imposed.

Each call has no previous conversation history and uses the same source text and instructions. The repetition number is not included in the prompt. A pinned version does not guarantee identical responses or perpetual service availability. The model field actually returned by the API is preserved.

Official references consulted on 9 November 2026:

- OpenAI snapshot: https://developers.openai.com/api/docs/models/gpt-5.5
- OpenAI Responses/reasoning: https://developers.openai.com/api/docs/guides/reasoning
- Claude versioning: https://platform.claude.com/docs/en/about-claude/models/model-ids-and-versions
- Claude API: https://platform.claude.com/docs/en/api/versioning
- Claude effort: https://platform.claude.com/docs/en/build-with-claude/effort
- Mistral versioning: https://docs.mistral.ai/inference/model-lifecycle
- Mistral model and reasoning: https://docs.mistral.ai/inference/conversations/reasoning

## Assessment

Two lawyers independently assess source grounding and normative fidelity using YES/NO. One technical reviewer assigns a single YES/NO rating to the correctness of the DECLARE formalisation relative to the stated requirement. Notes are optional. Sheets hide the provider, model and repetition: this masks metadata but does not guarantee that output styles are unrecognisable.

A proposal is accepted without changes only when all five ratings are positive and its activity arguments satisfy the fixed vocabulary restriction. The summary preserves `human_accepted_without_changes` separately from the final `accepted_without_changes` value. Original ratings are preserved separately. The report produces rates by model, repetition and family, each with an explicit denominator, together with agreement between the lawyers. Blank cells indicate pending assessments. Missing, refused, truncated or unparseable outputs are not converted into negative legal ratings: they appear in the administrative data and the yield per requested slot. Vocabulary failures are reported separately from those human judgements.

The software does not use an LLM as a judge, repair responses or derive a gold standard from corrections to rescore originals retrospectively. It does not yet perform conformance checking on CURIA proceedings.

## Suggested paper wording, after execution

Each model was queried in three separate runs using the same source text, prompt and generation settings. The repetition count follows the practical repeatability design of CoLLM for LLM-based knowledge engineering (Saeedizade et al., 2026). Results are reported descriptively by model and run; the repetitions are not treated as independent legal documents.

The `references.bib` file contains complete references. The `paper/` folder contains the manuscript section and the actual revised prompt as a readable LaTeX appendix. Update the wording if the actual number of runs or the configuration changes. Outputs generated before the vocabulary restriction belong to a different protocol and must not be pooled as repetitions of the new design.
