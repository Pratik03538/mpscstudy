#!/usr/bin/env python3
"""Sample one random question from every subtopic in the normalized MPSC DB.

The database is opened read-only. The script exports question metadata,
bilingual subject/subtopic labels, all four options, answer-key data,
explanations (when present), and basic structural consistency checks.
It does NOT fact-check the subject/subtopic assignment or the factual answer.
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import secrets
import sqlite3
from pathlib import Path
from typing import Any

DEFAULT_DB = Path(__file__).resolve().parent / "mpsc_output" / "mpsc_questions_structured.db"
DEFAULT_OUT = Path(__file__).resolve().parent / "mpsc_output" / "data_quality_audit"


def open_readonly(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise FileNotFoundError(
            f"Normalized DB not found: {path}. Run python audit_mpsc_dataset.py first."
        )
    con = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    return con


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8-sig")
        return
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export one random question for every subtopic from the normalized MPSC database."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="Normalized SQLite database.")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="Output directory.")
    parser.add_argument("--seed", type=int, default=None, help="Optional random seed to reproduce a sample.")
    args = parser.parse_args()

    seed = args.seed if args.seed is not None else secrets.randbits(32)
    rng = random.Random(seed)
    args.out.mkdir(parents=True, exist_ok=True)

    con = open_readonly(args.db)
    try:
        required = {"subjects", "subtopics", "questions", "options", "answers", "explanations", "question_audit"}
        present = {
            row["name"] for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        missing = required - present
        if missing:
            raise RuntimeError("Normalized DB is missing required tables: " + ", ".join(sorted(missing)))

        topic_rows = [
            dict(row) for row in con.execute("""
                SELECT
                    s.subject_key,
                    s.subject_id,
                    s.subject_slug,
                    s.subject_name_en,
                    s.subject_name_mr,
                    st.subtopic_key,
                    st.subtopic_id,
                    st.subtopic_slug,
                    st.subtopic_name_en,
                    st.subtopic_name_mr,
                    COUNT(q.question_id) AS subtopic_question_count
                FROM subtopics st
                JOIN subjects s ON s.subject_key = st.subject_key
                LEFT JOIN questions q ON q.subtopic_key = st.subtopic_key
                GROUP BY st.subtopic_key
                ORDER BY
                    COALESCE(s.subject_name_en, ''),
                    COALESCE(s.subject_name_mr, ''),
                    COALESCE(st.subtopic_name_en, ''),
                    COALESCE(st.subtopic_name_mr, ''),
                    COALESCE(st.subtopic_id, '')
            """).fetchall()
        ]

        question_rows = [
            dict(row) for row in con.execute("""
                SELECT
                    q.question_id,
                    q.subtopic_key,
                    q.question_mr,
                    q.question_en,
                    q.is_pyq,
                    q.pyq_exam,
                    q.pyq_year,
                    q.difficulty,
                    q.sort_order,
                    MAX(CASE WHEN o.option_key = 'A' THEN o.option_text END) AS option_a,
                    MAX(CASE WHEN o.option_key = 'B' THEN o.option_text END) AS option_b,
                    MAX(CASE WHEN o.option_key = 'C' THEN o.option_text END) AS option_c,
                    MAX(CASE WHEN o.option_key = 'D' THEN o.option_text END) AS option_d,
                    COUNT(DISTINCT o.option_key) AS option_count,
                    a.correct_option_raw,
                    a.correct_option_key,
                    a.correct_option_text,
                    e.explanation_mr,
                    e.explanation_en,
                    qa.audit_status,
                    qa.audit_errors,
                    qa.audit_warnings
                FROM questions q
                LEFT JOIN options o ON o.question_id = q.question_id
                LEFT JOIN answers a ON a.question_id = q.question_id
                LEFT JOIN explanations e ON e.question_id = q.question_id
                LEFT JOIN question_audit qa ON qa.question_id = q.question_id
                GROUP BY q.question_id
                ORDER BY q.subtopic_key, q.question_id
            """).fetchall()
        ]
    finally:
        con.close()

    by_topic: dict[str, list[dict[str, Any]]] = {}
    for row in question_rows:
        by_topic.setdefault(str(row["subtopic_key"]), []).append(row)

    sample_rows: list[dict[str, Any]] = []
    coverage_rows: list[dict[str, Any]] = []
    for topic in topic_rows:
        candidates = by_topic.get(str(topic["subtopic_key"]), [])
        selected = rng.choice(candidates) if candidates else None
        base = {
            "subject_id": topic["subject_id"],
            "subject_slug": topic["subject_slug"],
            "subject_name_en": topic["subject_name_en"],
            "subject_name_mr": topic["subject_name_mr"],
            "subtopic_id": topic["subtopic_id"],
            "subtopic_slug": topic["subtopic_slug"],
            "subtopic_name_en": topic["subtopic_name_en"],
            "subtopic_name_mr": topic["subtopic_name_mr"],
            "subtopic_question_count": topic["subtopic_question_count"],
        }

        if selected is None:
            coverage_rows.append({
                **base,
                "sample_status": "NO_QUESTIONS_IN_SUBTOPIC",
                "sampled_question_id": None,
                "sample_audit_status": None,
            })
            continue

        key = str(selected.get("correct_option_key") or "").upper()
        key_text = selected.get("option_" + key.lower()) if key in {"A", "B", "C", "D"} else None
        question_present = bool(
            (selected.get("question_mr") or "").strip()
            or (selected.get("question_en") or "").strip()
        )
        answer_text_matches = (
            str(key_text).strip() == str(selected.get("correct_option_text") or "").strip()
            if key_text is not None and selected.get("correct_option_text") is not None
            else False
        )
        sample = {
            **base,
            "question_id": selected["question_id"],
            "question_mr": selected["question_mr"],
            "question_en": selected["question_en"],
            "option_a": selected["option_a"],
            "option_b": selected["option_b"],
            "option_c": selected["option_c"],
            "option_d": selected["option_d"],
            "option_count": selected["option_count"],
            "correct_option_raw": selected["correct_option_raw"],
            "correct_option_key": selected["correct_option_key"],
            "correct_option_text": selected["correct_option_text"],
            "explanation_mr": selected["explanation_mr"],
            "explanation_en": selected["explanation_en"],
            "audit_status": selected["audit_status"],
            "audit_errors": selected["audit_errors"],
            "audit_warnings": selected["audit_warnings"],
            "is_pyq": selected["is_pyq"],
            "pyq_exam": selected["pyq_exam"],
            "pyq_year": selected["pyq_year"],
            "difficulty": selected["difficulty"],
            "check_question_text_present": question_present,
            "check_four_options_present": int(selected["option_count"] or 0) == 4,
            "check_answer_text_matches_keyed_option": answer_text_matches,
        }
        sample_rows.append(sample)
        coverage_rows.append({
            **base,
            "sample_status": "SAMPLED",
            "sampled_question_id": selected["question_id"],
            "sample_audit_status": selected["audit_status"],
            "sample_question_text": selected["question_mr"] or selected["question_en"],
            "sample_audit_warnings": selected["audit_warnings"],
        })

    prefix = "subtopic_random_sample"
    csv_path = args.out / f"{prefix}.csv"
    json_path = args.out / f"{prefix}.json"
    coverage_path = args.out / "subtopic_coverage_check.csv"
    write_csv(csv_path, sample_rows)
    write_csv(coverage_path, coverage_rows)

    payload = {
        "database": args.db.name,
        "random_seed": seed,
        "subtopics_total": len(topic_rows),
        "subtopics_sampled": len(sample_rows),
        "subtopics_without_questions": len(topic_rows) - len(sample_rows),
        "important_note": (
            "This is a random coverage sample, not a factual validation. "
            "Subject/subtopic correctness and answer correctness must be reviewed by a person "
            "against the question context and trusted references."
        ),
        "samples": sample_rows,
        "coverage": coverage_rows,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n========== SUBTOPIC RANDOM SAMPLE ==========")
    print(f"Database: {args.db.resolve()}")
    print(f"Random seed: {seed}")
    print(f"Subtopics in database: {len(topic_rows)}")
    print(f"Subtopics sampled: {len(sample_rows)}")
    print(f"Subtopics with no questions: {len(topic_rows) - len(sample_rows)}")
    print("\nSubject | Subtopic | Question ID | Audit")
    for row in sample_rows:
        subject = row["subject_name_en"] or row["subject_name_mr"] or row["subject_slug"] or row["subject_id"]
        topic = row["subtopic_name_en"] or row["subtopic_name_mr"] or row["subtopic_slug"] or row["subtopic_id"]
        print(f"{subject} | {topic} | {row['question_id']} | {row['audit_status']}")
    for row in coverage_rows:
        if row["sample_status"] != "SAMPLED":
            print(f"[EMPTY] {row['subject_name_en']} | {row['subtopic_name_en']} (ID={row['subtopic_id']})")
    print("\n[OUTPUT]")
    print(csv_path.resolve())
    print(json_path.resolve())
    print(coverage_path.resolve())
    print("\n[NOTE] Structural checks do not establish factual correctness or correct topic placement.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
