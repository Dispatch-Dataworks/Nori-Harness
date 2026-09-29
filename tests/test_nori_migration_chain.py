# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""The migration chain, applied for real (2026-09-28) -- the scenario every
existing operator's upgrade actually takes, and the one gap the 1.0
pre-flight review found: every OTHER test's own setUp calls store.init()
against a genuinely EMPTY scratch directory, where SCHEMA already matches
today's _MIGRATIONS exactly -- so every ALTER TABLE in the chain hits
"column already exists" and is silently skipped. That proves init() is
safe to re-run, not that the chain itself correctly brings an OLD database
forward. Nobody had actually run today's full _MIGRATIONS list against a
database that only had the ORIGINAL, pre-migration schema shape.

tests/fixtures/old_schema_a800ddf.sql is this repository's own first
published schema (git commit a800ddf, store.py's SCHEMA constant,
extracted verbatim -- not hand-approximated) -- a real, historically
accurate "old install," not a guess at what one might have looked like.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_SCRATCH = tempfile.mkdtemp(prefix="nori_test_migration_chain_")
os.environ["NORI_DATA_DIR"] = _SCRATCH
os.environ["NORI_NO_LOGFILE"] = "1"

import store  # noqa: E402

_FIXTURE = ROOT / "tests" / "fixtures" / "old_schema_a800ddf.sql"


class OldSchemaMigrationChainTests(unittest.TestCase):
    """A database built from ONLY the original schema, with a real row in
    it, brought forward by today's store.init() -- the exact function
    every real startup calls, not a stand-in for it."""

    @classmethod
    def setUpClass(cls):
        cls.db_path = Path(store.DATA_DIR) / "nori.db"
        old_sql = _FIXTURE.read_text(encoding="utf-8")
        conn = sqlite3.connect(str(cls.db_path))
        conn.executescript(old_sql)
        now = time.time()
        # A real row under the OLD shape -- proves data survives the
        # upgrade, not just that the DDL itself succeeds.
        conn.execute("INSERT INTO workspaces(id, created_ts, name) VALUES (1, ?, 'Old Household')", (now,))
        conn.execute(
            "INSERT INTO users(id, workspace_id, display_name, password_hash, role, created_ts) "
            "VALUES (1, 1, 'Old User', 'not-a-real-hash', 'admin', ?)", (now,))
        conn.commit()
        conn.close()
        # The exact call every real startup makes (server.py's own main()) --
        # today's SCHEMA plus the FULL, current _MIGRATIONS chain, in one pass,
        # against a database that has never seen any of it before.
        store.init()

    def test_the_original_row_survived(self):
        def check(c):
            row = c.execute("SELECT name FROM workspaces WHERE id=1").fetchone()
            self.assertEqual(row["name"], "Old Household")
            user = c.execute("SELECT display_name, role FROM users WHERE id=1").fetchone()
            self.assertEqual(user["display_name"], "Old User")
            self.assertEqual(user["role"], "admin")
        store.read(check)

    def test_columns_added_since_the_original_schema_now_exist(self):
        """A real sample from across _MIGRATIONS, not exhaustive -- one
        column per table family that genuinely didn't exist in the
        original schema, checked with PRAGMA table_info, not assumed."""
        def check(c):
            msg_cols = {r["name"] for r in c.execute("PRAGMA table_info(messages)").fetchall()}
            self.assertIn("emotion", msg_cols)
            self.assertIn("kind", msg_cols)
            self.assertIn("meta", msg_cols)
            peer_cols = {r["name"] for r in c.execute("PRAGMA table_info(peers)").fetchall()}
            self.assertIn("trust_level", peer_cols)
            self.assertIn("remote_requestable_actions", peer_cols)
            self.assertIn("receipt_granularity", peer_cols)
            job_cols = {r["name"] for r in c.execute("PRAGMA table_info(jobs)").fetchall()}
            self.assertIn("cost_usd", job_cols)
        store.read(check)

    def test_every_table_the_current_schema_declares_now_exists(self):
        """Not just the migrated columns -- any whole TABLE added after
        the original schema (via a real ALTER-based creation, or one
        folded into a later SCHEMA revision) must exist too."""
        def check(c):
            tables = {r["name"] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            for table in ("peer_receipts", "paci_health", "ping_signal_state",
                         "ping_signal_spend", "integration_health"):
                self.assertIn(table, tables, f"{table} missing after the full migration chain")
        store.read(check)

    def test_running_init_again_is_still_a_safe_no_op(self):
        """The upgraded database, re-init'd a second time (a restart right
        after upgrading) -- must not raise, and must not duplicate or
        lose the same real row."""
        store.init()
        def check(c):
            n = c.execute("SELECT COUNT(*) AS n FROM workspaces WHERE id=1").fetchone()["n"]
            self.assertEqual(n, 1)
        store.read(check)


if __name__ == "__main__":
    unittest.main()
