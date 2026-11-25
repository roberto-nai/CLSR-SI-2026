# InfoCuria event log downloader

This program is developed for Python 3.12.

Download case events and document texts from InfoCuria, then analyse and export event logs with PM4Py.

## Python 3.12 environment

Run all commands from this directory. For the initial setup:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

For subsequent sessions, activate the existing environment:

```bash
source .venv/bin/activate
```

## 1. Download the event dataset

Select cases by the closure year of their main proceedings (inclusive). Defaults: **2017–2026**, all three courts (`C`, `T`, `F`), English labels.

```bash
# Download the complete dataset for the selected interval.
python 01_dataset_downloader.py --start-year 2017 --end-year 2026

# Include historical cases.
python 01_dataset_downloader.py --start-year 1952 --end-year 2026

# Verify and save the case index without downloading events.
python 01_dataset_downloader.py --discover-only

# Download a sample or a specific case.
python 01_dataset_downloader.py --max-cases 20
python 01_dataset_downloader.py --case 'C-48/24'

# Repair Country values in an existing dataset, when needed.
python 01_dataset_downloader.py --enrich-countries data/curia_scraping_en_partial.csv
```

Rerun with the same `data/` directory to reuse cached case details, including when changing the year interval. Add `--refresh` to retrieve them again. The search index is always checked live.

The main output is `data/curia_scraping_en.csv`; case-detail errors produce `_partial` CSVs, and sample runs use `_sample`. Check `download_report_en.json` for completeness and errors.

## 2. Download document texts

Use the event CSV from phase 1 as input. Select the court with `--case-type` (`C`, `T`, `F`) and the case language with `--country` (for example, `EN`, `FR`, `IT`). Defaults: `C` and `EN`.

```bash
python 02_dataset_text_downloader.py data/curia_scraping_en.csv --case-type C --country EN

# Create only the filtered manifest, without downloading documents.
python 02_dataset_text_downloader.py data/curia_scraping_en.csv --manifest-only

# Disable the default initial/final activity filter.
python 02_dataset_text_downloader.py data/curia_scraping_en.csv --endpoint-events none
```

By default, `endpoint_events.json` selects whole cases whose first and last chronological activities match its `initial` and `final` lists. Use `--endpoint-events custom_endpoint_events.json` for another configuration.

Outputs include original HTML/PDF documents, extracted TXT files, a selection manifest, `_index.csv` and a JSON download report in `data_text/`. Rerunning reuses verified files and completes missing downloads or texts; add `--refresh` to download and extract them again.

## 3. Analyse and export event logs

Open `03_dataset_analyser.ipynb`, select the Python 3.12 kernel from `.venv` and run all cells from this directory.

In the configuration cell:

- `INPUT_FILE` selects the dataset; by default, the notebook uses `data/curia_scraping_en.csv`, falling back to `data/curia_scraping_en_partial.csv`.
- `START_YEAR` and `END_YEAR` select event timestamp years (default **2016–2026**, inclusive). Only cases whose valid timestamps all fall within the interval are retained, with their complete prepared history.
- `OUTPUT_DIR` selects the export directory (default `event_log/`).

The notebook exports the complete log and separate `C`, `T` and `F` logs as CSV and XES, plus statistics and activity lists. Filenames include the selected interval, for example `curia_log_en_C_2016-2026.xes`, `_stats_2016-2026.csv` and `_activities_2016-2026.csv`.

## Input and output folders

| Folder / file | Role and contents |
| --- | --- |
| `data/` | Output of phase 1 and input to phases 2–3: event CSVs, case inventory, search index, reports and persistent SQLite cache. |
| `data_text/` | Output of phase 2: original documents, extracted texts, manifests, file index and download reports. |
| `event_log/` | Output of phase 3: prepared CSV/XES logs, statistics and activity lists. |
| `endpoint_events.json` | Input configuration for the initial/final activity filter in phase 2. |

Generated folders contain a versionable `.gitkeep`; `.gitignore` excludes all their other contents, including subfolders. CSV files use UTF-8 and a semicolon delimiter.

For all script options:

```bash
python 01_dataset_downloader.py --help
python 02_dataset_text_downloader.py --help
```
