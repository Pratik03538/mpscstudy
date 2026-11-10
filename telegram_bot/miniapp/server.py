from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import time
from pathlib import Path
from urllib.parse import parse_qsl

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
load_dotenv(PROJECT_ROOT / ".env")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
STATE_DB = Path(os.getenv("BOT_STATE_DB_PATH", str(PROJECT_ROOT / "data" / "bot_progress.sqlite3"))).resolve()
MAX_AUTH_AGE = int(os.getenv("MINI_APP_AUTH_MAX_AGE", "86400"))

app = FastAPI(title="MPSC Arena API", version="1.0.0")


def db():
    if not STATE_DB.exists():
        raise HTTPException(status_code=503, detail="Bot state database is not available on this host.")
    con = sqlite3.connect(STATE_DB.as_uri() + "?mode=ro", uri=True, timeout=15)
    con.row_factory = sqlite3.Row
    return con


def verify_init_data(init_data: str) -> dict:
    if not BOT_TOKEN:
        raise HTTPException(status_code=503, detail="Mini App authentication is not configured.")
    if not init_data:
        raise HTTPException(status_code=401, detail="Open this dashboard from Telegram.")
    values = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = values.pop("hash", "")
    if not received_hash:
        raise HTTPException(status_code=401, detail="Telegram authentication hash is missing.")
    check_string = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode("utf-8"), hashlib.sha256).digest()
    calculated = hmac.new(secret_key, check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated, received_hash):
        raise HTTPException(status_code=401, detail="Telegram authentication could not be verified.")
    try:
        auth_date = int(values.get("auth_date", "0"))
        user = json.loads(values.get("user", "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        raise HTTPException(status_code=401, detail="Invalid Telegram authentication data.")
    now = int(time.time())
    if auth_date <= 0 or now - auth_date > MAX_AUTH_AGE or auth_date > now + 60:
        raise HTTPException(status_code=401, detail="Telegram authentication has expired. Reopen the Mini App.")
    if not isinstance(user, dict) or not user.get("id"):
        raise HTTPException(status_code=401, detail="Telegram user identity is unavailable.")
    return user


def current_user(init_data: str) -> dict:
    return verify_init_data(init_data)


@app.get("/")
def index():
    return FileResponse(ROOT / "index.html")


@app.get("/health")
def health():
    return {"ok": True, "service": "mpsc-arena-miniapp"}


@app.get("/api/profile")
def profile(x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    user = current_user(x_telegram_init_data)
    uid = str(user["id"])
    con = db()
    try:
        profile_row = con.execute(
            "SELECT display_name,username FROM community_profiles WHERE user_id=?", (uid,)
        ).fetchone()
        practice = con.execute("""
            SELECT COUNT(*) quizzes,COALESCE(SUM(correct_count),0) correct
            FROM quiz_sessions WHERE user_id=?
        """, (uid,)).fetchone()
        answers = con.execute("""
            SELECT COUNT(*) answered,COALESCE(SUM(q.is_correct),0) correct
            FROM quiz_session_questions q
            JOIN quiz_sessions s ON s.session_id=q.session_id
            WHERE s.user_id=? AND q.selected_option IS NOT NULL
        """, (uid,)).fetchone()
        subject_rows = con.execute("""
            SELECT s.subject_name,COUNT(q.position) answered,
                   COALESCE(SUM(q.is_correct),0) correct
            FROM quiz_sessions s
            JOIN quiz_session_questions q ON q.session_id=s.session_id
            WHERE s.user_id=? AND q.selected_option IS NOT NULL
            GROUP BY s.subject_id,s.subject_name
            ORDER BY s.subject_name
        """, (uid,)).fetchall()
        battle = con.execute(
            "SELECT battles_played,wins,correct_total FROM battle_player_totals WHERE user_id=?",
            (uid,),
        ).fetchone()
        official = con.execute(
            "SELECT COUNT(DISTINCT event_id) events FROM official_event_answers WHERE user_id=?",
            (uid,),
        ).fetchone()
        answered_count = int(answers["answered"])
        correct_count = int(answers["correct"])
        display = (
            profile_row["display_name"] if profile_row else
            " ".join(x for x in [user.get("first_name"), user.get("last_name")] if x) or "Student"
        )
        return {
            "telegram_id": uid,
            "display_name": display,
            "username": profile_row["username"] if profile_row else user.get("username"),
            "practice": {
                "quizzes": int(practice["quizzes"]),
                "answered": answered_count,
                "correct": correct_count,
                "accuracy": round(100 * correct_count / answered_count, 1) if answered_count else 0,
            },
            "battles": {
                "played": int(battle["battles_played"]) if battle else 0,
                "wins": int(battle["wins"]) if battle else 0,
                "correct_answers": int(battle["correct_total"]) if battle else 0,
            },
            "official_events_entered": int(official["events"]) if official else 0,
            "subjects": [
                {
                    "name": row["subject_name"],
                    "answered": int(row["answered"]),
                    "correct": int(row["correct"]),
                    "accuracy": round(100 * row["correct"] / row["answered"], 1) if row["answered"] else 0,
                }
                for row in subject_rows
            ],
        }
    finally:
        con.close()


@app.get("/api/leaderboard/community")
def community_leaderboard(limit: int = 20, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    current_user(x_telegram_init_data)
    con = db()
    try:
        rows = con.execute("""
            SELECT p.display_name,p.username,t.battles_played,t.wins,t.correct_total
            FROM battle_player_totals t
            JOIN community_profiles p ON p.user_id=t.user_id
            ORDER BY t.wins DESC,t.correct_total DESC,t.battles_played DESC
            LIMIT ?
        """, (max(1, min(limit, 50)),)).fetchall()
        return {"items": [
            {
                "name": ("@" + row["username"]) if row["username"] else row["display_name"],
                "battles": int(row["battles_played"]),
                "wins": int(row["wins"]),
                "correct": int(row["correct_total"]),
            } for row in rows
        ]}
    finally:
        con.close()


@app.get("/api/leaderboard/official")
def official_leaderboard(x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    current_user(x_telegram_init_data)
    con = db()
    try:
        event = con.execute(
            "SELECT * FROM official_events WHERE status='completed' ORDER BY finished_at DESC LIMIT 1"
        ).fetchone()
        if not event:
            return {"event": None, "items": []}
        rows = con.execute("""
            SELECT a.user_id,COUNT(*) answered,SUM(a.is_correct) correct,SUM(a.elapsed_ms) elapsed,
                   p.display_name,p.username
            FROM official_event_answers a
            LEFT JOIN community_profiles p ON p.user_id=a.user_id
            WHERE a.event_id=?
            GROUP BY a.user_id
            HAVING COUNT(*)=?
            ORDER BY correct DESC,elapsed ASC
            LIMIT 20
        """, (event["event_id"], event["total_questions"])).fetchall()
        return {
            "event": {"id": event["event_id"], "date": event["event_date"], "questions": event["total_questions"]},
            "items": [
                {
                    "name": ("@" + row["username"]) if row["username"] else row["display_name"] or "Student",
                    "answered": int(row["answered"]),
                    "correct": int(row["correct"]),
                    "points": int(row["correct"]) * 100,
                    "elapsed_ms": int(row["elapsed"]),
                } for row in rows
            ],
        }
    finally:
        con.close()


@app.get("/api/battles/recent")
def recent_battles(x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    user = current_user(x_telegram_init_data)
    uid = str(user["id"])
    con = db()
    try:
        rows = con.execute("""
            SELECT battle_id,challenger_name,opponent_name,subject_name,status,total_questions,
                   challenger_id,opponent_id,created_at,finished_at
            FROM battles
            WHERE challenger_id=? OR opponent_id=?
            ORDER BY created_at DESC LIMIT 10
        """, (uid, uid)).fetchall()
        return {"items": [
            {
                "id": row["battle_id"],
                "opponent": row["opponent_name"] if row["challenger_id"] == uid else row["challenger_name"],
                "subject": row["subject_name"],
                "status": row["status"],
                "questions": int(row["total_questions"]),
                "finished_at": row["finished_at"],
            } for row in rows
        ]}
    finally:
        con.close()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=False)
