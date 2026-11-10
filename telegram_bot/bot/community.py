from __future__ import annotations

import asyncio
import html
import logging
import os
import random
import secrets
import time
from datetime import datetime, time as day_time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup, InlineQueryResultArticle,
    InputTextMessageContent, Update, WebAppInfo,
)
from telegram.constants import ChatType, ParseMode
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
    InlineQueryHandler,
)

from db import get_subject, get_subjects, get_user_stats, question_connection, state_connection

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
QUESTION_DB = Path(os.getenv("BOT_DB_PATH", str(ROOT / "database" / "mpsc_subject_only.db"))).resolve()
STATE_DB = Path(os.getenv("BOT_STATE_DB_PATH", str(ROOT / "data" / "bot_progress.sqlite3"))).resolve()
CHANNEL_ID = os.getenv("PUBLIC_CHANNEL_ID", "").strip()
CHANNEL_LINK = os.getenv("PUBLIC_CHANNEL_LINK", "").strip()
DISCUSSION_GROUP_ID = os.getenv("DISCUSSION_GROUP_ID", "").strip()
DISCUSSION_GROUP_LINK = os.getenv("DISCUSSION_GROUP_LINK", "").strip()
MINI_APP_URL = os.getenv("MINI_APP_URL", "").strip()
ADMIN_IDS = {
    int(value.strip()) for value in os.getenv("ADMIN_USER_IDS", "").split(",")
    if value.strip().isdigit()
}
EVENT_HOUR = int(os.getenv("EVENT_HOUR", "20"))
EVENT_MINUTE = int(os.getenv("EVENT_MINUTE", "30"))
EVENT_QUESTION_COUNT = max(5, min(int(os.getenv("EVENT_QUESTION_COUNT", "15")), 30))
EVENT_QUESTION_SECONDS = max(15, int(os.getenv("EVENT_QUESTION_SECONDS", "35")))
BATTLE_QUESTION_SECONDS = max(20, int(os.getenv("BATTLE_QUESTION_SECONDS", "45")))
OPEN_CHALLENGE_TTL = max(60, int(os.getenv("OPEN_CHALLENGE_TTL", "600")))
START_CONFIRM_TTL = max(30, int(os.getenv("START_CONFIRM_TTL", "120")))
TZ = ZoneInfo("Asia/Kolkata")
logger = logging.getLogger("mpsc-community")

SUBJECT_ALIASES = {
    "history": ("history", "history"),
    "itihas": ("history", "history"),
    "geography": ("geography", "geography"),
    "bhugol": ("geography", "geography"),
    "economics": ("economics", "economics"),
    "arthashastra": ("economics", "economics"),
    "marathi": ("marathi-grammar", "marathi grammar"),
    "grammar": ("marathi-grammar", "marathi grammar"),
    "marathi-grammar": ("marathi-grammar", "marathi grammar"),
}


def _esc(value: object) -> str:
    return html.escape(str(value or ""), quote=False)


def _now() -> float:
    return time.time()


def initialize_community_db(path: Path = STATE_DB) -> None:
    with state_connection(path) as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS community_profiles(
            user_id TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            username TEXT,
            first_seen REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS battles(
            battle_id TEXT PRIMARY KEY,
            chat_id TEXT,
            message_id INTEGER,
            inline_message_id TEXT,
            challenger_id TEXT NOT NULL,
            challenger_name TEXT NOT NULL,
            opponent_id TEXT,
            opponent_name TEXT,
            subject_id TEXT NOT NULL,
            subject_name TEXT NOT NULL,
            status TEXT NOT NULL,
            challenger_ready INTEGER NOT NULL DEFAULT 0,
            opponent_ready INTEGER NOT NULL DEFAULT 0,
            current_position INTEGER NOT NULL DEFAULT 0,
            total_questions INTEGER NOT NULL,
            question_started_at REAL,
            created_at REAL NOT NULL,
            accepted_at REAL,
            started_at REAL,
            finished_at REAL
        );
        CREATE INDEX IF NOT EXISTS idx_battles_chat_status ON battles(chat_id,status,created_at);
        CREATE INDEX IF NOT EXISTS idx_battles_users ON battles(challenger_id,opponent_id,status);
        CREATE TABLE IF NOT EXISTS battle_questions(
            battle_id TEXT NOT NULL REFERENCES battles(battle_id) ON DELETE CASCADE,
            position INTEGER NOT NULL,
            question_id TEXT NOT NULL,
            PRIMARY KEY(battle_id,position)
        );
        CREATE TABLE IF NOT EXISTS battle_answers(
            battle_id TEXT NOT NULL REFERENCES battles(battle_id) ON DELETE CASCADE,
            user_id TEXT NOT NULL,
            position INTEGER NOT NULL,
            selected_option TEXT,
            is_correct INTEGER NOT NULL DEFAULT 0,
            elapsed_ms INTEGER NOT NULL DEFAULT 0,
            answered_at REAL NOT NULL,
            PRIMARY KEY(battle_id,user_id,position)
        );
        CREATE INDEX IF NOT EXISTS idx_battle_answers_user ON battle_answers(user_id,battle_id);
        CREATE TABLE IF NOT EXISTS battle_player_totals(
            user_id TEXT PRIMARY KEY,
            battles_played INTEGER NOT NULL DEFAULT 0,
            wins INTEGER NOT NULL DEFAULT 0,
            correct_total INTEGER NOT NULL DEFAULT 0,
            elapsed_total INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS battle_awards(
            battle_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            PRIMARY KEY(battle_id,user_id)
        );
        CREATE TABLE IF NOT EXISTS official_events(
            event_id TEXT PRIMARY KEY,
            event_date TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            status TEXT NOT NULL,
            total_questions INTEGER NOT NULL,
            created_at REAL NOT NULL,
            started_at REAL,
            finished_at REAL
        );
        CREATE INDEX IF NOT EXISTS idx_official_events_date ON official_events(event_date,status);
        CREATE TABLE IF NOT EXISTS official_event_questions(
            event_id TEXT NOT NULL REFERENCES official_events(event_id) ON DELETE CASCADE,
            position INTEGER NOT NULL,
            question_id TEXT NOT NULL,
            posted_at REAL,
            closes_at REAL,
            message_id INTEGER,
            PRIMARY KEY(event_id,position)
        );
        CREATE TABLE IF NOT EXISTS official_event_answers(
            event_id TEXT NOT NULL REFERENCES official_events(event_id) ON DELETE CASCADE,
            user_id TEXT NOT NULL,
            position INTEGER NOT NULL,
            selected_option TEXT NOT NULL,
            is_correct INTEGER NOT NULL,
            elapsed_ms INTEGER NOT NULL,
            answered_at REAL NOT NULL,
            PRIMARY KEY(event_id,user_id,position)
        );
        CREATE INDEX IF NOT EXISTS idx_event_answers_user ON official_event_answers(user_id,event_id);
        """)


def _upsert_profile(user) -> None:
    if not user:
        return
    display = (user.full_name or user.first_name or "Student")[:120]
    username = user.username
    stamp = _now()
    with state_connection(STATE_DB) as con:
        con.execute("""
            INSERT INTO community_profiles(user_id,display_name,username,first_seen,updated_at)
            VALUES(?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
              display_name=excluded.display_name, username=excluded.username, updated_at=excluded.updated_at
        """, (str(user.id), display, username, stamp, stamp))


def _profile_name(user_id: str) -> str:
    with state_connection(STATE_DB) as con:
        row = con.execute(
            "SELECT display_name,username FROM community_profiles WHERE user_id=?", (str(user_id),)
        ).fetchone()
    if not row:
        return "Student"
    return ("@" + row["username"]) if row["username"] else row["display_name"]


def _subject_by_slug(slug: str) -> dict | None:
    key = slug.strip().lower().replace("_", "-")
    canonical = SUBJECT_ALIASES.get(key, (key, key))[0]
    for subject in get_subjects(QUESTION_DB):
        if subject["subject_slug"] == canonical:
            return subject
    return None


def _subject_keyboard(prefix: str = "cs") -> InlineKeyboardMarkup:
    subjects = get_subjects(QUESTION_DB)
    rows = []
    for i in range(0, len(subjects), 2):
        rows.append([
            InlineKeyboardButton(
                subjects[j]["subject_name_en"],
                callback_data=f"{prefix}|{subjects[j]['subject_id']}",
            )
            for j in range(i, min(i + 2, len(subjects)))
        ])
    return InlineKeyboardMarkup(rows)


def _select_question_ids(subject_id: str | None, count: int) -> list[str]:
    with question_connection(QUESTION_DB) as con:
        if subject_id:
            rows = con.execute("""
                SELECT question_id FROM questions
                WHERE subject_id=? AND correct_option_key IN ('A','B','C','D')
                ORDER BY RANDOM() LIMIT ?
            """, (subject_id, count)).fetchall()
        else:
            rows = con.execute("""
                SELECT question_id FROM questions
                WHERE correct_option_key IN ('A','B','C','D')
                ORDER BY RANDOM() LIMIT ?
            """, (count,)).fetchall()
    return [str(row["question_id"]) for row in rows]


def _question(question_id: str) -> dict | None:
    with question_connection(QUESTION_DB) as con:
        row = con.execute("""
            SELECT q.*,s.subject_name_en,s.subject_name_mr
            FROM questions q JOIN subjects s ON s.subject_id=q.subject_id
            WHERE q.question_id=? LIMIT 1
        """, (question_id,)).fetchone()
    return dict(row) if row else None


def _format_mcq(row: dict, heading: str) -> str:
    subject = _esc(row.get("subject_name_en") or row.get("subject_name_mr") or "")
    q_mr, q_en = _esc(row.get("question_mr")), _esc(row.get("question_en"))
    stem = "\n\n".join(x for x in (q_mr, q_en if q_en and q_en != q_mr else "") if x)
    if len(stem) > 2400:
        stem = stem[:2399] + "…"
    opts = (
        "<b>A.</b> " + _esc(row.get("option_a"))[:350] + "\n" +
        "<b>B.</b> " + _esc(row.get("option_b"))[:350] + "\n" +
        "<b>C.</b> " + _esc(row.get("option_c"))[:350] + "\n" +
        "<b>D.</b> " + _esc(row.get("option_d"))[:350]
    )
    return f"<b>{_esc(heading)}</b> · {subject}\n\n{stem}\n\n{opts}"


def _battle_keyboard(battle: dict) -> InlineKeyboardMarkup:
    if battle["status"] == "open":
        rows = [[InlineKeyboardButton("⚔️ Accept Challenge", callback_data=f"ac|{battle['battle_id']}")]]
        rows.append([InlineKeyboardButton("✖ Cancel challenge", callback_data=f"ca|{battle['battle_id']}")])
        return InlineKeyboardMarkup(rows)
    if battle["status"] == "accepted":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(
                ("✅ " if battle["challenger_ready"] else "▶️ ") + "START · Challenger",
                callback_data=f"rd|{battle['battle_id']}|c",
            )],
            [InlineKeyboardButton(
                ("✅ " if battle["opponent_ready"] else "▶️ ") + "START · Accepter",
                callback_data=f"rd|{battle['battle_id']}|o",
            )],
        ])
    return InlineKeyboardMarkup([])


def _battle_answer_keyboard(battle_id: str, position: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("A", callback_data=f"ba|{battle_id}|{position}|A"),
         InlineKeyboardButton("B", callback_data=f"ba|{battle_id}|{position}|B")],
        [InlineKeyboardButton("C", callback_data=f"ba|{battle_id}|{position}|C"),
         InlineKeyboardButton("D", callback_data=f"ba|{battle_id}|{position}|D")],
    ])


def _battle_card_text(battle: dict) -> str:
    title = "⚔️ OPEN PUBLIC CHALLENGE" if battle["status"] == "open" else "⚔️ CHALLENGE ACCEPTED"
    text = (
        f"<b>{title}</b>\n\n"
        f"Challenger: <b>{_esc(battle['challenger_name'])}</b>\n"
        f"Opponent: <b>{_esc(battle.get('opponent_name') or 'Waiting for first acceptance…')}</b>\n"
        f"Subject: <b>{_esc(battle['subject_name'])}</b>\n"
        f"Questions: <b>{battle['total_questions']}</b> · Time per question: <b>{BATTLE_QUESTION_SECONDS}s</b>\n"
    )
    if battle["status"] == "accepted":
        text += (
            "\n<b>Both players must confirm START.</b>\n"
            f"Challenger: {'✅ Ready' if battle['challenger_ready'] else '⏳ Waiting'}\n"
            f"Accepter: {'✅ Ready' if battle['opponent_ready'] else '⏳ Waiting'}"
        )
    return text


def _get_battle(battle_id: str) -> dict | None:
    with state_connection(STATE_DB) as con:
        row = con.execute("SELECT * FROM battles WHERE battle_id=?", (battle_id,)).fetchone()
    return dict(row) if row else None


def _create_battle_record(
    chat_id: str | None, inline_message_id: str | None, creator, subject: dict
) -> dict:
    qids = _select_question_ids(str(subject["subject_id"]), 10)
    if len(qids) < 5:
        raise ValueError("Not enough answer-key-complete questions for this subject.")
    battle_id = secrets.token_hex(4)
    stamp = _now()
    _upsert_profile(creator)
    with state_connection(STATE_DB) as con:
        con.execute("""
            INSERT INTO battles(
                battle_id,chat_id,inline_message_id,challenger_id,challenger_name,
                subject_id,subject_name,status,total_questions,created_at
            ) VALUES(?,?,?,?,?,?,?,'open',?,?)
        """, (
            battle_id, chat_id, inline_message_id, str(creator.id),
            ("@" + creator.username) if creator.username else creator.full_name,
            str(subject["subject_id"]), subject["subject_name_en"],
            len(qids), stamp,
        ))
        con.executemany(
            "INSERT INTO battle_questions(battle_id,position,question_id) VALUES(?,?,?)",
            [(battle_id, i, qid) for i, qid in enumerate(qids)],
        )
    return _get_battle(battle_id)


async def _edit_battle(bot, battle: dict, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    kwargs = {"text": text, "parse_mode": ParseMode.HTML, "reply_markup": markup}
    if battle.get("inline_message_id"):
        await bot.edit_message_text(inline_message_id=battle["inline_message_id"], **kwargs)
    elif battle.get("chat_id") and battle.get("message_id"):
        await bot.edit_message_text(
            chat_id=int(battle["chat_id"]), message_id=int(battle["message_id"]), **kwargs
        )


def _schedule_challenge_expiry(application: Application, battle_id: str) -> None:
    if application.job_queue:
        application.job_queue.run_once(
            expire_open_battle, when=OPEN_CHALLENGE_TTL,
            data={"battle_id": battle_id}, name=f"battle-open-expiry-{battle_id}",
        )


async def expire_open_battle(context: ContextTypes.DEFAULT_TYPE) -> None:
    battle_id = context.job.data["battle_id"]
    battle = _get_battle(battle_id)
    if not battle or battle["status"] != "open":
        return
    with state_connection(STATE_DB) as con:
        con.execute(
            "UPDATE battles SET status='expired',finished_at=? WHERE battle_id=? AND status='open'",
            (_now(), battle_id),
        )
    battle = _get_battle(battle_id)
    try:
        await _edit_battle(
            context.bot, battle,
            "⌛ <b>CHALLENGE EXPIRED</b>\nNo opponent accepted this open challenge in time.",
        )
    except Exception:
        logger.exception("Could not update expired challenge card %s", battle_id)


async def expire_unstarted_battle(context: ContextTypes.DEFAULT_TYPE) -> None:
    battle_id = context.job.data["battle_id"]
    battle = _get_battle(battle_id)
    if not battle or battle["status"] != "accepted":
        return
    with state_connection(STATE_DB) as con:
        con.execute(
            "UPDATE battles SET status='expired',finished_at=? WHERE battle_id=? AND status='accepted'",
            (_now(), battle_id),
        )
    battle = _get_battle(battle_id)
    try:
        await _edit_battle(
            context.bot, battle,
            "⌛ <b>BATTLE NOT STARTED</b>\nBoth players did not confirm START in time. Create a new public challenge.",
        )
    except Exception:
        logger.exception("Could not update unstarted battle card %s", battle_id)


async def _post_open_battle(update: Update, subject: dict, application: Application | None = None) -> None:
    battle = _create_battle_record(str(update.effective_chat.id), None, update.effective_user, subject)
    message = await update.effective_message.reply_text(
        _battle_card_text(battle), parse_mode=ParseMode.HTML, reply_markup=_battle_keyboard(battle)
    )
    with state_connection(STATE_DB) as con:
        con.execute(
            "UPDATE battles SET message_id=? WHERE battle_id=?", (message.message_id, battle["battle_id"])
        )
    if application:
        _schedule_challenge_expiry(application, battle["battle_id"])


async def challenge_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or update.effective_chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await update.effective_message.reply_text(
            "Open public challenges group mein create hote hain. Discussion group mein /challenge likho."
        )
        return
    _upsert_profile(update.effective_user)
    subject_slug = context.args[0] if context.args else ""
    subject = _subject_by_slug(subject_slug) if subject_slug else None
    if subject_slug and not subject:
        await update.effective_message.reply_text(
            "Subject nahi mila. Choose one:", reply_markup=_subject_keyboard()
        )
        return
    if subject:
        await _post_open_battle(update, subject, context.application)
    else:
        await update.effective_message.reply_text(
            "⚔️ <b>Choose challenge subject</b>\nFirst eligible member to accept becomes your opponent.",
            parse_mode=ParseMode.HTML,
            reply_markup=_subject_keyboard(),
        )


async def duel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await challenge_command(update, context)


async def _accept_battle(query, battle_id: str, user, application: Application) -> str:
    battle = _get_battle(battle_id)
    if not battle:
        return "Challenge not found."
    if getattr(user, "is_bot", False):
        return "Bot accounts cannot accept player battles."
    if str(user.id) == battle["challenger_id"]:
        return "You created this challenge. Another player must accept it."
    _upsert_profile(user)
    name = ("@" + user.username) if user.username else user.full_name
    with state_connection(STATE_DB) as con:
        cur = con.execute("""
            UPDATE battles SET opponent_id=?,opponent_name=?,status='accepted',accepted_at=?
            WHERE battle_id=? AND status='open' AND opponent_id IS NULL
        """, (str(user.id), name, _now(), battle_id))
    if cur.rowcount != 1:
        return "Challenge already accepted, expired, or cancelled."
    battle = _get_battle(battle_id)
    await query.edit_message_text(
        _battle_card_text(battle), parse_mode=ParseMode.HTML, reply_markup=_battle_keyboard(battle)
    )
    if application.job_queue:
        application.job_queue.run_once(
            expire_unstarted_battle, when=START_CONFIRM_TTL,
            data={"battle_id": battle_id}, name=f"battle-start-expiry-{battle_id}",
        )
    return "Challenge accepted! Both players must press their own START button."


async def _ready_battle(query, battle_id: str, role: str, user_id: int, application: Application) -> str:
    battle = _get_battle(battle_id)
    if not battle or battle["status"] != "accepted":
        return "This battle is not waiting for Start confirmation."
    if role == "c" and str(user_id) == battle["challenger_id"]:
        column = "challenger_ready"
    elif role == "o" and str(user_id) == battle["opponent_id"]:
        column = "opponent_ready"
    else:
        return "Only the assigned player can confirm their own START."
    with state_connection(STATE_DB) as con:
        con.execute(f"UPDATE battles SET {column}=1 WHERE battle_id=? AND status='accepted'", (battle_id,))
        current = con.execute("SELECT * FROM battles WHERE battle_id=?", (battle_id,)).fetchone()
        updated = dict(current)
        if updated["challenger_ready"] and updated["opponent_ready"]:
            con.execute(
                "UPDATE battles SET status='countdown' WHERE battle_id=? AND status='accepted'",
                (battle_id,),
            )
            updated["status"] = "countdown"
    if updated["status"] == "countdown":
        await query.edit_message_text(
            "✅ <b>BOTH PLAYERS READY</b>\n\n⚔️ Battle starting in 3 seconds…",
            parse_mode=ParseMode.HTML,
        )
        application.create_task(_begin_battle_after_countdown(application, battle_id))
        return "Both confirmed. Battle starting!"
    await query.edit_message_text(
        _battle_card_text(updated), parse_mode=ParseMode.HTML, reply_markup=_battle_keyboard(updated)
    )
    return "Your START confirmation is saved. Waiting for the other player."


async def _begin_battle_after_countdown(application: Application, battle_id: str) -> None:
    await asyncio.sleep(3)
    with state_connection(STATE_DB) as con:
        cur = con.execute("""
            UPDATE battles SET status='active',started_at=?,question_started_at=?
            WHERE battle_id=? AND status='countdown'
        """, (_now(), _now(), battle_id))
    if cur.rowcount != 1:
        return
    await _render_battle_question(application.bot, battle_id, application)


def _battle_progress(battle_id: str) -> tuple[int, int]:
    battle = _get_battle(battle_id)
    if not battle:
        return 0, 0
    with state_connection(STATE_DB) as con:
        rows = con.execute(
            "SELECT user_id,COUNT(*) n FROM battle_answers WHERE battle_id=? AND position=? GROUP BY user_id",
            (battle_id, battle["current_position"]),
        ).fetchall()
    return len(rows), len(rows)


async def _render_battle_question(bot, battle_id: str, application: Application | None = None) -> None:
    battle = _get_battle(battle_id)
    if not battle:
        return
    if battle["status"] == "completed":
        await _render_battle_result(bot, battle_id)
        return
    with state_connection(STATE_DB) as con:
        item = con.execute(
            "SELECT question_id FROM battle_questions WHERE battle_id=? AND position=?",
            (battle_id, battle["current_position"]),
        ).fetchone()
        counts = con.execute(
            "SELECT COUNT(*) FROM battle_answers WHERE battle_id=? AND position=?",
            (battle_id, battle["current_position"]),
        ).fetchone()[0]
    if not item:
        return
    row = _question(item["question_id"])
    if not row:
        logger.error("Missing battle question %s", item["question_id"])
        return
    position = int(battle["current_position"])
    text = _format_mcq(row, f"⚔️ BATTLE · QUESTION {position + 1}/{battle['total_questions']}")
    text += (
        f"\n\n<b>{_esc(battle['challenger_name'])}</b>: "
        f"{'✅ Answer submitted' if _has_battle_answer(battle_id, battle['challenger_id'], position) else '⏳ Answering'}"
        f"\n<b>{_esc(battle['opponent_name'])}</b>: "
        f"{'✅ Answer submitted' if _has_battle_answer(battle_id, battle['opponent_id'], position) else '⏳ Answering'}"
        "\n\n<i>Answer correctness stays hidden until the match ends.</i>"
    )
    markup = _battle_answer_keyboard(battle_id, position)
    await _edit_battle(bot, battle, text, markup)
    if application and application.job_queue:
        application.job_queue.run_once(
                battle_question_timeout,
                when=BATTLE_QUESTION_SECONDS,
                data={"battle_id": battle_id, "position": position},
                name=f"battle-timeout-{battle_id}-{position}",
            )


def _has_battle_answer(battle_id: str, user_id: str, position: int) -> bool:
    with state_connection(STATE_DB) as con:
        row = con.execute(
            "SELECT 1 FROM battle_answers WHERE battle_id=? AND user_id=? AND position=?",
            (battle_id, user_id, position),
        ).fetchone()
    return row is not None


def _store_battle_answer(battle_id: str, user_id: str, position: int, option: str) -> tuple[str, dict | None]:
    battle = _get_battle(battle_id)
    if not battle or battle["status"] != "active" or int(battle["current_position"]) != position:
        return "inactive", battle
    if user_id not in (battle["challenger_id"], battle["opponent_id"]):
        return "not_player", battle
    with state_connection(STATE_DB) as con:
        exists = con.execute(
            "SELECT 1 FROM battle_answers WHERE battle_id=? AND user_id=? AND position=?",
            (battle_id, user_id, position),
        ).fetchone()
        if exists:
            return "duplicate", dict(con.execute("SELECT * FROM battles WHERE battle_id=?", (battle_id,)).fetchone())
        qrow = con.execute(
            "SELECT question_id FROM battle_questions WHERE battle_id=? AND position=?",
            (battle_id, position),
        ).fetchone()
        if not qrow:
            return "inactive", battle
        question_id = qrow["question_id"]
        row = _question(question_id)
        if not row:
            return "inactive", battle
        correct = str(row.get("correct_option_key") or "").upper()
        elapsed = max(0, int((_now() - float(battle.get("question_started_at") or _now())) * 1000))
        con.execute("""
            INSERT INTO battle_answers(
                battle_id,user_id,position,selected_option,is_correct,elapsed_ms,answered_at
            ) VALUES(?,?,?,?,?,?,?)
        """, (battle_id, user_id, position, option, int(option == correct), elapsed, _now()))
        answer_count = con.execute(
            "SELECT COUNT(*) FROM battle_answers WHERE battle_id=? AND position=?",
            (battle_id, position),
        ).fetchone()[0]
        if answer_count < 2:
            return "waiting", dict(con.execute("SELECT * FROM battles WHERE battle_id=?", (battle_id,)).fetchone())
        if position + 1 >= int(battle["total_questions"]):
            con.execute(
                "UPDATE battles SET status='completed',finished_at=? WHERE battle_id=? AND status='active' AND current_position=?",
                (_now(), battle_id, position),
            )
            return "completed", dict(con.execute("SELECT * FROM battles WHERE battle_id=?", (battle_id,)).fetchone())
        con.execute("""
            UPDATE battles SET current_position=?,question_started_at=?
            WHERE battle_id=? AND status='active' AND current_position=?
        """, (position + 1, _now(), battle_id, position))
        return "advance", dict(con.execute("SELECT * FROM battles WHERE battle_id=?", (battle_id,)).fetchone())


async def _render_battle_result(bot, battle_id: str) -> None:
    battle = _get_battle(battle_id)
    if not battle:
        return
    with state_connection(STATE_DB) as con:
        rows = con.execute("""
            SELECT user_id,SUM(is_correct) correct,COUNT(*) answered,SUM(elapsed_ms) elapsed
            FROM battle_answers WHERE battle_id=? GROUP BY user_id
        """, (battle_id,)).fetchall()
    scores = {r["user_id"]: dict(r) for r in rows}
    a = scores.get(battle["challenger_id"], {"correct": 0, "answered": 0, "elapsed": 0})
    b = scores.get(battle["opponent_id"], {"correct": 0, "answered": 0, "elapsed": 0})
    if int(a["correct"]) > int(b["correct"]):
        winner = battle["challenger_name"]
        winner_id = battle["challenger_id"]
    elif int(b["correct"]) > int(a["correct"]):
        winner = battle["opponent_name"]
        winner_id = battle["opponent_id"]
    elif int(a["elapsed"] or 0) < int(b["elapsed"] or 0):
        winner, winner_id = battle["challenger_name"], battle["challenger_id"]
    elif int(b["elapsed"] or 0) < int(a["elapsed"] or 0):
        winner, winner_id = battle["opponent_name"], battle["opponent_id"]
    else:
        winner, winner_id = "Draw", None
    for uid, stat in ((battle["challenger_id"], a), (battle["opponent_id"], b)):
        with state_connection(STATE_DB) as con:
            inserted = con.execute(
                "INSERT OR IGNORE INTO battle_awards(battle_id,user_id) VALUES(?,?)",
                (battle_id, uid),
            )
            if inserted.rowcount == 1:
                con.execute("""
                    INSERT INTO battle_player_totals(user_id,battles_played,wins,correct_total,elapsed_total)
                    VALUES(?,1,?,?,?)
                    ON CONFLICT(user_id) DO UPDATE SET
                      battles_played=battles_played+1,wins=wins+excluded.wins,
                      correct_total=correct_total+excluded.correct_total,
                      elapsed_total=elapsed_total+excluded.elapsed_total
                """, (uid, int(winner_id == uid), int(stat["correct"]), int(stat["elapsed"] or 0)))
    text = (
        "🏁 <b>BATTLE COMPLETED</b>\n\n"
        + (f"🏆 <b>Winner: {_esc(winner)}</b>\n\n" if winner_id else "🤝 <b>It's a draw!</b>\n\n")
        + f"<b>{_esc(battle['challenger_name'])}</b> — {int(a['correct'])}/{battle['total_questions']} correct\n"
        + f"<b>{_esc(battle['opponent_name'])}</b> — {int(b['correct'])}/{battle['total_questions']} correct\n\n"
        + "<i>Battle points update community ranking only, never the official channel-event leaderboard.</i>"
    )
    markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("⚔️ Rematch", callback_data=f"rm|{battle['subject_id']}")
    ]])
    await _edit_battle(bot, battle, text, markup)


async def battle_question_timeout(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = context.job.data
    battle_id, position = data["battle_id"], int(data["position"])
    battle = _get_battle(battle_id)
    if not battle or battle["status"] != "active" or int(battle["current_position"]) != position:
        return
    with state_connection(STATE_DB) as con:
        for uid in (battle["challenger_id"], battle["opponent_id"]):
            exists = con.execute(
                "SELECT 1 FROM battle_answers WHERE battle_id=? AND user_id=? AND position=?",
                (battle_id, uid, position),
            ).fetchone()
            if not exists:
                con.execute("""
                    INSERT INTO battle_answers(
                      battle_id,user_id,position,selected_option,is_correct,elapsed_ms,answered_at
                    ) VALUES(?,?,?,NULL,0,?,?)
                """, (battle_id, uid, position, BATTLE_QUESTION_SECONDS * 1000, _now()))
        if position + 1 >= int(battle["total_questions"]):
            con.execute(
                "UPDATE battles SET status='completed',finished_at=? WHERE battle_id=? AND status='active' AND current_position=?",
                (_now(), battle_id, position),
            )
        else:
            con.execute(
                "UPDATE battles SET current_position=?,question_started_at=? WHERE battle_id=? AND status='active' AND current_position=?",
                (position + 1, _now(), battle_id, position),
            )
    latest = _get_battle(battle_id)
    if latest and latest["status"] == "completed":
        await _render_battle_result(context.bot, battle_id)
    else:
        await _render_battle_question(context.bot, battle_id, context.application)


async def _cancel_battle(query, battle_id: str, user_id: int) -> str:
    battle = _get_battle(battle_id)
    if not battle or battle["status"] != "open":
        return "Only an open challenge can be cancelled."
    if str(user_id) != battle["challenger_id"]:
        return "Only the challenger can cancel this challenge."
    with state_connection(STATE_DB) as con:
        con.execute(
            "UPDATE battles SET status='cancelled',finished_at=? WHERE battle_id=? AND status='open'",
            (_now(), battle_id),
        )
    await query.edit_message_text("Challenge cancelled by its creator.")
    return "Challenge cancelled."


async def _create_inline_battle(query, subject_id: str, user, application: Application) -> str:
    subject = get_subject(QUESTION_DB, subject_id)
    if not subject:
        return "Subject not found."
    battle = _create_battle_record(None, query.inline_message_id, user, subject)
    await query.edit_message_text(
        _battle_card_text(battle), parse_mode=ParseMode.HTML, reply_markup=_battle_keyboard(battle)
    )
    _schedule_challenge_expiry(application, battle["battle_id"])
    return "Public challenge card activated in this chat."


async def community_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = (query.data or "").split("|")
    action = data[0]
    try:
        if action == "cs" and len(data) == 2:
            subject = get_subject(QUESTION_DB, data[1])
            if not subject:
                await query.answer("Subject not found.", show_alert=True)
                return
            await query.answer()
            await _post_open_battle(update, subject, context.application)
            return
        if action == "ic" and len(data) == 3:
            if str(update.effective_user.id) != data[2]:
                await query.answer("Only the person who shared this challenge card can activate it.", show_alert=True)
                return
            await query.answer(
                await _create_inline_battle(query, data[1], update.effective_user, context.application)
            )
            return
        if action == "ac" and len(data) == 2:
            result = await _accept_battle(query, data[1], update.effective_user, context.application)
            await query.answer(result, show_alert=("already" in result.lower() or "only" in result.lower()))
            return
        if action == "rd" and len(data) == 3:
            result = await _ready_battle(query, data[1], data[2], update.effective_user.id, context.application)
            await query.answer(result)
            return
        if action == "ba" and len(data) == 4:
            result, battle = _store_battle_answer(data[1], str(update.effective_user.id), int(data[2]), data[3])
            if result == "not_player":
                await query.answer("Only the two players can answer.", show_alert=True)
            elif result == "duplicate":
                await query.answer("Your answer is already recorded.")
            elif result == "inactive":
                await query.answer("This question is closed.", show_alert=True)
            elif result == "waiting":
                await query.answer("Answer recorded. Waiting for your opponent.")
                battle = _get_battle(data[1])
                with state_connection(STATE_DB) as con:
                    qrow = con.execute(
                        "SELECT question_id FROM battle_questions WHERE battle_id=? AND position=?",
                        (data[1], int(data[2])),
                    ).fetchone()
                qdata = _question(qrow["question_id"]) if qrow else None
                text = _format_mcq(
                    qdata, f"⚔️ BATTLE · QUESTION {int(battle['current_position']) + 1}/{battle['total_questions']}"
                ) if qdata and battle else "⚔️ Battle answer received."
                if battle:
                    text += (
                        f"\n\n{_esc(battle['challenger_name'])}: "
                        f"{'✅ Submitted' if _has_battle_answer(data[1], battle['challenger_id'], int(data[2])) else '⏳ Waiting'}"
                        f"\n{_esc(battle['opponent_name'])}: "
                        f"{'✅ Submitted' if _has_battle_answer(data[1], battle['opponent_id'], int(data[2])) else '⏳ Waiting'}"
                        "\n\n<i>Waiting for both answers. Correctness is hidden.</i>"
                    )
                await query.edit_message_text(
                    text, parse_mode=ParseMode.HTML,
                    reply_markup=_battle_answer_keyboard(data[1], int(data[2])),
                )
            elif result == "advance":
                await query.answer("Both answers recorded. Next question!")
                await _render_battle_question(context.bot, data[1], context.application)
            elif result == "completed":
                await query.answer("Battle completed!")
                await _render_battle_result(context.bot, data[1])
            return
        if action == "oc" and len(data) == 4:
            result = _record_official_answer(data[1], str(update.effective_user.id), int(data[2]), data[3], update.effective_user)
            await query.answer(result, show_alert=result != "Answer recorded privately.")
            return
        if action == "ca" and len(data) == 2:
            result = await _cancel_battle(query, data[1], update.effective_user.id)
            await query.answer(result)
            return
        if action == "rm" and len(data) == 2:
            subject = get_subject(QUESTION_DB, data[1])
            if subject and update.effective_chat:
                await query.answer("Creating a new open rematch challenge.")
                await _post_open_battle(update, subject, context.application)
            else:
                await query.answer("Rematch unavailable.", show_alert=True)
            return
        await query.answer("Unknown action.", show_alert=True)
    except Exception:
        logger.exception("Community callback failed: %s", query.data)
        try:
            await query.answer("Something went wrong. Please try again.", show_alert=True)
        except Exception:
            pass


async def ids_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        f"Your Telegram user ID: `{update.effective_user.id}`\n"
        f"Current chat ID: `{update.effective_chat.id}`\n\n"
        "Use these values in ADMIN_USER_IDS, PUBLIC_CHANNEL_ID, or DISCUSSION_GROUP_ID as appropriate."
    )


async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    _upsert_profile(user)
    stats = get_user_stats(STATE_DB, user.id)
    with state_connection(STATE_DB) as con:
        totals = con.execute(
            "SELECT battles_played,wins,correct_total FROM battle_player_totals WHERE user_id=?",
            (str(user.id),),
        ).fetchone()
    played = int(totals["battles_played"]) if totals else 0
    wins = int(totals["wins"]) if totals else 0
    accuracy = (100 * stats["correct"] / stats["answered"]) if stats["answered"] else 0
    text = (
        f"👤 <b>{_esc(user.full_name)}</b>\n"
        f"Telegram ID: <code>{user.id}</code>\n\n"
        f"📚 Practice: <b>{stats['quizzes']} quizzes</b> · {stats['answered']} questions\n"
        f"🎯 Accuracy: <b>{accuracy:.1f}%</b>\n"
        f"⚔️ Battles: <b>{played}</b> · Wins: <b>{wins}</b>"
    )
    rows = []
    if MINI_APP_URL and update.effective_chat and update.effective_chat.type == ChatType.PRIVATE:
        rows.append([InlineKeyboardButton("📊 Open Modern Dashboard", web_app=WebAppInfo(url=MINI_APP_URL))])
    elif MINI_APP_URL:
        rows.append([InlineKeyboardButton("📊 Open Dashboard", url=MINI_APP_URL)])
    await update.effective_message.reply_text(
        text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows) if rows else None
    )


async def top_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    with state_connection(STATE_DB) as con:
        rows = con.execute("""
            SELECT p.user_id,p.display_name,p.username,t.battles_played,t.wins,t.correct_total
            FROM battle_player_totals t JOIN community_profiles p ON p.user_id=t.user_id
            ORDER BY t.wins DESC,t.correct_total DESC,t.battles_played DESC LIMIT 10
        """).fetchall()
    if not rows:
        await update.effective_message.reply_text("Abhi community battle ranking mein koi completed match nahi hai.")
        return
    lines = ["🏆 <b>COMMUNITY BATTLE LEADERBOARD</b>"]
    for index, row in enumerate(rows, 1):
        name = ("@" + row["username"]) if row["username"] else row["display_name"]
        lines.append(f"{index}. {_esc(name)} — {row['wins']} wins / {row['battles_played']} battles")
    lines.append("\n<i>Only real completed player battles count here. Official channel-event ranking is separate.</i>")
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def battle_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = str(update.effective_user.id)
    with state_connection(STATE_DB) as con:
        row = con.execute("""
            SELECT * FROM battles
            WHERE (challenger_id=? OR opponent_id=?) AND status IN ('open','accepted','countdown','active')
            ORDER BY created_at DESC LIMIT 1
        """, (uid, uid)).fetchone()
    if not row:
        await update.effective_message.reply_text("You have no active challenge or battle.")
        return
    battle = dict(row)
    await update.effective_message.reply_text(
        _battle_card_text(battle) if battle["status"] in ("open", "accepted") else
        f"⚔️ Battle {battle['battle_id']} · {battle['status'].upper()}\nQuestion {battle['current_position'] + 1}/{battle['total_questions']}",
        parse_mode=ParseMode.HTML,
    )


async def events_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not CHANNEL_ID:
        await update.effective_message.reply_text("Official channel is not configured yet. Set PUBLIC_CHANNEL_ID in .env.")
        return
    local_now = datetime.now(TZ)
    start = local_now.replace(hour=EVENT_HOUR, minute=EVENT_MINUTE, second=0, microsecond=0)
    if start <= local_now:
        start = start.replace(day=local_now.day)  # display today's scheduled time as already passed
        message = f"Next Daily Championship: tomorrow at {EVENT_HOUR:02d}:{EVENT_MINUTE:02d} IST."
    else:
        message = f"Next Daily Championship: today at {EVENT_HOUR:02d}:{EVENT_MINUTE:02d} IST."
    if DISCUSSION_GROUP_LINK:
        message += f"\nDiscussion group: {DISCUSSION_GROUP_LINK}"
    await update.effective_message.reply_text(message)


async def _post_event_reminder(bot) -> None:
    if not CHANNEL_ID:
        return
    keyboard = []
    if DISCUSSION_GROUP_LINK:
        keyboard.append([InlineKeyboardButton("💬 Join Discussion Group", url=DISCUSSION_GROUP_LINK)])
    text = (
        "⏰ <b>MPSC DAILY CHAMPIONSHIP</b>\n\n"
        f"Official MCQ event starts in 10 minutes — {EVENT_QUESTION_COUNT} questions.\n"
        "Only answers submitted during this official channel event count toward its leaderboard."
    )
    await bot.send_message(chat_id=CHANNEL_ID, text=text, parse_mode=ParseMode.HTML,
                           reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None)
    if DISCUSSION_GROUP_ID:
        group_markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("📢 Open Official Channel", url=CHANNEL_LINK)
        ]]) if CHANNEL_LINK else None
        await bot.send_message(
            chat_id=DISCUSSION_GROUP_ID,
            text="⏰ <b>MPSC Daily Championship in 10 minutes!</b>\nJoin the official channel and take part in today's timed MCQ event.",
            parse_mode=ParseMode.HTML,
            reply_markup=group_markup,
        )


async def reminder_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await _post_event_reminder(context.bot)
    except Exception:
        logger.exception("Daily event reminder failed")


async def daily_event_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await start_official_event(context.application)
    except Exception:
        logger.exception("Scheduled official event failed")


async def start_official_event(application: Application, channel_id: str | None = None, manual: bool = False) -> str:
    bot = application.bot
    target = channel_id or CHANNEL_ID
    if not target:
        return "Set PUBLIC_CHANNEL_ID in .env before starting official events."
    if not application.job_queue:
        return "Install python-telegram-bot[job-queue] to enable timed question publishing."
    with state_connection(STATE_DB) as con:
        active = con.execute(
            "SELECT event_id FROM official_events WHERE status='running' LIMIT 1"
        ).fetchone()
    if active:
        return f"An official event is already running (ID {active['event_id']})."
    qids = _select_question_ids(None, EVENT_QUESTION_COUNT)
    if len(qids) < 5:
        return "Not enough answer-key-complete questions in the question bank."
    event_id = secrets.token_hex(4)
    local_date = datetime.now(TZ).date().isoformat()
    stamp = _now()
    with state_connection(STATE_DB) as con:
        con.execute("""
            INSERT INTO official_events(event_id,event_date,channel_id,status,total_questions,created_at,started_at)
            VALUES(?,?,?,'running',?,?,?)
        """, (event_id, local_date, str(target), len(qids), stamp, stamp))
        con.executemany(
            "INSERT INTO official_event_questions(event_id,position,question_id) VALUES(?,?,?)",
            [(event_id, i, qid) for i, qid in enumerate(qids)],
        )
    label = "TEST EVENT" if manual else "DAILY CHAMPIONSHIP"
    keyboard = []
    if DISCUSSION_GROUP_LINK:
        keyboard.append([InlineKeyboardButton("💬 Join Discussion Group", url=DISCUSSION_GROUP_LINK)])
    await bot.send_message(
        chat_id=target,
        text=(
            f"🏆 <b>MPSC {label} IS LIVE</b>\n\n"
            f"Event ID: <code>{event_id}</code>\n"
            f"{len(qids)} timed MCQs · {EVENT_QUESTION_SECONDS}s per question\n"
            "Select your answer on each question post. One answer per account per question.\n"
            "Final leaderboard appears automatically after the last question."
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None,
    )
    if DISCUSSION_GROUP_ID:
        group_markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("📢 Open Official Channel", url=CHANNEL_LINK)
        ]]) if CHANNEL_LINK else None
        await bot.send_message(
            chat_id=DISCUSSION_GROUP_ID,
            text=f"🏆 <b>MPSC {label} is LIVE!</b>\n{len(qids)} timed questions are being published in the official channel now.",
            parse_mode=ParseMode.HTML,
            reply_markup=group_markup,
        )
    job_queue = application.job_queue
    for position in range(len(qids)):
        job_queue.run_once(
            publish_event_question,
            when=1 + position * EVENT_QUESTION_SECONDS,
            data={"event_id": event_id, "position": position},
            name=f"event-question-{event_id}-{position}",
        )
    job_queue.run_once(
        finish_event_job,
        when=1 + len(qids) * EVENT_QUESTION_SECONDS + 2,
        data={"event_id": event_id},
        name=f"event-finish-{event_id}",
    )
    return f"Official event {event_id} started in {target}."


async def publish_event_question(context: ContextTypes.DEFAULT_TYPE) -> None:
    event_id = context.job.data["event_id"]
    position = int(context.job.data["position"])
    with state_connection(STATE_DB) as con:
        event = con.execute("SELECT * FROM official_events WHERE event_id=?", (event_id,)).fetchone()
        item = con.execute(
            "SELECT question_id FROM official_event_questions WHERE event_id=? AND position=?",
            (event_id, position),
        ).fetchone()
    if not event or event["status"] != "running" or not item:
        return
    row = _question(item["question_id"])
    if not row:
        return
    posted = _now()
    with state_connection(STATE_DB) as con:
        con.execute("""
            UPDATE official_event_questions SET posted_at=?,closes_at=?
            WHERE event_id=? AND position=?
        """, (posted, posted + EVENT_QUESTION_SECONDS, event_id, position))
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("A", callback_data=f"oc|{event_id}|{position}|A"),
         InlineKeyboardButton("B", callback_data=f"oc|{event_id}|{position}|B")],
        [InlineKeyboardButton("C", callback_data=f"oc|{event_id}|{position}|C"),
         InlineKeyboardButton("D", callback_data=f"oc|{event_id}|{position}|D")],
    ])
    message = await context.bot.send_message(
        chat_id=event["channel_id"],
        text=_format_mcq(row, f"🏆 OFFICIAL EVENT · Q{position + 1}/{event['total_questions']}"),
        parse_mode=ParseMode.HTML,
        reply_markup=markup,
    )
    with state_connection(STATE_DB) as con:
        con.execute(
            "UPDATE official_event_questions SET message_id=? WHERE event_id=? AND position=?",
            (message.message_id, event_id, position),
        )


def _record_official_answer(event_id: str, user_id: str, position: int, option: str, user) -> str:
    with state_connection(STATE_DB) as con:
        event = con.execute("SELECT * FROM official_events WHERE event_id=?", (event_id,)).fetchone()
        item = con.execute("""
            SELECT question_id,posted_at,closes_at FROM official_event_questions
            WHERE event_id=? AND position=?
        """, (event_id, position)).fetchone()
    if not event or event["status"] != "running" or not item or not item["posted_at"]:
        return "This event question is not open."
    if _now() > float(item["closes_at"]):
        return "Time is up for this question."
    row = _question(item["question_id"])
    if not row:
        return "Question unavailable."
    _upsert_profile(user)
    correct = str(row.get("correct_option_key") or "").upper()
    elapsed = int((_now() - float(item["posted_at"])) * 1000)
    try:
        with state_connection(STATE_DB) as con:
            con.execute("""
                INSERT INTO official_event_answers(
                  event_id,user_id,position,selected_option,is_correct,elapsed_ms,answered_at
                ) VALUES(?,?,?,?,?,?,?)
            """, (event_id, user_id, position, option, int(option == correct), elapsed, _now()))
    except Exception as exc:
        if "UNIQUE" in str(exc).upper():
            return "Your answer for this question is already recorded."
        raise
    return "Answer recorded privately."


async def finish_official_event(bot, event_id: str) -> None:
    with state_connection(STATE_DB) as con:
        event = con.execute("SELECT * FROM official_events WHERE event_id=?", (event_id,)).fetchone()
        if not event or event["status"] != "running":
            return
        con.execute(
            "UPDATE official_events SET status='completed',finished_at=? WHERE event_id=? AND status='running'",
            (_now(), event_id),
        )
        rows = con.execute("""
            SELECT a.user_id,COUNT(*) answered,SUM(a.is_correct) correct,SUM(a.elapsed_ms) elapsed,
                   p.display_name,p.username
            FROM official_event_answers a
            LEFT JOIN community_profiles p ON p.user_id=a.user_id
            WHERE a.event_id=?
            GROUP BY a.user_id
            ORDER BY correct DESC,elapsed ASC
        """, (event_id,)).fetchall()
    eligible = [r for r in rows if int(r["answered"]) == int(event["total_questions"])]
    lines = ["🏆 <b>OFFICIAL DAILY LEADERBOARD</b>", f"Event: <code>{event_id}</code>", ""]
    if not eligible:
        lines.append("No complete eligible attempts were recorded today.")
    else:
        for rank, row in enumerate(eligible[:20], 1):
            name = ("@" + row["username"]) if row["username"] else row["display_name"] or "Student"
            points = int(row["correct"]) * 100
            medal = "🥇" if rank == 1 else ("🥈" if rank == 2 else ("🥉" if rank == 3 else f"{rank}."))
            lines.append(
                f"{medal} {_esc(name)} — <b>{row['correct']}/{event['total_questions']}</b> · {points} pts"
            )
        lines.append("\n<i>Ranking is based only on completed official channel-event attempts. "
                     "Ties are resolved by lower total answer time.</i>")
        lines.append("\n💬 Use the discussion group for battles and explanations.")
    result_text = "\n".join(lines)
    await bot.send_message(chat_id=event["channel_id"], text=result_text, parse_mode=ParseMode.HTML)
    if DISCUSSION_GROUP_ID:
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("🏆 View Official Channel", url=CHANNEL_LINK)
        ]]) if CHANNEL_LINK else None
        await bot.send_message(
            chat_id=DISCUSSION_GROUP_ID,
            text=result_text,
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
        )


async def finish_event_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    await finish_official_event(context.bot, context.job.data["event_id"])


async def event_now_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        await update.effective_message.reply_text("This command is admin-only.")
        return
    result = await start_official_event(context.application, manual=True)
    await update.effective_message.reply_text(result)


async def event_stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        await update.effective_message.reply_text("This command is admin-only.")
        return
    with state_connection(STATE_DB) as con:
        row = con.execute(
            "SELECT event_id,channel_id FROM official_events WHERE status='running' ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        if not row:
            await update.effective_message.reply_text("No event is currently running.")
            return
        con.execute(
            "UPDATE official_events SET status='cancelled',finished_at=? WHERE event_id=?",
            (_now(), row["event_id"]),
        )
    await context.bot.send_message(chat_id=row["channel_id"], text="The official event was stopped by an administrator.")
    await update.effective_message.reply_text("Event stopped.")


async def event_top_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    with state_connection(STATE_DB) as con:
        event = con.execute(
            "SELECT * FROM official_events ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        if not event:
            await update.effective_message.reply_text("No official event results yet.")
            return
        rows = con.execute("""
            SELECT a.user_id,COUNT(*) answered,SUM(a.is_correct) correct,SUM(a.elapsed_ms) elapsed,
                   p.display_name,p.username
            FROM official_event_answers a LEFT JOIN community_profiles p ON p.user_id=a.user_id
            WHERE a.event_id=? GROUP BY a.user_id
            ORDER BY correct DESC,elapsed ASC LIMIT 10
        """, (event["event_id"],)).fetchall()
    lines = [f"🏆 <b>Latest Official Event · {event['event_date']}</b>"]
    for rank, row in enumerate(rows, 1):
        name = ("@" + row["username"]) if row["username"] else row["display_name"] or "Student"
        lines.append(f"{rank}. {_esc(name)} — {row['correct']}/{event['total_questions']}")
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def inline_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = (update.inline_query.query or "").strip().lower()
    subjects = get_subjects(QUESTION_DB)
    results = []
    for subject in subjects:
        key = subject["subject_slug"]
        if query and query not in key and query not in subject["subject_name_en"].lower():
            continue
        title = f"⚔️ Open {subject['subject_name_en']} challenge"
        results.append(InlineQueryResultArticle(
            id=str(subject["subject_id"]),
            title=title,
            description="Share a public challenge card in this chat.",
            input_message_content=InputTextMessageContent(
                f"⚔️ MPSC {subject['subject_name_en']} Challenge\nTap below to open a public challenge."
            ),
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "Create Open Challenge",
                    callback_data=f"ic|{subject['subject_id']}|{update.effective_user.id}",
                )
            ]]),
        ))
    await update.inline_query.answer(results[:20], cache_time=10, is_personal=True)


def setup_daily_jobs(application: Application) -> None:
    if not CHANNEL_ID:
        logger.warning("PUBLIC_CHANNEL_ID is not set; daily channel events are disabled.")
        return
    if not application.job_queue:
        logger.warning("PTB JobQueue is missing; install python-telegram-bot[job-queue].")
        return
    reminder_dt = datetime.now(TZ).replace(
        hour=EVENT_HOUR, minute=EVENT_MINUTE, second=0, microsecond=0
    ) - timedelta(minutes=10)
    reminder_time = day_time(hour=reminder_dt.hour, minute=reminder_dt.minute, tzinfo=TZ)
    application.job_queue.run_daily(
        reminder_job, time=reminder_time, name="daily-event-reminder",
    )
    application.job_queue.run_daily(
        daily_event_job, time=day_time(hour=EVENT_HOUR, minute=EVENT_MINUTE, tzinfo=TZ),
        name="daily-official-event",
    )
    logger.info("Daily official events scheduled for %02d:%02d Asia/Kolkata.", EVENT_HOUR, EVENT_MINUTE)


def register_community_handlers(application: Application) -> None:
    initialize_community_db(STATE_DB)
    application.add_handler(CommandHandler("challenge", challenge_command))
    application.add_handler(CommandHandler("duel", duel_command))
    application.add_handler(CommandHandler("profile", profile_command))
    application.add_handler(CommandHandler("ids", ids_command))
    application.add_handler(CommandHandler("top", top_command))
    application.add_handler(CommandHandler("rank", top_command))
    application.add_handler(CommandHandler("battle", battle_status_command))
    application.add_handler(CommandHandler("events", events_command))
    application.add_handler(CommandHandler("event_now", event_now_command))
    application.add_handler(CommandHandler("event_stop", event_stop_command))
    application.add_handler(CommandHandler("event_top", event_top_command))
    application.add_handler(InlineQueryHandler(inline_query_handler))
    application.add_handler(CallbackQueryHandler(
        community_callback, pattern=r"^(cs|ac|rd|ba|oc|ca|ic|rm)\|"
    ))
