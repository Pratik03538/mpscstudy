#!/usr/bin/env python3
"""Audit subject/subtopic taxonomy consistency from questions_sorted.csv.

This produces diagnostic reports only. It never silently changes a question's
subject/subtopic assignment. Source numeric IDs are checked together with
slugs and bilingual labels because numeric IDs are not globally unique.
"""
from __future__ import annotations

import argparse
import csv
import json
import random
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_INPUT = Path(__file__).resolve().parent / "mpsc_output" / "data_quality_audit" / "questions_sorted.csv"
DEFAULT_OUT = Path(__file__).resolve().parent / "reports" / "taxonomy"

REQUIRED = {
    "id", "subject_id", "topic_id", "subject_slug", "topic_slug",
    "subject_name_en", "subject_name_mr", "topic_name_en", "topic_name_mr",
    "question_mr", "question_en",
}
GRAMMAR_TOPIC_SLUGS = {
    "idioms---proverbs", "bhasha-lipi-varnvichar-sandhi",
    "parts-of-speech", "ling-vachan-vibhakti-samanya-roop",
    "kaal-prakar-kriyapad-arth", "prayog-samas-prakar", "shabdasiddhi",
    "vakya-prakar-roopantar-sankalan", "kavyashastra-alankar-vrutta-ras",
    "shuddhalekhan-viramchinhe", "shabdsangrah-vocabulary-literature",
    "varna-vichar-sandhi", "shabdanchya-jati", "prayog-types",
    "samas-types", "vakprachar-mhani",
}


def clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(
            f"Input CSV not found: {path}\n"
            "Run the dataset audit first so questions_sorted.csv is generated."
        )
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = REQUIRED - set(reader.fieldnames or [])
        if missing:
            raise ValueError("Input CSV missing required columns: " + ", ".join(sorted(missing)))
        return list(reader)


def group_key(row: dict[str, str]) -> tuple[str, ...]:
    return tuple(clean(row.get(key)) for key in (
        "subject_id", "subject_slug", "subject_name_en", "subject_name_mr",
        "topic_id", "topic_slug", "topic_name_en", "topic_name_mr",
    ))


def question_text(row: dict[str, str]) -> str:
    return clean(row.get("question_mr")) or clean(row.get("question_en"))


def classify_group(
    rows: list[dict[str, str]],
    subject_id_slug_count: dict[str, int],
    subject_topic_slug_count: dict[tuple[str, str], int],
) -> tuple[str, str]:
    first = rows[0]
    sid = clean(first.get("subject_id"))
    tid = clean(first.get("topic_id"))
    subject_slug = clean(first.get("subject_slug")).casefold()
    topic_slug = clean(first.get("topic_slug")).casefold()

    if subject_id_slug_count.get(sid, 0) > 1:
        return (
            "SUBJECT_ID_COLLISION_REVIEW",
            "The numeric subject ID is reused by multiple subject slugs/labels. "
            "Treat (subject ID + subject slug) as identity; review content placement.",
        )
    if subject_topic_slug_count.get((sid, tid), 0) > 1:
        return (
            "SUBTOPIC_ID_COLLISION_REVIEW",
            "The subject/topic numeric ID pair maps to multiple topic slugs/labels. "
            "Treat the topic slug and parent subject identity as part of the key.",
        )
    if topic_slug == "ancient-history" and subject_slug != "history":
        return (
            "SUBJECT_SUBTOPIC_CONFLICT_REVIEW",
            "Ancient History is assigned under a non-History subject. "
            "Review and reclassify individual questions; no automatic reassignment made.",
        )
    if subject_slug == "history" and topic_slug == "ancient-history":
        return (
            "HISTORY_PERIOD_REVIEW",
            "History questions may span ancient, medieval and modern periods. "
            "Review individual questions before retaining this subtopic.",
        )
    if subject_slug == "marathi-grammar" and topic_slug in GRAMMAR_TOPIC_SLUGS:
        return (
            "GRAMMAR_TOPIC_CONTENT_REVIEW",
            "The label is a Marathi grammar topic. Verify each question against the "
            "topic definition and check whether an alias/duplicate category exists.",
        )
    return (
        "CONTENT_REVIEW_REQUIRED",
        "No deterministic classification was inferred from metadata alone. "
        "Review sample questions and classify by content.",
    )


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8-sig")
        return
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create subject/subtopic taxonomy diagnostic reports without modifying questions."
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--seed", type=int, default=713133257)
    parser.add_argument("--samples-per-group", type=int, default=5)
    args = parser.parse_args()
    if args.samples_per_group < 1:
        parser.error("--samples-per-group must be at least 1")

    rows = read_rows(args.input)
    rng = random.Random(args.seed)
    grouped: dict[tuple[str, ...], list[dict[str, str]]] = defaultdict(list)
    by_subject_id: dict[str, set[tuple[str, str]]] = defaultdict(set)
    by_subject_topic_id: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)

    for row in rows:
        grouped[group_key(row)].append(row)
        sid = clean(row.get("subject_id"))
        tid = clean(row.get("topic_id"))
        by_subject_id[sid].add((
            clean(row.get("subject_slug")),
            clean(row.get("subject_name_en")) or clean(row.get("subject_name_mr")),
        ))
        by_subject_topic_id[(sid, tid)].add((
            clean(row.get("topic_slug")),
            clean(row.get("topic_name_en")) or clean(row.get("topic_name_mr")),
        ))

    subject_id_slug_count = {key: len(value) for key, value in by_subject_id.items()}
    subject_topic_slug_count = {key: len(value) for key, value in by_subject_topic_id.items()}

    group_report: list[dict[str, Any]] = []
    for key, group in sorted(grouped.items(), key=lambda item: item[0]):
        sid, sslug, sen, smr, tid, tslug, ten, tmr = key
        flag, note = classify_group(group, subject_id_slug_count, subject_topic_slug_count)
        sample = rng.sample(group, min(args.samples_per_group, len(group)))
        sample_ids = " | ".join(clean(row.get("id")) for row in sample)
        sample_questions = " || ".join(
            f"ID {clean(row.get('id'))}: {question_text(row)}" for row in sample
        )
        group_report.append({
            "subject_id": sid,
            "subject_slug": sslug,
            "subject_name_en": sen,
            "subject_name_mr": smr,
            "topic_id": tid,
            "topic_slug": tslug,
            "topic_name_en": ten,
            "topic_name_mr": tmr,
            "question_count": len(group),
            "share_of_dataset_pct": round(len(group) * 100 / max(len(rows), 1), 2),
            "mapping_audit_flag": flag,
            "audit_note": note,
            "sample_question_ids": sample_ids,
            "sample_questions": sample_questions,
        })

    conflict_report: list[dict[str, Any]] = []
    for sid, identities in sorted(by_subject_id.items()):
        if len(identities) > 1:
            matching_rows = [row for row in rows if clean(row.get("subject_id")) == sid]
            conflict_report.append({
                "entity_type": "SUBJECT_ID_COLLISION",
                "subject_id": sid,
                "topic_id": "",
                "row_count": len(matching_rows),
                "conflicting_slugs": " | ".join(sorted({item[0] for item in identities})),
                "conflicting_names": " | ".join(sorted({item[1] for item in identities})),
                "details": "Same numeric subject_id maps to multiple subject slugs/names; "
                           "source identity must include the subject slug.",
            })
    for (sid, tid), identities in sorted(by_subject_topic_id.items()):
        if len(identities) > 1:
            matching_rows = [
                row for row in rows
                if clean(row.get("subject_id")) == sid and clean(row.get("topic_id")) == tid
            ]
            conflict_report.append({
                "entity_type": "TOPIC_ID_COLLISION",
                "subject_id": sid,
                "topic_id": tid,
                "row_count": len(matching_rows),
                "conflicting_slugs": " | ".join(sorted({item[0] for item in identities})),
                "conflicting_names": " | ".join(sorted({item[1] for item in identities})),
                "details": "Same numeric subject_id/topic_id pair maps to multiple subtopic "
                           "slugs/names; identity must include the parent subject and topic slug.",
            })

    args.out.mkdir(parents=True, exist_ok=True)
    group_path = args.out / "taxonomy_group_audit.csv"
    conflict_path = args.out / "taxonomy_id_conflicts.csv"
    summary_path = args.out / "taxonomy_audit_summary.json"
    write_csv(group_path, group_report)
    write_csv(conflict_path, conflict_report)

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "input_file": args.input.name,
        "total_questions": len(rows),
        "subject_subtopic_combinations": len(group_report),
        "subject_id_collision_count": sum(item["entity_type"] == "SUBJECT_ID_COLLISION" for item in conflict_report),
        "subject_topic_id_collision_count": sum(item["entity_type"] == "TOPIC_ID_COLLISION" for item in conflict_report),
        "random_seed": args.seed,
        "samples_per_combination": args.samples_per_group,
        "important_note": (
            "These are diagnostic reports, not corrected mappings. No question subject/subtopic "
            "is changed automatically. All sample content and flagged combinations require review "
            "before creating a corrected database."
        ),
        "outputs": {
            "group_audit": group_path.name,
            "id_conflicts": conflict_path.name,
        },
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[TAXONOMY AUDIT] questions={len(rows):,} | subject-topic groups={len(group_report):,}")
    print(f"[TAXONOMY AUDIT] ID conflicts={len(conflict_report):,}")
    print(f"[OUTPUT] {group_path}")
    print(f"[OUTPUT] {conflict_path}")
    print(f"[OUTPUT] {summary_path}")
    print("[NOTE] Diagnostics only; no question mappings were changed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
