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
- `/challenge` opens a subject picker
- `/challenge history`, `/challenge geography`, `/challenge economics`, `/challenge marathi` create an open public challenge
- First eligible member to press Accept becomes the opponent (challenger cannot accept their own challenge)
- Both players must independently confirm START
- A 3-second countdown starts the match
- Same questions and options are shown in one editable battle message
- Only the two players can answer; group members see submitted/waiting status but not answer correctness
- Open challenges expire after 10 minutes by default; accepted matches expire if both players do not confirm START within 2 minutes
- If a player does not answer within the question timer, that question is recorded as unanswered
- Final result and community battle ranking are published in the group
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

The bot uses long polling, so the process must stay running. JobQueue handles daily events and battle question timeouts.

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

### 6. Mini App
See [miniapp/README.md](miniapp/README.md). The app needs a public HTTPS URL and a web process that reads the same persistent state database as the bot. Use `MINI_APP_URL` only after deployment and after configuring the app in `@BotFather`.

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
