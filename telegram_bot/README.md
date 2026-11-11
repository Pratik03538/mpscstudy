# MPSC Study Telegram Bot + MPSC Arena

A subject-only MPSC question bank bot with personal practice, scheduled official channel events, open group battles, community rankings, and a Telegram Mini App dashboard.

## Current features in this repository

### Personal bot chat
- `/start`, `/subjects`, `/stats`, `/weak`, `/profile`, `/events`, `/help`
- Four subjects: Economics, Geography, History, Marathi Grammar
- Random 10, 20 or 50 question practice sessions
- A/B/C/D inline answers, stored answer-key feedback, and available Marathi/English explanations
- Personal practice stats and a separate battle record

### Public channel
- Daily official event scheduler (default 8:30 PM Asia/Kolkata)
- Reminder post (default 8:20 PM)
- 15 question posts by default, each open for 35 seconds
- One answer per real Telegram user per question
- Final leaderboard includes only users who answered all event questions
- Ranking: correct answers first, then lowest total answer time
- Event scores are stored separately from personal practice and group battles

### Discussion group
- `/challenge` opens a subject picker; `/challenge history`, `/challenge geography`, `/challenge economics`, and `/challenge marathi` create a challenge
- When a member accepts, the challenger receives private **Accept player** / **Change player** controls. If the challenger has not started the bot and Telegram blocks the DM, protected fallback controls stay on the challenge card. A declined player cannot reclaim that challenge
- Both confirmed players use one clear **I'm ready** control; a 3-second countdown starts the match
- The group message then exposes an **Open Battle Room** Mini App deep link. Telegram requires each player to tap it; bots cannot forcibly open a Mini App on another user's device
- Questions and answers are private to each authenticated player in the Mini App. Each player advances independently; one player's slow answer or disconnect never blocks the other's next question
- Maximum 10 questions and one shared 10-minute server deadline. If both players finish early, the match closes early; otherwise unanswered questions are recorded as zero when the deadline expires
- Scoring: 100 points per correct answer; correct points decide the winner and lower total completion time breaks a tie. Accuracy, time, and points are shown for both
- Completed results are published back to the originating chat and count only toward community battle ranking, never the official channel-event leaderboard
- Full history is available from the Mini App: both selections, correctness, per-question response time, cumulative points after each question, and the correct answer
- `/duel`, `/battle`, `/top`, `/rank`, `/profile`, `/ids`

### Inline challenge sharing
- Enable inline mode with `/setinline` in `@BotFather`.
- In any chat, type `@YourBot history` (or geography/economics/marathi), choose the challenge-card result, then tap **Create Open Challenge**.
- The challenge card can be accepted by another real user in that chat.
- Telegram bots cannot read ordinary private messages between two people; inline mode is the supported way to insert the challenge into a user-to-user chat.

### Telegram Mini App
- Responsive MPSC Arena dashboard.
- Server-side validation of Telegram-signed `initData`.
- Real practice totals, subject accuracy, official event rankings, community battle rankings, and recent battles.
- It never trusts `initDataUnsafe` for identity and does not generate fake leaderboard entries.

## Windows setup

1. Python 3.10+ recommended.
2. Create a bot through `@BotFather` and copy its token.
3. Copy `.env.example` to `.env` and fill the configuration described below.
4. Ensure `database/mpsc_subject_only.db` exists.
5. From this directory run:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python .\bot\bot.py
```

Or run `./run_bot.ps1` after configuring `.env`.

The bot uses long polling, so the process must stay running. JobQueue handles daily events, challenge expiry, battle deadlines, and result publishing.

## Telegram setup (required)

### 1. Channel
- Add the bot to your public channel as an administrator.
- Grant permission to post messages.
- Set `PUBLIC_CHANNEL_ID` to `@your_public_channel` or the numeric channel ID.
- Set `PUBLIC_CHANNEL_LINK` to the public channel URL.

### 2. Discussion group
- Add the bot to the discussion group.
- Give it permission to send and edit messages.
- Set `DISCUSSION_GROUP_ID` and `DISCUSSION_GROUP_LINK`.
- Link the group to the channel in Telegram's channel settings if you want the native Discussion button.

### 3. Admin ID
- Start the bot and send `/ids` in a private chat to get your numeric Telegram user ID.
- Put that number in `ADMIN_USER_IDS` (comma-separated if there are multiple admins).
- Admins can use `/event_now` for a test event and `/event_stop` to stop an active event.

### 4. Challenge sharing in any chat
- In `@BotFather`, run `/setinline` and enable inline mode.
- Users can then type `@YourBot history` in a chat, select the result, and publish an open challenge card.
- Group commands such as `/challenge history` do not need inline mode.
- With Telegram privacy mode enabled, send group commands as `/challenge@YourBotUsername history`; alternatively disable privacy with `/setprivacy` in `@BotFather` and re-add the bot to the group. Telegram's privacy mode controls which group commands/messages the bot receives.

### 5. Daily event time
Default configuration is 8:20 PM reminder and 8:30 PM event time in India. Change `EVENT_HOUR`, `EVENT_MINUTE`, `EVENT_QUESTION_COUNT`, or `EVENT_QUESTION_SECONDS` in `.env`, then restart the bot.

### 6. Mini App battle rooms
See [miniapp/README.md](miniapp/README.md). The Mini App must be deployed over HTTPS, and the bot and web process must share the exact same persistent `BOT_STATE_DB_PATH` plus access to the question database. Create a named Mini App with `@BotFather` (for example through `/newapp`), set its HTTPS domain, then put the deployed URL in `MINI_APP_URL` and its BotFather short name in `MINI_APP_SHORT_NAME`. Restart the bot. The battle card deep link uses `https://t.me/<bot_username>/<short_name>?startapp=battle_<id>`; a player must tap it because Telegram does not allow a bot to force-open an app on a user's device. The Mini App also accepts `startapp=history_<id>` for the completed audit.

## Data integrity and limitations
- The original question database is opened read-only.
- Practice scores, official event scores, and PvP rankings are separate.
- No synthetic or invented Telegram IDs are inserted into public leaderboards.
- The source subject assignments and stored answer keys have not all been independently fact-checked. Verify question/answer keys before running high-stakes official competitions.
- The default official event only ranks complete attempts (all scheduled questions answered). Incomplete attempts are not eligible for the event leaderboard.
- A bot cannot initiate private messages to users who have never started it.
- The Mini App is included as runnable code but is not automatically hosted or deployed by a GitHub commit.

## Connection troubleshooting

Run:

```powershell
.\test_telegram_network.ps1
```

Do not paste your Telegram token into diagnostics or chat. HTTPX logs are suppressed because Bot API URLs contain the token.
