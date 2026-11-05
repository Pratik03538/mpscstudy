# MPSC Study

Python project for building a structured catalogue of MPSC learning resources and integrating quiz data where authorized.

## Current modules

- `main.py` — resumable, robots.txt-aware public catalogue scanner.
- `mpsc_quiz_audit.py` — audits known quiz pages and records page metadata, visible quiz controls, links, and assets.
- `mpsc_quiz_api_discovery.py` — observes normal browser-driven quiz API requests and records safe request/response schema metadata.
- `mpsc_quiz_api_contract_inspector.py` — inspects the quiz page's same-origin JavaScript assets for API action and pagination implementation hints.
- `mpsc_quiz_direct_api_probe.py` — browserless direct `requests` probe for the known History questions endpoint; checks `limit` behavior and frontend pagination hints without storing question content.
- `mpsc_quiz_id_overlap_probe.py` — makes three low-volume identical GET calls to compare returned question IDs and summarize subject/topic metadata; does not store question content.
- `mpsc_quiz_full_downloader.py` — resumable authorized question collector; backs up and cleans the local SQLite database before harvesting, skips existing IDs and exact question-plus-options duplicates, rejects structurally incomplete MCQs, and has no default per-filter call cap (pauses after 20 consecutive zero-new samples).

## Setup (Windows / PyCharm)

1. Open the project folder in PyCharm.
2. Create or activate the project's virtual environment.
3. Install dependencies: `pip install -r requirements.txt`.
4. Install Playwright Chromium: `playwright install chromium`.

## Run

- Catalogue scan: `python main.py`
- Quiz-only audit: `python mpsc_quiz_audit.py`
- Interactive API metadata capture: `python mpsc_quiz_api_discovery.py`
- Inspect frontend API contract: `python mpsc_quiz_api_contract_inspector.py`
- Direct terminal API probe (no Chrome): `python mpsc_quiz_direct_api_probe.py`
- Check whether the 100-question sample changes (no Chrome): `python mpsc_quiz_id_overlap_probe.py`
- Download quiz questions (no Chrome): `python mpsc_quiz_full_downloader.py` (unlimited calls by default; optional `--stall-rounds 20` controls the zero-new stopping threshold).

## Local data

Runtime databases, CSV exports, and API audit reports are stored under `mpsc_output/` and are intentionally excluded from Git. Keep local databases and exports out of the public repository.

## Authorized data use

Use the quiz integration/import modules only within the scope of the written permission obtained from the website administrator. Follow any stated limits, attribution requirements, rate limits, and retention conditions. Do not bypass authentication or access controls. Keep credentials, cookies, local databases, and private exports out of Git.

## Git

Commit tested code changes to the `main` branch. Never commit `.env`, account credentials, cookies, local databases, or private exports.