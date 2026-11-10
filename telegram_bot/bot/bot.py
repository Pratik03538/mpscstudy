from __future__ import annotations

import html
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import NetworkError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from db import (
    advance_session, create_quiz_session, get_question, get_subject,
    get_subjects, get_user_stats, initialize_state_db, record_answer, stop_session,
)

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
QUESTION_DB = Path(os.getenv("BOT_DB_PATH", str(ROOT / "database" / "mpsc_subject_only.db"))).resolve()
STATE_DB = Path(os.getenv("BOT_STATE_DB_PATH", str(ROOT / "data" / "bot_progress.sqlite3"))).resolve()
LOG_DIR = ROOT / "outputs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(LOG_DIR / "bot.log", encoding="utf-8")],
)
# httpx logs full request URLs at INFO, and Telegram Bot API URLs contain the
# bot token in the path. Suppress those logs so tokens are not written to bot.log.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger("mpsc-study-bot")


def esc(value: object) -> str:
    return html.escape(str(value or ""), quote=False)


def home_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📚 Choose Subject", callback_data="subjects")],
        [InlineKeyboardButton("📊 My Progress", callback_data="stats"),
         InlineKeyboardButton("ℹ️ Help", callback_data="help")],
    ])


def subject_keyboard() -> InlineKeyboardMarkup:
    subjects = get_subjects(QUESTION_DB)
    rows = []
    for index in range(0, len(subjects), 2):
        rows.append([
            InlineKeyboardButton(
                f"{row['subject_name_en']} ({row['question_count']:,})",
                callback_data=f"s|{row['subject_id']}",
            )
            for row in subjects[index:index + 2]
        ])
    rows.append([InlineKeyboardButton("🏠 Home", callback_data="home")])
    return InlineKeyboardMarkup(rows)


def subject_detail_keyboard(subject_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("10 MCQs", callback_data=f"q|{subject_id}|10"),
         InlineKeyboardButton("20 MCQs", callback_data=f"q|{subject_id}|20")],
        [InlineKeyboardButton("50 MCQs", callback_data=f"q|{subject_id}|50")],
        [InlineKeyboardButton("⬅️ Subjects", callback_data="subjects"),
         InlineKeyboardButton("🏠 Home", callback_data="home")],
    ])


def option_keyboard(session_id: int, position: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("A", callback_data=f"a|{session_id}|{position}|A"),
         InlineKeyboardButton("B", callback_data=f"a|{session_id}|{position}|B")],
        [InlineKeyboardButton("C", callback_data=f"a|{session_id}|{position}|C"),
         InlineKeyboardButton("D", callback_data=f"a|{session_id}|{position}|D")],
        [InlineKeyboardButton("⏹ Stop Quiz", callback_data=f"x|{session_id}")],
    ]
    return InlineKeyboardMarkup(rows)


def next_keyboard(session_id: int, position: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➡️ Next Question", callback_data=f"n|{session_id}|{position}")],
        [InlineKeyboardButton("⏹ Stop Quiz", callback_data=f"x|{session_id}")],
    ])


def format_question(question: dict, position: int, total: int) -> str:
    subject = esc(question.get("subject_name_en") or question.get("subject_name_mr"))
    header = f"<b>{subject}</b> | Question {position + 1}/{total} | ID: {esc(question.get('question_id'))}"
    q_mr, q_en = esc(question.get("question_mr")), esc(question.get("question_en"))
    question_text = "\n\n".join(x for x in (q_mr, q_en if q_en and q_en != q_mr else "") if x)
    options = (
        "<b>A.</b> " + esc(question.get("option_a")) + "\n" +
        "<b>B.</b> " + esc(question.get("option_b")) + "\n" +
        "<b>C.</b> " + esc(question.get("option_c")) + "\n" +
        "<b>D.</b> " + esc(question.get("option_d"))
    )
    max_question_chars = max(300, 3900 - len(header) - len(options) - 6)
    if len(question_text) > max_question_chars:
        question_text = question_text[:max_question_chars - 1] + "…"
    return header + "\n\n" + question_text + "\n\n" + options


async def show_home(update: Update, edit: bool = False) -> None:
    subjects = get_subjects(QUESTION_DB)
    total = sum(int(s["question_count"]) for s in subjects)
    first_name = update.effective_user.first_name if update.effective_user else "Student"
    text = (
        f"<b>Welcome to MPSC Study Bot, {esc(first_name)}!</b>\n\n"
        f"📚 Subjects: <b>{len(subjects)}</b>\n"
        f"📝 Available MCQs: <b>{total:,}</b>\n\nChoose a subject to practice."
    )
    if edit and update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=home_keyboard())
    else:
        await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=home_keyboard())


async def show_subjects(update: Update, edit: bool = True) -> None:
    text = "<b>Choose a subject</b>\nMCQ counts are shown beside each subject."
    if edit and update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=subject_keyboard())
    else:
        await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=subject_keyboard())


async def show_subject_detail(update: Update, subject_id: str) -> None:
    subject = get_subject(QUESTION_DB, subject_id)
    if not subject:
        await update.callback_query.edit_message_text("Subject not found.", reply_markup=subject_keyboard())
        return
    text = (
        f"<b>{esc(subject.get('subject_name_en'))}</b>\n"
        f"{esc(subject.get('subject_name_mr'))}\n\n"
        f"Available MCQs: <b>{int(subject['question_count']):,}</b>\n\nSelect quiz length:"
    )
    await update.callback_query.edit_message_text(
        text, parse_mode=ParseMode.HTML, reply_markup=subject_detail_keyboard(subject_id)
    )


async def send_question(update: Update, context: ContextTypes.DEFAULT_TYPE, session_id: int, position: int) -> None:
    question = get_question(QUESTION_DB, STATE_DB, session_id, position, update.effective_user.id)
    if not question:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="Quiz session not found or already closed. Use /start to begin again.",
        )
        return
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=format_question(question, position, int(question["session"]["total_questions"])),
        parse_mode=ParseMode.HTML,
        reply_markup=option_keyboard(session_id, position),
    )


async def show_stats(update: Update, edit: bool = False) -> None:
    stats = get_user_stats(STATE_DB, update.effective_user.id)
    accuracy = 100 * stats["correct"] / stats["answered"] if stats["answered"] else 0
    lines = [
        "<b>Your MPSC practice progress</b>",
        f"Quizzes started: <b>{stats['quizzes']}</b>",
        f"Questions answered: <b>{stats['answered']}</b>",
        f"Correct answers: <b>{stats['correct']}</b>",
        f"Accuracy: <b>{accuracy:.1f}%</b>",
    ]
    if stats["subject_stats"]:
        lines.append("\n<b>Subject-wise performance</b>")
        for row in stats["subject_stats"]:
            pct = 100 * row["correct"] / row["answered"] if row["answered"] else 0
            lines.append(f"• {esc(row['subject_name'])}: {row['correct']}/{row['answered']} ({pct:.1f}%)")
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("🏠 Home", callback_data="home")],
        [InlineKeyboardButton("📚 Subjects", callback_data="subjects")],
    ])
    text = "\n".join(lines)
    if edit and update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
    else:
        await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_home(update)


async def subjects_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_subjects(update, edit=False)


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_stats(update, edit=False)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "<b>Commands</b>\n/start — Main menu\n/subjects — Browse subjects\n/stats — Your score\n/help — Help\n\n"
        "Choose a subject, select 10/20/50 MCQs, then tap A/B/C/D. The bot shows the stored answer key and any available explanation.",
        parse_mode=ParseMode.HTML,
        reply_markup=home_keyboard(),
    )


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = (query.data or "").split("|")
    action = data[0]
    user_id = update.effective_user.id
    try:
        if action == "home":
            await show_home(update, edit=True)
        elif action == "subjects":
            await show_subjects(update)
        elif action == "stats":
            await show_stats(update, edit=True)
        elif action == "help":
            await query.edit_message_text(
                "Use Subjects → choose a subject → select quiz length → answer A/B/C/D.",
                reply_markup=home_keyboard(),
            )
        elif action == "s" and len(data) == 2:
            await show_subject_detail(update, data[1])
        elif action == "q" and len(data) == 3:
            session_id, count = create_quiz_session(
                QUESTION_DB, STATE_DB, user_id, data[1], int(data[2])
            )
            await query.edit_message_text(
                f"✅ Quiz ready: <b>{count} MCQs</b>.",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("Start first question", callback_data=f"n|{session_id}|-1")
                ]]),
            )
        elif action == "a" and len(data) == 4:
            session_id, position, option = int(data[1]), int(data[2]), data[3]
            result = record_answer(QUESTION_DB, STATE_DB, session_id, position, user_id, option)
            question = result["question"]
            correct = result["correct_option_key"]
            status = (
                "You have already answered this question."
                if result["already_answered"]
                else ("✅ Correct!" if result["is_correct"] else "❌ Incorrect.")
            )
            answer_text = esc(question.get("correct_option_text") or question.get(f"option_{str(correct).lower()}"))
            response = f"{status}\n\nCorrect answer: <b>{esc(correct)}. {answer_text}</b>"
            explanation_mr = esc(question.get("explanation_mr"))
            explanation_en = esc(question.get("explanation_en"))
            if explanation_mr:
                response += f"\n\n<b>स्पष्टीकरण</b>\n{explanation_mr}"
            if explanation_en and explanation_en != explanation_mr:
                response += f"\n\n<b>Explanation</b>\n{explanation_en}"
            response += (
                "\n\n<i>Answer is taken from the stored answer key and has not yet been independently fact-checked.</i>"
            )
            await query.edit_message_text(
                response, parse_mode=ParseMode.HTML,
                reply_markup=next_keyboard(session_id, position),
            )
        elif action == "n" and len(data) == 3:
            session_id, position = int(data[1]), int(data[2])
            if position == -1:
                await query.edit_message_text("Starting quiz…")
                await send_question(update, context, session_id, 0)
            else:
                result = advance_session(STATE_DB, session_id, position, user_id)
                if result["completed"]:
                    session = result["session"]
                    total, correct = int(session["total_questions"]), int(session["correct_count"])
                    accuracy = 100 * correct / total if total else 0
                    await query.edit_message_text(
                        f"<b>Quiz completed!</b>\n\nSubject: {esc(session['subject_name'])}\n"
                        f"Score: <b>{correct}/{total}</b>\nAccuracy: <b>{accuracy:.1f}%</b>",
                        parse_mode=ParseMode.HTML,
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("📚 Choose another subject", callback_data="subjects")],
                            [InlineKeyboardButton("📊 My Progress", callback_data="stats")],
                        ]),
                    )
                else:
                    await query.edit_message_text("Loading next question…")
                    await send_question(update, context, session_id, result["next_position"])
        elif action == "x" and len(data) == 2:
            session = stop_session(STATE_DB, int(data[1]), user_id)
            if not session:
                await query.edit_message_text("Quiz session not found.", reply_markup=home_keyboard())
            else:
                await query.edit_message_text(
                    f"Quiz stopped.\nScore: <b>{session['correct_count']}</b> correct.\n"
                    f"Answered: <b>{session['answered_questions']}/{session['total_questions']}</b>",
                    parse_mode=ParseMode.HTML, reply_markup=home_keyboard(),
                )
        else:
            await query.edit_message_text("Unknown action. Use /start.", reply_markup=home_keyboard())
    except (ValueError, TypeError) as exc:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"Action could not be completed: {esc(exc)}. Use /start.",
        )
    except Exception:
        logger.exception("Callback failed: %s", query.data)
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="Something went wrong. Please try again or use /start.",
        )


async def post_init(application: Application) -> None:
    await application.bot.set_my_commands([
        BotCommand("start", "Open the main menu"),
        BotCommand("subjects", "Choose an MPSC subject"),
        BotCommand("stats", "View your practice progress"),
        BotCommand("help", "How to use this bot"),
    ])


def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token or token == "PASTE_YOUR_BOT_TOKEN_HERE":
        raise SystemExit("TELEGRAM_BOT_TOKEN missing. Copy .env.example to .env and set the BotFather token.")
    if not QUESTION_DB.exists():
        raise SystemExit(f"Question DB not found: {QUESTION_DB}")
    initialize_state_db(STATE_DB)
    subjects = get_subjects(QUESTION_DB)
    if not subjects:
        raise SystemExit("No subjects found in the question database.")
    logger.info("Loaded %d subjects / %s MCQs.", len(subjects), sum(int(s["question_count"]) for s in subjects))
    # The default PTB connection timeout is short for slower or filtered networks.
    # Use explicit timeouts for both normal Bot API requests and long polling.
    app = (
        Application.builder()
        .token(token)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .get_updates_connect_timeout(30)
        .get_updates_read_timeout(40)
        .get_updates_write_timeout(30)
        .get_updates_pool_timeout(30)
        .post_init(post_init)
        .build()
    )
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("subjects", subjects_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CallbackQueryHandler(callback_handler))
    try:
        # Retry startup a few times for intermittent TLS/network delays.
        app.run_polling(allowed_updates=Update.ALL_TYPES, bootstrap_retries=5)
    except NetworkError as exc:
        logger.error(
            "Cannot reach the Telegram Bot API. Check connectivity to api.telegram.org:443, "
            "firewall/antivirus, and any required HTTPS proxy. Network error: %s",
            type(exc).__name__,
        )
        raise SystemExit(
            "Telegram connection failed. Run the network checks in telegram_bot/README.md. "
            "The local question database is valid and was not changed."
        ) from None


if __name__ == "__main__":
    main()
