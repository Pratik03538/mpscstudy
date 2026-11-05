"""Build a normalized, hierarchically sorted MPSC question database.

This module consumes the audited/cleaned pandas DataFrame produced by
audit_mpsc_dataset.py. It never opens or writes to the original collection DB.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


def _missing(value: Any) -> bool:
    if value is None:
        return True
    try:
        if pd.isna(value):
            return True
    except (TypeError, ValueError):
        pass
    return str(value).strip().casefold() in {"", "nan", "none", "null", "undefined", "n/a", "na", "-", "--"}


def _text(value: Any) -> str:
    if _missing(value):
        return ""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value))).strip()


def _sqlite_value(value: Any, force_text: bool = False) -> Any:
    if _missing(value):
        return None
    if hasattr(value, "item"):
        try:
            value = value.item()
        except (ValueError, AttributeError):
            pass
    if force_text:
        return str(value)
    if isinstance(value, (str, int, float, bytes)):
        return value
    if isinstance(value, bool):
        return int(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _json_value(value: Any) -> Any:
    if _missing(value):
        return None
    if hasattr(value, "item"):
        try:
            value = value.item()
        except (ValueError, AttributeError):
            pass
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _subject_key(row: dict[str, Any]) -> str:
    sid = _text(row.get("subject_id"))
    slug = _text(row.get("subject_slug")).casefold()
    name = _text(row.get("subject_name_en") or row.get("subject_name_mr")).casefold()
    if sid:
        return "subject-id:" + sid
    if slug:
        return "subject-slug:" + slug
    if name:
        return "subject-name:" + name
    return "subject:unclassified"


def _subtopic_key(row: dict[str, Any], subject_key: str) -> str:
    tid = _text(row.get("topic_id"))
    slug = _text(row.get("topic_slug")).casefold()
    name = _text(row.get("topic_name_en") or row.get("topic_name_mr")).casefold()
    if tid:
        return subject_key + "|topic-id:" + tid
    if slug:
        return subject_key + "|topic-slug:" + slug
    if name:
        return subject_key + "|topic-name:" + name
    return subject_key + "|topic:unclassified"


def _sort_frame(frame: pd.DataFrame) -> pd.DataFrame:
    ordered = frame.copy()
    preferred = [
        "subject_name_en", "subject_name_mr", "subject_slug", "subject_id",
        "topic_name_en", "topic_name_mr", "topic_slug", "topic_id", "id",
    ]
    columns = [column for column in preferred if column in ordered.columns]
    helpers = []
    for i, column in enumerate(columns):
        helper = f"__sort_{i}"
        ordered[helper] = ordered[column].map(lambda value: _text(value).casefold())
        helpers.append(helper)
    if helpers:
        ordered = ordered.sort_values(helpers, kind="mergesort").drop(columns=helpers)
    return ordered.reset_index(drop=True)


def build_structured_outputs(
    cleaned: pd.DataFrame,
    out_dir: Path,
    database_path: Path,
    audit_columns: list[str],
) -> dict[str, Any]:
    """Build a normalized SQLite DB and sorted CSV exports in one call."""
    out_dir.mkdir(parents=True, exist_ok=True)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = database_path.with_name(database_path.stem + ".building" + database_path.suffix)
    if temp_path.exists():
        temp_path.unlink()

    ordered = _sort_frame(cleaned)
    export_path = out_dir / "questions_sorted.csv"
    ordered.to_csv(export_path, index=False, encoding="utf-8-sig")

    subjects: dict[str, dict[str, Any]] = {}
    subtopics: dict[str, dict[str, Any]] = {}
    row_keys: list[tuple[str, str]] = []

    def merge_existing(existing: dict[str, Any], incoming: dict[str, Any]) -> None:
        for key, value in incoming.items():
            if existing.get(key) in (None, "") and value not in (None, ""):
                existing[key] = value

    row_records = ordered.to_dict(orient="records")
    for row in row_records:
        skey = _subject_key(row)
        tkey = _subtopic_key(row, skey)
        sdata = {
            "subject_key": skey,
            "subject_id": _sqlite_value(row.get("subject_id"), True),
            "subject_slug": _sqlite_value(row.get("subject_slug"), True),
            "subject_name_en": _sqlite_value(row.get("subject_name_en"), True),
            "subject_name_mr": _sqlite_value(row.get("subject_name_mr"), True),
        }
        tdata = {
            "subtopic_key": tkey,
            "subject_key": skey,
            "subtopic_id": _sqlite_value(row.get("topic_id"), True),
            "subtopic_slug": _sqlite_value(row.get("topic_slug"), True),
            "subtopic_name_en": _sqlite_value(row.get("topic_name_en"), True),
            "subtopic_name_mr": _sqlite_value(row.get("topic_name_mr"), True),
        }
        subjects.setdefault(skey, sdata)
        subtopics.setdefault(tkey, tdata)
        merge_existing(subjects[skey], sdata)
        merge_existing(subtopics[tkey], tdata)
        row_keys.append((skey, tkey))

    db = sqlite3.connect(temp_path)
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA journal_mode=DELETE")
    try:
        db.executescript("""
            CREATE TABLE subjects (
                subject_key TEXT PRIMARY KEY, subject_id TEXT, subject_slug TEXT,
                subject_name_en TEXT, subject_name_mr TEXT
            );
            CREATE TABLE subtopics (
                subtopic_key TEXT PRIMARY KEY,
                subject_key TEXT NOT NULL REFERENCES subjects(subject_key),
                subtopic_id TEXT, subtopic_slug TEXT,
                subtopic_name_en TEXT, subtopic_name_mr TEXT
            );
            CREATE TABLE questions (
                question_id TEXT PRIMARY KEY,
                source_content_hash TEXT,
                subject_key TEXT NOT NULL REFERENCES subjects(subject_key),
                subtopic_key TEXT NOT NULL REFERENCES subtopics(subtopic_key),
                question_mr TEXT, question_en TEXT,
                is_pyq TEXT, pyq_exam TEXT, pyq_year TEXT, difficulty TEXT,
                times_served REAL, times_attempted REAL, times_correct REAL,
                last_pushed_to_tg TEXT, is_published TEXT,
                created_at TEXT, updated_at TEXT, first_seen_at TEXT, last_seen_at TEXT,
                raw_json TEXT, source_row_json TEXT NOT NULL,
                sort_order INTEGER NOT NULL UNIQUE
            );
            CREATE TABLE options (
                question_id TEXT NOT NULL REFERENCES questions(question_id) ON DELETE CASCADE,
                option_key TEXT NOT NULL, option_text TEXT NOT NULL,
                option_order INTEGER NOT NULL, is_marked_correct INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (question_id, option_key)
            );
            CREATE TABLE answers (
                question_id TEXT PRIMARY KEY REFERENCES questions(question_id) ON DELETE CASCADE,
                correct_option_raw TEXT, correct_option_key TEXT, correct_option_text TEXT
            );
            CREATE TABLE explanations (
                question_id TEXT PRIMARY KEY REFERENCES questions(question_id) ON DELETE CASCADE,
                explanation_mr TEXT, explanation_en TEXT
            );
            CREATE TABLE question_audit (
                question_id TEXT PRIMARY KEY REFERENCES questions(question_id) ON DELETE CASCADE,
                audit_status TEXT, audit_errors TEXT, audit_warnings TEXT,
                question_fingerprint TEXT, question_duplicate_group TEXT,
                duplicate_question_text INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE INDEX idx_questions_subject_subtopic ON questions(subject_key, subtopic_key, sort_order);
            CREATE INDEX idx_subtopics_subject ON subtopics(subject_key, subtopic_name_en, subtopic_name_mr);
            CREATE INDEX idx_question_audit_fingerprint ON question_audit(question_fingerprint);
            CREATE VIEW question_catalogue AS
                SELECT q.question_id,
                       s.subject_id, s.subject_slug, s.subject_name_en, s.subject_name_mr,
                       st.subtopic_id, st.subtopic_slug, st.subtopic_name_en, st.subtopic_name_mr,
                       q.question_mr, q.question_en,
                       a.correct_option_raw, a.correct_option_key, a.correct_option_text,
                       e.explanation_mr, e.explanation_en,
                       q.is_pyq, q.pyq_exam, q.pyq_year, q.difficulty,
                       qa.audit_status, qa.audit_errors, qa.audit_warnings,
                       qa.question_fingerprint, qa.question_duplicate_group,
                       qa.duplicate_question_text, q.sort_order
                FROM questions q
                JOIN subjects s ON s.subject_key=q.subject_key
                JOIN subtopics st ON st.subtopic_key=q.subtopic_key
                LEFT JOIN answers a ON a.question_id=q.question_id
                LEFT JOIN explanations e ON e.question_id=q.question_id
                LEFT JOIN question_audit qa ON qa.question_id=q.question_id;
        """)

        db.executemany(
            "INSERT INTO subjects VALUES (:subject_key,:subject_id,:subject_slug,:subject_name_en,:subject_name_mr)",
            list(subjects.values()),
        )
        db.executemany(
            "INSERT INTO subtopics VALUES (:subtopic_key,:subject_key,:subtopic_id,:subtopic_slug,:subtopic_name_en,:subtopic_name_mr)",
            list(subtopics.values()),
        )

        q_sql = """INSERT INTO questions (
            question_id,source_content_hash,subject_key,subtopic_key,question_mr,question_en,
            is_pyq,pyq_exam,pyq_year,difficulty,times_served,times_attempted,times_correct,
            last_pushed_to_tg,is_published,created_at,updated_at,first_seen_at,last_seen_at,
            raw_json,source_row_json,sort_order
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""
        option_rows, answer_rows, explanation_rows, audit_rows = [], [], [], []
        option_letters = "ABCD"
        for order, (row, (skey, tkey)) in enumerate(zip(row_records, row_keys), start=1):
            qid = _text(row.get("id"))
            original = {
                str(key): _json_value(value) for key, value in row.items()
                if key not in audit_columns
            }
            normalized_key = _text(row.get("correct_option_normalized")).upper()
            if normalized_key not in option_letters:
                token = re.sub(r"[^a-z0-9]", "", _text(row.get("correct_option")).casefold())
                normalized_key = {
                    "a": "A", "b": "B", "c": "C", "d": "D",
                    "1": "A", "2": "B", "3": "C", "4": "D",
                    "optiona": "A", "optionb": "B", "optionc": "C", "optiond": "D",
                    "option1": "A", "option2": "B", "option3": "C", "option4": "D",
                }.get(token, "")
            option_text_by_key = {}
            for option_order, letter in enumerate(option_letters, start=1):
                option_text = _text(row.get("option_" + letter.lower()))
                if option_text:
                    option_text_by_key[letter] = option_text
                    option_rows.append((
                        qid, letter, option_text, option_order,
                        int(letter == normalized_key),
                    ))
            answer_rows.append((
                qid,
                _sqlite_value(row.get("correct_option"), True),
                normalized_key or None,
                option_text_by_key.get(normalized_key),
            ))
            expl_mr = _sqlite_value(row.get("explanation_mr"), True)
            expl_en = _sqlite_value(row.get("explanation_en"), True)
            if expl_mr is not None or expl_en is not None:
                explanation_rows.append((qid, expl_mr, expl_en))

            db.execute(q_sql, (
                qid, _sqlite_value(row.get("content_hash"), True), skey, tkey,
                _sqlite_value(row.get("question_mr"), True),
                _sqlite_value(row.get("question_en"), True),
                _sqlite_value(row.get("is_pyq"), True),
                _sqlite_value(row.get("pyq_exam"), True),
                _sqlite_value(row.get("pyq_year"), True),
                _sqlite_value(row.get("difficulty"), True),
                _sqlite_value(row.get("times_served")),
                _sqlite_value(row.get("times_attempted")),
                _sqlite_value(row.get("times_correct")),
                _sqlite_value(row.get("last_pushed_to_tg"), True),
                _sqlite_value(row.get("is_published"), True),
                _sqlite_value(row.get("created_at"), True),
                _sqlite_value(row.get("updated_at"), True),
                _sqlite_value(row.get("first_seen_at"), True),
                _sqlite_value(row.get("last_seen_at"), True),
                _sqlite_value(row.get("raw_json"), True),
                json.dumps(original, ensure_ascii=False, separators=(",", ":")),
                order,
            ))
            audit_rows.append((
                qid, _text(row.get("audit_status")) or "NEEDS_REVIEW",
                _text(row.get("audit_errors")), _text(row.get("audit_warnings")),
                _text(row.get("question_fingerprint")),
                _text(row.get("question_duplicate_group")),
                int(bool(row.get("duplicate_question_text", False))),
            ))

        db.executemany(
            "INSERT INTO options VALUES (?,?,?,?,?)", option_rows
        )
        db.executemany(
            "INSERT INTO answers VALUES (?,?,?,?)", answer_rows
        )
        db.executemany(
            "INSERT INTO explanations VALUES (?,?,?)", explanation_rows
        )
        db.executemany(
            "INSERT INTO question_audit VALUES (?,?,?,?,?,?,?)", audit_rows
        )
        metadata = {
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "source_row_count": str(len(ordered)),
            "schema_version": "1",
            "source_database_policy": "read-only; this is a separate processed database",
            "answer_accuracy_note": "correct_option_text is the option selected by the source answer key; factual correctness is not independently verified",
        }
        db.executemany("INSERT INTO metadata VALUES (?,?)", list(metadata.items()))
        db.commit()

        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
        fk_errors = db.execute("PRAGMA foreign_key_check").fetchall()
        if integrity != "ok" or fk_errors:
            raise RuntimeError(
                f"Structured database validation failed: integrity={integrity}; FK errors={len(fk_errors)}"
            )
        stats = {
            "questions": db.execute("SELECT COUNT(*) FROM questions").fetchone()[0],
            "subjects": db.execute("SELECT COUNT(*) FROM subjects").fetchone()[0],
            "subtopics": db.execute("SELECT COUNT(*) FROM subtopics").fetchone()[0],
            "options": db.execute("SELECT COUNT(*) FROM options").fetchone()[0],
            "answers": db.execute("SELECT COUNT(*) FROM answers").fetchone()[0],
            "explanations": db.execute("SELECT COUNT(*) FROM explanations").fetchone()[0],
            "integrity_check": integrity,
            "foreign_key_errors": len(fk_errors),
        }
    except Exception:
        db.rollback()
        db.close()
        if temp_path.exists():
            temp_path.unlink()
        raise
    else:
        db.close()

    if database_path.exists():
        backup_path = database_path.with_name(
            database_path.stem + ".backup_" + datetime.now().strftime("%Y%m%d_%H%M%S") + database_path.suffix
        )
        os.replace(database_path, backup_path)
    os.replace(temp_path, database_path)

    connection = sqlite3.connect(database_path)
    try:
        subjects_export = pd.read_sql_query("""
            SELECT s.subject_id, s.subject_slug, s.subject_name_en, s.subject_name_mr,
                   COUNT(DISTINCT st.subtopic_key) AS subtopic_count,
                   COUNT(DISTINCT q.question_id) AS question_count
            FROM subjects s
            LEFT JOIN subtopics st ON st.subject_key=s.subject_key
            LEFT JOIN questions q ON q.subject_key=s.subject_key
            GROUP BY s.subject_key
            ORDER BY COALESCE(s.subject_name_en,''), COALESCE(s.subject_name_mr,''),
                     COALESCE(s.subject_slug,''), COALESCE(s.subject_id,'')
        """, connection)
        subtopics_export = pd.read_sql_query("""
            SELECT s.subject_id, s.subject_name_en, s.subject_name_mr,
                   st.subtopic_id, st.subtopic_slug, st.subtopic_name_en, st.subtopic_name_mr,
                   COUNT(q.question_id) AS question_count
            FROM subtopics st
            JOIN subjects s ON s.subject_key=st.subject_key
            LEFT JOIN questions q ON q.subtopic_key=st.subtopic_key
            GROUP BY st.subtopic_key
            ORDER BY COALESCE(s.subject_name_en,''), COALESCE(s.subject_name_mr,''),
                     COALESCE(st.subtopic_name_en,''), COALESCE(st.subtopic_name_mr,''),
                     COALESCE(st.subtopic_slug,''), COALESCE(st.subtopic_id,'')
        """, connection)
        options_export = pd.read_sql_query("""
            SELECT s.subject_id, s.subject_name_en, s.subject_name_mr,
                   st.subtopic_id, st.subtopic_name_en, st.subtopic_name_mr,
                   q.question_id, o.option_key, o.option_text, o.option_order, o.is_marked_correct
            FROM options o JOIN questions q ON q.question_id=o.question_id
            JOIN subjects s ON s.subject_key=q.subject_key
            JOIN subtopics st ON st.subtopic_key=q.subtopic_key
            ORDER BY q.sort_order, o.option_order
        """, connection)
        answers_export = pd.read_sql_query("""
            SELECT s.subject_id, s.subject_name_en, s.subject_name_mr,
                   st.subtopic_id, st.subtopic_name_en, st.subtopic_name_mr,
                   q.question_id, q.question_mr, q.question_en,
                   a.correct_option_raw, a.correct_option_key, a.correct_option_text,
                   e.explanation_mr, e.explanation_en
            FROM questions q JOIN subjects s ON s.subject_key=q.subject_key
            JOIN subtopics st ON st.subtopic_key=q.subtopic_key
            LEFT JOIN answers a ON a.question_id=q.question_id
            LEFT JOIN explanations e ON e.question_id=q.question_id
            ORDER BY q.sort_order
        """, connection)
    finally:
        connection.close()

    exports = {
        "questions_sorted": export_path,
        "subject_index": out_dir / "subject_index.csv",
        "subtopic_index": out_dir / "subtopic_index.csv",
        "options_sorted": out_dir / "options_sorted.csv",
        "answers_explanations_sorted": out_dir / "answers_explanations_sorted.csv",
    }
    subjects_export.to_csv(exports["subject_index"], index=False, encoding="utf-8-sig")
    subtopics_export.to_csv(exports["subtopic_index"], index=False, encoding="utf-8-sig")
    options_export.to_csv(exports["options_sorted"], index=False, encoding="utf-8-sig")
    answers_export.to_csv(exports["answers_explanations_sorted"], index=False, encoding="utf-8-sig")

    stats["database_path"] = str(database_path.resolve())
    stats["exports"] = {key: str(value.resolve()) for key, value in exports.items()}
    return stats
