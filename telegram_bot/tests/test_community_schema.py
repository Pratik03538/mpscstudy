from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

BOT_DIR = Path(__file__).resolve().parents[1] / "bot"
sys.path.insert(0, str(BOT_DIR))

from db import initialize_state_db
from community import initialize_community_db


class CommunitySchemaTests(unittest.TestCase):
    def test_state_and_community_tables_initialize(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.sqlite3"
            initialize_state_db(path)
            initialize_community_db(path)
            con = sqlite3.connect(path)
            try:
                names = {
                    row[0] for row in con.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                expected = {
                    "quiz_sessions", "quiz_session_questions",
                    "community_profiles", "battles", "battle_questions",
                    "battle_answers", "battle_player_totals", "battle_awards",
                    "battle_players", "battle_declined_users",
                    "official_events", "official_event_questions", "official_event_answers",
                }
                self.assertTrue(expected.issubset(names), expected - names)
                self.assertEqual(
                    con.execute("PRAGMA integrity_check").fetchone()[0], "ok"
                )
            finally:
                con.close()


if __name__ == "__main__":
    unittest.main()
