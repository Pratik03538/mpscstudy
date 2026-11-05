"""Inspect MPSC Today's public quiz page assets for its frontend API contract.

This script downloads only the public quiz page and same-origin JavaScript assets.
It searches source code around the quiz API endpoint for action names and pagination
logic (limit, offset, page, cursor, topic). It does NOT request the question API,
submit answers, or download quiz question-bank content.

Respect the site's robots.txt and rate limits. Use the resulting report to build
an importer only for content covered by your written permission.
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.mpsctoday.com/quiz/history/"
BASE_HOSTS = {"mpsctoday.com", "www.mpsctoday.com"}
API_MARKER = "/api/quiz/index.php"
PROJECT_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = PROJECT_DIR / "mpsc_output" / "quiz_api_audit"
REPORT_JSON = OUTPUT_DIR / "frontend_api_contract.json"
REPORT_TXT = OUTPUT_DIR / "frontend_api_contract.txt"

REQUEST_TIMEOUT = 25
REQUEST_DELAY = 1.5
MAX_JS_BYTES = 5_000_000
USER_AGENT = "MPSCStudyContractInspector/1.0 (authorized quiz integration; add-contact-email)"

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")


def is_first_party(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in BASE_HOSTS


def redact_secrets(text: str) -> str:
    patterns = [
        (r"(?i)(authorization|cookie|api[_-]?key|secret|password|access[_-]?token)(\s*[:=]\s*)(['\"][^'\"]*['\"]|[^\s,;]+)",
         r"\1\2[REDACTED]"),
        (r"(?i)(Bearer\s+)[A-Za-z0-9._~+/-]+=*", r"\1[REDACTED]"),
    ]
    for pattern, replacement in patterns:
        text = re.sub(pattern, replacement, text)
    return text


def extract_context(text: str, marker: str, radius: int = 700) -> list[str]:
    snippets = []
    start = 0
    while True:
        index = text.find(marker, start)
        if index < 0:
            break
        left = max(0, index - radius)
        right = min(len(text), index + len(marker) + radius)
        snippets.append(redact_secrets(text[left:right]))
        start = index + len(marker)
        if len(snippets) >= 12:
            break
    return snippets


def build_robot_parser(session: requests.Session) -> RobotFileParser:
    robots_url = urljoin(BASE_URL, "/robots.txt")
    response = session.get(robots_url, timeout=REQUEST_TIMEOUT)
    if response.status_code >= 400 and response.status_code != 404:
        raise RuntimeError(f"Cannot verify robots.txt: HTTP {response.status_code}")
    parser = RobotFileParser()
    parser.set_url(robots_url)
    parser.parse(response.text.splitlines() if response.status_code == 200 else [])
    return parser


def can_fetch(robots: RobotFileParser, url: str) -> bool:
    if not is_first_party(url):
        return False
    if not robots.can_fetch(USER_AGENT, url):
        logging.warning("robots.txt disallows: %s", url)
        return False
    return True


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    robots = build_robot_parser(session)
    if not can_fetch(robots, BASE_URL):
        raise RuntimeError("Quiz history page is not allowed by robots.txt.")

    logging.info("Fetching quiz page: %s", BASE_URL)
    page_response = session.get(BASE_URL, timeout=REQUEST_TIMEOUT)
    page_response.raise_for_status()
    soup = BeautifulSoup(page_response.text, "html.parser")

    inline_scripts = [
        script.string or script.get_text()
        for script in soup.find_all("script")
        if not script.get("src") and (script.string or script.get_text()).strip()
    ]

    script_urls: list[str] = []
    for script in soup.find_all("script", src=True):
        script_url = urljoin(BASE_URL, script["src"])
        if is_first_party(script_url) and script_url not in script_urls:
            script_urls.append(script_url)

    sources: list[dict] = []
    for index, source in enumerate(inline_scripts, start=1):
        sources.append({
            "source_type": "inline_script",
            "source_url": BASE_URL,
            "source_name": f"inline-{index}",
            "content": source,
        })

    fetched_scripts = []
    skipped_scripts = []
    for index, script_url in enumerate(script_urls, start=1):
        if not can_fetch(robots, script_url):
            skipped_scripts.append({"url": script_url, "reason": "robots.txt disallowed"})
            continue
        try:
            response = session.get(script_url, timeout=REQUEST_TIMEOUT, stream=True)
            if response.status_code != 200:
                skipped_scripts.append({"url": script_url, "reason": f"HTTP {response.status_code}"})
                continue
            content_type = response.headers.get("Content-Type", "").lower()
            raw = response.raw.read(MAX_JS_BYTES + 1, decode_content=True)
            if len(raw) > MAX_JS_BYTES:
                skipped_scripts.append({"url": script_url, "reason": "larger than 5 MB"})
                continue
            if "javascript" not in content_type and not script_url.lower().split("?")[0].endswith(".js"):
                skipped_scripts.append({"url": script_url, "reason": f"not recognized as JS ({content_type})"})
                continue
            body = raw.decode(response.encoding or "utf-8", errors="replace")
            fetched_scripts.append(script_url)
            sources.append({
                "source_type": "external_script",
                "source_url": script_url,
                "source_name": f"external-{index}",
                "content": body,
            })
            logging.info("Read JS asset %s/%s: %s", len(fetched_scripts), len(script_urls), script_url)
        except requests.RequestException as exc:
            skipped_scripts.append({"url": script_url, "reason": f"request error: {exc}"})
        finally:
            time.sleep(REQUEST_DELAY)

    api_snippets = []
    pagination_snippets = []
    actions = set()
    parameter_hints = set()
    code_sources_with_api = []

    for source in sources:
        code = source["content"]
        api_contexts = extract_context(code, API_MARKER)
        if api_contexts:
            code_sources_with_api.append(source["source_url"])
            for snippet in api_contexts:
                api_snippets.append({
                    "source_type": source["source_type"],
                    "source_url": source["source_url"],
                    "snippet": snippet,
                })

        # Gather nearby source-code references to pagination-related terms.
        for term in ("offset", "page", "cursor", "limit", "topic", "subject", "action"):
            if re.search(rf"\b{term}\b", code, flags=re.I):
                parameter_hints.add(term)

        for match in re.finditer(
            r"""(?:action\s*[:=]\s*|['"]action['"]\s*:\s*)['"]([^'"]+)['"]""",
            code,
            flags=re.I,
        ):
            actions.add(match.group(1)[:100])

        for term in ("offset", "page", "cursor", "limit"):
            for snippet in extract_context(code, term, radius=250):
                if API_MARKER in snippet or "quiz" in snippet.lower():
                    pagination_snippets.append({
                        "source_url": source["source_url"],
                        "term": term,
                        "snippet": snippet,
                    })

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "page_url": BASE_URL,
        "page_http_status": page_response.status_code,
        "same_origin_script_count": len(script_urls),
        "fetched_script_count": len(fetched_scripts),
        "fetched_scripts": fetched_scripts,
        "skipped_scripts": skipped_scripts,
        "sources_containing_api_marker": sorted(set(code_sources_with_api)),
        "possible_action_literals": sorted(actions),
        "pagination_parameter_hints": sorted(parameter_hints),
        "api_source_snippets": api_snippets,
        "pagination_source_snippets": pagination_snippets[:80],
        "note": (
            "This is frontend source-code inspection only. No question API endpoint was "
            "called and no quiz content was downloaded."
        ),
    }

    REPORT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "MPSC TODAY QUIZ FRONTEND API CONTRACT INSPECTION",
        f"Page: {BASE_URL}",
        f"HTTP: {page_response.status_code}",
        f"Same-origin scripts: {len(script_urls)}",
        f"Fetched scripts: {len(fetched_scripts)}",
        f"Sources containing quiz API marker: {len(set(code_sources_with_api))}",
        f"Possible action literals: {', '.join(sorted(actions)) or 'none found'}",
        f"Pagination hints: {', '.join(sorted(parameter_hints)) or 'none found'}",
        "",
        "=== API SOURCE SNIPPETS ===",
    ]
    for item in api_snippets:
        lines.extend([
            f"\n--- {item['source_type']} | {item['source_url']} ---",
            item["snippet"],
        ])
    lines.append("\n=== PAGINATION SOURCE SNIPPETS ===")
    for item in pagination_snippets[:80]:
        lines.extend([
            f"\n--- term={item['term']} | {item['source_url']} ---",
            item["snippet"],
        ])
    lines.append("\nNo question API calls or question-bank downloads were performed.")
    REPORT_TXT.write_text("\n".join(lines), encoding="utf-8")

    print("\nFRONTEND CONTRACT INSPECTION COMPLETE")
    print(f"Page HTTP status                 : {page_response.status_code}")
    print(f"Same-origin JS assets             : {len(script_urls)}")
    print(f"JS assets fetched                 : {len(fetched_scripts)}")
    print(f"Sources containing API endpoint   : {len(set(code_sources_with_api))}")
    print(f"Possible action literals          : {', '.join(sorted(actions)) or 'none found'}")
    print(f"Pagination hints                  : {', '.join(sorted(parameter_hints)) or 'none found'}")
    print(f"JSON report                       : {REPORT_JSON.resolve()}")
    print(f"Text report                       : {REPORT_TXT.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
