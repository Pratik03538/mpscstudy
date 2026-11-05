"""MPSC Today quiz API discovery (limited request metadata only).

Use the website's normal UI manually. This script observes requests; it does not
make extra API calls, paginate, or bulk-download questions.

For the quiz GET request only, it records allowlisted filter values
(action/limit/subject/topic), response count, returned item count, and object
field names. It never saves question/option/answer/explanation values.
For POST requests it records field names only, never values or bodies.
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
DEFAULT_QUIZ_URL = "https://www.mpsctoday.com/quiz/history/"
EXCLUDED_QUIZ_PATHS = {"/quiz/history/social-reformers-maharashtra/"}
SAFE_GET_FILTERS = {"action", "limit", "subject", "topic"}
API_PATH = "/api/quiz/index.php"

OUTPUT_DIR = Path(__file__).resolve().parent / "mpsc_output" / "quiz_api_audit"
JSON_PATH = OUTPUT_DIR / "quiz_network_metadata.json"
CSV_PATH = OUTPUT_DIR / "quiz_network_metadata.csv"


def is_first_party(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host == BASE_HOST or host.endswith("." + BASE_HOST)


def safe_url(raw_url: str) -> str:
    """Keep parameter names but redact all query values in the displayed URL."""
    parts = urlsplit(raw_url)
    redacted = [(key, "REDACTED") for key, _ in parse_qsl(parts.query, keep_blank_values=True)]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(redacted), ""))


def allowed_filter_values(raw_url: str) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {}
    for key, value in parse_qsl(urlsplit(raw_url).query, keep_blank_values=True):
        if key.lower() in SAFE_GET_FILTERS:
            values.setdefault(key, []).append(value[:120])
    return values


def safe_post_field_names(request) -> list[str]:
    """Return POST field names only. Never persist POST field values."""
    names: set[str] = set()
    try:
        payload = request.post_data_json
        if isinstance(payload, dict):
            names.update(str(key) for key in payload)
        elif isinstance(payload, list):
            names.add("<json-array>")
    except Exception:
        pass
    if not names:
        try:
            names.update(key for key, _ in parse_qsl(request.post_data or "", keep_blank_values=True))
        except Exception:
            pass
    blocked = ("token", "secret", "password", "cookie", "authorization", "api_key", "apikey")
    return sorted(name for name in names if not any(word in name.lower() for word in blocked))


def summarize_question_schema(payload: Any) -> dict[str, Any]:
    """Return schema metadata only; never return any scalar question-bank values."""
    if not isinstance(payload, dict):
        return {"top_level_type": type(payload).__name__}

    data = payload.get("data")
    first_item_keys: list[str] = []
    if isinstance(data, list) and data and isinstance(data[0], dict):
        first_item_keys = sorted(str(key) for key in data[0].keys())

    return {
        "top_level_keys": sorted(str(key) for key in payload.keys()),
        "success_type": type(payload.get("success")).__name__,
        "count_type": type(payload.get("count")).__name__,
        "count_value": payload.get("count") if isinstance(payload.get("count"), (int, float)) else None,
        "data_type": type(data).__name__,
        "data_item_count": len(data) if isinstance(data, list) else None,
        "first_item_field_names": first_item_keys,
    }


def write_outputs(records: list[dict[str, Any]]) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    JSON_PATH.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    columns = [
        "time_utc", "resource_type", "method", "url", "get_filter_values",
        "post_field_names", "status", "content_type", "response_bytes",
        "response_schema_summary", "failed", "failure_reason",
        "endpoint_shape_seen_before",
    ]
    with CSV_PATH.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for item in records:
            row = dict(item)
            row["get_filter_values"] = json.dumps(item.get("get_filter_values", {}), ensure_ascii=False)
            row["post_field_names"] = ", ".join(item.get("post_field_names", []))
            row["response_schema_summary"] = json.dumps(item.get("response_schema_summary"), ensure_ascii=False)
            writer.writerow({key: row.get(key, "") for key in columns})


def main() -> int:
    quiz_url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_QUIZ_URL
    parts = urlsplit(quiz_url)
    quiz_path = parts.path.lower()
    if quiz_path in EXCLUDED_QUIZ_PATHS:
        print("SKIPPED: This quiz page was reported empty and is excluded.")
        return 2
    if (
        parts.scheme not in {"https", "http"}
        or not is_first_party(quiz_url)
        or parts.username
        or parts.password
        or not quiz_path.startswith("/quiz/")
    ):
        print("ERROR: Supply a quiz URL on mpsctoday.com under /quiz/.")
        return 2

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    request_records: dict[int, dict[str, Any]] = {}
    seen: set[tuple[str, str, str]] = set()
    stop_requested = False
    browser = context = None

    def persist() -> None:
        write_outputs(records)

    def on_request(request) -> None:
        try:
            if request.resource_type not in {"xhr", "fetch"} or not is_first_party(request.url):
                return
            path = urlsplit(request.url).path
            # Do not collect Cloudflare telemetry or unrelated application requests.
            if path != API_PATH:
                return

            is_quiz_get = request.method.upper() == "GET"
            redacted = safe_url(request.url)
            shape = (request.method.upper(), redacted, request.resource_type)
            duplicate = shape in seen
            seen.add(shape)

            item = {
                "time_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "resource_type": request.resource_type,
                "method": request.method.upper(),
                "url": redacted,
                "get_filter_values": allowed_filter_values(request.url) if is_quiz_get else {},
                "post_field_names": safe_post_field_names(request) if request.method.upper() == "POST" else [],
                "status": "pending",
                "content_type": "",
                "response_bytes": "",
                "response_schema_summary": None,
                "failed": False,
                "failure_reason": "",
                "endpoint_shape_seen_before": duplicate,
            }
            records.append(item)
            request_records[id(request)] = item
            persist()
            print(f"[API REQUEST] {item['method']} {item['url']}")
            if item["get_filter_values"]:
                print("[SAFE GET FILTERS]", json.dumps(item["get_filter_values"], ensure_ascii=False))
            if item["post_field_names"]:
                print("[POST FIELD NAMES ONLY]", ", ".join(item["post_field_names"]))
        except Exception as exc:
            print(f"[WARN] Request metadata capture failed: {exc}")

    def on_response(response) -> None:
        item = request_records.get(id(response.request))
        if item is None:
            return
        try:
            item["status"] = response.status
            item["content_type"] = response.headers.get("content-type", "")
            item["response_bytes"] = response.headers.get("content-length", "")
            # Only inspect JSON schema/count metadata for the GET quiz feed.
            # No response body values are saved or printed.
            if (
                response.request.method.upper() == "GET"
                and urlsplit(response.url).path == API_PATH
                and "application/json" in item["content_type"].lower()
            ):
                try:
                    body = response.body()
                    item["response_bytes"] = item["response_bytes"] or str(len(body))
                    payload = json.loads(body.decode("utf-8-sig"))
                    item["response_schema_summary"] = summarize_question_schema(payload)
                except Exception:
                    item["response_schema_summary"] = {"parse_error": "JSON unavailable or invalid"}
            persist()
            print(
                f"[API RESPONSE] {response.status} {item['content_type']} "
                f"bytes={item['response_bytes'] or '?'} "
                f"schema={json.dumps(item['response_schema_summary'], ensure_ascii=False)}"
            )
        except Exception as exc:
            print(f"[WARN] Response metadata capture failed: {exc}")

    def on_request_failed(request) -> None:
        item = request_records.get(id(request))
        if item is None:
            return
        item["failed"] = True
        item["failure_reason"] = "request failed"
        item["status"] = "failed"
        persist()
        print(f"[API FAILED] {item['url']}")

    def handle_sigint(_sig, _frame):
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGINT, handle_sigint)

    print("\nMPSC TODAY — QUIZ API INVENTORY")
    print("Target:", quiz_url)
    print("This script only observes normal UI requests; it does not call/paginate the API itself.")
    print("Click quiz start only. Do NOT select an answer or submit an attempt.")
    print("Press F8 in the browser or Ctrl+C in this terminal to save and stop.")
    print("Saved: allowlisted GET filter values, count, array size, field names, status/size.")
    print("Not saved: question text, options, correct answers, explanations, POST values, cookies, tokens.\n")

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=False)
            context = browser.new_context(viewport={"width": 1365, "height": 900})
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
            print("Browser ready. Click only Practice/Start if needed; do not answer questions.")
            while not stop_requested:
                try:
                    if page.is_closed() or page.evaluate("window.__MPSC_AUDIT_STOP === true"):
                        break
                    page.wait_for_timeout(400)
                except PlaywrightError:
                    break
            persist()
    except KeyboardInterrupt:
        print("\nInterrupted; saving captured metadata...")
    except PlaywrightError as exc:
        print(f"\nBrowser error: {exc}")
        persist()
    finally:
        persist()
        for handle in (context, browser):
            if handle is not None:
                try:
                    handle.close()
                except Exception:
                    pass

    counts = Counter((item["method"], item["url"], item["resource_type"]) for item in records)
    print("\nCAPTURE COMPLETE")
    print("API events:", len(records))
    print("Unique API endpoint shapes:", len(counts))
    print("JSON output:", JSON_PATH)
    print("CSV output :", CSV_PATH)
    print("No quiz question/option/answer/explanation values were saved.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
