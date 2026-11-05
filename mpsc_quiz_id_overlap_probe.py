"""Check whether the MPSC quiz endpoint returns a stable or changing 100-ID sample.

Uses three identical, sequential GET calls (3-second delay) for:
  action=questions, limit=100, subject=history

Stores only question IDs and topic/subject metadata for overlap analysis.
It does not store question text, options, correct answers, or explanations.
No browser, POST, answer submission, pagination guessing, or bulk downloading.

Run this before designing an importer. If the three ID sets are identical, inspect
the frontend/admin API contract for an authorized page/offset parameter. If they
differ, the endpoint may rotate/randomize results; do not brute-force repeat it.
"""
from __future__ import annotations

import json
import logging
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.robotparser import RobotFileParser

import requests

API_URL = "https://www.mpsctoday.com/api/quiz/index.php"
ROBOTS_URL = "https://www.mpsctoday.com/robots.txt"
SUBJECT = "history"
ACTION = "questions"
LIMIT = 100
REPEATS = 3
DELAY_SECONDS = 3.0
TIMEOUT_SECONDS = 25
USER_AGENT = "MPSCStudyAuthorizedIntegration/1.0 (add-admin-approved-contact)"

PROJECT_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = PROJECT_DIR / "mpsc_output" / "quiz_api_audit"
JSON_PATH = OUTPUT_DIR / "id_overlap_probe.json"
TXT_PATH = OUTPUT_DIR / "id_overlap_probe.txt"

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")


def check_robots(session: requests.Session) -> RobotFileParser:
    response = session.get(ROBOTS_URL, timeout=TIMEOUT_SECONDS)
    if response.status_code >= 400 and response.status_code != 404:
        raise RuntimeError(f"Cannot verify robots.txt (HTTP {response.status_code}); stopping.")
    parser = RobotFileParser()
    parser.set_url(ROBOTS_URL)
    parser.parse(response.text.splitlines() if response.status_code == 200 else [])
    if not parser.can_fetch(USER_AGENT, API_URL):
        raise RuntimeError("robots.txt disallows the quiz API endpoint.")
    return parser


def safe_record_metadata(item: dict) -> dict:
    """Keep identifiers and taxonomy only; exclude all question/answer content."""
    return {
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
    }


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    })
    check_robots(session)

    requests_log = []
    id_sets: list[set[str]] = []
    all_metadata: dict[str, dict] = {}

    for run_number in range(1, REPEATS + 1):
        params = {"action": ACTION, "limit": LIMIT, "subject": SUBJECT}
        response = session.get(API_URL, params=params, timeout=TIMEOUT_SECONDS)
        if response.status_code in (429, 503):
            raise RuntimeError(
                f"Server asked client to back off (HTTP {response.status_code}); "
                "probe stopped. Do not retry immediately."
            )
        response.raise_for_status()
        if "application/json" not in response.headers.get("Content-Type", "").lower():
            raise RuntimeError("API returned a non-JSON response.")

        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise RuntimeError("Unexpected API response shape; expected an object with data array.")

        metadata = [
            safe_record_metadata(item)
            for item in payload["data"]
            if isinstance(item, dict) and item.get("id") is not None
        ]
        ids = {item["id"] for item in metadata if item["id"]}
        id_sets.append(ids)
        for item in metadata:
            all_metadata[item["id"]] = item

        summary = {
            "run": run_number,
            "http_status": response.status_code,
            "requested_limit": LIMIT,
            "response_count_value": payload.get("count") if isinstance(payload.get("count"), (int, float)) else None,
            "returned_items": len(payload["data"]),
            "unique_id_count": len(ids),
            "response_bytes": len(response.content),
            "ids": sorted(ids),
        }
        requests_log.append(summary)
        logging.info(
            "Run %s/%s: status=%s returned=%s unique_ids=%s bytes=%s",
            run_number, REPEATS, response.status_code, len(payload["data"]),
            len(ids), len(response.content),
        )

        if run_number < REPEATS:
            time.sleep(DELAY_SECONDS)

    union_ids = set.union(*id_sets) if id_sets else set()
    intersections = {}
    for i in range(len(id_sets)):
        for j in range(i + 1, len(id_sets)):
            common = id_sets[i] & id_sets[j]
            intersections[f"run_{i+1}_vs_run_{j+1}"] = {
                "overlap_count": len(common),
                "jaccard_similarity": round(
                    len(common) / len(id_sets[i] | id_sets[j]), 4
                ) if id_sets[i] | id_sets[j] else 1.0,
            }

    topic_counter = Counter(
        (
            item.get("topic_id"),
            item.get("topic_slug"),
            item.get("topic_name_en"),
            item.get("topic_name_mr"),
        )
        for item in all_metadata.values()
    )
    topics = [
        {
            "topic_id": key[0],
            "topic_slug": key[1],
            "topic_name_en": key[2],
            "topic_name_mr": key[3],
            "unique_questions_seen": count,
        }
        for key, count in sorted(topic_counter.items(), key=lambda x: str(x[0]))
    ]

    pairwise_all_same = all(
        id_sets[i] == id_sets[j]
        for i in range(len(id_sets))
        for j in range(i + 1, len(id_sets))
    )
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "api": API_URL,
        "request": {"action": ACTION, "subject": SUBJECT, "limit": LIMIT, "repeats": REPEATS},
        "runs": requests_log,
        "pairwise_overlap": intersections,
        "unique_ids_union_count": len(union_ids),
        "all_three_id_sets_identical": pairwise_all_same,
        "unique_question_ids": sorted(union_ids),
        "topic_metadata_seen": topics,
        "safe_metadata_by_id": all_metadata,
        "interpretation": (
            "Same IDs on all runs: likely stable first batch; pagination contract still needed."
            if pairwise_all_same
            else "IDs changed across identical requests: result ordering/selection may rotate or randomize. "
                 "Confirm with site administrator; do not brute-force repeated calls."
        ),
        "content_saved": "IDs and subject/topic metadata only; no question text/options/answers/explanations.",
    }
    JSON_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "MPSC QUIZ ID OVERLAP PROBE",
        f"Subject: {SUBJECT}; action: {ACTION}; limit: {LIMIT}; repeats: {REPEATS}",
    ]
    for run in requests_log:
        lines.append(
            f"Run {run['run']}: HTTP {run['http_status']}; "
            f"returned={run['returned_items']}; unique_ids={run['unique_id_count']}; "
            f"bytes={run['response_bytes']}"
        )
    lines.append(f"Unique IDs across all runs: {len(union_ids)}")
    lines.append(f"All three ID sets identical: {pairwise_all_same}")
    for label, details in intersections.items():
        lines.append(
            f"{label}: overlap={details['overlap_count']}; "
            f"Jaccard={details['jaccard_similarity']}"
        )
    lines.append(f"Distinct topic metadata entries: {len(topics)}")
    lines.append(report["interpretation"])
    lines.append("Only IDs/taxonomy metadata saved; no question or answer content saved.")
    TXT_PATH.write_text("\n".join(lines), encoding="utf-8")

    print("\nID OVERLAP PROBE COMPLETE")
    for run in requests_log:
        print(
            f"run={run['run']} returned={run['returned_items']} "
            f"unique_ids={run['unique_id_count']} bytes={run['response_bytes']}"
        )
    print(f"Unique IDs union: {len(union_ids)}")
    print(f"All sets identical: {pairwise_all_same}")
    print("Pairwise overlap:", json.dumps(intersections))
    print(f"Distinct topic entries: {len(topics)}")
    print(f"JSON report: {JSON_PATH.resolve()}")
    print(f"Text report: {TXT_PATH.resolve()}")
    print(report["interpretation"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
