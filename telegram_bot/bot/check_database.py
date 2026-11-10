from __future__ import annotations

import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "database" / "mpsc_subject_only.db"
OUTPUTS = ROOT / "outputs"


def main() -> int:
    if not DB_PATH.exists():
        raise SystemExit(f"Question database not found: {DB_PATH}")
    con = sqlite3.connect(DB_PATH.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        fk_errors = con.execute("PRAGMA foreign_key_check").fetchall()
        total = con.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
        subjects = [
            dict(zip([col[0] for col in con.execute("SELECT * FROM subject_question_counts").description], row))
            for row in con.execute("SELECT * FROM subject_question_counts ORDER BY subject_name_en")
        ]
        missing = con.execute("""
            SELECT COUNT(*) FROM questions
            WHERE question_id IS NULL OR option_a IS NULL OR option_b IS NULL
               OR option_c IS NULL OR option_d IS NULL OR correct_option_key IS NULL
        """).fetchone()[0]
        topic_columns = [
            row[1] for row in con.execute("PRAGMA table_info(questions)")
            if "topic" in row[1].casefold()
        ]
    finally:
        con.close()
    result = {
        "database": "database/mpsc_subject_only.db",
        "integrity_check": integrity,
        "foreign_key_errors": len(fk_errors),
        "total_questions": total,
        "subjects": subjects,
        "questions_missing_required_fields": missing,
        "topic_columns": topic_columns,
        "status": "PASS" if integrity == "ok" and not fk_errors and not missing and not topic_columns else "FAIL",
    }
    OUTPUTS.mkdir(exist_ok=True)
    (OUTPUTS / "database_validation.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
