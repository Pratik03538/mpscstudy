"""Direct-terminal probe for MPSC Today quiz API and frontend pagination contract.

No browser or manual clicks are used. The script:
1) fetches the public History quiz page,
2) inspects same-origin JavaScript for API action/pagination implementation hints,
3) performs three low-volume GET probes with action=questions, subject=history,
   and limit=15/30/60,
4) stores only response counts, array sizes, object field names, and source snippets.

It does NOT save question text, options, answers, explanations, cookies, tokens,
or POST data. It does not attempt pagination or bulk import yet. Use its report
to confirm the server's actual pagination contract before creating the importer.
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

PAGE_URL = "https://www.mpsctoday.com/quiz/history/"
API_URL = "https://www.mpsctoday.com/api/quiz/index.php"
SUBJECT = "history"
KNOWN_ACTION = "questions"
LIMIT_PROBES = (15, 30, 60)

BASE_HOSTS = {"mpsctoday.com", "www.mpsctoday.com"}
API_MARKER = "/api/quiz/index.php"
PROJECT_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = PROJECT_DIR / "mpsc_output" / "quiz_api_audit"
JSON_REPORT = OUTPUT_DIR / "direct_api_probe.json"
TXT_REPORT = OUTPUT_DIR / "direct_api_probe.txt"

TIMEOUT = 25
DELAY = 2.0
MAX_SCRIPT_BYTES = 4_000_000
USER_AGENT = "MPSCStudyAuthorizedIntegration/1.0 (add-admin-approved-contact)"


def is_first_party(url: str) -> bool:
    return (urlparse(url).hostname or "").lower() in BASE_HOSTS


def robot_parser(session: requests.Session) -> RobotFileParser:
    url = "https://www.mpsctoday.com/robots.txt"
    response = session.get(url, timeout=TIMEOUT)
    if response.status_code >= 400 and response.status_code != 404:
        raise RuntimeError(f"Cannot verify robots.txt (HTTP {response.status_code}); stop.")
    parser = RobotFileParser()
    parser.set_url(url)
    parser.parse(response.text.splitlines() if response.status_code == 200 else [])
    return parser


def extract_snippets(text: str, marker: str, radius: int = 550, max_items: int = 10) -> list[str]:
    results: list[str] = []
    start = 0
    while len(results) < max_items:
        pos = text.find(marker, start)
        if pos < 0:
            break
        left = max(0, pos - radius)
        right = min(len(text), pos + len(marker) + radius)
        snippet = text[left:right]
        # Redact likely credential assignments if any appear near the code.
        snippet = re.sub(
            r"(?i)(authorization|cookie|secret|password|api[_-]?key|token)(\s*[:=]\s*)(['\"][^'\"]*['\"]|[^\s,;]+)",
            r"\1\2[REDACTED]",
            snippet,
        )
        results.append(snippet)
        start = pos + len(marker)
    return results


def inspect_frontend(session: requests.Session, robots: RobotFileParser) -> dict:
    if not robots.can_fetch(USER_AGENT, PAGE_URL):
        raise RuntimeError("robots.txt disallows the History quiz page.")
    response = session.get(PAGE_URL, timeout=TIMEOUT)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")

    script_urls = []
    for tag in soup.find_all("script", src=True):
        url = urljoin(PAGE_URL, tag["src"])
        if is_first_party(url) and url not in script_urls and robots.can_fetch(USER_AGENT, url):
            script_urls.append(url)

    snippets = []
    pagination_terms = set()
    action_literals = set()
    fetched = []
    skipped = []

    inline_scripts = [
        tag.string or tag.get_text()
        for tag in soup.find_all("script")
        if not tag.get("src") and (tag.string or tag.get_text()).strip()
    ]
    all_sources = [(PAGE_URL, code) for code in inline_scripts]

    for script_url in script_urls:
        try:
            script_response = session.get(script_url, timeout=TIMEOUT, stream=True)
            if script_response.status_code != 200:
                skipped.append({"url": script_url, "reason": f"HTTP {script_response.status_code}"})
                continue
            raw = script_response.raw.read(MAX_SCRIPT_BYTES + 1, decode_content=True)
            if len(raw) > MAX_SCRIPT_BYTES:
                skipped.append({"url": script_url, "reason": "larger than 4 MB"})
                continue
            code = raw.decode(script_response.encoding or "utf-8", errors="replace")
            all_sources.append((script_url, code))
            fetched.append(script_url)
        except requests.RequestException as exc:
            skipped.append({"url": script_url, "reason": str(exc)})
        time.sleep(DELAY)

    for source_url, code in all_sources:
        for snippet in extract_snippets(code, API_MARKER):
            snippets.append({"source_url": source_url, "snippet": snippet})
        for term in ("offset", "page", "cursor", "limit", "topic", "subject", "action"):
            if re.search(rf"\b{term}\b", code, flags=re.I):
                pagination_terms.add(term)
                if term in {"offset", "page", "cursor", "limit"}:
                    for snippet in extract_snippets(code, term, radius=260, max_items=12):
                        if API_MARKER in snippet or "quiz" in snippet.lower():
                            snippets.append({"source_url": source_url, "snippet": snippet})
        for match in re.finditer(
            r"""(?:action\s*[:=]\s*|['"]action['"]\s*:\s*)['"]([^'"]+)['"]""",
            code,
            flags=re.I,
        ):
            action_literals.add(match.group(1)[:100])

    # Deduplicate source snippets.
    seen = set()
    unique_snippets = []
    for item in snippets:
        key = (item["source_url"], item["snippet"])
        if key not in seen:
            seen.add(key)
            unique_snippets.append(item)

    return {
        "page_status": response.status_code,
        "same_origin_script_count": len(script_urls),
        "scripts_fetched": fetched,
        "scripts_skipped": skipped,
        "possible_action_literals": sorted(action_literals),
        "pagination_terms_seen_in_source": sorted(pagination_terms),
        "source_snippets": unique_snippets[:100],
    }


def summarize_question_response(payload) -> dict:
    if not isinstance(payload, dict):
        return {"top_level_type": type(payload).__name__}
    data = payload.get("data")
    first_keys = []
    if isinstance(data, list) and data and isinstance(data[0], dict):
        first_keys = sorted(data[0].keys())
    count = payload.get("count")
    return {
        "success": payload.get("success") if isinstance(payload.get("success"), bool) else None,
        "count_value": count if isinstance(count, (int, float)) else None,
        "data_type": type(data).__name__,
        "returned_items": len(data) if isinstance(data, list) else None,
        "first_item_field_names": first_keys,
    }


def probe_api(session: requests.Session, robots: RobotFileParser) -> list[dict]:
    if not robots.can_fetch(USER_AGENT, API_URL):
        raise RuntimeError("robots.txt disallows the quiz API endpoint.")
    reports = []
    for limit in LIMIT_PROBES:
        params = {"action": KNOWN_ACTION, "limit": limit, "subject": SUBJECT}
        response = session.get(API_URL, params=params, timeout=TIMEOUT)
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "").lower()
        item = {
            "subject": SUBJECT,
            "action": KNOWN_ACTION,
            "requested_limit": limit,
            "http_status": response.status_code,
            "content_type": content_type,
            "response_bytes": len(response.content),
            "schema_summary": None,
            "error": None,
        }
        if "application/json" in content_type:
            try:
                item["schema_summary"] = summarize_question_response(response.json())
            except ValueError:
                item["error"] = "Invalid JSON response."
        else:
            item["error"] = "API did not return JSON."
        reports.append(item)
        logging.info(
            "limit=%s HTTP=%s bytes=%s summary=%s",
            limit,
            response.status_code,
            len(response.content),
            item["schema_summary"],
        )
        time.sleep(DELAY)
    return reports


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json, text/html;q=0.9"})
    robots = robot_parser(session)

    logging.info("Inspecting frontend JavaScript (no browser)...")
    frontend = inspect_frontend(session, robots)
    logging.info("Probing known GET endpoint using limits %s...", LIMIT_PROBES)
    api_probes = probe_api(session, robots)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "page_url": PAGE_URL,
        "api_url": API_URL,
        "frontend": frontend,
        "api_limit_probes": api_probes,
        "note": (
            "Only schema/count/field names and frontend code snippets are stored. "
            "Question text, options, correct answers, and explanations are not stored. "
            "The probe does not test page/offset pagination or perform a bulk import."
        ),
    }
    JSON_REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "MPSC TODAY DIRECT API PROBE",
        f"Page HTTP: {frontend['page_status']}",
        f"Same-origin script tags: {frontend['same_origin_script_count']}",
        f"Scripts fetched: {len(frontend['scripts_fetched'])}",
        f"Possible actions: {', '.join(frontend['possible_action_literals']) or 'none'}",
        f"Pagination terms: {', '.join(frontend['pagination_terms_seen_in_source']) or 'none'}",
        "",
        "=== API LIMIT PROBES ===",
    ]
    for item in api_probes:
        lines.append(json.dumps(item, ensure_ascii=False))
    lines.extend(["", "=== FRONTEND SOURCE SNIPPETS ==="])
    for item in frontend["source_snippets"]:
        lines.extend([f"\n--- {item['source_url']} ---", item["snippet"]])
    lines.append("\nNo browser opened. No quiz data values were saved.")
    TXT_REPORT.write_text("\n".join(lines), encoding="utf-8")

    print("\nDIRECT API PROBE COMPLETE")
    print(f"Action: {KNOWN_ACTION}")
    print(f"Subject: {SUBJECT}")
    for item in api_probes:
        summary = item.get("schema_summary") or {}
        print(
            f"limit={item['requested_limit']} -> HTTP {item['http_status']}, "
            f"returned={summary.get('returned_items')}, count={summary.get('count_value')}, "
            f"bytes={item['response_bytes']}"
        )
    print(f"Possible actions: {', '.join(frontend['possible_action_literals']) or 'none found'}")
    print(f"Pagination terms: {', '.join(frontend['pagination_terms_seen_in_source']) or 'none found'}")
    print(f"JSON report: {JSON_REPORT.resolve()}")
    print(f"Text report: {TXT_REPORT.resolve()}")
    print("No browser opened; no question text/options/answers/explanations were saved.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
