"""MPSC Today quiz API discovery (metadata-only).

Opens one public quiz page in a visible browser and records same-site Fetch/XHR
request metadata while the user manually operates the quiz.

It does NOT save cookies, authorization headers, POST bodies, response bodies,
question text, answer keys, or explanations.
"""

from __future__ import annotations

import csv
import json
import signal
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from playwright.sync_api import sync_playwright, Error as PlaywrightError

BASE_HOST = "mpsctoday.com"
DEFAULT_QUIZ_URL = (
    "https://www.mpsctoday.com/quiz/history/social-reformers-maharashtra/"
)
OUTPUT_DIR = Path(__file__).resolve().parent / "mpsc_output" / "quiz_api_audit"
JSON_PATH = OUTPUT_DIR / "quiz_network_metadata.json"
CSV_PATH = OUTPUT_DIR / "quiz_network_metadata.csv"


def safe_url(raw_url: str) -> str:
    """Return a URL with all query values redacted and its fragment removed."""
    parts = urlsplit(raw_url)
    redacted_query = [
        (key, "REDACTED")
        for key, _value in parse_qsl(parts.query, keep_blank_values=True)
    ]
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(redacted_query), "")
    )


def is_first_party(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host == BASE_HOST or host.endswith("." + BASE_HOST)


def write_outputs(records: list[dict[str, Any]]) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    JSON_PATH.write_text(
        json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    columns = [
        "time_utc",
        "resource_type",
        "method",
        "url",
        "status",
        "content_type",
        "response_bytes",
        "failed",
        "failure_reason",
        "endpoint_shape_seen_before",
    ]
    with CSV_PATH.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for item in records:
            writer.writerow({key: item.get(key, "") for key in columns})


def main() -> int:
    quiz_url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_QUIZ_URL
    parts = urlsplit(quiz_url)
    if (
        parts.scheme not in {"https", "http"}
        or not is_first_party(quiz_url)
        or parts.username
        or parts.password
    ):
        print("ERROR: Supply an http(s) URL on mpsctoday.com.")
        return 2

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    request_records: dict[int, dict[str, Any]] = {}
    seen_requests: set[tuple[str, str, str]] = set()

    def persist() -> None:
        write_outputs(records)

    def on_request(request) -> None:
        try:
            if request.resource_type not in {"xhr", "fetch"}:
                return
            if not is_first_party(request.url):
                return

            redacted_url = safe_url(request.url)
            key = (request.method, redacted_url, request.resource_type)
            duplicate_shape = key in seen_requests
            seen_requests.add(key)

            item = {
                "time_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "resource_type": request.resource_type,
                "method": request.method,
                "url": redacted_url,
                "status": "pending",
                "content_type": "",
                "response_bytes": "",
                "failed": False,
                "failure_reason": "",
                "endpoint_shape_seen_before": duplicate_shape,
            }
            records.append(item)
            request_records[id(request)] = item
            persist()
            print(f"[REQUEST] {request.method} {redacted_url}")
        except Exception as exc:
            print(f"[WARN] Could not record request metadata: {exc}")

    def on_response(response) -> None:
        try:
            item = request_records.get(id(response.request))
            if item is None:
                return
            headers = response.headers
            item["status"] = response.status
            item["content_type"] = headers.get("content-type", "")
            # Content-Length is metadata only; response bodies are never read.
            item["response_bytes"] = headers.get("content-length", "")
            persist()
            print(
                f"[RESPONSE] {response.status} "
                f"{item['content_type']} bytes={item['response_bytes'] or '?'}"
            )
        except Exception as exc:
            print(f"[WARN] Could not record response metadata: {exc}")

    def on_request_failed(request) -> None:
        item = request_records.get(id(request))
        if item is None:
            return
        item["failed"] = True
        # Avoid printing or persisting raw request URLs or headers here.
        item["failure_reason"] = request.failure or "request failed"
        item["status"] = "failed"
        persist()
        print(f"[FAILED] {item['url']}")

    print("\nMPSC TODAY — QUIZ API DISCOVERY (METADATA ONLY)")
    print("Target:", quiz_url)
    print("Output:", OUTPUT_DIR)
    print("\nA visible browser will open. Manually:")
    print("  1) Click Practice / Start Quiz.")
    print("  2) Change Marathi/English if available.")
    print("  3) Move to another question or page if the quiz allows it.")
    print("  4) Press F8 while the page is focused to stop and save.")
    print("     Ctrl+C in this terminal also stops and saves.\n")
    print("Only same-site Fetch/XHR metadata is recorded; no bodies or credentials.\n")

    stop_requested = False

    def handle_sigint(_sig, _frame):
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGINT, handle_sigint)

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=False)
            context = browser.new_context(
                viewport={"width": 1365, "height": 900},
                ignore_https_errors=False,
            )
            page = context.new_page()
            page.on("request", on_request)
            page.on("response", on_response)
            page.on("requestfailed", on_request_failed)

            page.goto(quiz_url, wait_until="domcontentloaded", timeout=60000)
            page.evaluate(
                """() => {
                    window.__MPSC_AUDIT_STOP = false;
                    window.addEventListener('keydown', (event) => {
                        if (event.key === 'F8') window.__MPSC_AUDIT_STOP = true;
                    });
                }"""
            )
            print("Browser ready. Operate the quiz now; press F8 in the browser to stop.")

            while not stop_requested:
                try:
                    if page.is_closed():
                        break
                    if page.evaluate("window.__MPSC_AUDIT_STOP === true"):
                        break
                    page.wait_for_timeout(400)
                except PlaywrightError:
                    break

            persist()
            context.close()
            browser.close()

    except KeyboardInterrupt:
        print("\nInterrupted; saving captured metadata...")
    except PlaywrightError as exc:
        print(f"\nBrowser error: {exc}")
        persist()
        print("Metadata captured before the error has been saved.")
        return 1
    finally:
        persist()

    endpoint_counts = Counter(
        (item["method"], item["url"], item["resource_type"])
        for item in records
    )
    print("\nCAPTURE COMPLETE")
    print("Fetch/XHR events:", len(records))
    print("Unique endpoint shapes:", len(endpoint_counts))
    print("JSON output:", JSON_PATH)
    print("CSV output :", CSV_PATH)
    if endpoint_counts:
        print("\nUnique endpoints:")
        for (method, url, resource_type), count in sorted(endpoint_counts.items()):
            print(f"  {method:6} {resource_type:5} repeats={count:3}  {url}")
    else:
        print("\nNo same-site Fetch/XHR requests were seen.")
        print("Try clicking Start/Practice again, or inspect whether the quiz is")
        print("loaded as a normal document/script rather than Fetch/XHR.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
