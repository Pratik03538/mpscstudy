"""Inspect quiz-runner topic configuration on MPSC Today topic pages.

This checks only page HTML metadata (data-subject/data-topic on #quiz-runner).
It does not call the quiz API, submit answers, or download question content.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

BASE = "https://www.mpsctoday.com"
PAGES = [
    BASE + "/quiz/history/",
    BASE + "/quiz/history/freedom-movement-1857-1947",
]
USER_AGENT = "MPSCStudyAuthorizedIntegration/1.0 (add-admin-approved-contact)"
TIMEOUT = 25
DELAY_SECONDS = 1.5
OUTPUT_DIR = Path(__file__).resolve().parent / "mpsc_output" / "quiz_api_audit"
JSON_PATH = OUTPUT_DIR / "topic_page_config.json"
TXT_PATH = OUTPUT_DIR / "topic_page_config.txt"


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html"})
    robots_url = BASE + "/robots.txt"
    robots_response = session.get(robots_url, timeout=TIMEOUT)
    if robots_response.status_code >= 400 and robots_response.status_code != 404:
        raise RuntimeError(
            f"Cannot verify robots.txt (HTTP {robots_response.status_code}); stopping."
        )

    robots = RobotFileParser()
    robots.set_url(robots_url)
    robots.parse(
        robots_response.text.splitlines()
        if robots_response.status_code == 200
        else []
    )

    records = []
    for index, page_url in enumerate(PAGES):
        row = {
            "requested_url": page_url,
            "status": None,
            "final_url": None,
            "quiz_runner_found": False,
            "data_subject": None,
            "data_topic": None,
            "data_daily": None,
            "data_date": None,
            "data_title_present": False,
            "error": None,
        }
        try:
            if not robots.can_fetch(USER_AGENT, page_url):
                row["error"] = "robots.txt disallows this page"
            else:
                response = session.get(
                    page_url, timeout=TIMEOUT, allow_redirects=True
                )
                row["status"] = response.status_code
                row["final_url"] = response.url
                if not robots.can_fetch(USER_AGENT, response.url):
                    row["error"] = "robots.txt disallows the redirected page"
                elif response.status_code >= 400:
                    row["error"] = f"HTTP {response.status_code}"
                else:
                    soup = BeautifulSoup(response.text, "html.parser")
                    node = soup.find(id="quiz-runner")
                    if node is not None:
                        attrs = node.attrs
                        row["quiz_runner_found"] = True
                        row["data_subject"] = attrs.get("data-subject")
                        row["data_topic"] = attrs.get("data-topic")
                        row["data_daily"] = attrs.get("data-daily")
                        row["data_date"] = attrs.get("data-date")
                        row["data_title_present"] = "data-title" in attrs
                    else:
                        candidates = soup.select("[data-subject], [data-topic]")
                        row["error"] = (
                            "quiz-runner element not found; "
                            f"other data-subject/data-topic elements={len(candidates)}"
                        )
        except requests.RequestException as exc:
            row["error"] = f"request error: {type(exc).__name__}: {exc}"
        records.append(row)
        print(
            f"PAGE {index + 1}: HTTP={row['status']} "
            f"quiz_runner={row['quiz_runner_found']} "
            f"subject={row['data_subject']!r} topic={row['data_topic']!r} "
            f"error={row['error']!r}"
        )
        if index + 1 < len(PAGES):
            time.sleep(DELAY_SECONDS)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pages": records,
        "interpretation": (
            "The frontend JavaScript reads the topic filter from "
            "#quiz-runner[data-topic]. A blank value means the page does not "
            "send a topic parameter; a nonblank value is the exact value "
            "the frontend sends as topic."
        ),
        "content_saved": "Page configuration metadata only; no quiz questions or answers.",
    }
    JSON_PATH.write_text(
        json.dumps(report, ensure_ascii=True, indent=2), encoding="utf-8"
    )
    lines = [
        "MPSC TODAY TOPIC PAGE CONFIG",
        "Only #quiz-runner data attributes were inspected.",
        "",
    ]
    for row in records:
        lines.extend(
            [
                f"Requested URL: {row['requested_url']}",
                f"HTTP status: {row['status']}",
                f"Final URL: {row['final_url']}",
                f"quiz-runner found: {row['quiz_runner_found']}",
                f"data-subject: {row['data_subject']!r}",
                f"data-topic: {row['data_topic']!r}",
                f"data-daily: {row['data_daily']!r}",
                f"data-date: {row['data_date']!r}",
                f"Error: {row['error']}",
                "",
            ]
        )
    lines.append(
        "No quiz API calls; no question text, options, answers, or explanations saved."
    )
    TXT_PATH.write_text("\n".join(lines), encoding="utf-8")
    print("\nTOPIC PAGE CONFIG INSPECTION COMPLETE")
    print(f"JSON report: {JSON_PATH.resolve()}")
    print(f"Text report: {TXT_PATH.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
