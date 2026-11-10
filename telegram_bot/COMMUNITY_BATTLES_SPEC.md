# Community Battles and Transparent Leaderboards — Product Specification

This specification extends the existing subject-only MPSC Telegram bot. It separates real participation from clearly labelled demo activity and defines public group challenges, channel events, and Mini App battle rooms.

## Product rules

1. Never present invented usernames, Telegram IDs, scores, or bot-generated activity as real students.
2. Production leaderboards contain only verified Telegram users who actually completed eligible quizzes.
3. Demo participants use internal IDs such as `demo_player_001`, carry a permanent `DEMO` label, and are excluded from real rankings, event winners, profile statistics, and matchmaking against real users unless the user explicitly chooses a Practice Bot match.
4. If no cash reward is being paid, do not advertise a cash prize or imply a payout. Use non-cash recognition such as Champion title, badge, streak, and Hall of Fame.
5. Keep official channel-event scores separate from group practice and PvP scores.

## Telegram surfaces

### Public channel
- Publish event announcements, countdowns, official MCQ questions, final rankings, daily champion cards, and explanations.
- Official questions use inline answer buttons. The backend records the callback user's real Telegram ID, answer, and server timestamp.
- Only event attempts tagged `OFFICIAL_CHANNEL_EVENT` enter the official event leaderboard.
- Channel posts link to the discussion group and the shared battle Mini App when applicable.

### Discussion group
- Host discussion, doubts, open public challenges, battle invitations, progress cards, tournament updates, and results.
- Use one editable challenge message instead of flooding the group with new status messages.
- Suggested group topics: General, History, Geography, Economics, Marathi Grammar, Battle Arena, Event Discussion, Results.

### Private chat / any user-to-user chat
- A bot cannot read a normal private conversation between two people just because they are chatting there.
- For a challenge card inserted into any chat, enable Telegram inline mode. A user types `@YourBot challenge history`, chooses the bot's challenge card, and sends it into that chat.
- In groups, use an open challenge command such as `/challenge history`; do not require an opponent username or Telegram ID.
- A deep link can also invite a friend to open the bot/Mini App. The bot must not assume it can DM a user who has never started it.

### Mini App
- Use a shared battle room for each accepted challenge, with synchronized Start confirmation, countdown, private question display, timer, and progress.
- Open it from the group/inline challenge card using a battle-specific start parameter.
- The backend verifies Telegram Mini App init data and uses the chat context for the shared room. The client is never trusted to decide identity, score, timing, or winner.
- The bot continues to publish challenge state and results in the originating chat.

## Open public player-vs-player challenge flow

State machine:

`OPEN -> ACCEPTED -> WAITING_FOR_START -> COUNTDOWN -> ACTIVE -> COMPLETED`

Terminal alternatives: `CANCELLED`, `EXPIRED`, `ABORTED`.

1. A real group member posts `/challenge history`, `/challenge geography`, `/challenge economics`, or `/challenge marathi`. If no subject is supplied, the bot displays a subject-selection keyboard.
2. The bot publishes one public challenge card in that same group. It shows the challenger's verified Telegram display name, subject, question count, time limit, and an **Accept Challenge** button. No opponent is preselected.
3. Any other eligible real group member may accept. The first valid acceptance wins atomically. The bot immediately locks the challenge to that accepter and edits the same card to show both participants. Simultaneous later accept attempts receive “Challenge already accepted”.
4. The challenger cannot accept their own challenge. A user can only occupy one seat in the same battle. Demo users cannot accept or claim a real-user public challenge.
5. After acceptance, both players see their own **START** confirmation in the shared battle room or via participant-specific bot controls. A player cannot confirm for the other person.
6. Only after both players confirm Start does the server begin a short countdown and set one authoritative server-side start time. If either player does not confirm before the start-confirmation timeout, the battle expires or returns to the open state according to the configured rule; never silently start one-sided.
7. Both players receive the same question set and time limit. Each player's answers are private; the opponent and group see only progress, not selected answers or correctness.
8. The group card shows progress such as Player A 4/10 and Player B 3/10. It is updated in place rather than sending a new message for every answer.
9. At completion, the server calculates both scores, accuracy, and server-measured answer times, then publishes one result card in the originating group.
10. Update both real player profiles and the separate Battle Rating ledger. Never add battle points to the official channel-event leaderboard.

### Public challenge controls

- `/challenge [subject]` — create an open challenge in the current group; no opponent argument.
- **Accept Challenge** — any eligible member except the challenger can claim the open seat; first valid click wins.
- **START** — shown independently to both confirmed participants after acceptance.
- **Cancel Challenge** — challenger can cancel while the challenge is still open.
- **Leave / Abort** — participant can report inability to continue; apply an explicit forfeit/abort policy.
- **Rematch** — creates a fresh open challenge; it does not auto-start a new match.

Suggested battle modes:
- Open Public Challenge: any eligible group member can accept.
- Random Duel: matchmaking by Battle Rating and subject strength.
- Subject Battle: History, Geography, Economics, or Marathi Grammar.
- Practice Bot: explicitly labelled AI/demo opponent; never represented as a human.
- Weekly Knockout: bracket with real registered players.

## Challenge card UI states

### Open
- Challenger display name
- Subject, question count, time limit
- Accept Challenge button
- Expiry time and Cancel control for the challenger

### Accepted / waiting for Start
- Challenger name and accepter name
- Separate START confirmation status for each participant: Waiting / Confirmed
- No participant can confirm for the other
- A start-confirmation deadline

### Active
- Shared countdown and question number
- Each player's answered count and progress bar
- No answer key shown during the match
- Server-owned deadline and one submission per question

### Completed
- Winner / Draw
- Score, accuracy, and total response time for both players
- Rematch / Create Open Challenge
- Group result message and profile update

## Suggested commands

- `/challenge` — show subject selection and create an open public challenge.
- `/challenge history` — open a History challenge; first eligible member to accept becomes opponent.
- `/challenge geography`, `/challenge economics`, `/challenge marathi` — corresponding subject challenges.
- `/duel` — join public/random matchmaking.
- `/battle` — show the current battle state.
- `/cancelbattle` — cancel own open challenge before it is accepted.
- `/profile` — show the player's profile.
- `/rank` — show the player's community rank.
- `/top` — show genuine community rankings.
- `/stats` — subject-wise progress.
- `/help` — command help.

Commands in a group must be addressed to the bot if Telegram privacy mode requires it. A challenge placed into a normal one-to-one user chat should use inline mode or a shareable link, not a command the bot cannot see.

## Leaderboards and scoring separation

Use independent ledgers:

- `official_event_attempts`: only actual completed channel MCQ attempts.
- `community_quiz_attempts`: group and practice quizzes.
- `battle_results`: real PvP outcomes.
- `demo_activity`: synthetic fixture data for development/UI testing only.

Production public rankings query only real registered users and the eligible ledger. Demo fixtures never appear in real rankings. If participation is low, show the true participant count and an empty/early leaderboard rather than padding it with invented accounts.

Suggested PvP scoring:
- Correct answers determine the primary score.
- Incorrect and unanswered answers score zero.
- Accuracy/score is the primary ranking criterion.
- Server-measured completion time is the tie-breaker.
- Use the same question set and time limit for both players.
- Prevent duplicate submissions and reject answers after the server deadline.

## Required state database entities

- `users`: Telegram user ID, username snapshot, display name, first-seen time, registration state.
- `battles`: battle ID, chat ID/context, challenger ID, nullable accepter ID, subject, status, created/accepted/start-confirmed/started/ended timestamps, expiry time.
- `battle_players`: battle ID, Telegram user ID, role (challenger/accepter), ready state, score, correct count, answered count, elapsed time.
- `battle_questions`: battle ID, position, question ID, per-player option order if options are shuffled.
- `battle_answers`: battle ID, player ID, position, selected option, server received timestamp, correctness.
- `official_events`: event ID, channel ID, schedule, status, question set, scoring configuration.
- `official_event_attempts`: event ID, real Telegram user ID, answers, score, accuracy, response time, completion status.
- `community_points`: real-user community XP and Battle Rating, separate from official event score.
- `demo_activity`: synthetic test data with a mandatory demo flag and no Telegram user ID.

Add indexes on event/battle status, chat ID, user IDs, timestamps, and battle question positions. Use database transactions for claiming a public challenge, marking Start, finalizing a result, and awarding points so simultaneous accepts or duplicate callbacks cannot create duplicate state changes.

## Anti-abuse and integrity

- Verify Telegram Mini App init data on the server.
- Accept callback actions only from eligible participants and only in the expected state.
- Use server timestamps, not client clocks.
- Enforce one answer per player per question.
- Use idempotent result finalization.
- Add cooldowns, challenge expiry, cancellation, rematch limits, and report/block controls.
- Preserve an audit trail for disputes.
- Verify the stored MPSC answer key before using a question in a scored official event; existing source keys have not all been independently fact-checked.

## Build order

1. Expand the SQLite state DB with users, battles, event ledgers, and transaction-safe state transitions.
2. Add open public group challenge cards, first-accept locking, independent two-player Start confirmations, expiry, and result publishing.
3. Enable inline-mode challenge cards for user-to-user chats.
4. Build the shared Mini App battle room and server-side authentication.
5. Add official channel event scoring and its separate leaderboard.
6. Add matchmaking, leagues, tournaments, profile analytics, and admin tools.

This is a product specification, not a claim that these features are already implemented or deployed.
