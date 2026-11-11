from __future__ import annotations

import time
from pathlib import Path

from db import question_connection, state_connection

MAX_QUESTIONS = 10
DURATION_SECONDS = 10 * 60
POINTS_PER_CORRECT = 100


def initialize_battle_schema(path: Path) -> None:
    """Additive, restart-safe migrations for independent player progress."""
    with state_connection(path) as con:
        columns = {row["name"] for row in con.execute("PRAGMA table_info(battles)")}
        for name, declaration in (
            ("deadline_at", "REAL"),
            ("result_posted_at", "REAL"),
        ):
            if name not in columns:
                con.execute(f"ALTER TABLE battles ADD COLUMN {name} {declaration}")
        con.executescript("""
        CREATE TABLE IF NOT EXISTS battle_players(
            battle_id TEXT NOT NULL REFERENCES battles(battle_id) ON DELETE CASCADE,
            user_id TEXT NOT NULL,
            role TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            current_position INTEGER NOT NULL DEFAULT 0,
            correct_count INTEGER NOT NULL DEFAULT 0,
            points INTEGER NOT NULL DEFAULT 0,
            elapsed_ms INTEGER NOT NULL DEFAULT 0,
            question_started_at REAL,
            finished_at REAL,
            total_elapsed_ms INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(battle_id,user_id)
        );
        CREATE INDEX IF NOT EXISTS idx_battle_players_user ON battle_players(user_id,status);
        CREATE TABLE IF NOT EXISTS battle_declined_users(
            battle_id TEXT NOT NULL REFERENCES battles(battle_id) ON DELETE CASCADE,
            user_id TEXT NOT NULL,
            declined_at REAL NOT NULL,
            PRIMARY KEY(battle_id,user_id)
        );
        """)
        # Existing shared-message battles have no independent player rows and cannot
        # safely continue under the new per-player Mini App state machine.
        con.execute("""
            UPDATE battles SET status='aborted',finished_at=COALESCE(finished_at,?)
            WHERE status IN ('accepted','active')
              AND NOT EXISTS (
                SELECT 1 FROM battle_players bp WHERE bp.battle_id=battles.battle_id
              )
        """, (time.time(),))


def _get_question(question_db: Path, question_id: str) -> dict | None:
    with question_connection(question_db) as con:
        row = con.execute("""
            SELECT q.*,s.subject_name_en,s.subject_name_mr
            FROM questions q JOIN subjects s ON s.subject_id=q.subject_id
            WHERE q.question_id=? LIMIT 1
        """, (question_id,)).fetchone()
    return dict(row) if row else None


def _battle(con, battle_id: str):
    row = con.execute("SELECT * FROM battles WHERE battle_id=?", (battle_id,)).fetchone()
    return dict(row) if row else None


def _players(con, battle_id: str):
    return [dict(row) for row in con.execute(
        "SELECT * FROM battle_players WHERE battle_id=? ORDER BY role", (battle_id,)
    ).fetchall()]


def _award_and_close_if_ready(con, battle_id: str, finished_at: float) -> bool:
    battle = _battle(con, battle_id)
    if not battle or battle["status"] not in ("active", "completed"):
        return False
    players = _players(con, battle_id)
    if len(players) != 2 or any(p["status"] == "active" for p in players):
        return False
    con.execute("""
        UPDATE battles SET status='completed',finished_at=COALESCE(finished_at,?)
        WHERE battle_id=? AND status IN ('active','completed')
    """, (finished_at, battle_id))
    # The unique award row makes this safe under duplicate callbacks and retries.
    new_awards = 0
    for player in players:
        inserted = con.execute(
            "INSERT OR IGNORE INTO battle_awards(battle_id,user_id) VALUES(?,?)",
            (battle_id, player["user_id"]),
        )
        if inserted.rowcount == 1:
            new_awards += 1
            con.execute("""
                INSERT INTO battle_player_totals(user_id,battles_played,wins,correct_total,elapsed_total)
                VALUES(?,1,?,?,?)
                ON CONFLICT(user_id) DO UPDATE SET
                    battles_played=battles_played+1,
                    wins=wins+excluded.wins,
                    correct_total=correct_total+excluded.correct_total,
                    elapsed_total=elapsed_total+excluded.elapsed_total
            """, (
                player["user_id"], 0, int(player["correct_count"]),
                int(player["total_elapsed_ms"]),
            ))
    current = _players(con, battle_id)
    if new_awards and len(current) == 2:
        winner = _winner(current)
        if winner:
            con.execute(
                "UPDATE battle_player_totals SET wins=wins+1 WHERE user_id=?",
                (winner["user_id"],),
            )
    return True


def _winner(players: list[dict]) -> dict | None:
    if len(players) != 2:
        return None
    left, right = players
    lk = (int(left["points"]), -int(left["total_elapsed_ms"] or 0))
    rk = (int(right["points"]), -int(right["total_elapsed_ms"] or 0))
    if lk == rk:
        return None
    return left if lk > rk else right


def begin_battle(path: Path, battle_id: str, duration_seconds: int = DURATION_SECONDS) -> dict | None:
    now = time.time()
    deadline = now + max(60, min(int(duration_seconds), DURATION_SECONDS))
    with state_connection(path) as con:
        battle = _battle(con, battle_id)
        if not battle or battle["status"] != "countdown":
            return battle
        total = min(MAX_QUESTIONS, int(battle["total_questions"]))
        if total < 1 or not battle["challenger_id"] or not battle["opponent_id"]:
            return None
        con.execute("""
            UPDATE battles SET status='active',started_at=?,question_started_at=?,deadline_at=?,
                total_questions=? WHERE battle_id=? AND status='countdown'
        """, (now, now, deadline, total, battle_id))
        for uid, role in (
            (str(battle["challenger_id"]), "challenger"),
            (str(battle["opponent_id"]), "opponent"),
        ):
            con.execute("""
                INSERT OR IGNORE INTO battle_players(
                    battle_id,user_id,role,status,current_position,question_started_at
                ) VALUES(?,?,?,'active',0,?)
            """, (battle_id, uid, role, now))
        return _battle(con, battle_id)


def _finalize_timeout_in_connection(con, battle: dict, now: float) -> bool:
    if not battle or battle["status"] != "active":
        return False
    deadline = float(battle.get("deadline_at") or (float(battle["started_at"] or now) + DURATION_SECONDS))
    if now < deadline:
        return False
    total = min(MAX_QUESTIONS, int(battle["total_questions"]))
    for player in _players(con, battle["battle_id"]):
        if player["status"] != "active":
            continue
        current = int(player["current_position"])
        first_elapsed = max(
            0, int((deadline - float(player.get("question_started_at") or deadline)) * 1000)
        )
        for position in range(current, total):
            con.execute("""
                INSERT OR IGNORE INTO battle_answers(
                    battle_id,user_id,position,selected_option,is_correct,elapsed_ms,answered_at
                ) VALUES(?,?,?,NULL,0,?,?)
            """, (
                battle["battle_id"], player["user_id"], position,
                first_elapsed if position == current else 0, deadline,
            ))
        con.execute("""
            UPDATE battle_players SET status='timed_out',current_position=?,
                question_started_at=NULL,finished_at=?,total_elapsed_ms=?
            WHERE battle_id=? AND user_id=? AND status='active'
        """, (
            total, deadline, max(0, int((deadline - float(battle["started_at"] or deadline)) * 1000)),
            battle["battle_id"], player["user_id"],
        ))
    _award_and_close_if_ready(con, battle["battle_id"], deadline)
    return True


def finalize_battle_if_expired(path: Path, battle_id: str, now: float | None = None) -> bool:
    with state_connection(path) as con:
        return _finalize_timeout_in_connection(
            con, _battle(con, battle_id), time.time() if now is None else now
        )


def submit_battle_answer(
    state_db: Path, question_db: Path, battle_id: str, user_id: str,
    position: int, option: str,
) -> dict:
    option = str(option).upper()
    if option not in {"A", "B", "C", "D"}:
        raise ValueError("Choose A, B, C or D.")
    finalize_battle_if_expired(state_db, battle_id)
    with state_connection(state_db) as con:
        battle = _battle(con, battle_id)
        if not battle:
            raise LookupError("Battle not found.")
        if battle["status"] != "active":
            raise ValueError("This battle is not accepting answers.")
        if time.time() >= float(battle.get("deadline_at") or 0):
            _finalize_timeout_in_connection(con, battle, time.time())
            raise ValueError("The 10-minute battle timer has ended.")
        player = con.execute(
            "SELECT * FROM battle_players WHERE battle_id=? AND user_id=?",
            (battle_id, str(user_id)),
        ).fetchone()
        if not player:
            raise PermissionError("Only the two players can answer this battle.")
        if player["status"] != "active":
            raise ValueError("Your attempt is already submitted.")
        if int(player["current_position"]) != int(position):
            raise ValueError("This question is outdated. Refreshing your battle room.")
        item = con.execute(
            "SELECT question_id FROM battle_questions WHERE battle_id=? AND position=?",
            (battle_id, int(position)),
        ).fetchone()
        if not item:
            raise ValueError("Question not found.")
        question_id = str(item["question_id"])
        question = _get_question(question_db, question_id)
        if not question:
            raise ValueError("Question content is unavailable.")
        correct_key = str(question.get("correct_option_key") or "").upper()
        stamp = time.time()
        elapsed = max(0, int((stamp - float(player["question_started_at"] or stamp)) * 1000))
        is_correct = int(option == correct_key)
        con.execute("""
            INSERT INTO battle_answers(
                battle_id,user_id,position,selected_option,is_correct,elapsed_ms,answered_at
            ) VALUES(?,?,?,?,?,?,?)
        """, (battle_id, str(user_id), int(position), option, is_correct, elapsed, stamp))
        next_position = int(position) + 1
        next_correct = int(player["correct_count"]) + is_correct
        next_points = int(player["points"]) + (POINTS_PER_CORRECT if is_correct else 0)
        complete = next_position >= min(MAX_QUESTIONS, int(battle["total_questions"]))
        total_elapsed = max(0, int((stamp - float(battle["started_at"] or stamp)) * 1000)) if complete else 0
        con.execute("""
            UPDATE battle_players SET current_position=?,correct_count=?,points=?,elapsed_ms=elapsed_ms+?,
                question_started_at=?,status=?,finished_at=?,total_elapsed_ms=?
            WHERE battle_id=? AND user_id=? AND status='active' AND current_position=?
        """, (
            next_position, next_correct, next_points, elapsed,
            None if complete else stamp, "completed" if complete else "active",
            stamp if complete else None, total_elapsed,
            battle_id, str(user_id), int(position),
        ))
        _award_and_close_if_ready(con, battle_id, stamp)
    return get_battle_room(state_db, question_db, battle_id, user_id)


def get_battle_room(state_db: Path, question_db: Path, battle_id: str, user_id: str) -> dict:
    finalize_battle_if_expired(state_db, battle_id)
    with state_connection(state_db) as con:
        battle = _battle(con, battle_id)
        if not battle:
            raise LookupError("Battle not found.")
        if str(user_id) not in (str(battle["challenger_id"]), str(battle["opponent_id"])):
            raise PermissionError("This battle belongs to its two registered players.")
        players = _players(con, battle_id)
        mine = next((p for p in players if p["user_id"] == str(user_id)), None)
        if not mine:
            raise ValueError("Battle player state has not started yet.")
        opponent = next((p for p in players if p["user_id"] != str(user_id)), None)
        question = None
        if battle["status"] == "active" and mine["status"] == "active":
            item = con.execute(
                "SELECT question_id FROM battle_questions WHERE battle_id=? AND position=?",
                (battle_id, int(mine["current_position"])),
            ).fetchone()
            if item:
                question = _get_question(question_db, str(item["question_id"]))
        deadline = float(battle.get("deadline_at") or 0)
        now = time.time()
        remaining = max(0, int(deadline - now)) if deadline else 0
        return {
            "battle_id": battle_id,
            "status": battle["status"],
            "subject": battle["subject_name"],
            "total_questions": min(MAX_QUESTIONS, int(battle["total_questions"])),
            "deadline_at": deadline,
            "remaining_seconds": remaining,
            "me": {
                "name": mine["user_id"] == battle["challenger_id"] and battle["challenger_name"] or battle["opponent_name"],
                "status": mine["status"],
                "answered": int(mine["current_position"]),
                "correct": int(mine["correct_count"]),
                "points": int(mine["points"]),
                "accuracy": round(100 * int(mine["correct_count"]) / max(1, int(battle["total_questions"])), 1),
                "elapsed_ms": int(mine["elapsed_ms"]),
                "total_elapsed_ms": int(mine["total_elapsed_ms"]),
                "position": int(mine["current_position"]),
            },
            "opponent": ({
                "name": battle["opponent_name"] if mine["user_id"] == battle["challenger_id"] else battle["challenger_name"],
                "status": opponent["status"],
                "answered": int(opponent["current_position"]),
            } if opponent else None),
            "question": ({
                "position": int(mine["current_position"]),
                "question_mr": question.get("question_mr"),
                "question_en": question.get("question_en"),
                "option_a": question.get("option_a"),
                "option_b": question.get("option_b"),
                "option_c": question.get("option_c"),
                "option_d": question.get("option_d"),
            } if question else None),
        }


def get_battle_history(state_db: Path, question_db: Path, battle_id: str) -> dict:
    finalize_battle_if_expired(state_db, battle_id)
    with state_connection(state_db) as con:
        battle = _battle(con, battle_id)
        if not battle:
            raise LookupError("Battle not found.")
        if battle["status"] != "completed":
            raise ValueError("Full answer history becomes available after both attempts finish or the 10-minute timer ends.")
        players = _players(con, battle_id)
        answer_rows = con.execute("""
            SELECT user_id,position,selected_option,is_correct,elapsed_ms,answered_at
            FROM battle_answers WHERE battle_id=? ORDER BY position,user_id
        """, (battle_id,)).fetchall()
        answers = {(row["user_id"], int(row["position"])): dict(row) for row in answer_rows}
        cumulative = {p["user_id"]: 0 for p in players}
        questions = []
        qrows = con.execute(
            "SELECT position,question_id FROM battle_questions WHERE battle_id=? ORDER BY position",
            (battle_id,),
        ).fetchall()
        for item in qrows:
            q = _get_question(question_db, str(item["question_id"]))
            if not q:
                continue
            entry = {
                "position": int(item["position"]),
                "question": q.get("question_mr") or q.get("question_en") or "",
                "question_en": q.get("question_en") or "",
                "options": {
                    "A": q.get("option_a"), "B": q.get("option_b"),
                    "C": q.get("option_c"), "D": q.get("option_d"),
                },
                "correct_option": q.get("correct_option_key"),
                "answers": {},
            }
            for player in players:
                answer = answers.get((player["user_id"], int(item["position"])))
                if answer and answer.get("is_correct"):
                    cumulative[player["user_id"]] += POINTS_PER_CORRECT
                entry["answers"][player["user_id"]] = ({
                    "selected_option": answer.get("selected_option"),
                    "is_correct": bool(answer.get("is_correct")),
                    "elapsed_ms": int(answer.get("elapsed_ms") or 0),
                    "answered_at": float(answer.get("answered_at") or 0),
                    "score_after": cumulative[player["user_id"]],
                } if answer else {
                    "selected_option": None, "is_correct": False,
                    "elapsed_ms": 0, "answered_at": None,
                    "score_after": cumulative[player["user_id"]],
                })
            questions.append(entry)
    result_players = []
    for player in players:
        result_players.append({
            "user_id": player["user_id"],
            "name": (battle["challenger_name"] if player["user_id"] == battle["challenger_id"] else battle["opponent_name"]),
            "role": player["role"],
            "status": player["status"],
            "correct": int(player["correct_count"]),
            "points": int(player["points"]),
            "accuracy": round(100 * int(player["correct_count"]) / max(1, int(battle["total_questions"])), 1),
            "answered": int(player["current_position"]),
            "total_questions": min(MAX_QUESTIONS, int(battle["total_questions"])),
            "elapsed_ms": int(player["elapsed_ms"]),
            "total_elapsed_ms": int(player["total_elapsed_ms"]),
        })
    winner = _winner(players)
    return {
        "battle_id": battle_id,
        "subject": battle["subject_name"],
        "status": battle["status"],
        "started_at": battle["started_at"],
        "finished_at": battle["finished_at"],
        "duration_seconds": DURATION_SECONDS,
        "points_per_correct": POINTS_PER_CORRECT,
        "winner_user_id": winner["user_id"] if winner else None,
        "players": result_players,
        "questions": questions,
    }
