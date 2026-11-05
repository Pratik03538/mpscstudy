"""Resume and expand the MPSC Today public catalogue scan without a 200-page cap.

- Reuses the existing mpsc_output/mpsc_catalog.db created by main.py.
- Does NOT refetch URLs already present in the pages table.
- Uses previously discovered links in the existing links table as the resume queue.
- Skips Dinvishesh/date pages, directory listings, tags, authors, feeds and login/admin paths.
- Crawls all reachable, in-scope public pages allowed by robots.txt; no MAX_PAGES or MAX_DEPTH cap.
- Saves every processed page/link immediately so an interrupted run can resume.

This is a metadata/catalogue crawler. It records public page metadata and downloadable
resource URLs; it does not copy MCQ text/answers, submit tests, bypass access controls,
or bulk-download protected/copyrighted resources. Obtain the site's permission before
bulk importing their question-bank content.
"""

from __future__ import annotations

import csv
import heapq
import itertools
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

# IMPORTANT: keep this script in the same project folder as your existing mpsc_output folder.
# This is the DB created by your first 200-page scan. This script updates it in place.
PROJECT_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = PROJECT_DIR / "mpsc_output"
DB_PATH = OUTPUT_DIR / "mpsc_catalog.db"
PAGES_CSV = OUTPUT_DIR / "catalog_pages.csv"
LINKS_CSV = OUTPUT_DIR / "catalog_links.csv"
RESOURCES_CSV = OUTPUT_DIR / "downloadable_resources.csv"

SEED_URLS = [
    "https://www.mpsctoday.com/quiz/",
    "https://www.mpsctoday.com/daily-test/",
    "https://www.mpsctoday.com/test-series/",
    "https://www.mpsctoday.com/download/question-papers/",
    "https://www.mpsctoday.com/syllabus/",
    "https://www.mpsctoday.com/study-material/",
    "https://www.mpsctoday.com/current-affairs/",
    "https://www.mpsctoday.com/",
]

REQUEST_DELAY_SECONDS = 2.0
REQUEST_TIMEOUT_SECONDS = 25
USER_AGENT = "MPSCStudyCatalogBot/2.0 (personal study catalogue; replace-with-your-contact-email)"

RESOURCE_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".zip"
}

# Dinvishesh and non-study areas are intentionally excluded from all new crawling.
SKIP_PATH_PARTS = (
    "/wp-admin", "/wp-login", "/cart", "/checkout", "/account",
    "/login", "/register", "/feed", "/comments", "/directory/",
    "/dinvishesh", "/tag/", "/author/", "/search/",
)

SUBJECT_PATHS = {
    "/history/", "/geography/", "/indian-polity/", "/economics/",
    "/science/", "/reasoning/", "/mathematics/", "/marathi-language/",
    "/english/", "/general-knowledge/", "/gk/",
}

EXACT_MPSC_PATHS = {
    "/", "/mpsc-exams/", "/mpsc-exam-calendar/", "/notifications/",
    "/book-list/", "/syllabus/", "/download/question-papers/",
    "/current-affairs/", "/general-knowledge/",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)


def normalize_url(url: str) -> str:
    """Normalize same-site URLs and discard tracking/query variants."""
    if not url:
        return ""
    url, _fragment = urldefrag(url.strip())
    parsed = urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"}:
        return ""
    host = (parsed.hostname or "").lower()
    if host not in {"mpsctoday.com", "www.mpsctoday.com"}:
        return ""
    path = parsed.path or "/"
    while "//" in path:
        path = path.replace("//", "/")
    if path != "/" and not path.endswith("/") and not any(
        path.lower().endswith(ext) for ext in RESOURCE_EXTENSIONS
    ):
        path += "/"
    # Keep only canonical host; query strings are ignored to avoid duplicate filters/session URLs.
    return urlunparse(("https", "www.mpsctoday.com", path, "", "", ""))


def same_site(url: str) -> bool:
    return (urlparse(url).hostname or "").lower() in {
        "mpsctoday.com", "www.mpsctoday.com"
    }


def is_resource(url: str) -> bool:
    path = urlparse(url).path.lower()
    return any(path.endswith(ext) for ext in RESOURCE_EXTENSIONS)


def is_skipped_path(url: str) -> bool:
    path = urlparse(url).path.lower()
    return any(part in path for part in SKIP_PATH_PARTS)


def is_in_scope(url: str) -> bool:
    """Include MPSC learning, quiz, test-series and study-resource areas."""
    if not url or not same_site(url) or is_resource(url) or is_skipped_path(url):
        return False

    path = urlparse(url).path.lower()
    if path in EXACT_MPSC_PATHS or path in SUBJECT_PATHS:
        return True

    # Include all topic/subject pages nested below these useful hubs.
    prefixes = (
        "/quiz/",
        "/daily-test/",
        "/test-series/",
        "/study-material/",
        "/current-affairs/",
        "/download/",
    )
    if path.startswith(prefixes):
        return True

    # Other MPSC subject hubs discovered during the original scan.
    if path.startswith(("/history/", "/geography/", "/indian-polity/", "/economics/",
                        "/science/", "/reasoning/", "/mathematics/",
                        "/marathi-language/", "/english/", "/gk/")):
        return True

    return False


def page_type(url: str) -> str:
    path = urlparse(url).path.lower()
    if path == "/":
        return "home_navigation"
    if path == "/quiz/":
        return "quiz_hub"
    if path.startswith("/quiz/"):
        return "quiz_subject_or_topic"
    if path == "/daily-test/" or path.startswith("/daily-test/"):
        return "daily_test"
    if path.startswith("/test-series/"):
        return "test_series"
    if path.startswith("/download/"):
        return "download_or_question_papers"
    if path == "/syllabus/":
        return "syllabus"
    if path.startswith("/study-material/") or path in SUBJECT_PATHS:
        return "study_material"
    if path.startswith("/current-affairs/"):
        return "current_affairs"
    return "other_mpsc_page"


def priority(url: str) -> int:
    """Lower number is fetched first; all in-scope URLs remain eligible."""
    path = urlparse(url).path.lower()
    if path.startswith("/quiz/"):
        return 0
    if path.startswith("/test-series/") or path.startswith("/daily-test/"):
        return 1
    if path.startswith("/download/"):
        return 2
    if path.startswith("/study-material/") or path in SUBJECT_PATHS:
        return 3
    if path.startswith("/current-affairs/"):
        return 4
    if path in EXACT_MPSC_PATHS:
        return 5
    return 6


def setup_database(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS pages (
            url TEXT PRIMARY KEY, title TEXT, h1 TEXT, meta_description TEXT,
            page_type TEXT, depth INTEGER, status_code INTEGER, scanned_at TEXT
        );
        CREATE TABLE IF NOT EXISTS links (
            source_url TEXT NOT NULL, target_url TEXT NOT NULL, anchor_text TEXT,
            link_type TEXT, scanned_at TEXT,
            PRIMARY KEY (source_url, target_url)
        );
        CREATE TABLE IF NOT EXISTS resources (
            url TEXT PRIMARY KEY, title TEXT, extension TEXT,
            source_page TEXT, scanned_at TEXT
        );
        CREATE TABLE IF NOT EXISTS mcqs (
            mcq_id TEXT PRIMARY KEY, source_page_url TEXT, exam_name TEXT,
            subject TEXT NOT NULL, chapter TEXT, topic TEXT, language TEXT,
            question_text TEXT NOT NULL, option_a TEXT, option_b TEXT,
            option_c TEXT, option_d TEXT, correct_option TEXT, explanation TEXT,
            difficulty TEXT, source_type TEXT, source_year INTEGER,
            content_rights_note TEXT, imported_at TEXT,
            CHECK (correct_option IS NULL OR correct_option IN ('A','B','C','D'))
        );
        CREATE TABLE IF NOT EXISTS tests (
            test_id TEXT PRIMARY KEY, source_page_url TEXT, title TEXT NOT NULL,
            exam_name TEXT, subject TEXT, test_date TEXT, duration_minutes INTEGER,
            total_marks REAL, content_rights_note TEXT
        );
        CREATE TABLE IF NOT EXISTS test_questions (
            test_id TEXT NOT NULL, mcq_id TEXT NOT NULL, question_order INTEGER NOT NULL,
            marks REAL, negative_marks REAL,
            PRIMARY KEY (test_id, question_order),
            FOREIGN KEY (test_id) REFERENCES tests(test_id),
            FOREIGN KEY (mcq_id) REFERENCES mcqs(mcq_id)
        );
        CREATE TABLE IF NOT EXISTS crawl_state (
            url TEXT PRIMARY KEY, state TEXT NOT NULL, http_status INTEGER,
            note TEXT, updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_links_target ON links(target_url);
        CREATE INDEX IF NOT EXISTS idx_pages_type ON pages(page_type);
        """
    )
    conn.commit()


def get_robot_parser(session: requests.Session) -> RobotFileParser:
    robots_url = urljoin(BASE_URL, "robots.txt")
    response = session.get(robots_url, timeout=REQUEST_TIMEOUT_SECONDS)
    if response.status_code >= 400 and response.status_code != 404:
        raise RuntimeError(
            f"Cannot verify robots.txt (HTTP {response.status_code}); stopping safely."
        )
    parser = RobotFileParser()
    parser.set_url(robots_url)
    parser.parse(response.text.splitlines() if response.status_code == 200 else [])
    return parser


def canonical_url(soup: BeautifulSoup, fallback_url: str) -> str:
    tag = soup.find("link", rel=lambda value: value and "canonical" in value)
    if tag and tag.get("href"):
        candidate = normalize_url(urljoin(fallback_url, tag["href"]))
        if candidate and same_site(candidate):
            return candidate
    return fallback_url


def set_state(conn: sqlite3.Connection, url: str, state: str,
              http_status: int | None = None, note: str = "") -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute(
        """INSERT INTO crawl_state(url,state,http_status,note,updated_at)
           VALUES(?,?,?,?,?)
           ON CONFLICT(url) DO UPDATE SET state=excluded.state,
           http_status=excluded.http_status,note=excluded.note,updated_at=excluded.updated_at""",
        (url, state, http_status, note[:500], now),
    )


def export_csv(conn: sqlite3.Connection) -> None:
    exports = [
        ("SELECT * FROM pages ORDER BY page_type, url", PAGES_CSV),
        ("SELECT * FROM links ORDER BY source_url, target_url", LINKS_CSV),
        ("SELECT * FROM resources ORDER BY source_page, url", RESOURCES_CSV),
    ]
    for query, output_path in exports:
        rows = conn.execute(query).fetchall()
        headers = [column[0] for column in conn.execute(query).description]
        with output_path.open("w", newline="", encoding="utf-8-sig") as file:
            writer = csv.writer(file)
            writer.writerow(headers)
            writer.writerows(rows)


def enqueue(queue, queued: set[str], url: str, depth: int, counter,
            already_done: set[str]) -> None:
    normalized = normalize_url(url)
    if not normalized or not is_in_scope(normalized):
        return
    if normalized in queued or normalized in already_done:
        return
    queued.add(normalized)
    heapq.heappush(queue, (priority(normalized), next(counter), normalized, depth))


def import_prior_log_if_present(conn: sqlite3.Connection) -> int:
    """Optionally mark URLs from a saved old console log as already attempted.

    Save the previous scan console output as mpsc_previous_scan.log in the project
    root to prevent retries of unsuccessful/404 URLs that are not in the pages table.
    """
    log_path = PROJECT_DIR / "mpsc_previous_scan.log"
    if not log_path.exists():
        return 0
    found = set(re.findall(r"https?://[^\s)]+", log_path.read_text(encoding="utf-8", errors="ignore")))
    imported = 0
    for raw_url in found:
        url = normalize_url(raw_url.rstrip(".,]"))
        if url and same_site(url):
            exists = conn.execute("SELECT 1 FROM pages WHERE url=?", (url,)).fetchone()
            if not exists:
                set_state(conn, url, "prior_attempt")
                imported += 1
    conn.commit()
    return imported


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    if not DB_PATH.exists():
        raise FileNotFoundError(
            f"Existing database not found: {DB_PATH.resolve()}\n"
            "Keep the original mpsc_output folder beside this script. "
            "Do not start with an empty DB if you want to resume the first scan."
        )

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    logging.info("Opening existing database: %s", DB_PATH.resolve())
    conn = sqlite3.connect(DB_PATH)
    setup_database(conn)

    # All pages already stored in the old DB are considered done and are never fetched again.
    already_done = {row[0] for row in conn.execute("SELECT url FROM pages")}
    states_done = {
        row[0] for row in conn.execute(
            "SELECT url FROM crawl_state WHERE state IN ('done','blocked','prior_attempt','not_html')"
        )
    }
    already_done.update(states_done)
    for old_url in already_done:
        if conn.execute("SELECT 1 FROM pages WHERE url=?", (old_url,)).fetchone():
            set_state(conn, old_url, "done")
    conn.commit()

    prior_log_count = import_prior_log_if_present(conn)
    if prior_log_count:
        logging.info("Imported %s previously attempted URLs from mpsc_previous_scan.log", prior_log_count)
        already_done.update(
            row[0] for row in conn.execute(
                "SELECT url FROM crawl_state WHERE state='prior_attempt'"
            )
        )

    logging.info("Checking robots.txt before new requests...")
    robots = get_robot_parser(session)

    queue = []
    queued: set[str] = set()
    counter = itertools.count()

    # Resume from every link already discovered by the original 200-request scan.
    old_links = conn.execute(
        "SELECT l.target_url, COALESCE(p.depth, 0) + 1 FROM links l "
        "LEFT JOIN pages p ON p.url=l.source_url"
    ).fetchall()
    for target_url, depth in old_links:
        enqueue(queue, queued, target_url, depth, counter, already_done)

    # Also enqueue useful entry points; stored pages will be skipped automatically.
    for seed in SEED_URLS:
        enqueue(queue, queued, seed, 0, counter, already_done)

    logging.info("Existing successful pages skipped: %s", len(already_done))
    logging.info("Previously discovered link candidates: %s", len(old_links))
    logging.info("New scan has NO fixed page/request cap and NO depth cap.")
    logging.info("Dinvishesh and directory paths will be skipped.")

    requests_made = 0
    pages_saved = 0
    failures = 0
    skipped_robots = 0

    while queue:
        _prio, _order, url, depth = heapq.heappop(queue)
        if url in already_done:
            continue
        if not is_in_scope(url):
            continue
        if not robots.can_fetch(USER_AGENT, url):
            skipped_robots += 1
            already_done.add(url)
            set_state(conn, url, "blocked", note="Disallowed by robots.txt")
            conn.commit()
            logging.info("robots.txt disallows: %s", url)
            continue

        try:
            response = session.get(url, timeout=REQUEST_TIMEOUT_SECONDS, allow_redirects=True)
        except requests.RequestException as exc:
            failures += 1
            set_state(conn, url, "failed", note=str(exc))
            conn.commit()
            logging.warning("Request failed: %s | %s", url, exc)
            time.sleep(REQUEST_DELAY_SECONDS)
            continue
        finally:
            # One request at a time; no concurrency or burst traffic.
            time.sleep(REQUEST_DELAY_SECONDS)

        requests_made += 1
        final_url = normalize_url(response.url)
        if response.status_code in {429, 503}:
            set_state(conn, url, "deferred", response.status_code,
                      "Server asked crawler to slow down; stop this run and resume later.")
            conn.commit()
            logging.warning("Server returned HTTP %s. Stopping politely; rerun later to resume.", response.status_code)
            break

        if response.status_code != 200:
            failures += 1
            set_state(conn, url, "not_html", response.status_code, "HTTP response was not 200")
            already_done.add(url)
            conn.commit()
            logging.info("Not successful: %s (%s)", url, response.status_code)
            continue

        content_type = response.headers.get("Content-Type", "").lower()
        if "text/html" not in content_type:
            set_state(conn, url, "not_html", response.status_code, content_type)
            already_done.add(url)
            conn.commit()
            logging.info("Not HTML; skipped: %s (%s)", url, content_type)
            continue

        if not final_url or not same_site(final_url) or not robots.can_fetch(USER_AGENT, final_url):
            set_state(conn, url, "blocked", response.status_code,
                      "Redirected outside permitted same-site/robots scope")
            already_done.add(url)
            conn.commit()
            logging.info("Redirect/out-of-scope skipped: %s", response.url)
            continue

        soup = BeautifulSoup(response.text, "html.parser")
        canon = canonical_url(soup, final_url)
        if not is_in_scope(canon):
            set_state(conn, url, "done", response.status_code, "Canonical URL outside focused scope")
            already_done.add(url)
            conn.commit()
            continue

        # If canonical target was already saved, do not insert/fetch it a second time.
        if canon in already_done and canon != url:
            set_state(conn, url, "done", response.status_code, f"Canonical duplicate: {canon}")
            already_done.add(url)
            conn.commit()
            continue

        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        h1_tag = soup.find("h1")
        h1 = h1_tag.get_text(" ", strip=True) if h1_tag else ""
        desc_tag = soup.find("meta", attrs={"name": "description"})
        description = desc_tag.get("content", "").strip() if desc_tag else ""
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        kind = page_type(canon)

        conn.execute(
            """INSERT OR IGNORE INTO pages
               (url,title,h1,meta_description,page_type,depth,status_code,scanned_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (canon, title, h1, description, kind, depth, response.status_code, now),
        )
        set_state(conn, url, "done", response.status_code)
        already_done.add(url)
        already_done.add(canon)
        pages_saved += 1

        # Record public resource links but do not bulk-download file contents here.
        for anchor in soup.find_all("a", href=True):
            raw_target = urljoin(canon, anchor.get("href", "").strip())
            target = normalize_url(raw_target)
            if not target or not same_site(target):
                continue
            label = anchor.get_text(" ", strip=True)[:300]
            if is_resource(target):
                conn.execute(
                    """INSERT OR IGNORE INTO resources(url,title,extension,source_page,scanned_at)
                       VALUES(?,?,?,?,?)""",
                    (target, label, Path(urlparse(target).path).suffix.lower(), canon, now),
                )
                conn.execute(
                    """INSERT OR IGNORE INTO links(source_url,target_url,anchor_text,link_type,scanned_at)
                       VALUES(?,?,?,?,?)""",
                    (canon, target, label, "resource_link", now),
                )
                continue

            if is_in_scope(target):
                conn.execute(
                    """INSERT OR IGNORE INTO links(source_url,target_url,anchor_text,link_type,scanned_at)
                       VALUES(?,?,?,?,?)""",
                    (canon, target, label, "page_link", now),
                )
                enqueue(queue, queued, target, depth + 1, counter, already_done)

        conn.commit()
        logging.info(
            "New pages saved: %s | Queue: %s | %s | %s",
            pages_saved, len(queue), kind, canon
        )

    export_csv(conn)
    total_pages = conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
    total_links = conn.execute("SELECT COUNT(*) FROM links").fetchone()[0]
    total_resources = conn.execute("SELECT COUNT(*) FROM resources").fetchone()[0]
    remaining = len(queue)
    conn.close()

    print("\nRESUMABLE CATALOG SCAN FINISHED")
    print(f"New HTTP requests this run : {requests_made}")
    print(f"New pages saved this run   : {pages_saved}")
    print(f"Existing/handled URLs skip : {len(already_done)}")
    print(f"Failed responses           : {failures}")
    print(f"Robots-disallowed URLs     : {skipped_robots}")
    print(f"Total pages in DB          : {total_pages}")
    print(f"Total links in DB          : {total_links}")
    print(f"Resource URLs in DB        : {total_resources}")
    print(f"Queue remaining in memory  : {remaining}")
    print(f"Database                   : {DB_PATH.resolve()}")
    print("No fixed 200-request cap was used.")
    print("Only robots-permitted, linked, in-scope public pages were catalogued.")
    print("Question text/answers and protected file contents were not copied.")


if __name__ == "__main__":
    main()
