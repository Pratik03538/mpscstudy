# Deploy MPSC Arena on Railway

This deployment runs the Telegram bot and FastAPI Mini App in one Railway service. They share one Railway volume, so battle state and the question database remain available to both processes. Keep the service at **one replica** while using SQLite.

## 1. Create the Railway service

1. Sign in to [Railway](https://railway.com/).
2. Choose **New Project → Deploy from GitHub repo**.
3. Connect the GitHub account if prompted and select `Pratik03538/mpscstudy`.
4. Deploy the `main` branch. Railway uses the repository's `railway.json` and `telegram_bot/Dockerfile`.

The container starts the Mini App API immediately. The bot waits until the question database exists on the volume, then starts automatically.

## 2. Add persistent storage

On the Railway service, add a Volume and set its mount path to:

```text
/data
```

Do not mount separate volumes for the bot and Mini App. They must use the same service and the same `/data` volume.

## 3. Set service variables

In the Railway service's **Variables** tab, add these values. Use your own existing Telegram IDs and secret token; never put them in GitHub or share them in chat.

```env
TELEGRAM_BOT_TOKEN=YOUR_BOTFATHER_TOKEN
BOT_DB_PATH=/data/mpsc_subject_only.db
BOT_STATE_DB_PATH=/data/bot_progress.sqlite3

PUBLIC_CHANNEL_ID=@your_public_channel
PUBLIC_CHANNEL_LINK=https://t.me/your_public_channel
DISCUSSION_GROUP_ID=-1001234567890
DISCUSSION_GROUP_LINK=https://t.me/your_discussion_group
ADMIN_USER_IDS=YOUR_NUMERIC_TELEGRAM_USER_ID

EVENT_HOUR=20
EVENT_MINUTE=30
EVENT_QUESTION_COUNT=15
EVENT_QUESTION_SECONDS=35
OPEN_CHALLENGE_TTL=600
START_CONFIRM_TTL=120

MINI_APP_URL=
MINI_APP_SHORT_NAME=
```

The event values above are examples/defaults. Keep your real current channel/group configuration. Railway automatically provides `PORT`; do not add a fixed PORT variable.

## 4. Generate the HTTPS domain

In the service's **Settings → Networking**, generate a Railway domain. Open:

```text
https://YOUR-RAILWAY-DOMAIN/health
```

Expected response:

```json
{"ok":true,"service":"mpsc-arena-miniapp"}
```

## 5. Upload the question database to the volume

The database is intentionally excluded from the Docker image and Git. Upload the existing local file to the attached volume.

Install the Railway CLI on Windows (Node.js 16+ required):

```powershell
npm i -g @railway/cli
railway login
```

From the local repository folder:

```powershell
cd C:\Users\Pratik\PycharmProjects\mpsckatta
railway link
```

Select the project, production environment, and deployed service when prompted. Then upload the local database. Replace the volume name below with the exact name shown in Railway:

```powershell
railway volume files --volume mpsc-arena-data upload ".\mpsc_output\mpsc_subject_only.db" /mpsc_subject_only.db
railway volume files --volume mpsc-arena-data list /
```

The bot process is waiting for `/data/mpsc_subject_only.db`; it starts automatically after the upload completes. Check Railway deployment logs for `Loaded 4 subjects / ... MCQs` and `Application started`.

## 6. Connect the Telegram Mini App

1. In `@BotFather`, open `/mybots` → select your bot → Bot Settings → Configure Mini App, then create/configure the named Mini App and use the generated Railway HTTPS domain as its URL.
2. Record the exact Mini App short name.
3. Add the two service variables:
   - `MINI_APP_URL=https://YOUR-RAILWAY-DOMAIN`
   - `MINI_APP_SHORT_NAME=YOUR_BOTFATHER_SHORT_NAME`
4. Redeploy/restart the Railway service.
5. Open the bot's profile dashboard and test a private battle room from a challenge card.

Telegram requires the user to tap the battle deep link; a bot cannot forcibly open a Mini App on another person's device.

## 7. Important operating rules

- Keep this Railway service at one replica. Do not scale the SQLite-backed bot horizontally.
- Never remove or wipe the `/data` volume; it contains the live battle ledger, user progress, event results, and question bank.
- Back up the Railway volume before destructive database changes.
- Keep the real bot token only in Railway Variables. If it was ever exposed, revoke it with BotFather and set the replacement only in Railway.
- The Railway free tier has limited monthly resource credit. The Hobby plan currently has a $5/month minimum usage charge with $5 of usage included; extra CPU/RAM/storage/network usage can increase the bill. Check Railway's current pricing before upgrading.
