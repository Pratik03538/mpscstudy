#!/usr/bin/env python3
"""Build a subject-only MPSC SQLite database with no topic/subtopic fields.

Input: mpsc_output/data_quality_audit/questions_sorted.csv
Outputs:
  mpsc_output/mpsc_subject_only.db
  mpsc_output/subject_only/subject_counts.csv
  mpsc_output/subject_only/subject_alias_audit.csv
  mpsc_output/subject_only/subject_only_summary.json
  mpsc_output/subject_only/by_subject/<subject_slug>.csv

Canonical subject identity is subject_slug, not the numeric source subject_id.
Original numeric IDs and source names are retained per question for traceability.
The source subject assignment is preserved; it is not fact-checked or reclassified.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DEFAULT_INPUT = ROOT / "mpsc_output" / "data_quality_audit" / "questions_sorted.csv"
DEFAULT_DB = ROOT / "mpsc_output" / "mpsc_subject_only.db"
DEFAULT_OUT = ROOT / "mpsc_output" / "subject_only"
REQUIRED = {
    "id", "subject_id", "subject_slug", "subject_name_en", "subject_name_mr",
    "question_mr", "question_en", "option_a", "option_b", "option_c", "option_d",
    "correct_option",
}
CORRECT_MAP = {
    "a": "A", "b": "B", "c": "C", "d": "D",
    "1": "A", "2": "B", "3": "C", "4": "D",
    "optiona": "A", "optionb": "B", "optionc": "C", "optiond": "D",
    "option1": "A", "option2": "B", "option3": "C", "option4": "D",
}


def val(value: Any) -> str:
    return "" if value is None else str(value).strip()


def boolean(value: Any) -> int | None:
    token = val(value).casefold()
    if token in {"1", "true", "yes", "y"}:
        return 1
    if token in {"0", "false", "no", "n"}:
        return 0
    return None


def number(value: Any) -> float | None:
    token = val(value)
    try:
        return float(token) if token else None
    except ValueError:
        return None


def answer_key(row: dict[str, str]) -> str | None:
    token = re.sub(
        r"[^a-z0-9]", "",
        val(row.get("correct_option_normalized") or row.get("correct_option")).casefold(),
    )
    if token in {"10", "20", "30", "40"}:
        token = token[0]
    return CORRECT_MAP.get(token)


def majority(values: list[str]) -> str:
    cleaned = [x for x in values if x]
    if not cleaned:
        return ""
    counts = Counter(cleaned)
    return sorted(counts, key=lambda x: (-counts[x], x.casefold()))[0]


def load_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(
            f"Input not found: {path}. Run python audit_mpsc_dataset.py --similarity 0 first."
        )
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = REQUIRED - set(reader.fieldnames or [])
        if missing:
            raise ValueError("Missing CSV fields: " + ", ".join(sorted(missing)))
        rows = list(reader)
    if not rows:
        raise ValueError("Input CSV contains no questions.")
    return rows


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8-sig")
        return
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def build(rows: list[dict[str, str]], db_path: Path, out_dir: Path) -> dict[str, Any]:
    by_slug: dict[str, list[dict[str, str]]] = defaultdict(list)
    source_variants: dict[tuple[str, str, str, str], int] = defaultdict(int)

    for row in rows:
        slug = val(row.get("subject_slug")).casefold()
        if not slug:
            name = val(row.get("subject_name_en") or row.get("subject_name_mr"))
            if not name:
                raise ValueError(f"Question ID {row.get('id')} has no subject identity.")
            slug = "unclassified-" + re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")
        by_slug[slug].append(row)
        source_variants[(slug, val(row.get("subject_id")),
                         val(row.get("subject_name_en")), val(row.get("subject_name_mr")))] += 1

    subjects = []
    for order, slug in enumerate(sorted(by_slug), start=1):
        group = by_slug[slug]
        subjects.append({
            "subject_id": slug,
            "subject_slug": slug,
            "subject_name_en": majority([val(r.get("subject_name_en")) for r in group]) or slug,
            "subject_name_mr": majority([val(r.get("subject_name_mr")) for r in group]),
            "question_count": len(group),
            "source_subject_ids_json": json.dumps(sorted({val(r.get("subject_id")) for r in group if val(r.get("subject_id"))}), ensure_ascii=False),
            "source_names_en_json": json.dumps(sorted({val(r.get("subject_name_en")) for r in group if val(r.get("subject_name_en"))}), ensure_ascii=False),
            "source_names_mr_json": json.dumps(sorted({val(r.get("subject_name_mr")) for r in group if val(r.get("subject_name_mr"))}), ensure_ascii=False),
            "sort_order": order,
        })

    db_path.parent.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    temp = db_path.with_name(db_path.stem + ".building" + db_path.suffix)
    if temp.exists():
        temp.unlink()

    db = sqlite3.connect(temp)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    try:
        db.executescript("""
            CREATE TABLE subjects (
                subject_id TEXT PRIMARY KEY,
                subject_slug TEXT NOT NULL UNIQUE,
                subject_name_en TEXT NOT NULL,
                subject_name_mr TEXT,
                question_count INTEGER NOT NULL CHECK(question_count >= 0),
                source_subject_ids_json TEXT NOT NULL,
                source_names_en_json TEXT NOT NULL,
                source_names_mr_json TEXT NOT NULL,
                sort_order INTEGER NOT NULL UNIQUE
            );
            CREATE TABLE questions (
                question_id TEXT PRIMARY KEY,
                subject_id TEXT NOT NULL REFERENCES subjects(subject_id),
                source_subject_id TEXT,
                source_subject_slug TEXT,
                source_subject_name_en TEXT,
                source_subject_name_mr TEXT,
                question_mr TEXT,
                question_en TEXT,
                option_a TEXT NOT NULL,
                option_b TEXT NOT NULL,
                option_c TEXT NOT NULL,
                option_d TEXT NOT NULL,
                correct_option_raw TEXT,
                correct_option_key TEXT CHECK(correct_option_key IN ('A','B','C','D') OR correct_option_key IS NULL),
                correct_option_text TEXT,
                explanation_mr TEXT,
                explanation_en TEXT,
                is_pyq TEXT,
                pyq_exam TEXT,
                pyq_year TEXT,
                difficulty TEXT,
                times_served REAL,
                times_attempted REAL,
                times_correct REAL,
                is_published TEXT,
                created_at TEXT,
                updated_at TEXT,
                first_seen_at TEXT,
                last_seen_at TEXT,
                audit_status TEXT,
                audit_errors TEXT,
                audit_warnings TEXT,
                is_structurally_valid INTEGER,
                correct_answer_present INTEGER,
                question_fingerprint TEXT,
                question_duplicate_group TEXT,
                question_duplicate_group_size INTEGER,
                duplicate_question_text INTEGER,
                source_content_hash TEXT,
                sort_order INTEGER NOT NULL UNIQUE
            );
            CREATE INDEX idx_questions_subject_sort ON questions(subject_id, sort_order);
            CREATE INDEX idx_questions_pyq ON questions(is_pyq, pyq_year);
            CREATE INDEX idx_questions_audit_status ON questions(audit_status);
            CREATE VIEW subject_question_counts AS
                SELECT s.subject_id, s.subject_slug, s.subject_name_en, s.subject_name_mr,
                       COUNT(q.question_id) AS question_count
                FROM subjects s LEFT JOIN questions q ON q.subject_id=s.subject_id
                GROUP BY s.subject_id ORDER BY s.sort_order;
            CREATE VIEW question_catalogue AS
                SELECT q.*, s.subject_name_en AS canonical_subject_name_en,
                       s.subject_name_mr AS canonical_subject_name_mr
                FROM questions q JOIN subjects s ON s.subject_id=q.subject_id;
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)

        db.executemany("""
            INSERT INTO subjects (
                subject_id,subject_slug,subject_name_en,subject_name_mr,question_count,
                source_subject_ids_json,source_names_en_json,source_names_mr_json,sort_order
            ) VALUES (
                :subject_id,:subject_slug,:subject_name_en,:subject_name_mr,:question_count,
                :source_subject_ids_json,:source_names_en_json,:source_names_mr_json,:sort_order
            )
        """, subjects)

        insert_sql = """INSERT INTO questions (
            question_id,subject_id,source_subject_id,source_subject_slug,source_subject_name_en,source_subject_name_mr,
            question_mr,question_en,option_a,option_b,option_c,option_d,
            correct_option_raw,correct_option_key,correct_option_text,explanation_mr,explanation_en,
            is_pyq,pyq_exam,pyq_year,difficulty,times_served,times_attempted,times_correct,is_published,
            created_at,updated_at,first_seen_at,last_seen_at,audit_status,audit_errors,audit_warnings,
            is_structurally_valid,correct_answer_present,question_fingerprint,question_duplicate_group,
            question_duplicate_group_size,duplicate_question_text,source_content_hash,sort_order
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""

        subject_labels = {s["subject_id"]: s for s in subjects}
        all_rows = sorted(rows, key=lambda r: (
            val(r.get("subject_slug")).casefold(),
            val(r.get("subject_name_en")).casefold(),
            val(r.get("subject_id")),
            val(r.get("id")),
        ))
        db_rows = []
        export_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)

        for order, row in enumerate(all_rows, start=1):
            source_slug = val(row.get("subject_slug")).casefold()
            if not source_slug:
                nm = val(row.get("subject_name_en") or row.get("subject_name_mr"))
                source_slug = "unclassified-" + re.sub(r"[^a-z0-9]+", "-", nm.casefold()).strip("-")
            sid = source_slug
            key = answer_key(row)
            correct_text = val(row.get(f"option_{key.lower()}")) if key else ""
            qid = val(row.get("id"))
            try:
                duplicate_group_size = int(float(row.get("question_duplicate_group_size") or 1))
            except (TypeError, ValueError):
                duplicate_group_size = 1

            db_rows.append((
                qid, sid, val(row.get("subject_id")), source_slug,
                val(row.get("subject_name_en")), val(row.get("subject_name_mr")),
                val(row.get("question_mr")) or None, val(row.get("question_en")) or None,
                val(row.get("option_a")), val(row.get("option_b")), val(row.get("option_c")), val(row.get("option_d")),
                val(row.get("correct_option")) or None, key, correct_text or None,
                val(row.get("explanation_mr")) or None, val(row.get("explanation_en")) or None,
                val(row.get("is_pyq")) or None, val(row.get("pyq_exam")) or None,
                val(row.get("pyq_year")) or None, val(row.get("difficulty")) or None,
                number(row.get("times_served")), number(row.get("times_attempted")), number(row.get("times_correct")),
                val(row.get("is_published")) or None, val(row.get("created_at")) or None,
                val(row.get("updated_at")) or None, val(row.get("first_seen_at")) or None,
                val(row.get("last_seen_at")) or None, val(row.get("audit_status")) or None,
                val(row.get("audit_errors")) or None, val(row.get("audit_warnings")) or None,
                boolean(row.get("is_structurally_valid")), boolean(row.get("correct_answer_present")),
                val(row.get("question_fingerprint")) or None, val(row.get("question_duplicate_group")) or None,
                duplicate_group_size, boolean(row.get("duplicate_question_text")),
                val(row.get("content_hash")) or None, order,
            ))

            label = subject_labels[sid]
            export_rows[sid].append({
                "question_id": qid, "subject_id": sid,
                "source_subject_id": val(row.get("subject_id")),
                "subject_name_en": label["subject_name_en"], "subject_name_mr": label["subject_name_mr"],
                "source_subject_name_en": val(row.get("subject_name_en")),
                "source_subject_name_mr": val(row.get("subject_name_mr")),
                "question_mr": val(row.get("question_mr")), "question_en": val(row.get("question_en")),
                "option_a": val(row.get("option_a")), "option_b": val(row.get("option_b")),
                "option_c": val(row.get("option_c")), "option_d": val(row.get("option_d")),
                "correct_option_key": key or "", "correct_option_text": correct_text,
                "explanation_mr": val(row.get("explanation_mr")), "explanation_en": val(row.get("explanation_en")),
                "is_pyq": val(row.get("is_pyq")), "pyq_exam": val(row.get("pyq_exam")),
                "pyq_year": val(row.get("pyq_year")), "difficulty": val(row.get("difficulty")),
                "audit_status": val(row.get("audit_status")), "audit_warnings": val(row.get("audit_warnings")),
            })

        db.executemany(insert_sql, db_rows)
        db.executemany("INSERT INTO metadata(key,value) VALUES (?,?)", [
            ("created_at_utc", datetime.now(timezone.utc).isoformat(timespec="seconds")),
            ("source_file", "questions_sorted.csv"),
            ("source_question_count", str(len(rows))),
            ("topic_policy", "Topic and subtopic columns and tables are intentionally absent."),
            ("canonical_subject_identity", "subject_slug"),
            ("source_subject_ids", "Preserved per question; not used as unique identity."),
            ("subject_assignment_policy", "Original source subject_slug retained; per-question subject accuracy not independently verified."),
        ])

        view_names = []
        for subject in subjects:
            slug = subject["subject_slug"]
            view = "questions_" + re.sub(r"[^a-z0-9_]", "_", slug)
            db.execute(
                f"CREATE VIEW \"{view}\" AS "
                f"SELECT * FROM questions WHERE subject_id = '{slug}'"
            )
            view_names.append(view)
        db.commit()

        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
        fk_errors = db.execute("PRAGMA foreign_key_check").fetchall()
        if integrity != "ok" or fk_errors:
            raise RuntimeError(f"SQLite validation failed: integrity={integrity}, FK errors={len(fk_errors)}")
        question_count = db.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
        counts = [dict(r) for r in db.execute("SELECT * FROM subject_question_counts ORDER BY subject_name_en").fetchall()]
        topic_columns = [r[1] for r in db.execute("PRAGMA table_info(questions)").fetchall() if "topic" in r[1].casefold()]
        if question_count != len(rows) or sum(r["question_count"] for r in counts) != len(rows):
            raise RuntimeError("Input, database and subject counts do not reconcile.")
        if topic_columns:
            raise RuntimeError("Unexpected topic columns remain: " + ", ".join(topic_columns))
        view_counts = {
            view: db.execute(f'SELECT COUNT(*) FROM "{view}"').fetchone()[0]
            for view in view_names
        }
        if sum(view_counts.values()) != len(rows):
            raise RuntimeError("Subject-specific views do not cover all questions.")
    except Exception:
        db.rollback()
        db.close()
        if temp.exists():
            temp.unlink()
        raise
    else:
        db.close()

    if db_path.exists():
        backup = db_path.with_name(
            db_path.stem + ".backup_" + datetime.now().strftime("%Y%m%d_%H%M%S") + db_path.suffix
        )
        shutil.copy2(db_path, backup)
    temp.replace(db_path)

    count_export = []
    for s in subjects:
        count_export.append({
            "subject_id": s["subject_id"], "subject_slug": s["subject_slug"],
            "subject_name_en": s["subject_name_en"], "subject_name_mr": s["subject_name_mr"],
            "question_count": s["question_count"],
            "source_subject_ids": ";".join(json.loads(s["source_subject_ids_json"])),
            "source_names_en": " | ".join(json.loads(s["source_names_en_json"])),
            "source_names_mr": " | ".join(json.loads(s["source_names_mr_json"])),
            "question_view": "questions_" + re.sub(r"[^a-z0-9_]", "_", s["subject_slug"]),
        })
    write_csv(out_dir / "subject_counts.csv", count_export)

    aliases = []
    for (slug, source_id, name_en, name_mr), count in sorted(source_variants.items()):
        canonical = subject_labels[slug]
        aliases.append({
            "canonical_subject_id": slug,
            "canonical_subject_name_en": canonical["subject_name_en"],
            "canonical_subject_name_mr": canonical["subject_name_mr"],
            "source_subject_id": source_id, "source_subject_slug": slug,
            "source_subject_name_en": name_en, "source_subject_name_mr": name_mr,
            "question_count": count,
            "alias_action": "CANONICAL" if (
                name_en == canonical["subject_name_en"] and name_mr == canonical["subject_name_mr"]
            ) else "MERGED_BY_SAME_SLUG",
        })
    write_csv(out_dir / "subject_alias_audit.csv", aliases)

    for slug, export in export_rows.items():
        write_csv(out_dir / "by_subject" / f"{slug}.csv", export)

    summary = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_file": "questions_sorted.csv",
        "total_questions": len(rows), "canonical_subject_count": len(subjects),
        "subject_counts": count_export, "integrity_check": integrity,
        "foreign_key_errors": len(fk_errors), "topic_columns_in_questions_table": topic_columns,
        "subject_specific_views": view_counts,
        "important_note": (
            "Subject grouping uses source subject_slug. Source IDs and label aliases are retained. "
            "This removes topic/subtopic structure but does not independently verify every question's subject assignment."
        ),
    }
    (out_dir / "subject_only_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {"counts": count_export, "aliases": aliases, "summary": summary}


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a topic-free, subject-wise MPSC SQLite database.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    rows = load_rows(args.input)
    result = build(rows, args.db, args.out)
    print("\n========== SUBJECT-ONLY DATABASE COMPLETE ==========")
    print(f"Total questions: {len(rows):,}")
    print(f"Subjects: {len(result['counts'])}")
    for subject in result["counts"]:
        print(f"{subject['subject_name_en']} ({subject['subject_id']}): {subject['question_count']:,} MCQs")
    print(f"Database: {args.db.resolve()}")
    print(f"Subject counts: {(args.out / 'subject_counts.csv').resolve()}")
    print(f"Alias report: {(args.out / 'subject_alias_audit.csv').resolve()}")
    print(f"Per-subject CSVs: {(args.out / 'by_subject').resolve()}")
    print(f"SQLite integrity: {result['summary']['integrity_check']}")
    print(f"Foreign-key errors: {result['summary']['foreign_key_errors']}")
    print(f"Topic columns: {result['summary']['topic_columns_in_questions_table']}")
    print("[NOTE] Original subject assignments preserved, not independently fact-checked.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"[ERROR] {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
