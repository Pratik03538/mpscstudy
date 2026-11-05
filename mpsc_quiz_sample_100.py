"""Download a small first sample of MPSC Today MCQs into a normalized SQLite database.

Default behaviour:
  - Subject: history
  - Target: 100 unique question IDs total
  - Maximum records stored: exactly the remaining amount up to target
  - API: one GET batch of up to 100 per request, repeating only if needed
  - No POSTs, answer submissions, or browser automation

The normalized database separates subjects, topics, questions, and options.
It also retains raw JSON for traceability and creates CSV/JSONL exports.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.robotparser import RobotFileParser

import requests

API_URL = "https://www.mpsctoday.com/api/quiz/index.php"
ROBOTS_URL = "https://www.mpsctoday.com/robots.txt"
USER_AGENT = "MPSCStudyAuthorizedIntegration/1.0 (authorized quiz data collection)"
TIMEOUT = 30
DEFAULT_SUBJECT = "history"
DEFAULT_TARGET = 100
DEFAULT_LIMIT = 100
DEFAULT_MAX_CALLS = 5
DEFAULT_DELAY = 2.0

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "mpsc_output" / "sample_100"
DB_PATH = OUT / "mpsc_questions_sample.db"
CSV_PATH = OUT / "questions_sample.csv"
JSONL_PATH = OUT / "questions_sample.jsonl"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def as_text(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def as_int(value) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def progress_bar(done: int, total: int, width: int = 24) -> str:
    total = max(total, 1)
    done = min(max(done, 0), total)
    filled = int(width * done / total)
    pct = 100.0 * done / total
    return f"[{'#' * filled}{'-' * (width - filled)}] {done}/{total} ({pct:.1f}%)"


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")

    con.executescript("""
        CREATE TABLE IF NOT EXISTS schema_info (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS subjects (
            subject_id INTEGER PRIMARY KEY,
            slug TEXT NOT NULL UNIQUE,
            name_en TEXT,
            name_mr TEXT,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS topics (
            topic_id INTEGER PRIMARY KEY,
            subject_id INTEGER NOT NULL,
            slug TEXT NOT NULL,
            name_en TEXT,
            name_mr TEXT,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            UNIQUE(subject_id, slug),
            FOREIGN KEY(subject_id) REFERENCES subjects(subject_id)
        );

        CREATE TABLE IF NOT EXISTS questions (
            question_id TEXT PRIMARY KEY,
            subject_id INTEGER NOT NULL,
            topic_id INTEGER,
            question_mr TEXT,
            question_en TEXT,
            correct_option TEXT,
            explanation_mr TEXT,
            explanation_en TEXT,
            is_pyq INTEGER,
            pyq_exam TEXT,
            pyq_year INTEGER,
            difficulty INTEGER,
            times_served INTEGER,
            times_attempted INTEGER,
            times_correct INTEGER,
            last_pushed_to_tg TEXT,
            is_published INTEGER,
            source_created_at TEXT,
            source_updated_at TEXT,
            raw_json TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            FOREIGN KEY(subject_id) REFERENCES subjects(subject_id),
            FOREIGN KEY(topic_id) REFERENCES topics(topic_id)
        );

        CREATE TABLE IF NOT EXISTS question_options (
            question_id TEXT NOT NULL,
            option_key TEXT NOT NULL CHECK(option_key IN ('A','B','C','D')),
            option_text TEXT,
            PRIMARY KEY(question_id, option_key),
            FOREIGN KEY(question_id) REFERENCES questions(question_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS import_runs (
            run_id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            subject_slug TEXT NOT NULL,
            target_unique_questions INTEGER NOT NULL,
            request_limit INTEGER NOT NULL,
            api_calls INTEGER NOT NULL DEFAULT 0,
            records_received INTEGER NOT NULL DEFAULT 0,
            new_questions_saved INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL,
            error_message TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_questions_subject_topic
            ON questions(subject_id, topic_id);
        CREATE INDEX IF NOT EXISTS idx_questions_pyq
            ON questions(is_pyq, pyq_year);
        CREATE INDEX IF NOT EXISTS idx_questions_difficulty
            ON questions(difficulty);
        CREATE INDEX IF NOT EXISTS idx_topics_subject
            ON topics(subject_id);
    """)

    con.execute(
        "INSERT OR REPLACE INTO schema_info(key, value) VALUES (?, ?)",
        ("schema_version", "1"),
    )
    con.execute(
        "INSERT OR REPLACE INTO schema_info(key, value) VALUES (?, ?)",
        ("database_purpose", "Initial 100-question sample from MPSC Today API"),
    )
    con.commit()
    return con


def count_questions(con: sqlite3.Connection) -> int:
    return int(con.execute("SELECT COUNT(*) FROM questions").fetchone()[0])


def check_robots(session: requests.Session) -> RobotFileParser:
    response = session.get(ROBOTS_URL, timeout=TIMEOUT)
    if response.status_code >= 400 and response.status_code != 404:
        raise RuntimeError(
            f"Could not verify robots.txt (HTTP {response.status_code}); stopping."
        )
    parser = RobotFileParser()
    parser.set_url(ROBOTS_URL)
    parser.parse(response.text.splitlines() if response.status_code == 200 else [])
    if not parser.can_fetch(USER_AGENT, API_URL):
        raise RuntimeError("robots.txt disallows the quiz API endpoint.")
    return parser


def ensure_taxonomy(con: sqlite3.Connection, item: dict) -> tuple[int | None, int | None]:
    subject_id = as_int(item.get("subject_id"))
    subject_slug = (as_text(item.get("subject_slug")) or "").strip().lower()
    if subject_id is None or not subject_slug:
        return None, None

    stamp = now_utc()
    con.execute("""
        INSERT INTO subjects(subject_id, slug, name_en, name_mr, first_seen_at, last_seen_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(subject_id) DO UPDATE SET
            slug=excluded.slug,
            name_en=COALESCE(excluded.name_en, subjects.name_en),
            name_mr=COALESCE(excluded.name_mr, subjects.name_mr),
            last_seen_at=excluded.last_seen_at
    """, (
        subject_id, subject_slug, as_text(item.get("subject_name_en")),
        as_text(item.get("subject_name_mr")), stamp, stamp,
    ))

    topic_id = as_int(item.get("topic_id"))
    topic_slug = (as_text(item.get("topic_slug")) or "").strip().lower()
    if topic_id is not None and topic_slug:
        con.execute("""
            INSERT INTO topics(topic_id, subject_id, slug, name_en, name_mr, first_seen_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(topic_id) DO UPDATE SET
                subject_id=excluded.subject_id,
                slug=excluded.slug,
                name_en=COALESCE(excluded.name_en, topics.name_en),
                name_mr=COALESCE(excluded.name_mr, topics.name_mr),
                last_seen_at=excluded.last_seen_at
        """, (
            topic_id, subject_id, topic_slug, as_text(item.get("topic_name_en")),
            as_text(item.get("topic_name_mr")), stamp, stamp,
        ))
    else:
        topic_id = None

    return subject_id, topic_id


def save_question(con: sqlite3.Connection, item: dict) -> bool:
    question_id = as_text(item.get("id"))
    if not question_id:
        return False

    subject_id, topic_id = ensure_taxonomy(con, item)
    if subject_id is None:
        logging.warning("Skipping question %s: missing subject_id/subject_slug", question_id)
        return False

    existed = con.execute(
        "SELECT 1 FROM questions WHERE question_id=?", (question_id,)
    ).fetchone() is not None
    stamp = now_utc()

    con.execute("""
        INSERT INTO questions (
            question_id, subject_id, topic_id,
            question_mr, question_en, correct_option,
            explanation_mr, explanation_en,
            is_pyq, pyq_exam, pyq_year, difficulty,
            times_served, times_attempted, times_correct,
            last_pushed_to_tg, is_published,
            source_created_at, source_updated_at,
            raw_json, first_seen_at, last_seen_at
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        ON CONFLICT(question_id) DO UPDATE SET
            subject_id=excluded.subject_id,
            topic_id=excluded.topic_id,
            question_mr=COALESCE(excluded.question_mr, questions.question_mr),
            question_en=COALESCE(excluded.question_en, questions.question_en),
            correct_option=COALESCE(excluded.correct_option, questions.correct_option),
            explanation_mr=COALESCE(excluded.explanation_mr, questions.explanation_mr),
            explanation_en=COALESCE(excluded.explanation_en, questions.explanation_en),
            is_pyq=COALESCE(excluded.is_pyq, questions.is_pyq),
            pyq_exam=COALESCE(excluded.pyq_exam, questions.pyq_exam),
            pyq_year=COALESCE(excluded.pyq_year, questions.pyq_year),
            difficulty=COALESCE(excluded.difficulty, questions.difficulty),
            times_served=COALESCE(excluded.times_served, questions.times_served),
            times_attempted=COALESCE(excluded.times_attempted, questions.times_attempted),
            times_correct=COALESCE(excluded.times_correct, questions.times_correct),
            last_pushed_to_tg=COALESCE(excluded.last_pushed_to_tg, questions.last_pushed_to_tg),
            is_published=COALESCE(excluded.is_published, questions.is_published),
            source_created_at=COALESCE(excluded.source_created_at, questions.source_created_at),
            source_updated_at=COALESCE(excluded.source_updated_at, questions.source_updated_at),
            raw_json=excluded.raw_json,
            last_seen_at=excluded.last_seen_at
    """, (
        question_id, subject_id, topic_id,
        as_text(item.get("question_mr")), as_text(item.get("question_en")),
        as_text(item.get("correct_option")),
        as_text(item.get("explanation_mr")), as_text(item.get("explanation_en")),
        as_int(item.get("is_pyq")), as_text(item.get("pyq_exam")),
        as_int(item.get("pyq_year")), as_int(item.get("difficulty")),
        as_int(item.get("times_served")), as_int(item.get("times_attempted")),
        as_int(item.get("times_correct")), as_text(item.get("last_pushed_to_tg")),
        as_int(item.get("is_published")), as_text(item.get("created_at")),
        as_text(item.get("updated_at")),
        json.dumps(item, ensure_ascii=False, separators=(",", ":")), stamp, stamp,
    ))

    for option_key, field in (
        ("A", "option_a"), ("B", "option_b"),
        ("C", "option_c"), ("D", "option_d"),
    ):
        con.execute("""
            INSERT INTO question_options(question_id, option_key, option_text)
            VALUES (?, ?, ?)
            ON CONFLICT(question_id, option_key) DO UPDATE SET
                option_text=COALESCE(excluded.option_text, question_options.option_text)
        """, (question_id, option_key, as_text(item.get(field))))

    return not existed


def export_data(con: sqlite3.Connection) -> None:
    rows = con.execute("""
        SELECT
            q.question_id,
            s.slug AS subject_slug,
            s.name_en AS subject_name_en,
            s.name_mr AS subject_name_mr,
            t.slug AS topic_slug,
            t.name_en AS topic_name_en,
            t.name_mr AS topic_name_mr,
            q.question_mr, q.question_en,
            MAX(CASE WHEN o.option_key='A' THEN o.option_text END) AS option_a,
            MAX(CASE WHEN o.option_key='B' THEN o.option_text END) AS option_b,
            MAX(CASE WHEN o.option_key='C' THEN o.option_text END) AS option_c,
            MAX(CASE WHEN o.option_key='D' THEN o.option_text END) AS option_d,
            q.correct_option, q.explanation_mr, q.explanation_en,
            q.is_pyq, q.pyq_exam, q.pyq_year, q.difficulty,
            q.is_published, q.source_created_at, q.source_updated_at,
            q.raw_json
        FROM questions q
        JOIN subjects s ON s.subject_id=q.subject_id
        LEFT JOIN topics t ON t.topic_id=q.topic_id
        LEFT JOIN question_options o ON o.question_id=q.question_id
        GROUP BY q.question_id
        ORDER BY s.slug, t.slug, q.question_id
    """).fetchall()

    if rows:
        with CSV_PATH.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(dict(row) for row in rows)
    else:
        with CSV_PATH.open("w", encoding="utf-8-sig") as handle:
            handle.write("question_id,subject_slug,topic_slug,question_mr,question_en\n")

    with JSONL_PATH.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(row["raw_json"] + "\n")

    print(
        f"[EXPORT DONE] {len(rows)} questions | DB={DB_PATH} | CSV={CSV_PATH} | JSONL={JSONL_PATH}",
        flush=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Download a first sample of 100 MPSC Today MCQs into normalized SQLite."
    )
    parser.add_argument("--subject", default=DEFAULT_SUBJECT, help="Subject slug (default: history)")
    parser.add_argument("--target", type=int, default=DEFAULT_TARGET, help="Maximum total unique questions to store (default: 100)")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="Maximum records requested per API call (default: 100)")
    parser.add_argument("--max-calls", type=int, default=DEFAULT_MAX_CALLS, help="Maximum API calls in this sample run (default: 5)")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY, help="Delay between API calls in seconds (default: 2)")
    args = parser.parse_args()

    if args.target < 1 or args.limit < 1 or args.max_calls < 1 or args.delay < 0:
        raise SystemExit("target/limit/max-calls must be positive; delay cannot be negative.")

    OUT.mkdir(parents=True, exist_ok=True)
    con = open_db(DB_PATH)
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})

    run_id = None
    saved_this_run = 0
    api_calls = 0
    records_received = 0
    status = "running"

    try:
        robots = check_robots(session)
        current = count_questions(con)
        print("\nMPSC TODAY — INITIAL 100 MCQ SAMPLE", flush=True)
        print(f"Subject: {args.subject} | Target: {args.target} unique questions", flush=True)
        print(f"Already in sample DB: {current}", flush=True)
        print(f"Database schema: subjects → topics → questions → question_options", flush=True)

        if current >= args.target:
            print(f"[TARGET ALREADY MET] {current}/{args.target}; no further API request sent.", flush=True)
            status = "target_already_met"
            export_data(con)
            return 0

        con.execute("""
            INSERT INTO import_runs(started_at, subject_slug, target_unique_questions,
                                    request_limit, status)
            VALUES (?, ?, ?, ?, 'running')
        """, (now_utc(), args.subject, args.target, args.limit))
        run_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
        con.commit()

        while count_questions(con) < args.target and api_calls < args.max_calls:
            remaining = args.target - count_questions(con)
            requested_limit = min(args.limit, max(remaining, 1))
            params = {
                "action": "questions",
                "limit": requested_limit,
                "subject": args.subject.strip().lower(),
            }

            print(
                f"\n[REQUEST {api_calls + 1}/{args.max_calls}] subject={args.subject} "
                f"| asking for up to {requested_limit} | {progress_bar(count_questions(con), args.target)}",
                flush=True,
            )
            started = time.perf_counter()
            response = session.get(API_URL, params=params, timeout=TIMEOUT)
            elapsed = time.perf_counter() - started
            api_calls += 1

            if response.status_code in (429, 503):
                status = "paused_rate_limit"
                raise RuntimeError(
                    f"Server asked us to slow down (HTTP {response.status_code}); stopping safely."
                )
            response.raise_for_status()
            if "application/json" not in response.headers.get("Content-Type", "").lower():
                status = "paused_invalid_response"
                raise RuntimeError("API returned a non-JSON response.")

            payload = response.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                status = "paused_invalid_response"
                raise RuntimeError("API response did not contain the expected data array.")

            records = [item for item in payload["data"] if isinstance(item, dict)]
            records_received += len(records)
            if not records:
                status = "no_data"
                print("[NO DATA] API returned zero MCQs for this subject.", flush=True)
                break

            new_count = 0
            rejected = 0
            for item in records:
                response_subject = (as_text(item.get("subject_slug")) or "").strip().lower()
                if response_subject and response_subject != args.subject.strip().lower():
                    rejected += 1
                    continue

                # Never cross the requested total target, even if the API ignores limit.
                already_saved = count_questions(con)
                if already_saved >= args.target:
                    break
                if save_question(con, item):
                    new_count += 1

            con.commit()
            saved_this_run += new_count
            total = count_questions(con)

            con.execute("""
                UPDATE import_runs
                SET api_calls=?, records_received=?, new_questions_saved=?, status=?
                WHERE run_id=?
            """, (api_calls, records_received, saved_this_run, "running", run_id))
            con.commit()

            print(
                f"{progress_bar(total, args.target)} | HTTP {response.status_code} | "
                f"received={len(records)} | NEW +{new_count} | rejected={rejected} | "
                f"DB TOTAL={total}/{args.target} | {len(response.content):,} bytes | {elapsed:.2f}s",
                flush=True,
            )

            if total >= args.target:
                status = "target_reached"
                break
            if new_count == 0:
                print(
                    "[NO NEW IDS] This sample did not add new unique questions; "
                    "will try another sample if call budget remains.",
                    flush=True,
                )
            time.sleep(args.delay)

        if count_questions(con) >= args.target:
            status = "target_reached"
        elif status == "running":
            status = "call_limit_reached"

    except KeyboardInterrupt:
        status = "interrupted"
        print("\n[INTERRUPTED] Saved questions are committed; exporting current sample.", flush=True)
    except Exception as exc:
        if status == "running":
            status = "error"
        print(f"\n[STOPPED] {type(exc).__name__}: {exc}", flush=True)
    finally:
        total = count_questions(con)
        export_data(con)
        if run_id is not None:
            con.execute("""
                UPDATE import_runs
                SET finished_at=?, api_calls=?, records_received=?,
                    new_questions_saved=?, status=?, error_message=?
                WHERE run_id=?
            """, (
                now_utc(), api_calls, records_received, saved_this_run,
                status, None if status not in {"error", "paused_rate_limit", "paused_invalid_response"} else "See terminal output",
                run_id,
            ))
            con.commit()

        print("\n" + "=" * 62, flush=True)
        print(f"FINAL STATUS: {status}", flush=True)
        print(f"Unique MCQs in database: {total}/{args.target}", flush=True)
        print(f"New MCQs saved this run: {saved_this_run}", flush=True)
        print(f"API requests made: {api_calls}", flush=True)
        print(f"SQLite: {DB_PATH.resolve()}", flush=True)
        print(f"CSV:    {CSV_PATH.resolve()}", flush=True)
        print(f"JSONL:  {JSONL_PATH.resolve()}", flush=True)
        print("=" * 62, flush=True)
        con.close()
        session.close()

    return 0 if status in {"target_reached", "target_already_met"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
