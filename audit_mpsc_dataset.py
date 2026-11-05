#!/usr/bin/env python3
"""Read-only data-quality audit for the MPSC Today SQLite question bank.

Creates audit reports and a structurally-clean, exact-deduplicated candidate
CSV in mpsc_output/data_quality_audit. The source SQLite database is never
modified. A valid A/B/C/D key is NOT proof that the answer is factually correct.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sqlite3
import sys
import unicodedata
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

DEFAULT_DB = Path(__file__).resolve().parent / "mpsc_output" / "mpsc_questions.db"
DEFAULT_OUT = Path(__file__).resolve().parent / "mpsc_output" / "data_quality_audit"
PLACEHOLDERS = {"", "nan", "none", "null", "undefined", "n/a", "na", "-", "--", "---"}
MOJIBAKE_RE = re.compile(r"(?:Ã.|Â.|â€™|â€œ|â€|ðŸ|�)")
HTML_RE = re.compile(r"<\s*/?\s*[a-zA-Z][^>]*>")
CORRECT_MAP = {
    "a": "A", "b": "B", "c": "C", "d": "D",
    "1": "A", "2": "B", "3": "C", "4": "D",
    "optiona": "A", "optionb": "B", "optionc": "C", "optiond": "D",
    "option1": "A", "option2": "B", "option3": "C", "option4": "D",
}


def is_missing(value: Any) -> bool:
    if value is None:
        return True
    try:
        if pd.isna(value):
            return True
    except (TypeError, ValueError):
        pass
    return str(value).strip().casefold() in PLACEHOLDERS


def text_value(value: Any) -> str:
    if is_missing(value):
        return ""
    value = html.unescape(str(value))
    value = unicodedata.normalize("NFKC", value)
    return re.sub(r"\s+", " ", value).strip()


def normalize_for_match(value: Any) -> str:
    value = text_value(value).casefold()
    return "".join(ch for ch in value if ch.isalnum())


def normalized_correct_option(value: Any) -> str:
    token = normalize_for_match(value)
    if token in {"10", "20", "30", "40"}:  # Handles values serialized as 1.0, 2.0, etc.
        token = token[0]
    return CORRECT_MAP.get(token, "")


def get_question_text(row: dict[str, Any]) -> str:
    return text_value(row.get("question_mr")) or text_value(row.get("question_en"))


def fingerprint(row: dict[str, Any]) -> str:
    question = normalize_for_match(get_question_text(row))
    options = [normalize_for_match(row.get(f"option_{letter}")) for letter in "abcd"]
    if not question or not all(options):
        return ""
    payload = json.dumps([question, *options], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def safe_csv(df: pd.DataFrame, path: Path) -> None:
    df.to_csv(path, index=False, encoding="utf-8-sig")


def open_database_readonly(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")
    uri = db_path.resolve().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True)


def audit_row(row: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    if is_missing(row.get("id")):
        errors.append("missing_id")

    question = get_question_text(row)
    if not question:
        errors.append("missing_question_text")
    elif len(question) < 4:
        warnings.append("very_short_question")

    for letter in "abcd":
        if is_missing(row.get(f"option_{letter}")):
            errors.append(f"missing_option_{letter}")

    correct_norm = normalized_correct_option(row.get("correct_option"))
    if not correct_norm:
        errors.append("invalid_correct_option_label")

    normalized_options = [normalize_for_match(row.get(f"option_{letter}")) for letter in "abcd"]
    nonempty_options = [option for option in normalized_options if option]
    if len(nonempty_options) != len(set(nonempty_options)):
        errors.append("repeated_options_within_question")

    text_blob = " ".join([question] + [
        text_value(row.get(f"option_{letter}")) for letter in "abcd"
    ])
    if MOJIBAKE_RE.search(text_blob):
        warnings.append("possible_encoding_mojibake")
    if HTML_RE.search(text_blob):
        warnings.append("html_markup_in_text")
    if is_missing(row.get("explanation_mr")) and is_missing(row.get("explanation_en")):
        warnings.append("missing_explanation")
    if is_missing(row.get("subject_slug")):
        warnings.append("missing_subject_slug")
    if is_missing(row.get("topic_slug")):
        warnings.append("missing_topic_slug")

    raw_json_valid = True
    raw_json = row.get("raw_json")
    if is_missing(raw_json):
        raw_json_valid = False
        warnings.append("missing_raw_json")
    else:
        try:
            json.loads(str(raw_json))
        except (TypeError, json.JSONDecodeError):
            raw_json_valid = False
            warnings.append("malformed_raw_json")

    return {
        "audit_status": "INVALID" if errors else ("NEEDS_REVIEW" if warnings else "PASS"),
        "audit_errors": ";".join(errors),
        "audit_warnings": ";".join(warnings),
        "correct_option_normalized": correct_norm,
        "raw_json_valid": raw_json_valid,
        "content_fingerprint": fingerprint(row),
        "is_structurally_valid": not errors,
    }


def run_similarity(df: pd.DataFrame, threshold: float, max_rows: int) -> pd.DataFrame:
    try:
        import numpy as np
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.neighbors import NearestNeighbors
    except ImportError as exc:
        raise RuntimeError(
            "Similarity mode needs scikit-learn and numpy. Run: "
            "python -m pip install scikit-learn numpy"
        ) from exc

    work = df.copy()
    work["_match_text"] = work.apply(lambda row: get_question_text(row.to_dict()), axis=1)
    work = work[work["_match_text"].str.len() >= 4].copy()
    if len(work) > max_rows:
        print(f"[SIMILARITY] Limiting to {max_rows:,} of {len(work):,} rows.")
        work = work.head(max_rows)
    if len(work) < 2:
        return pd.DataFrame(columns=["id_1", "id_2", "similarity", "question_1", "question_2"])

    texts = work["_match_text"].tolist()
    print(f"[SIMILARITY] TF-IDF vectorizing {len(texts):,} questions...", flush=True)
    vectorizer = TfidfVectorizer(
        analyzer="char_wb", ngram_range=(3, 5), min_df=2,
        max_features=60000, sublinear_tf=True, dtype=np.float32,
    )
    matrix = vectorizer.fit_transform(texts)
    neighbors = NearestNeighbors(metric="cosine", algorithm="brute", n_neighbors=2, n_jobs=-1)
    neighbors.fit(matrix)

    ids = work["id"].astype(str).tolist() if "id" in work.columns else [str(i) for i in work.index]
    pairs: list[dict[str, Any]] = []
    batch_size = 256
    print("[SIMILARITY] Finding nearest question pairs...", flush=True)
    for start in range(0, matrix.shape[0], batch_size):
        distances, indices = neighbors.kneighbors(
            matrix[start:start + batch_size], return_distance=True
        )
        for local_i, (distance_row, index_row) in enumerate(zip(distances, indices)):
            i = start + local_i
            for distance, candidate in zip(distance_row, index_row):
                j = int(candidate)
                if j <= i:
                    continue
                score = 1.0 - float(distance)
                if score >= threshold:
                    pairs.append({
                        "id_1": ids[i], "id_2": ids[j],
                        "similarity": round(score, 5),
                        "question_1": texts[i], "question_2": texts[j],
                    })
    result = pd.DataFrame(pairs)
    if not result.empty:
        result = result.sort_values("similarity", ascending=False).reset_index(drop=True)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit the MPSC question database without modifying the source."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="Path to mpsc_questions.db")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="Audit output directory")
    parser.add_argument(
        "--similarity", type=float, default=0.93,
        help="TF-IDF cosine threshold from 0 to 1; use 0 to skip (default: 0.93).",
    )
    parser.add_argument(
        "--similarity-limit", type=int, default=30000,
        help="Maximum rows used for similarity analysis (default: 30000).",
    )
    args = parser.parse_args()
    if not 0 <= args.similarity <= 1:
        parser.error("--similarity must be between 0 and 1")
    if args.similarity_limit < 2:
        parser.error("--similarity-limit must be at least 2")

    args.out.mkdir(parents=True, exist_ok=True)
    print("[START] MPSC question-bank data-quality audit", flush=True)
    print(f"[DATABASE] {args.db.resolve()}", flush=True)
    print("[SAFETY] Source database is opened READ-ONLY.", flush=True)

    con = open_database_readonly(args.db)
    try:
        exists = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='questions'"
        ).fetchone()
        if not exists:
            raise RuntimeError("Table 'questions' was not found.")
        df = pd.read_sql_query('SELECT * FROM "questions"', con)
    finally:
        con.close()

    total = len(df)
    if total == 0:
        raise RuntimeError("The questions table is empty.")
    print(f"[LOAD] Loaded {total:,} rows and {len(df.columns)} columns.", flush=True)

    audit = pd.DataFrame([audit_row(row) for row in df.to_dict(orient="records")])
    full = pd.concat([df.reset_index(drop=True), audit], axis=1)

    # Identify every row in an exact-duplicate group; the candidate export keeps
    # the most complete structurally valid representative from each group.
    has_fp = full[full["content_fingerprint"] != ""]
    duplicate_mask = has_fp.duplicated("content_fingerprint", keep=False)
    exact_dups = has_fp[duplicate_mask].copy()
    full["exact_duplicate_content"] = False
    if not exact_dups.empty:
        full.loc[exact_dups.index, "exact_duplicate_content"] = True
        full.loc[exact_dups.index, "audit_warnings"] = full.loc[
            exact_dups.index, "audit_warnings"
        ].apply(lambda value: ";".join(filter(None, [str(value), "exact_duplicate_content"])))
        full.loc[exact_dups.index, "audit_status"] = full.loc[
            exact_dups.index, "audit_status"
        ].replace({"PASS": "NEEDS_REVIEW"})

    question_keys = full.apply(
        lambda row: normalize_for_match(get_question_text(row.to_dict())), axis=1
    )
    full["same_question_text_group_size"] = (
        question_keys.map(question_keys.value_counts()).fillna(0).astype(int)
    )

    core = full[full["is_structurally_valid"]].copy()
    completeness_fields = [
        col for col in [
            "question_mr", "question_en", "option_a", "option_b", "option_c",
            "option_d", "correct_option", "explanation_mr", "explanation_en",
            "subject_slug", "topic_slug",
        ] if col in core.columns
    ]
    core["_completeness_score"] = core.apply(
        lambda row: sum(not is_missing(row.get(col)) for col in completeness_fields), axis=1
    )
    core_nonempty = core[core["content_fingerprint"] != ""].sort_values(
        "_completeness_score", ascending=False
    )
    core_unique = core_nonempty.drop_duplicates("content_fingerprint", keep="first")
    no_fingerprint = core[core["content_fingerprint"] == ""]
    cleaned = pd.concat([core_unique, no_fingerprint], ignore_index=True)
    cleaned.drop(columns=["_completeness_score"], inplace=True, errors="ignore")

    audit_path = args.out / "question_level_audit.csv"
    clean_path = args.out / "cleaned_questions_candidate.csv"
    duplicate_path = args.out / "exact_duplicates.csv"
    safe_csv(full, audit_path)
    safe_csv(cleaned[df.columns.tolist()], clean_path)
    safe_csv(exact_dups, duplicate_path)

    missing_rows = []
    for column in df.columns:
        count = int(df[column].apply(is_missing).sum())
        missing_rows.append({
            "column": column, "missing_count": count,
            "missing_percent": round(100 * count / total, 2),
        })
    safe_csv(
        pd.DataFrame(missing_rows).sort_values("missing_count", ascending=False),
        args.out / "missingness_report.csv",
    )

    subject_col = "subject_slug" if "subject_slug" in df.columns else None
    topic_col = "topic_slug" if "topic_slug" in df.columns else None
    group_cols = [column for column in [subject_col, topic_col] if column]
    if group_cols:
        aggregations = {
            "invalid_count": ("is_structurally_valid", lambda values: int((~values).sum())),
            "needs_review_count": ("audit_status", lambda values: int((values == "NEEDS_REVIEW").sum())),
            "exact_duplicate_rows": ("exact_duplicate_content", "sum"),
        }
        if "id" in full.columns:
            aggregations["question_count"] = ("id", "count")
        else:
            aggregations["question_count"] = (group_cols[0], "size")
        summary = full.groupby(group_cols, dropna=False).agg(**aggregations).reset_index()
        safe_csv(summary, args.out / "subject_topic_summary.csv")

    answer_groups = [column for column in [subject_col, "correct_option_normalized"] if column]
    if answer_groups:
        answer_distribution = (
            full.groupby(answer_groups, dropna=False).size().reset_index(name="question_count")
        )
        safe_csv(answer_distribution, args.out / "answer_key_distribution.csv")

    status_counts = full["audit_status"].value_counts().to_dict()
    error_counts = Counter(
        item for value in full["audit_errors"].fillna("")
        for item in str(value).split(";") if item
    )
    warning_counts = Counter(
        item for value in full["audit_warnings"].fillna("")
        for item in str(value).split(";") if item
    )

    similarity_path = None
    similar_count = 0
    if args.similarity > 0:
        similar = run_similarity(full, args.similarity, args.similarity_limit)
        similarity_path = args.out / "near_duplicate_candidates.csv"
        safe_csv(similar, similarity_path)
        similar_count = len(similar)

    summary = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_database": str(args.db.resolve()),
        "source_open_mode": "READ-ONLY",
        "total_rows": total,
        "column_count": len(df.columns),
        "audit_status_counts": {str(key): int(value) for key, value in status_counts.items()},
        "structurally_valid_rows_before_exact_dedup": int(full["is_structurally_valid"].sum()),
        "cleaned_candidate_rows_after_exact_dedup": int(len(cleaned)),
        "exact_duplicate_rows_flagged": int(len(exact_dups)),
        "near_duplicate_pairs_found": int(similar_count),
        "near_duplicate_threshold": args.similarity,
        "error_counts": dict(error_counts),
        "warning_counts": dict(warning_counts),
        "important_limit": (
            "This audit checks structure, formatting, metadata and similarity. It does NOT "
            "determine whether the marked answer is factually correct; that requires "
            "question-wise verification against trusted sources."
        ),
        "outputs": {
            "question_level_audit": str(audit_path),
            "cleaned_questions_candidate": str(clean_path),
            "exact_duplicates": str(duplicate_path),
            "missingness_report": str(args.out / "missingness_report.csv"),
            "subject_topic_summary": str(args.out / "subject_topic_summary.csv"),
            "answer_key_distribution": str(args.out / "answer_key_distribution.csv"),
            "near_duplicate_candidates": str(similarity_path) if similarity_path else None,
        },
    }
    summary_path = args.out / "audit_summary.json"
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print("\n========== AUDIT SUMMARY ==========")
    print(f"Total questions:                      {total:,}")
    print(f"PASS:                                 {status_counts.get('PASS', 0):,}")
    print(f"NEEDS REVIEW:                         {status_counts.get('NEEDS_REVIEW', 0):,}")
    print(f"INVALID structure:                    {status_counts.get('INVALID', 0):,}")
    print(f"Exact duplicate rows flagged:         {len(exact_dups):,}")
    print(f"Clean candidate rows:                 {len(cleaned):,}")
    print(f"Near-duplicate candidate pairs:       {similar_count:,}")
    print("\n[OUTPUT]", args.out.resolve())
    print("[DONE] Original SQLite database was not modified.")
    print("[NOTE] Correct-answer factual accuracy needs independent verification.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"[ERROR] {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
