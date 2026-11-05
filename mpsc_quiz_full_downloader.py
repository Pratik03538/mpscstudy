"""Authorized, resumable MPSC Today quiz question collector.

Discovers quiz pages under /quiz/, reads #quiz-runner data-subject/data-topic,
then calls the public GET question endpoint. It stores complete returned records
in SQLite, deduplicates by question ID, and exports CSV + JSONL.

The endpoint has so far returned changing 100-record samples for identical
subject queries, and no documented page/offset cursor was found in the frontend.
This collector re-samples until repeated calls add no new valid, unique question
content. Existing IDs and exact question+option duplicates are skipped. Each run
backs up the current SQLite database, removes malformed rows and exact duplicates,
then resumes. The default per-filter call cap is unlimited; repeated zero-new
samples pause that filter to avoid an endless loop. This is still not a mathematical
proof of completeness if the API exposes only rotating samples without pagination.

No POST requests or answer submissions are made.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import logging
import re
import sqlite3
import sys
import time
import unicodedata
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
DEFAULT_MAX_CALLS = 0  # 0 means unlimited; stop only after repeated zero-new samples.
DEFAULT_STALL_ROUNDS = 20
DEFAULT_MAX_PAGES = 500

PROJECT_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = PROJECT_DIR / "mpsc_output"
DB_PATH = OUTPUT_DIR / "mpsc_questions.db"
EXPORT_DIR = OUTPUT_DIR / "question_exports"
CSV_PATH = EXPORT_DIR / "questions.csv"
JSONL_PATH = EXPORT_DIR / "questions.jsonl"

QUESTION_FIELDS = (
    "id",
    "content_hash",
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
    "id", "content_hash", "question_mr", "question_en", "option_a", "option_b",
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
    existing_columns = {
        row[1] for row in con.execute("PRAGMA table_info(questions)").fetchall()
    }
    if "content_hash" not in existing_columns:
        con.execute('ALTER TABLE questions ADD COLUMN "content_hash" TEXT')
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


def clean_text(value: object) -> str:
    """Normalize text for exact-content duplicate detection."""
    raw = html.unescape(str(value or ""))
    plain = BeautifulSoup(raw, "html.parser").get_text(" ", strip=True)
    plain = unicodedata.normalize("NFKC", plain).casefold()
    return re.sub(r"\s+", " ", plain).strip()


def content_fingerprint(item: dict) -> str:
    """Hash the question and all four options; translations use a stable fallback."""
    question = clean_text(item.get("question_mr")) or clean_text(item.get("question_en"))
    options = [
        clean_text(item.get("option_a")),
        clean_text(item.get("option_b")),
        clean_text(item.get("option_c")),
        clean_text(item.get("option_d")),
    ]
    if not question or not all(options):
        return ""
    payload = json.dumps([question, *options], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def valid_question(item: dict) -> bool:
    """Reject structurally broken MCQs while allowing Marathi-only or English-only records."""
    if item.get("id") is None or not str(item.get("id")).strip():
        return False
    if not (clean_text(item.get("question_mr")) or clean_text(item.get("question_en"))):
        return False
    if not all(clean_text(item.get(f"option_{letter}")) for letter in "abcd"):
        return False
    correct = re.sub(r"[^a-z0-9]", "", clean_text(item.get("correct_option")))
    valid_answers = {
        "a", "b", "c", "d", "1", "2", "3", "4",
        "optiona", "optionb", "optionc", "optiond",
        "option1", "option2", "option3", "option4",
    }
    return correct in valid_answers


def clean_existing_database(con: sqlite3.Connection) -> tuple[int, int, str | None]:
    """
    Back up the current DB, then remove malformed rows and exact content duplicates.
    For duplicate groups, retain the most complete row.
    """
    total = con.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
    if not total:
        con.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_questions_content_hash "
            "ON questions(content_hash) WHERE content_hash IS NOT NULL AND content_hash <> ''"
        )
        con.commit()
        print("[CLEANUP] Database is empty; unique-content protection is ready.", flush=True)
        return 0, 0, None

    backup_path = DB_PATH.with_name(
        f"{DB_PATH.stem}.backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}{DB_PATH.suffix}"
    )
    backup_con = sqlite3.connect(backup_path)
    try:
        con.backup(backup_con)
    finally:
        backup_con.close()
    print(f"[BACKUP] Existing database safely copied to: {backup_path}", flush=True)

    rows = con.execute("SELECT * FROM questions ORDER BY rowid").fetchall()
    groups: dict[str, list[dict]] = {}
    invalid_ids: list[str] = []

    def completeness_score(row: dict) -> int:
        fields = (
            "question_en", "explanation_mr", "explanation_en",
            "pyq_exam", "pyq_year", "difficulty", "subject_name_mr",
            "topic_name_mr",
        )
        return sum(1 for field in fields if row.get(field) not in (None, ""))

    for sqlite_row in rows:
        row = dict(sqlite_row)
        qid = str(row.get("id") or "").strip()
        if not qid or not valid_question(row):
            if qid:
                invalid_ids.append(qid)
            else:
                invalid_ids.append("")
            continue
        fingerprint = content_fingerprint(row)
        if not fingerprint:
            invalid_ids.append(qid)
            continue
        groups.setdefault(fingerprint, []).append(row)

    keep_rows: list[tuple[str, str]] = []
    duplicate_ids: list[str] = []
    for fingerprint, group in groups.items():
        group.sort(
            key=lambda item: (
                completeness_score(item),
                str(item.get("first_seen_at") or ""),
                str(item.get("id") or ""),
            ),
            reverse=True,
        )
        winner = group[0]
        keep_rows.append((str(winner["id"]), fingerprint))
        duplicate_ids.extend(str(item["id"]) for item in group[1:])

    con.execute("CREATE TEMP TABLE IF NOT EXISTS _mpsc_keep_ids (id TEXT PRIMARY KEY, fingerprint TEXT)")
    con.execute("DELETE FROM _mpsc_keep_ids")
    con.executemany("INSERT INTO _mpsc_keep_ids(id, fingerprint) VALUES (?, ?)", keep_rows)
    con.execute(
        "DELETE FROM questions WHERE id NOT IN (SELECT id FROM _mpsc_keep_ids)"
    )
    con.execute(
        """UPDATE questions
           SET content_hash = (
               SELECT fingerprint FROM _mpsc_keep_ids
               WHERE _mpsc_keep_ids.id = questions.id
           )"""
    )
    con.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_questions_content_hash "
        "ON questions(content_hash) WHERE content_hash IS NOT NULL AND content_hash <> ''"
    )
    con.commit()

    removed = len(invalid_ids) + len(duplicate_ids)
    print(
        f"[CLEANUP DONE] before={total:,} | kept={len(keep_rows):,} | "
        f"corrupted_removed={len(invalid_ids):,} | content_duplicates_removed={len(duplicate_ids):,} | "
        f"after={database_question_count(con):,}",
        flush=True,
    )
    return len(invalid_ids), len(duplicate_ids), str(backup_path)


def insert_questions(
    con: sqlite3.Connection,
    records: list[dict],
    subject_filter: str,
    topic_filter: str,
) -> tuple[int, int]:
    """Insert only valid, previously unseen IDs and unseen exact question+option content."""
    prepared: list[tuple[dict, str, str]] = []
    rejected = 0
    batch_ids: set[str] = set()
    batch_hashes: set[str] = set()

    for item in records:
        if not valid_question(item):
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

        qid = str(item["id"]).strip()
        fingerprint = content_fingerprint(item)
        if not fingerprint or qid in batch_ids or fingerprint in batch_hashes:
            rejected += 1
            continue
        batch_ids.add(qid)
        batch_hashes.add(fingerprint)
        prepared.append((item, qid, fingerprint))

    if not prepared:
        return 0, rejected

    ids = [qid for _, qid, _ in prepared]
    hashes = [fingerprint for _, _, fingerprint in prepared]
    existing_ids: set[str] = set()
    existing_hashes: set[str] = set()
    for start in range(0, len(ids), 800):
        chunk = ids[start:start + 800]
        placeholders = ",".join("?" for _ in chunk)
        existing_ids.update(
            str(row[0]) for row in con.execute(
                f"SELECT id FROM questions WHERE id IN ({placeholders})", chunk
            ).fetchall()
        )
    for start in range(0, len(hashes), 800):
        chunk = hashes[start:start + 800]
        placeholders = ",".join("?" for _ in chunk)
        existing_hashes.update(
            str(row[0]) for row in con.execute(
                f"SELECT content_hash FROM questions WHERE content_hash IN ({placeholders})",
                chunk,
            ).fetchall()
        )

    columns = list(QUESTION_FIELDS) + ["raw_json", "first_seen_at", "last_seen_at"]
    quoted_columns = ", ".join(f'"{name}"' for name in columns)
    placeholders = ", ".join("?" for _ in columns)
    sql = f"INSERT OR IGNORE INTO questions ({quoted_columns}) VALUES ({placeholders})"
    now = utc_now()
    inserted = 0

    for item, qid, fingerprint in prepared:
        # Existing IDs and exact content are deliberately left untouched.
        if qid in existing_ids or fingerprint in existing_hashes:
            continue
        values = []
        for field in QUESTION_FIELDS:
            value = fingerprint if field == "content_hash" else item.get(field)
            if value is not None and field in TEXT_FIELDS:
                value = str(value)
            values.append(value)
        values.extend([
            json.dumps(item, ensure_ascii=False, separators=(",", ":")),
            now,
            now,
        ])
        cursor = con.execute(sql, values)
        if cursor.rowcount:
            inserted += 1
            existing_ids.add(qid)
            existing_hashes.add(fingerprint)

    return inserted, rejected


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
    if state["status"] in {"complete", "stalled_no_new", "max_calls_reached", "no_data"}:
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
        f"[PROGRESS] API call cap={'unlimited' if max_calls == 0 else max_calls} | "
        f"stop after {stall_rounds} consecutive calls add zero new valid questions.",
        flush=True,
    )
    logging.info(
        "Starting harvest: subject=%s topic=%s max_calls_this_run=%s stall_rounds=%s",
        subject, topic or "<all topics>", ("unlimited" if max_calls == 0 else max_calls), stall_rounds,
    )
    calls_this_run = 0
    total_new_this_run = 0
    global_stop = False

    attempt = 1
    while max_calls == 0 or calls_this_run < max_calls:
        if not robots.can_fetch(USER_AGENT, API_URL):
            raise RuntimeError("robots.txt disallows API URL; stopping.")
        print(
            f"[REQUEST] {key} | API call {attempt} | "
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

        new_count, rejected_count = insert_questions(con, records, subject, topic)

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
        new_per_second = (new_count / request_seconds) if request_seconds > 0 else 0.0
        print(
            f"[CALL {attempt}] | {key} | HTTP {response.status_code} | "
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
        attempt += 1
        time.sleep(delay)

    if max_calls > 0 and calls_this_run >= max_calls:
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
        help="Optional safety cap per filter; 0 (default) means unlimited.",
    )
    parser.add_argument(
        "--stall-rounds",
        type=int,
        default=DEFAULT_STALL_ROUNDS,
        help=f"Stop a filter after this many consecutive calls add zero new valid, unique questions (default: {DEFAULT_STALL_ROUNDS}).",
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
    if args.max_calls < 0 or args.stall_rounds < 1 or args.delay < 0 or args.max_pages < 1:
        raise SystemExit("max-calls must be 0 (unlimited) or positive; other limits must be valid.")

    session = make_session()
    robots = load_robots(session)
    print("[START] MPSC Today authorized question collection", flush=True)
    print(
        f"[SETTINGS] API batch={API_LIMIT} | max calls/filter="
        f"{'UNLIMITED' if args.max_calls == 0 else args.max_calls} | "
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
        clean_existing_database(con)
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
