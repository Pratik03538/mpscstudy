# MPSC Study

Python project for building a structured catalogue of MPSC learning resources and auditing publicly visible quiz interfaces.

## Current modules

- `main.py` — resumable, robots.txt-aware public catalogue scanner. Reuses the local `mpsc_output/mpsc_catalog.db` database and records page/link/resource metadata.
- `mpsc_quiz_audit.py` — audits only known `/quiz/` pages from the catalogue and records page metadata, visible quiz controls, links, and script/iframe asset URLs.
- `mpsc_quiz_api_discovery.py` — opens a quiz page in a visible Playwright browser and records same-site Fetch/XHR request metadata while a person manually operates the page.

## Setup (Windows / PyCharm)

1. Open the project folder in PyCharm.
2. Create or activate the project's virtual environment.
3. Install dependencies: `pip install -r requirements.txt`.
4. Install Playwright Chromium: `playwright install chromium`.

## Run

- Catalogue scan: `python main.py`
- Quiz-only audit: `python mpsc_quiz_audit.py`
- Interactive network metadata capture: `python mpsc_quiz_api_discovery.py`
- Optional quiz URL argument: `python mpsc_quiz_api_discovery.py https://www.mpsctoday.com/quiz/history/`

## Local data

Runtime databases, CSV exports, and network audit files are stored under `mpsc_output/` and are intentionally excluded from Git. Keep your existing local `mpsc_output/mpsc_catalog.db` in the project folder; do not delete it when updating code.

## Important usage boundary

These modules are for public-page catalogue discovery and quiz-interface/network metadata inspection. They do not bulk-copy question text, options, answers, explanations, protected files, cookies, authorization headers, or response payloads. The MPSC Today policy text has conflicting statements about reuse and copying; obtain written permission before bulk-importing its question-bank content. Follow robots.txt, site terms, and any rate limits.

## Git

Commit tested code changes to the `main` branch. Never commit `.env`, account credentials, cookies, local databases, or private exports.