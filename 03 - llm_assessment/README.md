# CURIA LLM procedural compliance assessment

Developed for **Python 3.12**.

## Virtual environment

```bash
python3.12 -m venv .venv       # first setup only
source .venv/bin/activate
python -m pip install -r requirements.txt
```

If `.env` is absent, copy `.env.example` to `.env` and fill in `OPENAI_API_KEY`,
`ANTHROPIC_API_KEY` and `MISTRAL_API_KEY`.
PDF extraction requires `pdftotext`; XLSX exports require Node.js and `@oai/artifact-tool`.
For a custom Node installation, set `CURIA_SPREADSHEET_NODE` and `CURIA_SPREADSHEET_NODE_MODULES`.

## Configuration

- `config.json`: input/output paths, case selection, providers/models, token limits and concurrency.
  Paths are relative to the configuration file. For a fresh phase-1 study, set
  `conditions: ["A"]`, `baseline_phase1_dir: null` and `cohort_source_dir: null`.
- `conditions: ["A"]`: phase 1, requirements, DECLARE formulas and event log.
  `conditions: ["B"]`: phase 2, adding case documents. Set `baseline_phase1_dir`
  to a compatible frozen phase-1 study for a paired comparison; `cohort_source_dir`
  reuses a frozen case/rule selection without model answers.
- `config*.frozen.json`: local, Git-ignored settings for continuing a specific study, generated during preparation.
  Configuration, source and inputs must stay unchanged; historical studies require their archived executable snapshot.
- `.env`: local API credentials, excluded from Git; `.env.example` is the shared template.

## Execution

Prepare a new study offline; the launcher prints a timestamped output directory and frozen configuration:

```bash
python new_assessment.py prepare --config config.json
```

Replace `CONFIG_FROZEN.json` below with the printed path to continue that same study:

```bash
python run_assessment.py run --config CONFIG_FROZEN.json      # API calls
python run_assessment.py resume --config CONFIG_FROZEN.json   # retry missing/failed requests
python run_assessment.py analyse --config CONFIG_FROZEN.json  # reports and exports, offline
```

`resume` skips valid completed requests. Add `--provider openai` (or `anthropic`, `mistral`)
to `run`/`resume` to select a provider, or `--max-requests 1` to limit new attempts.
For an existing configuration, offline preparation is also available as
`python run_assessment.py prepare --config CONFIG_FROZEN.json`.

Alternatively, create, run and analyse a new study in one command:

```bash
python new_assessment.py all --config config.json --no-open
```

Each `new_assessment.py` invocation creates a separate study.

For the integrated critical phase-1 assessment (cases and rule-formalisation audit),
provide the configuration of a compatible frozen 50-case phase-1 baseline, available locally with its outputs:

```bash
python critical_assessment.py prepare --baseline-config PHASE1_CONFIG_FROZEN.json
python critical_assessment.py run --study outputs/PRINTED_STUDY_DIRECTORY
python critical_assessment.py resume --study outputs/PRINTED_STUDY_DIRECTORY
python critical_assessment.py analyse --study outputs/PRINTED_STUDY_DIRECTORY
```

Optional offline steps (activation selection requires the verified historical baseline locally):

```bash
python select_activation_cases.py --config config.json --baseline-dir outputs/LOCAL_BASELINE_DIRECTORY
python report_event_log_statistics.py --study outputs/EXISTING_STUDY_DIRECTORY
```

## Input and output directories

| Path | Contents |
| --- | --- |
| `data/` | Input event log, `rules_current.csv`, `regole_c.decl` and the formal comparator `declare_results_current.zip` (supplied locally). |
| `../00 - event_log/data_text/` | Case documents in TXT, HTML or PDF; configured by `case_texts_dir` and required for case eligibility in both phases. |
| `data/phase2_ocr_20261123/` | OCR sidecars for scanned documents, configured by `ocr_sidecars_dir`. |
| `outputs/<study>/` | Results, CSV/XLSX exports, HTML reports, resource statistics and expert-review packages. |
| `outputs/<study>/_audit/` | Frozen inputs/configuration, provenance, raw requests/responses and checkpoints. Integrated studies also contain `case_assessment/` and `formalisation_audit/`. |

Generated output files, local archives and ZIP files are excluded from Git; `outputs/.gitkeep` preserves the output directory.
