from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


@contextmanager
def question_connection(path: Path):
    if not path.exists():
        raise FileNotFoundError(f"Question DB not found: {path}")
    con = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    try:
        yield con
    finally:
        con.close()


@contextmanager
def state_connection(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA journal_mode=WAL")
    try:
        with con:
            yield con
    finally:
        con.close()


def initialize_state_db(path: Path) -> None:
    with state_connection(path) as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS quiz_sessions(
                session_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL, subject_id TEXT NOT NULL, subject_name TEXT NOT NULL,
                total_questions INTEGER NOT NULL, current_index INTEGER NOT NULL DEFAULT 0,
                correct_count INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL, finished_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_sessions_user ON quiz_sessions(user_id, created_at);
            CREATE TABLE IF NOT EXISTS quiz_session_questions(
                session_id INTEGER NOT NULL REFERENCES quiz_sessions(session_id) ON DELETE CASCADE,
                position INTEGER NOT NULL, question_id TEXT NOT NULL, selected_option TEXT,
                is_correct INTEGER, answered_at TEXT, PRIMARY KEY(session_id, position)
            );
        """)


def get_subjects(question_db: Path) -> list[dict]:
    with question_connection(question_db) as con:
        return [dict(r) for r in con.execute("""
            SELECT subject_id, subject_slug, subject_name_en, subject_name_mr, question_count, sort_order
            FROM subjects ORDER BY sort_order
        """)]


def get_subject(question_db: Path, subject_id: str) -> dict | None:
    with question_connection(question_db) as con:
        row = con.execute("""
            SELECT subject_id, subject_slug, subject_name_en, subject_name_mr, question_count
            FROM subjects WHERE subject_id=?
        """, (subject_id,)).fetchone()
        return dict(row) if row else None


def create_quiz_session(question_db: Path, state_db: Path, user_id: int,
                         subject_id: str, requested_count: int) -> tuple[int, int]:
    count = max(1, min(int(requested_count), 50))
    with question_connection(question_db) as qcon:
        subject = qcon.execute(
            "SELECT subject_name_en, subject_name_mr FROM subjects WHERE subject_id=?",
            (subject_id,)
        ).fetchone()
        if not subject:
            raise ValueError("Subject not found.")
        ids = [r["question_id"] for r in qcon.execute("""
            SELECT question_id FROM questions
            WHERE subject_id=? AND correct_option_key IN ('A','B','C','D')
            ORDER BY RANDOM() LIMIT ?
        """, (subject_id, count))]
    if not ids:
        raise ValueError("No answer-key-complete MCQs found for this subject.")
    name = subject["subject_name_en"] or subject["subject_name_mr"] or subject_id
    with state_connection(state_db) as con:
        cursor = con.execute("""
            INSERT INTO quiz_sessions(user_id,subject_id,subject_name,total_questions,created_at)
            VALUES(?,?,?,?,?)
        """, (str(user_id), subject_id, name, len(ids), datetime.now(timezone.utc).isoformat()))
        sid = int(cursor.lastrowid)
        con.executemany(
            "INSERT INTO quiz_session_questions(session_id,position,question_id) VALUES(?,?,?)",
            [(sid, index, qid) for index, qid in enumerate(ids)]
        )
    return sid, len(ids)


def get_session(state_db: Path, session_id: int, user_id: int) -> dict | None:
    with state_connection(state_db) as con:
        row = con.execute(
            "SELECT * FROM quiz_sessions WHERE session_id=? AND user_id=?",
            (session_id, str(user_id))
        ).fetchone()
        return dict(row) if row else None


def get_question(question_db: Path, state_db: Path, session_id: int,
                 position: int, user_id: int) -> dict | None:
    session = get_session(state_db, session_id, user_id)
    if not session or session["status"] != "active":
        return None
    with state_connection(state_db) as con:
        item = con.execute("""
            SELECT question_id,selected_option,is_correct FROM quiz_session_questions
            WHERE session_id=? AND position=?
        """, (session_id, position)).fetchone()
    if not item:
        return None
    with question_connection(question_db) as con:
        row = con.execute("""
            SELECT q.*,s.subject_name_en,s.subject_name_mr
            FROM questions q JOIN subjects s ON s.subject_id=q.subject_id
            WHERE q.question_id=? AND q.subject_id=?
        """, (item["question_id"], session["subject_id"])).fetchone()
    if not row:
        return None
    result = dict(row)
    result.update(selected_option=item["selected_option"], is_correct=item["is_correct"], session=session)
    return result


def record_answer(question_db: Path, state_db: Path, session_id: int, position: int,
                  user_id: int, selected_option: str) -> dict:
    option = selected_option.upper()
    if option not in {"A", "B", "C", "D"}:
        raise ValueError("Invalid option.")
    session = get_session(state_db, session_id, user_id)
    if not session or session["status"] != "active":
        raise ValueError("Quiz is no longer active.")
    if int(session["current_index"]) != int(position):
        raise ValueError("This question is no longer active.")
    item = get_question(question_db, state_db, session_id, position, user_id)
    if not item:
        raise ValueError("Question not found.")
    correct_key = str(item["correct_option_key"] or "").upper()
    is_correct = int(option == correct_key)
    with state_connection(state_db) as con:
        cursor = con.execute("""
            UPDATE quiz_session_questions
            SET selected_option=?,is_correct=?,answered_at=?
            WHERE session_id=? AND position=? AND selected_option IS NULL
        """, (option, is_correct, datetime.now(timezone.utc).isoformat(), session_id, position))
        if cursor.rowcount == 1:
            con.execute("UPDATE quiz_sessions SET correct_count=correct_count+? WHERE session_id=?",
                        (is_correct, session_id))
        else:
            return {"already_answered": True, "is_correct": bool(item["is_correct"]),
                    "correct_option_key": correct_key, "question": item}
    return {"already_answered": False, "is_correct": bool(is_correct),
            "correct_option_key": correct_key, "question": item}


def advance_session(state_db: Path, session_id: int, position: int, user_id: int) -> dict:
    with state_connection(state_db) as con:
        session = con.execute(
            "SELECT * FROM quiz_sessions WHERE session_id=? AND user_id=?",
            (session_id, str(user_id))
        ).fetchone()
        if not session or session["status"] != "active":
            raise ValueError("Quiz is no longer active.")
        if int(session["current_index"]) != int(position):
            raise ValueError("This quiz screen is out of date.")
        item = con.execute(
            "SELECT selected_option FROM quiz_session_questions WHERE session_id=? AND position=?",
            (session_id, position)
        ).fetchone()
        if not item or not item["selected_option"]:
            raise ValueError("Answer this question first.")
        next_pos = position + 1
        if next_pos >= int(session["total_questions"]):
            con.execute("UPDATE quiz_sessions SET status='completed',finished_at=? WHERE session_id=?",
                        (datetime.now(timezone.utc).isoformat(), session_id))
            final = con.execute("SELECT * FROM quiz_sessions WHERE session_id=?", (session_id,)).fetchone()
            return {"completed": True, "session": dict(final)}
        con.execute("UPDATE quiz_sessions SET current_index=? WHERE session_id=?", (next_pos, session_id))
        return {"completed": False, "next_position": next_pos}


def stop_session(state_db: Path, session_id: int, user_id: int) -> dict | None:
    with state_connection(state_db) as con:
        con.execute("""
            UPDATE quiz_sessions SET status='stopped',finished_at=?
            WHERE session_id=? AND user_id=? AND status='active'
        """, (datetime.now(timezone.utc).isoformat(), session_id, str(user_id)))
        row = con.execute(
            "SELECT * FROM quiz_sessions WHERE session_id=? AND user_id=?",
            (session_id, str(user_id))
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["answered_questions"] = con.execute("""
            SELECT COUNT(*) FROM quiz_session_questions
            WHERE session_id=? AND selected_option IS NOT NULL
        """, (session_id,)).fetchone()[0]
        return result


def get_user_stats(state_db: Path, user_id: int) -> dict:
    with state_connection(state_db) as con:
        overall = con.execute("""
            SELECT COUNT(*) quizzes,COALESCE(SUM(correct_count),0) correct
            FROM quiz_sessions WHERE user_id=?
        """, (str(user_id),)).fetchone()
        answered = con.execute("""
            SELECT COUNT(*) answered,COALESCE(SUM(q.is_correct),0) correct
            FROM quiz_session_questions q JOIN quiz_sessions s ON s.session_id=q.session_id
            WHERE s.user_id=? AND q.selected_option IS NOT NULL
        """, (str(user_id),)).fetchone()
        subjects = con.execute("""
            SELECT s.subject_name,s.subject_id,COUNT(q.position) answered,COALESCE(SUM(q.is_correct),0) correct
            FROM quiz_sessions s LEFT JOIN quiz_session_questions q ON q.session_id=s.session_id
            WHERE s.user_id=? AND q.selected_option IS NOT NULL
            GROUP BY s.subject_id,s.subject_name ORDER BY s.subject_name
        """, (str(user_id),)).fetchall()
    return {"quizzes": overall["quizzes"], "correct": overall["correct"],
            "answered": answered["answered"], "subject_stats": [dict(x) for x in subjects]}
