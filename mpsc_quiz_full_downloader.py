"""Authorized, resumable MPSC Today quiz question collector.

Discovers quiz pages under /quiz/, reads #quiz-runner data-subject/data-topic,
then calls the public GET question endpoint. It stores complete returned records
in SQLite, deduplicates by question ID, and exports CSV + JSONL.

The endpoint has so far returned changing 100-record samples for identical
subject queries, and no documented page/offset cursor was found in the frontend.
This collector therefore re-samples conservatively until repeated calls add no
new IDs, with a hard per-filter call cap. This is a practical harvest, not a
mathematical proof of completeness. If the endpoint does not expose all records,
request an official database export or documented pagination contract from the
site administrator.

No POST requests or answer submissions are made.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sqlite3
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.mpsctoday.com"
HUB_URL = BASE_URL + "/quiz/"
API_URL = BASE_URL + "/api/quiz/index.php"
ROBOTS_URL = BASE_URL + "/robots.txt"
USER_AGENT = "MPSCStudyAuthorizedIntegration/1.0 (authorized quiz data collection)"
REQUEST_TIMEOUT = 30
PAGE_DELAY_SECONDS = 0.5
API_LIMIT = 100
DEFAULT_API_DELAY = 2.0
DEFAULT_MAX_CALLS = 250
DEFAULT_STALL_ROUNDS = 8
DEFAULT_MAX_PAGES = 500

PROJECT_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = PROJECT_DIR / "mpsc_output"
DB_PATH = OUTPUT_DIR / "mpsc_questions.db"
EXPORT_DIR = OUTPUT_DIR / "question_exports"
CSV_PATH = EXPORT_DIR / "questions.csv"
JSONL_PATH = EXPORT_DIR / "questions.jsonl"

QUESTION_FIELDS = (
    "id",
    "subject_id",
    "topic_id",
    "question_mr",
    "question_en",
    "option_a",
    "option_b",
    "option_c",
    "option_d",
    "correct_option",
    "explanation_mr",
    "explanation_en",
    "is_pyq",
    "pyq_exam",
    "pyq_year",
    "difficulty",
    "times_served",
    "times_attempted",
    "times_correct",
    "last_pushed_to_tg",
    "is_published",
    "created_at",
    "updated_at",
    "subject_slug",
    "subject_name_en",
    "subject_name_mr",
    "topic_slug",
    "topic_name_en",
    "topic_name_mr",
)

TEXT_FIELDS = {
    "id", "question_mr", "question_en", "option_a", "option_b",
    "option_c", "option_d", "correct_option", "explanation_mr",
    "explanation_en", "pyq_exam", "created_at", "updated_at",
    "last_pushed_to_tg", "subject_slug", "subject_name_en",
    "subject_name_mr", "topic_slug", "topic_name_en", "topic_name_mr",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def progress_bar(current: int, total: int, width: int = 22) -> str:
    """Visual indicator of API calls used in this run, not question-bank completeness."""
    total = max(total, 1)
    current = min(max(current, 0), total)
    filled = int(width * current / total)
    return "[" + "#" * filled + "-" * (width - filled) + f"] {current}/{total}"


def database_question_count(con: sqlite3.Connection) -> int:
    return int(con.execute("SELECT COUNT(*) FROM questions").fetchone()[0])


def normalize_quiz_url(raw_url: str) -> str | None:
    parts = urlsplit(raw_url)
    host = (parts.hostname or "").lower()
    if host not in {"mpsctoday.com", "www.mpsctoday.com"}:
        return None
    path = parts.path or "/"
    if not path.startswith("/quiz/"):
        return None
    path = "/" + "/".join(part for part in path.split("/") if part)
    if path == "/quiz":
        path = "/quiz/"
    elif raw_url.endswith("/") and not path.endswith("/"):
        path += "/"
    return urlunsplit(("https", "www.mpsctoday.com", path, "", ""))


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/html;q=0.9",
    })
    return session


def load_robots(session: requests.Session) -> RobotFileParser:
    response = session.get(ROBOTS_URL, timeout=REQUEST_TIMEOUT)
    if response.status_code >= 400 and response.status_code != 404:
        raise RuntimeError(
            f"Could not verify robots.txt (HTTP {response.status_code}); stopping."
        )
    parser = RobotFileParser()
    parser.set_url(ROBOTS_URL)
    parser.parse(response.text.splitlines() if response.status_code == 200 else [])
    return parser


def init_db() -> sqlite3.Connection:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA foreign_keys=ON")

    columns = []
    for field in QUESTION_FIELDS:
        if field == "id":
            columns.append('"id" TEXT PRIMARY KEY')
        elif field in TEXT_FIELDS:
            columns.append(f'"{field}" TEXT')
        else:
            columns.append(f'"{field}" NUMERIC')
    columns.extend([
        '"raw_json" TEXT NOT NULL',
        '"first_seen_at" TEXT NOT NULL',
        '"last_seen_at" TEXT NOT NULL',
    ])
    con.execute(
        "CREATE TABLE IF NOT EXISTS questions ("
        + ", ".join(columns)
        + ")"
    )
    con.execute("""
        CREATE TABLE IF NOT EXISTS harvest_filters (
            filter_key TEXT PRIMARY KEY,
            subject_slug TEXT NOT NULL,
            topic_slug TEXT NOT NULL DEFAULT '',
            page_urls_json TEXT NOT NULL,
            api_calls INTEGER NOT NULL DEFAULT 0,
            new_rows INTEGER NOT NULL DEFAULT 0,
            consecutive_no_new INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'pending',
            last_response_count INTEGER,
            last_error TEXT,
            updated_at TEXT NOT NULL
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS harvest_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filter_key TEXT NOT NULL,
            call_number INTEGER NOT NULL,
            returned_rows INTEGER NOT NULL,
            new_rows INTEGER NOT NULL,
            response_bytes INTEGER NOT NULL,
            http_status INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    con.commit()
    return con


def discover_quiz_filters(
    session: requests.Session,
    robots: RobotFileParser,
    max_pages: int,
) -> dict[str, dict]:
    queue = deque([HUB_URL])
    queued = {HUB_URL}
    visited: set[str] = set()
    filters: dict[str, dict] = {}

    print(f"\n[DISCOVERY] Scanning quiz pages from {HUB_URL}", flush=True)
    while queue and len(visited) < max_pages:
        page_url = queue.popleft()
        if page_url in visited:
            continue
        visited.add(page_url)
        print(
            f"\r[DISCOVERY] pages={len(visited)}/{max_pages} | "
            f"filters found={len(filters)} | links queued={len(queue)}",
            end="",
            flush=True,
        )

        if not robots.can_fetch(USER_AGENT, page_url):
            logging.warning("robots.txt disallows page; skipped: %s", page_url)
            continue

        try:
            response = session.get(page_url, timeout=REQUEST_TIMEOUT)
            if response.status_code >= 400:
                logging.warning("Page HTTP %s; skipped: %s", response.status_code, page_url)
                continue
            if "text/html" not in response.headers.get("Content-Type", "").lower():
                continue
            soup = BeautifulSoup(response.text, "html.parser")
        except requests.RequestException as exc:
            logging.warning("Page fetch failed (%s): %s", exc, page_url)
            continue

        runner = soup.find(id="quiz-runner")
        if runner is not None:
            subject = str(runner.get("data-subject") or "").strip().lower()
            topic = str(runner.get("data-topic") or "").strip().lower()
            daily = str(runner.get("data-daily") or "").strip().lower()
            if subject and daily != "true":
                key = subject + "|" + topic
                if key not in filters:
                    filters[key] = {
                        "subject": subject,
                        "topic": topic,
                        "page_urls": [],
                    }
                filters[key]["page_urls"].append(response.url)
                logging.info(
                    "Found API filter: subject=%s topic=%s (%s)",
                    subject, topic or "<all topics>", response.url,
                )

        for anchor in soup.find_all("a", href=True):
            candidate = normalize_quiz_url(urljoin(response.url, anchor["href"]))
            if candidate and candidate not in queued and candidate not in visited:
                queued.add(candidate)
                queue.append(candidate)

        time.sleep(PAGE_DELAY_SECONDS)

    print()
    logging.info(
        "Page discovery finished: pages=%s filters=%s queue_remaining=%s",
        len(visited), len(filters), len(queue),
    )
    print(
        f"[DISCOVERY DONE] {len(visited)} pages checked | "
        f"{len(filters)} subject/topic filters found | {len(queue)} links left in queue",
        flush=True,
    )
    return filters


def register_filter(con: sqlite3.Connection, key: str, config: dict) -> sqlite3.Row:
    existing = con.execute(
        "SELECT * FROM harvest_filters WHERE filter_key=?", (key,)
    ).fetchone()
    page_urls = sorted(set(config["page_urls"]))
    if existing:
        old_urls = json.loads(existing["page_urls_json"])
        merged_urls = sorted(set(old_urls) | set(page_urls))
        con.execute(
            """UPDATE harvest_filters
               SET page_urls_json=?, updated_at=?
               WHERE filter_key=?""",
            (json.dumps(merged_urls, ensure_ascii=False), utc_now(), key),
        )
    else:
        con.execute(
            """INSERT INTO harvest_filters
               (filter_key, subject_slug, topic_slug, page_urls_json, updated_at)
               VALUES (?, ?, ?, ?, ?)""",
            (
                key,
                config["subject"],
                config["topic"],
                json.dumps(page_urls, ensure_ascii=False),
                utc_now(),
            ),
        )
    con.commit()
    return con.execute(
        "SELECT * FROM harvest_filters WHERE filter_key=?", (key,)
    ).fetchone()


def insert_questions(
    con: sqlite3.Connection,
    records: list[dict],
    subject_filter: str,
    topic_filter: str,
) -> tuple[int, int]:
    inserted = 0
    rejected = 0
    now = utc_now()
    columns = list(QUESTION_FIELDS) + ["raw_json", "first_seen_at", "last_seen_at"]
    quoted_columns = ", ".join(f'"{name}"' for name in columns)
    placeholders = ", ".join("?" for _ in columns)
    updates = []
    for field in QUESTION_FIELDS:
        if field == "id":
            continue
        updates.append(
            f'"{field}"=COALESCE(excluded."{field}", questions."{field}")'
        )
    updates.extend([
        '"raw_json"=excluded."raw_json"',
        '"last_seen_at"=excluded."last_seen_at"',
    ])
    sql = (
        f"INSERT INTO questions ({quoted_columns}) VALUES ({placeholders}) "
        f"ON CONFLICT(id) DO UPDATE SET {', '.join(updates)}"
    )

    values_to_write = []
    for item in records:
        qid = item.get("id")
        if qid is None or str(qid).strip() == "":
            rejected += 1
            continue

        item_subject = str(item.get("subject_slug") or "").strip().lower()
        item_topic = str(item.get("topic_slug") or "").strip().lower()
        if item_subject and item_subject != subject_filter:
            rejected += 1
            continue
        if topic_filter and item_topic and item_topic != topic_filter:
            rejected += 1
            continue

        row = []
        for field in QUESTION_FIELDS:
            value = item.get(field)
            if value is not None and field in TEXT_FIELDS:
                value = str(value)
            row.append(value)
        row.extend([
            json.dumps(item, ensure_ascii=False, separators=(",", ":")),
            now,
            now,
        ])
        values_to_write.append(tuple(row))

    before = con.total_changes
    if values_to_write:
        con.executemany(sql, values_to_write)
    changed = con.total_changes - before

    # Count genuinely new IDs separately from updates to existing IDs.
    inserted_ids = {
        str(item.get("id"))
        for item in records
        if item.get("id") is not None
        and str(item.get("subject_slug") or subject_filter).strip().lower() == subject_filter
        and (
            not topic_filter
            or not str(item.get("topic_slug") or topic_filter).strip().lower()
            or str(item.get("topic_slug") or "").strip().lower() == topic_filter
        )
    }
    # SQLite total_changes includes updates; use a pre-insert existence check.
    new_count = 0
    for qid in inserted_ids:
        # The caller records IDs before insertion in normal operation. This fallback
        # is intentionally replaced by exact change tracking in harvest_filter().
        pass
    return changed, rejected


def collect_one_filter(
    session: requests.Session,
    robots: RobotFileParser,
    con: sqlite3.Connection,
    key: str,
    config: dict,
    delay: float,
    max_calls: int,
    stall_rounds: int,
) -> tuple[int, bool]:
    state = register_filter(con, key, config)
    if state["status"] == "no_data":
        logging.info("Skipping filter with confirmed empty response: %s", key)
        print(f"[SKIP] {key} | previous API response contained no records.", flush=True)
        return 0, False
    if state["status"] in {"complete", "stalled_no_new", "max_calls_reached"}:
        # A previous no-new streak is not proof that the complete bank was downloaded.
        con.execute(
            """UPDATE harvest_filters
               SET status='running', consecutive_no_new=0, updated_at=?
               WHERE filter_key=?""",
            (utc_now(), key),
        )
        con.commit()
        state = con.execute(
            "SELECT * FROM harvest_filters WHERE filter_key=?", (key,)
        ).fetchone()
        print(
            f"[RESUME] {key} | starting a fresh sample pass; earlier no-new calls "
            "were not treated as proof of full completeness.",
            flush=True,
        )

    subject = config["subject"]
    topic = config["topic"]
    params = {"action": "questions", "limit": API_LIMIT, "subject": subject}
    if topic:
        params["topic"] = topic

    existing_total = database_question_count(con)
    print(
        f"\n[DOWNLOAD START] {key} | existing unique questions in DB={existing_total}",
        flush=True,
    )
    print(
        f"[PROGRESS] Bar below tracks API calls used out of {max_calls}; "
        "it is not a percentage of the site's full question bank.",
        flush=True,
    )
    logging.info(
        "Starting harvest: subject=%s topic=%s max_calls_this_run=%s",
        subject, topic or "<all topics>", max_calls,
    )
    calls_this_run = 0
    total_new_this_run = 0
    global_stop = False

    for attempt in range(1, max_calls + 1):
        if not robots.can_fetch(USER_AGENT, API_URL):
            raise RuntimeError("robots.txt disallows API URL; stopping.")
        print(
            f"[REQUEST] {key} | API call {attempt}/{max_calls} | "
            f"requesting up to {API_LIMIT} records...",
            flush=True,
        )
        request_started = time.perf_counter()
        try:
            response = session.get(API_URL, params=params, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            con.execute(
                """UPDATE harvest_filters SET status='paused_error', last_error=?,
                   updated_at=? WHERE filter_key=?""",
                (f"{type(exc).__name__}: {exc}", utc_now(), key),
            )
            con.commit()
            print(
                f"\n[DOWNLOAD PAUSED] {key} | request failed: {type(exc).__name__}: {exc} | "
                f"saved so far={database_question_count(con)}",
                flush=True,
            )
            logging.error("Request error for %s: %s", key, exc)
            break

        calls_this_run += 1
        request_seconds = time.perf_counter() - request_started
        if response.status_code in (429, 503):
            message = f"Server requested backoff: HTTP {response.status_code}"
            con.execute(
                """UPDATE harvest_filters SET status='paused_rate_limit',
                   last_error=?, updated_at=? WHERE filter_key=?""",
                (message, utc_now(), key),
            )
            con.commit()
            print(
                f"\n[SERVER BACKOFF] {key} | HTTP {response.status_code}. "
                "Stopped requests; existing database data is preserved.",
                flush=True,
            )
            logging.error("%s; stopping all filters without retry.", message)
            global_stop = True
            break

        if response.status_code >= 400:
            message = f"HTTP {response.status_code}"
            con.execute(
                """UPDATE harvest_filters SET status='paused_http_error',
                   last_error=?, updated_at=? WHERE filter_key=?""",
                (message, utc_now(), key),
            )
            con.commit()
            print(
                f"\n[HTTP ERROR] {key} | HTTP {response.status_code} | "
                f"saved so far={database_question_count(con)}",
                flush=True,
            )
            logging.error("%s for filter %s; moving on.", message, key)
            break

        if "application/json" not in response.headers.get("Content-Type", "").lower():
            message = "API returned non-JSON content"
            con.execute(
                """UPDATE harvest_filters SET status='paused_invalid_response',
                   last_error=?, updated_at=? WHERE filter_key=?""",
                (message, utc_now(), key),
            )
            con.commit()
            print(
                f"\n[INVALID RESPONSE] {key} | expected JSON, got "
                f"{response.headers.get('Content-Type', 'unknown')}",
                flush=True,
            )
            logging.error("%s for filter %s.", message, key)
            break

        try:
            payload = response.json()
        except ValueError:
            payload = None
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            message = "Unexpected API JSON shape"
            con.execute(
                """UPDATE harvest_filters SET status='paused_invalid_response',
                   last_error=?, updated_at=? WHERE filter_key=?""",
                (message, utc_now(), key),
            )
            con.commit()
            print(
                f"\n[INVALID JSON SHAPE] {key} | saved so far={database_question_count(con)}",
                flush=True,
            )
            logging.error("%s for filter %s.", message, key)
            break

        records = [item for item in payload["data"] if isinstance(item, dict)]
        accepted = []
        for item in records:
            item_subject = str(item.get("subject_slug") or "").strip().lower()
            item_topic = str(item.get("topic_slug") or "").strip().lower()
            if item_subject and item_subject != subject:
                continue
            if topic and item_topic and item_topic != topic:
                continue
            if item.get("id") is not None:
                accepted.append(item)

        ids = [str(item["id"]) for item in accepted]
        existing_ids = set()
        if ids:
            for start in range(0, len(ids), 800):
                chunk = ids[start:start + 800]
                placeholders = ",".join("?" for _ in chunk)
                existing_ids.update(
                    row[0] for row in con.execute(
                        f"SELECT id FROM questions WHERE id IN ({placeholders})", chunk
                    ).fetchall()
                )
        new_count = len(set(ids) - existing_ids)
        _, rejected_count = insert_questions(con, records, subject, topic)

        state = con.execute(
            "SELECT * FROM harvest_filters WHERE filter_key=?", (key,)
        ).fetchone()
        next_no_new = state["consecutive_no_new"] + 1 if new_count == 0 else 0
        status = "running"
        count_value = payload.get("count") if isinstance(payload.get("count"), int) else None

        if not records and count_value == 0:
            status = "no_data"
        elif next_no_new >= stall_rounds:
            status = "stalled_no_new"

        con.execute(
            """UPDATE harvest_filters
               SET api_calls=api_calls+1,
                   new_rows=new_rows+?,
                   consecutive_no_new=?,
                   status=?,
                   last_response_count=?,
                   last_error=?,
                   updated_at=?
               WHERE filter_key=?""",
            (
                new_count,
                next_no_new,
                status,
                len(records),
                None,
                utc_now(),
                key,
            ),
        )
        con.execute(
            """INSERT INTO harvest_events
               (filter_key, call_number, returned_rows, new_rows,
                response_bytes, http_status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                key,
                state["api_calls"] + 1,
                len(records),
                new_count,
                len(response.content),
                response.status_code,
                utc_now(),
            ),
        )
        con.commit()

        total_new_this_run += new_count
        current_total = database_question_count(con)
        request_bar = progress_bar(attempt, max_calls)
        new_per_second = (new_count / request_seconds) if request_seconds > 0 else 0.0
        print(
            f"{request_bar} | {key} | HTTP {response.status_code} | "
            f"received={len(records)} | NEW +{new_count} | "
            f"DB TOTAL={current_total} | {len(response.content):,} bytes | "
            f"{request_seconds:.2f}s | new-rate={new_per_second:.1f}/s",
            flush=True,
        )
        logging.info(
            "filter=%s call=%s returned=%s new=%s total_filter_new=%s "
            "no_new_streak=%s rejected=%s",
            key, state["api_calls"] + 1, len(records), new_count,
            state["new_rows"] + new_count, next_no_new, rejected_count,
        )

        if status in {"stalled_no_new", "no_data"}:
            if status == "stalled_no_new":
                print(
                    f"[FILTER PAUSED] {key} | {stall_rounds} consecutive samples added "
                    "no new IDs. This is NOT proof that the full topic is downloaded; "
                    "rerun to sample again or obtain official pagination/export details.",
                    flush=True,
                )
            else:
                print(
                    f"[FILTER EMPTY] {key} | API returned zero records for this filter.",
                    flush=True,
                )
            print(
                f"[FILTER RESULT] {key} | status={status} | "
                f"new this run={total_new_this_run} | DB total={database_question_count(con)}",
                flush=True,
            )
            break
        time.sleep(delay)

    if calls_this_run >= max_calls:
        current = con.execute(
            "SELECT status FROM harvest_filters WHERE filter_key=?", (key,)
        ).fetchone()
        if current and current["status"] == "running":
            con.execute(
                """UPDATE harvest_filters SET status='max_calls_reached',
                   updated_at=? WHERE filter_key=?""",
                (utc_now(), key),
            )
            con.commit()
            logging.warning(
                "Reached per-run call cap for %s. Re-run to resume this filter.",
                key,
            )
            print(
                f"[FILTER PAUSED AT CALL CAP] {key} | new this run={total_new_this_run} | "
                f"DB total={database_question_count(con)} | rerun to continue",
                flush=True,
            )

    print(
        f"[DOWNLOAD RESULT] {key} | API calls this run={calls_this_run} | "
        f"new unique IDs={total_new_this_run} | DB total={database_question_count(con)}",
        flush=True,
    )
    return total_new_this_run, global_stop


def export_database(con: sqlite3.Connection) -> None:
    total_before_export = database_question_count(con)
    print(
        f"\n[EXPORT] Writing {total_before_export} unique questions to CSV and JSONL...",
        flush=True,
    )
    rows = con.execute(
        "SELECT * FROM questions ORDER BY subject_slug, topic_slug, id"
    ).fetchall()
    fieldnames = list(rows[0].keys()) if rows else list(QUESTION_FIELDS) + [
        "raw_json", "first_seen_at", "last_seen_at"
    ]

    with CSV_PATH.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(dict(row))

    with JSONL_PATH.open("w", encoding="utf-8") as handle:
        for row in rows:
            try:
                record = json.loads(row["raw_json"])
            except (KeyError, TypeError, json.JSONDecodeError):
                record = dict(row)
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    summary = con.execute("""
        SELECT subject_slug, topic_slug, COUNT(*) AS question_count
        FROM questions
        GROUP BY subject_slug, topic_slug
        ORDER BY subject_slug, topic_slug
    """).fetchall()
    logging.info(
        "Exported %s unique questions. SQLite=%s CSV=%s JSONL=%s",
        len(rows), DB_PATH, CSV_PATH, JSONL_PATH,
    )
    print(
        f"[EXPORT DONE] {len(rows)} unique questions | "
        f"SQLite: {DB_PATH} | CSV: {CSV_PATH} | JSONL: {JSONL_PATH}",
        flush=True,
    )
    for row in summary:
        logging.info(
            "database subject=%s topic=%s questions=%s",
            row["subject_slug"], row["topic_slug"], row["question_count"],
        )


def print_summary(con: sqlite3.Connection) -> None:
    total = con.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
    filters = con.execute(
        """SELECT filter_key, api_calls, new_rows, status
           FROM harvest_filters ORDER BY filter_key"""
    ).fetchall()
    print("\n" + "=" * 68)
    print("MPSC TODAY QUESTION COLLECTION SUMMARY")
    print(f"Unique question IDs stored: {total}")
    print(f"SQLite database: {DB_PATH.resolve()}")
    print(f"CSV export:      {CSV_PATH.resolve()}")
    print(f"JSONL export:    {JSONL_PATH.resolve()}")
    print("\nFILTER STATUS:")
    for row in filters:
        print(
            f"{row['filter_key']}: calls={row['api_calls']} "
            f"new_ids={row['new_rows']} status={row['status']}"
        )
    print("=" * 68)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Discover MPSC Today quiz pages and harvest API question records into SQLite."
    )
    parser.add_argument(
        "--max-calls",
        type=int,
        default=DEFAULT_MAX_CALLS,
        help=f"Maximum API calls per filter in this run (default: {DEFAULT_MAX_CALLS}).",
    )
    parser.add_argument(
        "--stall-rounds",
        type=int,
        default=DEFAULT_STALL_ROUNDS,
        help=f"Stop a filter after this many consecutive calls add zero new IDs (default: {DEFAULT_STALL_ROUNDS}).",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=DEFAULT_API_DELAY,
        help=f"Seconds between API requests (default: {DEFAULT_API_DELAY}).",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=DEFAULT_MAX_PAGES,
        help=f"Maximum /quiz/ pages to inspect (default: {DEFAULT_MAX_PAGES}).",
    )
    parser.add_argument(
        "--subject",
        type=str,
        default="",
        help="Optional subject slug to collect only one subject (for example: history).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.max_calls < 1 or args.stall_rounds < 1 or args.delay < 0 or args.max_pages < 1:
        raise SystemExit("All limits must be positive (delay may be zero).")

    session = make_session()
    robots = load_robots(session)
    print("[START] MPSC Today authorized question collection", flush=True)
    print(
        f"[SETTINGS] API batch={API_LIMIT} | max calls/filter={args.max_calls} | "
        f"delay={args.delay:.2f}s | stall rounds={args.stall_rounds}",
        flush=True,
    )
    filters = discover_quiz_filters(session, robots, args.max_pages)

    if args.subject:
        wanted = args.subject.strip().lower()
        filters = {
            key: value for key, value in filters.items()
            if value["subject"] == wanted
        }

    if not filters:
        print("No quiz-runner filters discovered. Check the /quiz/ page and robots.txt.")
        return 2

    con = init_db()
    stop_all = False
    try:
        total_filters = len(filters)
        for index, (key, config) in enumerate(sorted(filters.items()), start=1):
            logging.info("FILTER %s/%s: %s", index, total_filters, key)
            print(
                f"\n[FILTER {index}/{total_filters}] {key} | "
                f"pages={len(config['page_urls'])}",
                flush=True,
            )
            _, stop_all = collect_one_filter(
                session=session,
                robots=robots,
                con=con,
                key=key,
                config=config,
                delay=args.delay,
                max_calls=args.max_calls,
                stall_rounds=args.stall_rounds,
            )
            export_database(con)
            if stop_all:
                break
    except KeyboardInterrupt:
        logging.warning("Interrupted by user; committing and exporting collected records.")
        con.commit()
    finally:
        export_database(con)
        print_summary(con)
        con.close()
        session.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
