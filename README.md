# MPSC Study

Python project for building a structured catalogue of MPSC learning resources and integrating quiz data where authorized.

## Current modules

- `main.py` — resumable, robots.txt-aware public catalogue scanner.
- `mpsc_quiz_audit.py` — audits known quiz pages and records page metadata, visible quiz controls, links, and assets.
- `mpsc_quiz_api_discovery.py` — observes normal browser-driven quiz API requests and records safe request/response schema metadata.
- `mpsc_quiz_api_contract_inspector.py` — inspects the quiz page's same-origin JavaScript assets for API action and pagination implementation hints.
- `mpsc_quiz_direct_api_probe.py` — browserless direct `requests` probe for the known History questions endpoint; checks `limit` behavior and frontend pagination hints without storing question content.
- `mpsc_quiz_id_overlap_probe.py` — makes three low-volume identical GET calls to compare returned question IDs and summarize subject/topic metadata; does not store question content.
- `mpsc_quiz_full_downloader.py` — resumable authorized question collector; backs up and cleans the local SQLite database before harvesting, skips existing IDs and exact question-plus-options duplicates, rejects structurally incomplete MCQs, and has no default per-filter call cap (pauses after 20 consecutive zero-new samples). Discovery scans `/quiz/` plus exactly the four user-supplied seed URLs (Indian Polity, English, Maths Quiz 1, and Science). It does not follow links from those four pages or add any other related pages. It only sends pages with the known `#quiz-runner` metadata to the API; static/embedded quizzes are reported for a separate extractor.
- `audit_mpsc_dataset.py` — one-command read-only data-quality audit and processing pipeline. Checks missing fields, invalid MCQ structure and answer-key labels, repeated options, malformed JSON, text/encoding issues, exact duplicates, and TF-IDF near-duplicate candidates. It calls `mpsc_question_db_builder.py` to create a separate normalized SQLite database and sorted subject/subtopic exports; the source collection DB is never modified.
- `mpsc_question_db_builder.py` — creates the normalized database tables `subjects`, `subtopics`, `questions`, `options`, `answers`, `explanations`, `question_audit`, and `metadata`. It preserves original available source fields in each question's `source_row_json`; absent source IDs/names/explanations remain NULL rather than being invented. `answers.correct_option_text` is taken from the option named by the supplied answer key, not independently fact-checked.

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
- Download quiz questions (no Chrome): `python mpsc_quiz_full_downloader.py` (unlimited calls by default; resumes interrupted filters from the saved SQLite checkpoint and skips filters previously paused as empty/stalled).
- Re-sample previously stalled/empty filters explicitly: `python mpsc_quiz_full_downloader.py --resample-finished`.
- Run the complete audit + cleaning + sorting + normalized DB build in one command: `python audit_mpsc_dataset.py`.
- Skip TF-IDF near-duplicate similarity for a faster run: `python audit_mpsc_dataset.py --similarity 0`.
- Use a non-default source database/output directory: `python audit_mpsc_dataset.py --db path/to/mpsc_questions.db --out path/to/audit_output`.
- Audit CSV/JSON reports and sorted exports are written under `mpsc_output/data_quality_audit/`; the new normalized DB is `mpsc_output/mpsc_questions_structured.db`.
- Exports include `questions_sorted.csv`, `subject_index.csv`, `subtopic_index.csv`, `options_sorted.csv`, and `answers_explanations_sorted.csv`, in addition to the detailed audit reports.
- The structured DB keeps IDs and bilingual names only when present in the source. It separates question options, answer-key label/selected option text, and Marathi/English explanations into their own tables. The full available source row is preserved in `questions.source_row_json`.
- The original `mpsc_questions.db` is opened read-only and is never changed. If a structured DB already exists, the builder makes a timestamped backup before replacing it.
- The audit's PASS/INVALID labels describe structural checks only. Correct-answer factual accuracy needs independent question-wise verification against trusted references.
- The API currently has no verified offset/page cursor, so resume preserves saved unique questions and per-filter progress rather than guaranteeing the exact next unseen server record.

## Local data

Runtime databases, CSV exports, and API audit reports are stored under `mpsc_output/` and are intentionally excluded from Git. Keep local databases and exports out of the public repository.

## Authorized data use

Use the quiz integration/import modules only within the scope of the written permission obtained from the website administrator. Follow any stated limits, attribution requirements, rate limits, and retention conditions. Do not bypass authentication or access controls. Keep credentials, cookies, local databases, and private exports out of Git.

## Git

Commit tested code changes to the `main` branch. Never commit `.env`, account credentials, cookies, local databases, or private exports.
