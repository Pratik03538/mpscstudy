# Community Battles and Transparent Leaderboards — Product Specification

This specification extends the existing subject-only MPSC Telegram bot. It separates genuine participation from simulated/demo data and defines the group, inline-chat, and Mini App battle experience.

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
- Host discussion, doubts, friend challenges, battle invitations, progress cards, tournament updates, and results.
- Use one editable challenge message instead of flooding the group with new status messages.
- Suggested group topics: General, History, Geography, Economics, Marathi Grammar, Battle Arena, Event Discussion, Results.

### Private chat / any user-to-user chat
- A bot cannot read a normal private conversation between two people just because they are chatting there.
- For a challenge inserted into any chat, enable Telegram inline mode. A user types `@YourBot challenge history`, chooses the bot's challenge card, and sends it into that chat.
- In groups, users can use a bot command such as `/challenge @username history`.
- A deep link can also invite a friend to register/join the bot. The bot must not assume it can DM a user who has never started it.

### Mini App
- Use a shared battle room for synchronized Ready/Start, countdown, private question display, timer, and progress.
- Open it from the group/inline challenge card using a battle-specific start parameter.
- The backend verifies Telegram Mini App init data and uses the chat context for the shared room. The client is never trusted to decide identity, score, timing, or winner.
- The bot continues to publish challenge state and results in the originating chat.

## Player-vs-player battle flow

State machine:

`PENDING_ACCEPTANCE -> ACCEPTED -> WAITING_FOR_READY -> COUNTDOWN -> ACTIVE -> COMPLETED`

Terminal alternatives: `DECLINED`, `CANCELLED`, `EXPIRED`, `ABORTED`.

1. Challenger creates a battle with a subject and mode. The bot posts a challenge card in the current group/chat.
2. Only the invited Telegram user may press Accept. Other users receive a polite “This challenge is not for you” response.
3. On acceptance, the same card updates with both verified player names and two independent Ready states.
4. Each player presses their own Ready/Start control in the shared Mini App room. A user cannot set the other player's state.
5. When both are ready, the server starts a short countdown and sets one authoritative server-side start time.
6. Both players receive the same question set and time limit. Each player's answers are private; the opponent and group see only progress, not the selected answer.
7. The group card can show progress such as Player A 4/10 and Player B 3/10. It must not reveal correctness until the battle ends.
8. At completion, the server calculates both scores, accuracy, and server-measured answer times, then publishes one result card.
9. Update both real player profiles and the separate Battle Rating ledger. Never add battle points to the official channel-event leaderboard.

Suggested battle modes:
- Friend Challenge: invited opponent.
- Random Duel: matchmaking by Battle Rating and subject strength.
- Subject Battle: History, Geography, Economics, or Marathi Grammar.
- Practice Bot: explicitly labelled AI/demo opponent; never represented as a human.
- Weekly Knockout: bracket with real registered players.

## Challenge card UI states

### Pending
- Challenger display name
- Invited opponent
- Subject, question count, time limit
- Accept / Decline controls

### Accepted
- Challenger: Accepted / Ready
- Opponent: Accepted / Ready
- A Ready control for each participant
- Non-participants cannot change either state

### Active
- Shared countdown and question number
- Each player's answered count and progress bar
- No answer key shown during the match
- Server-owned deadline and one submission per question

### Completed
- Winner / Draw
- Score, accuracy, and total response time for both players
- Rematch / Challenge another player
- Group result message and profile update

## Suggested commands

- `/challenge @username history` — invite a known/registered group member.
- `/duel history` — enter random matchmaking.
- `/accept` and `/decline` — accept or reject the pending invitation.
- `/battle` — show the current battle state.
- `/cancelbattle` — cancel before the match starts.
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
- `battles`: battle ID, chat ID/context, challenger ID, invited player ID, subject, status, created/accepted/started/ended timestamps.
- `battle_players`: battle ID, Telegram user ID, ready state, score, correct count, answered count, elapsed time.
- `battle_questions`: battle ID, position, question ID, per-player option order if options are shuffled.
- `battle_answers`: battle ID, player ID, position, selected option, server received timestamp, correctness.
- `official_events`: event ID, channel ID, schedule, status, question set, scoring configuration.
- `official_event_attempts`: event ID, real Telegram user ID, answers, score, accuracy, response time, completion status.
- `community_points`: real-user community XP and Battle Rating, separate from official event score.
- `demo_activity`: synthetic test data with a mandatory demo flag and no Telegram user ID.

Add indexes on event/battle status, user IDs, timestamps, and battle question positions. Use database transactions for accepting a challenge, marking Ready, finalizing a result, and awarding points so two callbacks cannot create duplicate state changes.

## Anti-abuse and integrity

- Verify Telegram Mini App init data on the server.
- Accept callback actions only from the correct participant and only in the expected state.
- Use server timestamps, not client clocks.
- Enforce one answer per player per question.
- Use idempotent result finalization.
- Add cooldowns, battle expiry, cancellation, rematch limits, and report/block controls.
- Preserve an audit trail for disputes.
- Verify the stored MPSC answer key before using a question in a scored official event; existing source keys have not all been independently fact-checked.

## Build order

1. Expand the SQLite state DB with users, battles, event ledgers, and transaction-safe state transitions.
2. Add group challenge cards, target-only Accept/Decline, both-player Ready states, expiry, and result publishing.
3. Enable inline mode for challenges shared into user-to-user chats.
4. Build the shared Mini App battle room and server-side authentication.
5. Add official channel event scoring and its separate leaderboard.
6. Add matchmaking, leagues, tournaments, profile analytics, and admin tools.

This is a product specification, not a claim that these features are already implemented or deployed.
