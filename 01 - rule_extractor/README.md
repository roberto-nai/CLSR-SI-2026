# CURIA GPAI — DECLARE rule extraction

This program is developed for **Python 3.12**.

Extract DECLARE constraints from the English Rules of Procedure using three API providers, then collect and summarise expert assessments. Activity arguments must exactly match the event log's `Activity` labels.

## Environment

Run all commands from this project folder. First-time setup:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp -n .env.example .env
```

Enter `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` and `MISTRAL_API_KEY` in `.env`. The runner reads keys from this file. In each new terminal, activate the existing environment:

```bash
source .venv/bin/activate
```

## Execution phases

### 1. Prepare the inputs

Place the original English PDF `CELEX_32012Q0929(01)_EN_TXT.pdf` and the event log `curia_log_en.csv` in the project folder, then run:

```bash
cp -n curia_log_en.csv data/curia_log_en.csv
python prepare_source.py --input 'CELEX_32012Q0929(01)_EN_TXT.pdf'
```

Check the extracted text in `data/` against the PDF. Skip preparation if the source is already prepared; use `--force` only to replace it deliberately.

### 2. Freeze the experiment plan

Choose models and parameters in `config.json` before creating a new experiment:

```bash
python run_experiments.py plan --name study_activity_01 --repetitions 3
```

Use `--repetitions 2` for two repetitions. Choose a new name for each experiment. Planning preserves the inputs, prompts, configuration and code without making API calls.

### 3. Run and check progress

```bash
python run_experiments.py run --experiment experiments/study_activity_01
python run_experiments.py status --experiment experiments/study_activity_01
```

`run` consumes API credit. Running it again continues jobs that have never started. Diagnose failures without making calls, then explicitly retry errors or interrupted calls if needed:

```bash
python run_experiments.py diagnose --experiment experiments/study_activity_01
python run_experiments.py run --experiment experiments/study_activity_01 --retry-errors
```

### 4. Export the review sheets

```bash
python run_experiments.py export --experiment experiments/study_activity_01
```

In `reviews/`, the two lawyers complete `source_grounding` and `normative_fidelity` in their respective `domain_expert_law_1.csv` and `domain_expert_law_2.csv` sheets; the technical reviewer completes `declare_correct` in `domain_expert_cscience.csv`. Use `YES`/`NO` and follow `INSTRUCTIONS.txt`.

Export does not overwrite existing review sheets.

### 5. Summarise the assessments

After collecting the completed sheets in `reviews/`:

```bash
python run_experiments.py summarise --experiment experiments/study_activity_01
```

Results are written to `summary/`. A blank rating remains pending; acceptance requires all five positive judgements and valid activity labels.

## Input and output folders

| Folder | Contents |
| --- | --- |
| `data/` | Local input event log, prepared source PDF and text, and provenance manifest. |
| `prompts/` | Extraction instructions and DECLARE template definitions. |
| `experiments/<name>/frozen/` | Preserved inputs, prompts, configuration and code. |
| `experiments/<name>/runs/` | API requests, responses and attempt records. |
| `experiments/<name>/reviews/` | Expert review sheets, instructions and coordinator files. |
| `experiments/<name>/summary/` | Aggregated assessments in CSV and JSON format. |

Each experiment also contains its plan and `metrics.csv`. Git retains `data/` and `experiments/` through `.gitkeep`; their local contents are ignored. Archive, test and temporary folders such as `_old/` and `_test/` are ignored at any depth; the project's `tests/` folder remains included.

See [METHODOLOGY.md](METHODOLOGY.md) for the protocol and assessment criteria.
