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
- `sample_one_question_per_subtopic.py` — samples one random question from every subtopic in the normalized DB, exporting bilingual subject/subtopic IDs and names, question ID/text, four options, answer-key data, available explanations, audit warnings, and a coverage report. Opens the normalized DB read-only; does not fact-check question placement or answer correctness.
- `mpsc_taxonomy_audit.py` — groups the full question export by current subject/subtopic labels, detects reused numeric IDs, and writes diagnostic taxonomy reports. It does not automatically change question assignments.
- `audit_mpsc_dataset.py` — one-command read-only audit and processing pipeline. Required fields are question text, all four options, and a valid correct-answer key pointing to an available option. Missing explanations are allowed and do not reduce the audit status. It checks text/encoding quality and repeated options as warnings. Exact duplicate detection compares question text only (Marathi OR English); options, answer keys and explanations are ignored. Indic-script Unicode combining marks (including Devanagari vowel signs) are preserved during matching so distinct Marathi spellings are not collapsed. TF-IDF near-duplicate analysis also uses question text only. It calls `mpsc_question_db_builder.py` to create a separate normalized SQLite database and sorted subject/subtopic exports; the source collection DB is never modified.
- `mpsc_question_db_builder.py` — creates the normalized database tables `subjects`, `subtopics`, `questions`, `options`, `answers`, `explanations`, `question_audit`, and `metadata`. It preserves original available source fields in each question's `source_row_json`; absent source IDs/names/explanations remain NULL rather than being invented. `answers.correct_option_text` is taken from the option named by the supplied answer key, not independently fact-checked. Duplicate group metadata is stored in `question_audit`. Internal subject/subtopic keys combine source numeric IDs with their slugs, because the source data reuses subject ID 3 and topic ID 7 for different labels; this prevents unrelated records from being merged in the normalized database. It does not by itself correct inaccurate source subject/subtopic assignments.
- `github_report_sync.py` — uploads the generated audit CSV/JSON/JSONL reports and the new structured SQLite database to `mpsc_output/audit_reports/latest/` in this repository. Authentication is read from `MPSC_GITHUB_TOKEN` (or `GITHUB_TOKEN`); no token is stored in source code.

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
- Export one random question from every subtopic: `python sample_one_question_per_subtopic.py` (optional reproducible selection: `python sample_one_question_per_subtopic.py --seed 12345`).
- Run subject/subtopic taxonomy diagnostics after `questions_sorted.csv` exists: `python mpsc_taxonomy_audit.py`. Reports are written locally under `mpsc_output/data_quality_audit/`; reviewed aggregate copies are committed under `mpsc_output/audit_reports/taxonomy/`.
- After the audit has already completed, upload existing local reports/database without repeating the analysis: `python github_report_sync.py`.
- Skip TF-IDF near-duplicate similarity for a faster run: `python audit_mpsc_dataset.py --similarity 0`.
- Use a non-default source database/output directory: `python audit_mpsc_dataset.py --db path/to/mpsc_questions.db --out path/to/audit_output`.
- Audit CSV/JSON reports and sorted exports are written under `mpsc_output/data_quality_audit/`; the new normalized DB is `mpsc_output/mpsc_questions_structured.db`.
- Exports include `questions_sorted.csv`, `subject_index.csv`, `subtopic_index.csv`, `options_sorted.csv`, and `answers_explanations_sorted.csv`, in addition to the detailed audit reports.
- The structured DB keeps IDs and bilingual names only when present in the source. It separates question options, answer-key label/selected option text, and Marathi/English explanations into their own tables. The full available source row is preserved in `questions.source_row_json`. The audit also writes `pass_questions_only.csv`, `review_queue_unique.csv` (one retained record per exact question-text group), and `invalid_questions.csv` for a repeatable manual-review workflow.
- The original `mpsc_questions.db` is opened read-only and is never changed. If a structured DB already exists, the builder makes a timestamped backup before replacing it.
- Automatic GitHub report sync runs at the end of the audit. To enable uploads, create a fine-grained personal access token scoped only to this repository with **Contents: Read and write** permission, then set it as the Windows user environment variable `MPSC_GITHUB_TOKEN`. Do not paste the token into code or commit it. Reports and the structured DB will be visible in this public repository.
- To store the token securely from PowerShell (input is hidden), run:
  ```powershell
  $secure = Read-Host "GitHub token" -AsSecureString
  $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
  try {
      $token = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
      [Environment]::SetEnvironmentVariable("MPSC_GITHUB_TOKEN", $token, "User")
      $env:MPSC_GITHUB_TOKEN = $token
  } finally {
      [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)
  }
  ```
  Restart PyCharm after storing the user environment variable. Rerun `python audit_mpsc_dataset.py`; the latest report files are replaced in GitHub and the upload status is saved in `github_sync_manifest.json`.
- The audit's PASS/INVALID labels describe the presence/format of question, four options and a valid answer key, plus text-quality warnings. They do not confirm factual answer accuracy; that still needs independent question-wise verification against trusted references.
- The API currently has no verified offset/page cursor, so resume preserves saved unique questions and per-filter progress rather than guaranteeing the exact next unseen server record.

## Local data

Runtime databases, CSV exports, and API audit reports are stored under `mpsc_output/` and are intentionally excluded from Git. Keep local databases and exports out of the public repository.

## Authorized data use

Use the quiz integration/import modules only within the scope of the written permission obtained from the website administrator. Follow any stated limits, attribution requirements, rate limits, and retention conditions. Do not bypass authentication or access controls. Keep credentials, cookies, local databases, and private exports out of Git.

## Git

Commit tested code changes to the `main` branch. Never commit `.env`, account credentials, cookies, local databases, or private exports.
