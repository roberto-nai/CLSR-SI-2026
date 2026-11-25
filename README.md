# From Legal Rules to Accountable AI: Evaluating GPAI-Assisted Procedural Compliance

Authors: \<anonymized>

## Abstract

Assessing procedural compliance requires connecting legal requirements with evidence about the conduct of individual proceedings. Process Mining (PM) provides methods for examining recorded behaviour against formalised requirements, while the legal interpretation of the results depends on the scope of the rules and the available evidence. This paper examines how PM, declarative process modelling and large language models (LLMs) can support procedural compliance assessment in a judicial setting. Based on the original English 2012 Rules of Procedure of the Court of Justice and publicly available judicial records, the study combines computational analysis with independent expert assessment. Its evaluation design addresses the quality of formalised requirements, the interpretation of conformance results and the role of case documents in supporting LLM judgements. The reported findings highlight variation in the acceptance of extracted constraints and show how vacuous satisfaction affects aggregate conformance. The contribution is a method for evaluating the use of general-purpose artificial intelligence (GPAI) in legal workflows, with explicit links between normative sources, recorded behaviour and expert judgement.

---

## Repository structure

- `00 - event_log`: Downloads InfoCuria case records and documents and builds event logs for procedural analysis.
- `01 - rule_extractor`: Uses LLMs to extract procedural requirements and DECLARE constraints from the 2012 Rules of Procedure for expert review.
- `02 - rule_checker`: Cleans event logs and checks recorded traces against DECLARE constraints, reporting conformance and vacuous satisfaction.
- `03 - llm_assessment`: Evaluates LLM procedural compliance judgements and rule formalisation, producing results and independent expert-review materials.
