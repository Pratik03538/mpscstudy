from __future__ import annotations

import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOT_DIR = ROOT / "bot"
sys.path.insert(0, str(BOT_DIR))

from db import initialize_state_db
from community import initialize_community_db
from battle_engine import (
    begin_battle, get_battle_history, get_battle_room,
    initialize_battle_schema, submit_battle_answer,
)


class IndependentBattleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state_db = self.root / "state.sqlite3"
        self.question_db = self.root / "questions.sqlite3"
        initialize_state_db(self.state_db)
        initialize_community_db(self.state_db)
        qcon = sqlite3.connect(self.question_db)
        qcon.executescript("""
            CREATE TABLE subjects(
                subject_id TEXT PRIMARY KEY,
                subject_name_en TEXT,
                subject_name_mr TEXT
            );
            CREATE TABLE questions(
                question_id TEXT PRIMARY KEY,
                subject_id TEXT,
                question_mr TEXT,
                question_en TEXT,
                option_a TEXT,
                option_b TEXT,
                option_c TEXT,
                option_d TEXT,
                correct_option_key TEXT
            );
            INSERT INTO subjects VALUES('history','History','इतिहास');
        """)
        for index in range(10):
            qcon.execute("""
                INSERT INTO questions VALUES(?,?,?,?,?,?,?,?,?)
            """, (
                f"q{index}", "history", f"History question {index+1}",
                f"History question {index+1}", "Correct", "Wrong B", "Wrong C", "Wrong D", "A",
            ))
        qcon.commit()
        qcon.close()
        self.battle_id = "testbattle"
        with sqlite3.connect(self.state_db) as con:
            con.execute("""
                INSERT INTO battles(
                    battle_id,challenger_id,challenger_name,opponent_id,opponent_name,
                    subject_id,subject_name,status,challenger_ready,opponent_ready,
                    total_questions,created_at,engine_version
                ) VALUES(?,?,?,?,?,? ,?,'countdown',1,1,10,?,2)
            """, (
                self.battle_id, "101", "Alice", "202", "Bob",
                "history", "History", time.time(),
            ))
            con.executemany(
                "INSERT INTO battle_questions(battle_id,position,question_id) VALUES(?,?,?)",
                [(self.battle_id, i, f"q{i}") for i in range(10)],
            )
        initialize_battle_schema(self.state_db)
        begin_battle(self.state_db, self.battle_id)

    def tearDown(self):
        self.temp.cleanup()

    def test_each_player_advances_without_waiting_for_opponent(self):
        alice = submit_battle_answer(
            self.state_db, self.question_db, self.battle_id, "101", 0, "A"
        )
        self.assertEqual(alice["me"]["answered"], 1)
        self.assertEqual(alice["opponent"]["answered"], 0)
        self.assertEqual(alice["status"], "active")

        bob = get_battle_room(self.state_db, self.question_db, self.battle_id, "202")
        self.assertEqual(bob["me"]["answered"], 0)
        self.assertEqual(bob["opponent"]["answered"], 1)

        bob = submit_battle_answer(
            self.state_db, self.question_db, self.battle_id, "202", 0, "B"
        )
        self.assertEqual(bob["me"]["answered"], 1)
        self.assertEqual(bob["opponent"]["answered"], 1)

    def test_scores_points_accuracy_and_full_answer_audit(self):
        for position in range(10):
            submit_battle_answer(
                self.state_db, self.question_db, self.battle_id, "101", position, "A"
            )
        alice_waiting = get_battle_room(
            self.state_db, self.question_db, self.battle_id, "101"
        )
        self.assertEqual(alice_waiting["me"]["status"], "completed")
        self.assertEqual(alice_waiting["status"], "active")

        for position in range(10):
            submit_battle_answer(
                self.state_db, self.question_db, self.battle_id, "202", position, "B"
            )
        history = get_battle_history(self.state_db, self.question_db, self.battle_id)
        self.assertEqual(history["status"], "completed")
        self.assertEqual(history["winner_user_id"], "101")
        self.assertEqual(history["players"][0]["points"], 1000)
        self.assertEqual(history["players"][0]["accuracy"], 100.0)
        self.assertEqual(history["players"][1]["points"], 0)
        self.assertEqual(len(history["questions"]), 10)
        q1 = history["questions"][0]
        self.assertEqual(q1["answers"]["101"]["selected_option"], "A")
        self.assertEqual(q1["answers"]["101"]["score_after"], 100)
        self.assertEqual(q1["answers"]["202"]["selected_option"], "B")
        self.assertFalse(q1["answers"]["202"]["is_correct"])

        with sqlite3.connect(self.state_db) as con:
            alice_totals = con.execute(
                "SELECT battles_played,wins,correct_total FROM battle_player_totals WHERE user_id='101'"
            ).fetchone()
            bob_totals = con.execute(
                "SELECT battles_played,wins,correct_total FROM battle_player_totals WHERE user_id='202'"
            ).fetchone()
        self.assertEqual(alice_totals, (1, 1, 10))
        self.assertEqual(bob_totals, (1, 0, 0))

    def test_ten_minute_deadline_finishes_inactive_players(self):
        with sqlite3.connect(self.state_db) as con:
            con.execute(
                "UPDATE battles SET deadline_at=? WHERE battle_id=?",
                (time.time() - 2, self.battle_id),
            )
        state = get_battle_room(
            self.state_db, self.question_db, self.battle_id, "101"
        )
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["me"]["status"], "timed_out")
        history = get_battle_history(self.state_db, self.question_db, self.battle_id)
        self.assertEqual(len(history["questions"]), 10)
        self.assertTrue(all(
            q["answers"]["101"]["selected_option"] is None
            and q["answers"]["202"]["selected_option"] is None
            for q in history["questions"]
        ))


if __name__ == "__main__":
    unittest.main()
