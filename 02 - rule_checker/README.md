# CURIA: formal conformance and activation analysis

Developed for **Python 3.12**. The program cleans CURIA event logs, checks DECLARE rules with Declare4Py, and exports summaries and charts.

## Virtual environment

Run these commands from this directory:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-lock.txt
```

On Windows, activate with `.venv\Scripts\activate`. `requirements.txt` lists the main pinned dependencies; `requirements-lock.txt` also pins their dependencies for reproducibility.

## Execution

```bash
# Complete run: cleaning, conformance, summaries and charts
python run_conformance.py

# Cleaning only: retained/excluded logs and case selection reports
python run_conformance.py --clean-only

# Complete run with comparison of timestamp-tie ordering conventions
python run_conformance.py --compare-tie-orders

# Complete run with a proposed reduction of the rule catalogue
python run_conformance.py --propose-rules

# Analyse the proposed catalogue generated from the default model
python run_conformance.py --model results/regole_c_20_proposed.decl --output-dir results_20_rules
```

Every conformance run includes cleaning. To change inputs, output directory or selection:

```bash
python run_conformance.py --log data_input/another_log.csv --model data_input/another_model.decl --output-dir results_custom
python run_conformance.py --min-events 3 --rule-ids R01,R04,R14
python run_conformance.py --endopoint-events none
```

The default retains cases with at least two events and the initial/final activities configured in `endpoint_events.json`. Use `--endopoint-events path/to/config.json` for another configuration. All options are listed by `python run_conformance.py --help`.

## Input and output

- `data_input/`: source CSV log (default: `curia_log_en_C_2016-2026.csv`) and DECLARE rule catalogue (`regole_c.decl`). The CSV requires `CaseID`, `Activity` and `Timestamp`; its default separator is `;` (`--separator` to change it).
- `results/`: cleaned and excluded logs, selection reports, conformance results, summaries, charts and execution manifests. `--output-dir` changes the destination. Generated files stay local; `.gitkeep` preserves the directory in Git.
- `results.zip`: archive recreated after every successful run, including cleaning only, from the selected output directory. ZIP archives are ignored by Git.

## Configuration

- `endpoint_events.json`: allowed activity labels in the `initial` and `final` lists; labels must match the log exactly.
- `run_conformance.py`: `DATA_INPUT`, `RESULTS` and `ENDPOINT_EVENTS_CONFIG` define default paths; command-line options override them.
- No API keys or environment variables are required. `.env.example` is available as a placeholder; local `.env` files are ignored by Git.
