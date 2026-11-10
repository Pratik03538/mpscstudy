# MPSC Arena Mini App

Responsive Telegram Mini App dashboard for the MPSC Study bot.

## Features
- Authenticates users using Telegram Mini App `initData` on the FastAPI server.
- Displays real practice totals, subject accuracy, official event participation, battle wins, community battle rankings, and recent personal battles.
- No demo accounts or generated leaderboard identities are included.
- A normal browser preview shows an authentication notice and does not invent a student profile.

## Local run (Windows PowerShell)

From `telegram_bot`:

```powershell
python -m pip install -r .\miniapp\requirements.txt
$env:PORT = "8000"
python .\miniapp\server.py
```

Open `http://localhost:8000/health` to verify the service. To use it inside Telegram, it must be served over HTTPS and its domain must be configured with `@BotFather`.

## Required environment

The Mini App reads the same `.env` as the bot:
- `TELEGRAM_BOT_TOKEN` — used only on the server to verify Telegram's signed init data.
- `BOT_STATE_DB_PATH` — path to the same state database used by the running bot.

The web process must be able to read the exact same persistent state database that the bot writes. Do not deploy the bot and web app onto separate ephemeral filesystems with separate SQLite copies. For multi-instance or separate-host production deployment, migrate state to a shared PostgreSQL database and update both services to use it.

## Telegram configuration

1. Enable the bot's Main Mini App or configure a Web App URL with `@BotFather`.
2. Set the domain to the HTTPS host serving this app.
3. Set `MINI_APP_URL` in the bot's `.env` to that HTTPS URL.
4. Restart the bot after changing its environment.
5. Open the app from the bot's **My Profile** button. Telegram-signed `initData` is verified server-side; the page never trusts `initDataUnsafe` for identity.

The Mini App is a dashboard. Public group battles are playable directly using Telegram inline keyboards in group messages; the dashboard shows profile, rankings, and match history.

## Deployment note

Deploy the web process and bot with access to one persistent shared state volume, or use PostgreSQL for production. The included server is a runnable service, but it is not automatically deployed by committing this repository.
