FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000 \
    BOT_DB_PATH=/data/mpsc_subject_only.db \
    BOT_STATE_DB_PATH=/data/bot_progress.sqlite3

WORKDIR /app

COPY telegram_bot/requirements.txt /app/requirements.txt
COPY telegram_bot/miniapp/requirements.txt /app/miniapp-requirements.txt

RUN python -m pip install --upgrade pip \
    && python -m pip install -r /app/requirements.txt \
    && python -m pip install -r /app/miniapp-requirements.txt \
    && python -m pip install supervisor==4.2.5

COPY telegram_bot /app/telegram_bot
COPY telegram_bot/deploy/supervisord.conf /etc/supervisor/supervisord.conf

RUN mkdir -p /data \
    && chmod +x /app/telegram_bot/deploy/start-bot.sh

EXPOSE 8000

CMD ["/usr/local/bin/supervisord", "-n", "-c", "/etc/supervisor/supervisord.conf"]
