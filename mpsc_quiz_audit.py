"""Audit known MPSC Today quiz URLs, excluding confirmed empty pages.

Reuses mpsc_output/mpsc_catalog.db; no site-wide crawl is performed.
Only page metadata and quiz-interface indicators are stored. It does not copy
question text, options, answers, explanations, or protected page contents.
Get written permission before bulk-importing question-bank content.
"""
from __future__ import annotations

import csv
import logging
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse, urldefrag, urlunparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.mpsctoday.com/"
PROJECT_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = PROJECT_DIR / "mpsc_output"
DB_PATH = OUTPUT_DIR / "mpsc_catalog.db"
AUDIT_CSV = OUTPUT_DIR / "quiz_audit.csv"
LINKS_CSV = OUTPUT_DIR / "quiz_links.csv"
ASSETS_CSV = OUTPUT_DIR / "quiz_assets.csv"

REQUEST_DELAY_SECONDS = 3.0
REQUEST_TIMEOUT_SECONDS = 25
USER_AGENT = "MPSCQuizCatalogAudit/1.1 (personal study catalogue; add-your-contact-email)"

# Confirmed empty/unwanted quiz pages. They are excluded even if they remain
# in the old catalogue's pages or links tables.
EXCLUDED_QUIZ_PATHS = {
    "/quiz/history/social-reformers-maharashtra/",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")


def normalize_quiz_url(raw: str) -> str:
    if not raw:
        return ""
    raw, _ = urldefrag(raw.strip())
    p = urlparse(raw)
    if p.scheme.lower() not in {"http", "https"}:
        return ""
    if (p.hostname or "").lower() not in {"mpsctoday.com", "www.mpsctoday.com"}:
        return ""
    path = p.path or "/"
    while "//" in path:
        path = path.replace("//", "/")
    if path != "/" and not path.endswith("/"):
        path += "/"
    return urlunparse(("https", "www.mpsctoday.com", path, "", "", ""))


def is_quiz_url(url: str) -> bool:
    p = urlparse(url)
    path = p.path.lower()
    return (
        (p.hostname or "").lower() in {"mpsctoday.com", "www.mpsctoday.com"}
        and path.startswith("/quiz/")
        and path not in EXCLUDED_QUIZ_PATHS
    )


def create_tables(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS quiz_audit (
        url TEXT PRIMARY KEY,
        http_status INTEGER,
        content_type TEXT,
        title TEXT,
        h1 TEXT,
        page_language TEXT,
        practice_exam_controls TEXT,
        language_controls TEXT,
        visible_question_dom_markers INTEGER,
        visible_radio_inputs INTEGER,
        visible_checkbox_inputs INTEGER,
        form_count INTEGER,
        pagination_links INTEGER,
        quiz_ui_detected INTEGER,
        note TEXT,
        checked_at TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS quiz_audit_links (
        source_url TEXT NOT NULL,
        target_url TEXT NOT NULL,
        anchor_text TEXT,
        link_kind TEXT,
        PRIMARY KEY(source_url, target_url)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS quiz_audit_assets (
        source_url TEXT NOT NULL,
        asset_url TEXT NOT NULL,
        asset_kind TEXT,
        PRIMARY KEY(source_url, asset_url)
    )""")
    conn.commit()


def load_known_quiz_urls(conn: sqlite3.Connection) -> list[str]:
    urls = {BASE_URL.rstrip("/") + "/quiz/"}
    for (raw,) in conn.execute("SELECT url FROM pages"):
        url = normalize_quiz_url(raw)
        if url and is_quiz_url(url):
            urls.add(url)
    for (raw,) in conn.execute("SELECT target_url FROM links"):
        url = normalize_quiz_url(raw)
        if url and is_quiz_url(url):
            urls.add(url)
    return sorted(urls, key=lambda u: (u.count("/"), u))


def remove_excluded_audit_rows(conn: sqlite3.Connection) -> None:
    """Remove stale audit metadata/links for specifically excluded empty pages."""
    for path in EXCLUDED_QUIZ_PATHS:
        url = BASE_URL.rstrip("/") + path
        conn.execute("DELETE FROM quiz_audit WHERE url=?", (url,))
        conn.execute(
            "DELETE FROM quiz_audit_links WHERE source_url=? OR target_url=?",
            (url, url),
        )
        conn.execute("DELETE FROM quiz_audit_assets WHERE source_url=?", (url,))
    conn.commit()


def make_robot_parser(session: requests.Session) -> RobotFileParser:
    robots_url = urljoin(BASE_URL, "robots.txt")
    response = session.get(robots_url, timeout=REQUEST_TIMEOUT_SECONDS)
    if response.status_code >= 400 and response.status_code != 404:
        raise RuntimeError(
            f"Could not verify robots.txt (HTTP {response.status_code}); stopping."
        )
    parser = RobotFileParser()
    parser.set_url(robots_url)
    parser.parse(response.text.splitlines() if response.status_code == 200 else [])
    return parser


def visible_control_labels(soup: BeautifulSoup, patterns: tuple[str, ...]) -> list[str]:
    found: list[str] = []
    for node in soup.find_all(["button", "a", "label", "input"]):
        value = " ".join(
            [
                node.get_text(" ", strip=True),
                str(node.get("aria-label", "")),
                str(node.get("value", "")),
                str(node.get("title", "")),
            ]
        ).strip()
        if value and any(re.search(pattern, value, flags=re.I) for pattern in patterns):
            found.append(value[:120])
    return list(dict.fromkeys(found))


def write_csv(conn: sqlite3.Connection, query: str, path: Path) -> None:
    cur = conn.execute(query)
    rows = cur.fetchall()
    headers = [d[0] for d in cur.description]
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(headers)
        writer.writerows(rows)


def main() -> None:
    if not DB_PATH.exists():
        raise FileNotFoundError(
            f"Database not found: {DB_PATH}\n"
            "Keep this script in the project root beside mpsc_output."
        )

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    logging.info("Using existing database: %s", DB_PATH)
    conn = sqlite3.connect(DB_PATH)
    create_tables(conn)
    remove_excluded_audit_rows(conn)
    urls = load_known_quiz_urls(conn)
    logging.info("Known quiz URLs found in existing catalogue: %s", len(urls))
    logging.info("Excluded empty quiz paths: %s", sorted(EXCLUDED_QUIZ_PATHS))

    try:
        robots = make_robot_parser(session)
        checked = skipped = failed = 0

        for index, url in enumerate(urls, start=1):
            old = conn.execute(
                "SELECT http_status FROM quiz_audit WHERE url=?", (url,)
            ).fetchone()
            if old and old[0] == 200:
                skipped += 1
                logging.info("[%s/%s] Already audited; skip: %s", index, len(urls), url)
                continue

            if not robots.can_fetch(USER_AGENT, url):
                now = datetime.now(timezone.utc).isoformat(timespec="seconds")
                conn.execute(
                    """INSERT OR REPLACE INTO quiz_audit
                    (url,http_status,note,checked_at) VALUES(?,?,?,?)""",
                    (url, 0, "Disallowed by robots.txt", now),
                )
                conn.commit()
                skipped += 1
                logging.info("[%s/%s] robots.txt disallows: %s", index, len(urls), url)
                continue

            try:
                response = session.get(
                    url, timeout=REQUEST_TIMEOUT_SECONDS, allow_redirects=True
                )
            except requests.RequestException as exc:
                failed += 1
                now = datetime.now(timezone.utc).isoformat(timespec="seconds")
                conn.execute(
                    """INSERT OR REPLACE INTO quiz_audit
                    (url,note,checked_at) VALUES(?,?,?)""",
                    (url, f"Request error: {str(exc)[:350]}", now),
                )
                conn.commit()
                logging.warning("[%s/%s] Request failed: %s | %s", index, len(urls), url, exc)
                time.sleep(REQUEST_DELAY_SECONDS)
                continue

            time.sleep(REQUEST_DELAY_SECONDS)
            if response.status_code in (429, 503):
                now = datetime.now(timezone.utc).isoformat(timespec="seconds")
                conn.execute(
                    """INSERT OR REPLACE INTO quiz_audit
                    (url,http_status,note,checked_at) VALUES(?,?,?,?)""",
                    (
                        url,
                        response.status_code,
                        "Server rate-limited; stop and resume later.",
                        now,
                    ),
                )
                conn.commit()
                logging.warning("Server returned HTTP %s; stopping politely.", response.status_code)
                break

            content_type = response.headers.get("Content-Type", "").lower()
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            if response.status_code != 200 or "text/html" not in content_type:
                failed += 1
                conn.execute(
                    """INSERT OR REPLACE INTO quiz_audit
                    (url,http_status,content_type,note,checked_at) VALUES(?,?,?,?,?)""",
                    (
                        url,
                        response.status_code,
                        content_type,
                        "Not a successful HTML page.",
                        now,
                    ),
                )
                conn.commit()
                logging.info("[%s/%s] HTTP %s / non-HTML: %s", index, len(urls), response.status_code, url)
                continue

            final_url = normalize_quiz_url(response.url)
            if not final_url or not is_quiz_url(final_url) or not robots.can_fetch(USER_AGENT, final_url):
                failed += 1
                conn.execute(
                    """INSERT OR REPLACE INTO quiz_audit
                    (url,http_status,content_type,note,checked_at) VALUES(?,?,?,?,?)""",
                    (
                        url,
                        response.status_code,
                        content_type,
                        "Redirect outside permitted /quiz/ scope or to excluded page.",
                        now,
                    ),
                )
                conn.commit()
                continue

            soup = BeautifulSoup(response.text, "html.parser")
            title = soup.title.get_text(" ", strip=True) if soup.title else ""
            h1 = soup.find("h1")
            h1_text = h1.get_text(" ", strip=True) if h1 else ""
            lang = soup.find("html")
            page_language = lang.get("lang", "") if lang else ""
            practice_exam = visible_control_labels(soup, (r"practice", r"exam", r"सराव", r"परीक्षा"))
            language_controls = visible_control_labels(soup, (r"मराठी", r"english", r"हिंदी"))
            question_markers = soup.select(
                '[class*="question"], [id*="question"], '
                ".wpProQuiz_listItem, .tutor-question-item, [data-question-id]"
            )
            radio_count = len(soup.select('input[type="radio"]'))
            checkbox_count = len(soup.select('input[type="checkbox"]'))
            form_count = len(soup.find_all("form"))
            quiz_ui = bool(question_markers or radio_count or checkbox_count or form_count or practice_exam)

            conn.execute(
                """INSERT OR REPLACE INTO quiz_audit
                (url,http_status,content_type,title,h1,page_language,practice_exam_controls,
                 language_controls,visible_question_dom_markers,visible_radio_inputs,
                 visible_checkbox_inputs,form_count,pagination_links,quiz_ui_detected,note,checked_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    url,
                    response.status_code,
                    content_type,
                    title,
                    h1_text,
                    page_language,
                    " | ".join(practice_exam),
                    " | ".join(language_controls),
                    len(question_markers),
                    radio_count,
                    checkbox_count,
                    form_count,
                    0,
                    int(quiz_ui),
                    "Visible DOM counts are not total bank size; question text was not stored.",
                    now,
                ),
            )

            for anchor in soup.find_all("a", href=True):
                target_raw = urljoin(final_url, anchor.get("href", "").strip())
                label = anchor.get_text(" ", strip=True)[:250]
                target_path = urlparse(target_raw).path.lower()
                if target_path.endswith((".pdf", ".doc", ".docx", ".ppt", ".pptx")):
                    conn.execute(
                        "INSERT OR IGNORE INTO quiz_audit_links VALUES(?,?,?,?)",
                        (final_url, target_raw, label, "document_link_candidate"),
                    )
                    continue
                target = normalize_quiz_url(target_raw)
                if target and is_quiz_url(target):
                    conn.execute(
                        "INSERT OR IGNORE INTO quiz_audit_links VALUES(?,?,?,?)",
                        (final_url, target, label, "quiz_page_link"),
                    )

            for script in soup.find_all("script", src=True):
                asset = urljoin(final_url, script.get("src", ""))
                conn.execute(
                    "INSERT OR IGNORE INTO quiz_audit_assets VALUES(?,?,?)",
                    (final_url, asset, "javascript"),
                )
            for iframe in soup.find_all("iframe", src=True):
                asset = urljoin(final_url, iframe.get("src", ""))
                conn.execute(
                    "INSERT OR IGNORE INTO quiz_audit_assets VALUES(?,?,?)",
                    (final_url, asset, "iframe"),
                )

            actual_pagination = conn.execute(
                """SELECT COUNT(*) FROM quiz_audit_links
                WHERE source_url=? AND target_url LIKE '%/page/%'""",
                (final_url,),
            ).fetchone()[0]
            conn.execute(
                "UPDATE quiz_audit SET pagination_links=? WHERE url=?",
                (actual_pagination, url),
            )
            conn.commit()
            checked += 1
            logging.info(
                "[%s/%s] Audited | quiz_ui=%s | visible_q_markers=%s | %s",
                index,
                len(urls),
                bool(quiz_ui),
                len(question_markers),
                final_url,
            )

        write_csv(conn, "SELECT * FROM quiz_audit ORDER BY url", AUDIT_CSV)
        write_csv(conn, "SELECT * FROM quiz_audit_links ORDER BY source_url,target_url", LINKS_CSV)
        write_csv(conn, "SELECT * FROM quiz_audit_assets ORDER BY source_url,asset_url", ASSETS_CSV)
        totals = conn.execute("SELECT COUNT(*) FROM quiz_audit").fetchone()[0]
        print("\nQUIZ-ONLY AUDIT FINISHED")
        print(f"Known quiz URLs         : {len(urls)}")
        print(f"Audited this run        : {checked}")
        print(f"Already audited skipped : {skipped}")
        print(f"Failed/non-HTML         : {failed}")
        print(f"Audit rows in database  : {totals}")
        print(f"Database                : {DB_PATH.resolve()}")
        print(f"Audit CSV               : {AUDIT_CSV.resolve()}")
        print(f"Quiz links CSV           : {LINKS_CSV.resolve()}")
        print(f"JS/iframe assets CSV     : {ASSETS_CSV.resolve()}")
        print("Question text/options/answers/explanations were NOT copied or downloaded.")
        print("Get written permission before bulk-importing quiz content.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
