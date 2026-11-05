"""MPSC Today quiz API discovery (schema/metadata only).

Records same-site Fetch/XHR endpoint metadata during manual browser interaction.
It stores URL paths, query parameter names, POST field names, response status,
byte size, and JSON structure (key paths/types/array lengths) only.

It does NOT save query values, POST values, cookies, authorization headers,
response values, question text, answer keys, or explanations.
Obtain written permission before any bulk import of question-bank content.
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
    """Redact every query value; preserve parameter names for API analysis."""
    parts = urlsplit(raw_url)
    redacted_query = [
        (key, "REDACTED")
        for key, _value in parse_qsl(parts.query, keep_blank_values=True)
    ]
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(redacted_query), "")
    )


def query_parameter_names(raw_url: str) -> list[str]:
    return sorted({key for key, _ in parse_qsl(urlsplit(raw_url).query, keep_blank_values=True)})


def is_first_party(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host == BASE_HOST or host.endswith("." + BASE_HOST)


def safe_post_field_names(request) -> list[str]:
    """Return field names only; never return POST field values."""
    names: set[str] = set()
    try:
        data = request.post_data_json
        if isinstance(data, dict):
            names.update(str(key) for key in data.keys())
        elif isinstance(data, list):
            names.add("<json-array>")
    except Exception:
        pass

    if not names:
        try:
            raw = request.post_data or ""
            names.update(key for key, _ in parse_qsl(raw, keep_blank_values=True))
        except Exception:
            pass

    # Do not expose names that look like credential fields.
    blocked = ("token", "secret", "password", "cookie", "authorization", "api_key", "apikey")
    return sorted(name for name in names if not any(part in name.lower() for part in blocked))


def summarize_json_shape(payload: Any) -> dict[str, Any]:
    """Summarize JSON schema only; never return scalar values or question text."""
    paths: list[dict[str, str]] = []
    arrays: list[dict[str, Any]] = []
    queue: list[tuple[str, Any, int]] = [("$", payload, 0)]
    seen = 0

    while queue and seen < 250:
        path, value, depth = queue.pop(0)
        seen += 1
        if isinstance(value, dict):
            paths.append({"path": path, "type": "object"})
            if depth < 6:
                for key, child in list(value.items())[:80]:
                    queue.append((f"{path}.{key}", child, depth + 1))
        elif isinstance(value, list):
            item_type = type(value[0]).__name__ if value else "empty"
            arrays.append({"path": path, "item_count": len(value), "first_item_type": item_type})
            paths.append({"path": path, "type": "array"})
            if value and depth < 6 and isinstance(value[0], (dict, list)):
                queue.append((f"{path}[]", value[0], depth + 1))
        elif value is None:
            paths.append({"path": path, "type": "null"})
        elif isinstance(value, bool):
            paths.append({"path": path, "type": "boolean"})
        elif isinstance(value, (int, float)):
            paths.append({"path": path, "type": "number"})
        else:
            paths.append({"path": path, "type": "string"})

    top_level_keys = sorted(payload.keys()) if isinstance(payload, dict) else []
    return {
        "top_level_keys": top_level_keys,
        "paths": paths,
        "arrays": arrays,
        "truncated": bool(queue),
    }


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
        "query_parameter_names",
        "post_field_names",
        "status",
        "content_type",
        "response_bytes",
        "response_top_level_keys",
        "response_array_summary",
        "failed",
        "failure_reason",
        "endpoint_shape_seen_before",
    ]
    with CSV_PATH.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for item in records:
            row = dict(item)
            row["query_parameter_names"] = ", ".join(item.get("query_parameter_names", []))
            row["post_field_names"] = ", ".join(item.get("post_field_names", []))
            schema = item.get("response_schema") or {}
            row["response_top_level_keys"] = ", ".join(schema.get("top_level_keys", []))
            row["response_array_summary"] = json.dumps(schema.get("arrays", []), ensure_ascii=False)
            writer.writerow({key: row.get(key, "") for key in columns})


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
    stop_requested = False
    browser = context = None

    def persist() -> None:
        write_outputs(records)

    def on_request(request) -> None:
        try:
            if request.resource_type not in {"xhr", "fetch"} or not is_first_party(request.url):
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
                "query_parameter_names": query_parameter_names(request.url),
                "post_field_names": safe_post_field_names(request),
                "status": "pending",
                "content_type": "",
                "response_bytes": "",
                "response_schema": None,
                "failed": False,
                "failure_reason": "",
                "endpoint_shape_seen_before": duplicate_shape,
            }
            records.append(item)
            request_records[id(request)] = item
            persist()
            print(f"[REQUEST] {request.method} {redacted_url}")
            if item["query_parameter_names"]:
                print("[QUERY KEYS]", ", ".join(item["query_parameter_names"]))
            if item["post_field_names"]:
                print("[POST FIELD NAMES]", ", ".join(item["post_field_names"]))
        except Exception as exc:
            print(f"[WARN] Could not record request metadata: {exc}")

    def on_response(response) -> None:
        item = request_records.get(id(response.request))
        if item is None:
            return
        try:
            headers = response.headers
            item["status"] = response.status
            item["content_type"] = headers.get("content-type", "")
            item["response_bytes"] = headers.get("content-length", "")
            if "application/json" in item["content_type"].lower():
                try:
                    body = response.body()
                    item["response_bytes"] = item["response_bytes"] or str(len(body))
                    parsed = json.loads(body.decode("utf-8-sig"))
                    item["response_schema"] = summarize_json_shape(parsed)
                except Exception:
                    item["response_schema"] = {"parse_error": "JSON body unavailable or invalid"}
            persist()
            schema = item.get("response_schema") or {}
            print(
                f"[RESPONSE] {response.status} {item['content_type']} "
                f"bytes={item['response_bytes'] or '?'} "
                f"json_keys={schema.get('top_level_keys', [])}"
            )
        except Exception as exc:
            print(f"[WARN] Could not record response metadata: {exc}")

    def on_request_failed(request) -> None:
        item = request_records.get(id(request))
        if item is None:
            return
        item["failed"] = True
        item["failure_reason"] = "request failed"
        item["status"] = "failed"
        persist()
        print(f"[FAILED] {item['url']}")

    def handle_sigint(_sig, _frame):
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGINT, handle_sigint)

    print("\nMPSC TODAY — QUIZ API DISCOVERY (SCHEMA/METADATA ONLY)")
    print("Target:", quiz_url)
    print("Output:", OUTPUT_DIR)
    print("Manually click Practice/Start, change language, and navigate a few questions.")
    print("Press F8 in the browser or Ctrl+C in this terminal to stop.")
    print("No query values, POST values, cookies, credentials, or response values are saved.\n")

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
            print("Browser ready. Operate the quiz now.")
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
        print("Metadata captured before the error has been saved.")
    finally:
        persist()
        # The browser may already have exited (for example, when its window was
        # closed manually). Cleanup errors must not discard the saved audit.
        for handle in (context, browser):
            if handle is not None:
                try:
                    handle.close()
                except Exception:
                    pass

    counts = Counter((item["method"], item["url"], item["resource_type"]) for item in records)
    print("\nCAPTURE COMPLETE")
    print("Fetch/XHR events:", len(records))
    print("Unique endpoint shapes:", len(counts))
    print("JSON output:", JSON_PATH)
    print("CSV output :", CSV_PATH)
    print("Response values/question text/options/answers were not saved.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
