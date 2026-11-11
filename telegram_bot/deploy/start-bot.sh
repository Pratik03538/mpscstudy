#!/bin/sh
set -eu

DB_PATH="${BOT_DB_PATH:-/data/mpsc_subject_only.db}"

echo "[mpsc-bot] Waiting for question database at ${DB_PATH}"
while [ ! -s "${DB_PATH}" ]; do
  sleep 5
done

echo "[mpsc-bot] Question database found. Starting Telegram bot."
exec python /app/telegram_bot/bot/bot.py
