"""Run one controlled API GET to verify the confirmed topic slug filter.

Mirrors the frontend contract:
  action=questions, limit=15, subject=history,
  topic=freedom-movement-1857-1947

This script makes one GET request only. It never submits answers and saves only
response status/count, question IDs, and subject/topic metadata. No question
text, options, correct answers, or explanations are stored.
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.robotparser import RobotFileParser

import requests

API_URL = "https://www.mpsctoday.com/api/quiz/index.php"
ROBOTS_URL = "https://www.mpsctoday.com/robots.txt"
SUBJECT = "history"
TOPIC = "freedom-movement-1857-1947"
LIMIT = 15  # Matches the page's observed frontend request.
USER_AGENT = "MPSCStudyAuthorizedIntegration/1.0 (add-admin-approved-contact)"
TIMEOUT_SECONDS = 25

PROJECT_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = PROJECT_DIR / "mpsc_output" / "quiz_api_audit"
JSON_PATH = OUTPUT_DIR / "topic_filter_probe.json"
TXT_PATH = OUTPUT_DIR / "topic_filter_probe.txt"

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    })

    robots_response = session.get(ROBOTS_URL, timeout=TIMEOUT_SECONDS)
    if robots_response.status_code >= 400 and robots_response.status_code != 404:
        raise RuntimeError(
            f"Cannot verify robots.txt (HTTP {robots_response.status_code}); stopping."
        )

    robots = RobotFileParser()
    robots.set_url(ROBOTS_URL)
    robots.parse(
        robots_response.text.splitlines()
        if robots_response.status_code == 200
        else []
    )
    if not robots.can_fetch(USER_AGENT, API_URL):
        raise RuntimeError("robots.txt disallows the quiz API endpoint.")

    params = {
        "action": "questions",
        "limit": LIMIT,
        "subject": SUBJECT,
        "topic": TOPIC,
    }
    logging.info(
        "Sending one GET: action=%s limit=%s subject=%s topic=%s",
        params["action"], params["limit"], params["subject"], params["topic"],
    )

    response = session.get(API_URL, params=params, timeout=TIMEOUT_SECONDS)
    if response.status_code in (429, 503):
        raise RuntimeError(
            f"Server requested backoff (HTTP {response.status_code}); stopping without retry."
        )
    response.raise_for_status()

    content_type = response.headers.get("Content-Type", "").lower()
    if "application/json" not in content_type:
        raise RuntimeError(f"Expected JSON, received Content-Type: {content_type!r}")

    payload = response.json()
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise RuntimeError("Unexpected JSON shape; expected an object containing data array.")

    data = [item for item in payload["data"] if isinstance(item, dict)]
    metadata = []
    for item in data:
        metadata.append({
            "id": str(item.get("id", "")),
            "subject_id": item.get("subject_id"),
            "subject_slug": item.get("subject_slug"),
            "subject_name_en": item.get("subject_name_en"),
            "subject_name_mr": item.get("subject_name_mr"),
            "topic_id": item.get("topic_id"),
            "topic_slug": item.get("topic_slug"),
            "topic_name_en": item.get("topic_name_en"),
            "topic_name_mr": item.get("topic_name_mr"),
            "is_pyq": item.get("is_pyq"),
            "difficulty": item.get("difficulty"),
            "is_published": item.get("is_published"),
        })

    ids = [row["id"] for row in metadata if row["id"]]
    topic_counts = Counter(
        (
            row.get("topic_id"),
            row.get("topic_slug"),
            row.get("topic_name_en"),
            row.get("topic_name_mr"),
        )
        for row in metadata
    )
    subject_counts = Counter(
        (row.get("subject_id"), row.get("subject_slug"), row.get("subject_name_en"))
        for row in metadata
    )
    matching = sum(row.get("topic_slug") == TOPIC for row in metadata)
    mismatching = len(metadata) - matching

    if not metadata:
        verdict = "INCONCLUSIVE: API returned no usable records for this topic filter."
    elif matching == len(metadata):
        verdict = "CONFIRMED: every returned record has the requested topic_slug."
    elif matching == 0:
        verdict = "NOT CONFIRMED: returned records do not carry the requested topic_slug."
    else:
        verdict = "PARTIAL: response contains a mixture of matching and non-matching topic metadata."

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "request": {
            "endpoint": API_URL,
            "method": "GET",
            "params": params,
        },
        "http_status": response.status_code,
        "content_type": content_type,
        "response_bytes": len(response.content),
        "success": payload.get("success") if isinstance(payload.get("success"), bool) else None,
        "count_value": payload.get("count") if isinstance(payload.get("count"), (int, float)) else None,
        "returned_items": len(data),
        "unique_id_count": len(set(ids)),
        "matching_topic_records": matching,
        "nonmatching_topic_records": mismatching,
        "topic_metadata": [
            {
                "topic_id": key[0],
                "topic_slug": key[1],
                "topic_name_en": key[2],
                "topic_name_mr": key[3],
                "records": count,
            }
            for key, count in sorted(topic_counts.items(), key=lambda entry: str(entry[0]))
        ],
        "subject_metadata": [
            {
                "subject_id": key[0],
                "subject_slug": key[1],
                "subject_name_en": key[2],
                "records": count,
            }
            for key, count in sorted(subject_counts.items(), key=lambda entry: str(entry[0]))
        ],
        "safe_metadata_by_id": {row["id"]: row for row in metadata if row["id"]},
        "verdict": verdict,
        "content_saved": "IDs and taxonomy metadata only; no question text/options/answers/explanations.",
    }

    JSON_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    lines = [
        "MPSC TODAY TOPIC FILTER PROBE",
        f"GET action=questions limit={LIMIT} subject={SUBJECT} topic={TOPIC}",
        f"HTTP status: {response.status_code}",
        f"Content-Type: {content_type}",
        f"Response bytes: {len(response.content)}",
        f"Success: {report['success']}",
        f"API count value: {report['count_value']}",
        f"Returned items: {len(data)}",
        f"Unique IDs: {len(set(ids))}",
        f"Matching topic records: {matching}",
        f"Nonmatching topic records: {mismatching}",
        "",
        "TOPIC METADATA:",
    ]
    for row in report["topic_metadata"]:
        lines.append(
            "topic_id={topic_id} | topic_slug={topic_slug} | "
            "English={topic_name_en} | Marathi={topic_name_mr} | records={records}".format(**row)
        )
    lines.extend(["", "SUBJECT METADATA:"])
    for row in report["subject_metadata"]:
        lines.append(str(row))
    lines.extend(["", verdict, report["content_saved"]])
    TXT_PATH.write_text("\n".join(lines), encoding="utf-8")

    print("\nTOPIC FILTER PROBE COMPLETE")
    print(f"HTTP: {response.status_code}")
    print(f"Returned: {len(data)} | unique IDs: {len(set(ids))}")
    print(f"Matching topic: {matching} | nonmatching: {mismatching}")
    for row in report["topic_metadata"]:
        print(
            f"topic_id={row['topic_id']} slug={row['topic_slug']} "
            f"name={row['topic_name_en']} records={row['records']}"
        )
    print(verdict)
    print(f"JSON report: {JSON_PATH.resolve()}")
    print(f"Text report: {TXT_PATH.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
