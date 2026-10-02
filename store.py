# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""SQLite storage for Nori. One WAL-mode file, one writer lock — same shape
as a sibling application/store.py, retyped rather than imported (no shared code between
the two apps).

This module owns the connection primitives, the schema, and migrations.
Each subsystem (accounts, memory, tools, jobs, ...) gets its own module that
is the ONLY place raw SQL for its tables is written — not ad-hoc queries
scattered through every tool implementation. That's a deliberate "narrow
storage abstraction" decision: it's what makes later encryption,
export, or deletion a change in one place instead of a rewrite, and it's
what lets the tool-dispatch enforcement point (Phase 4) trust that a table's
own accessor functions are the only path to it.

workspaces   the shared household container — one or more users belong to one.
users        per-user identity: role, auth, invite state. Belongs to a workspace.
sessions     web sessions, user-scoped. A session never outlives its user's
             deactivation (see accounts.py).
messages     conversation, per user — never shared across users. See conversation.py.

memory       typed, per-user facts. See memory.py.
memory_events audit trail for memory.py's writers.

More tables land per build phase — this file grows
the same way a sibling application/store.py did, feature by feature, not all at once.

── Migration posture (2026-09-11) ──────────────────────────────────────────
The operator intends to move to MySQL (or another real relational database)
at Docker time. This is a POC, not a production system -- so the goal here
isn't a database-agnostic abstraction layer (that would be over-engineering
for a database migration that isn't happening yet), it's keeping the actual
migration TRACTABLE: every table uses portable column types (INTEGER/TEXT/
REAL only; booleans as INTEGER 0/1, which is how MySQL represents them too),
Unix-epoch REAL timestamps everywhere (not SQLite date strings -- trivially
a DOUBLE or BIGINT column in MySQL), `id INTEGER PRIMARY KEY` + `cursor.
lastrowid` (the standard portable pattern, not raw ROWID), and no ON
CONFLICT/UPSERT syntax anywhere in this app (SQLite's and MySQL's upsert
syntax genuinely differ -- avoided rather than papered over). SQL stays
confined to store.py and each subsystem's own accessor module (accounts.py,
conversation.py, memory.py, ...) -- tools.py and server.py never see a raw
query. That's what actually makes the eventual migration a schema+driver
swap instead of a rewrite: it only has to happen in these narrow places.

What's deliberately left SQLite-only, because the portable version would be
real, undue effort for a POC -- a list, not a surprise, at migration time:
  - `connect()`'s PRAGMA calls (WAL, foreign_keys, busy_timeout) and the
    single in-process `_WRITE_LOCK` are SQLite's own concurrency model.
    MySQL's is different by nature (InnoDB locking, connection pooling,
    `innodb_lock_wait_timeout`) -- this isn't a portability gap to close,
    connect() is expected to be rewritten wholesale for a MySQL driver.
  - Inline `REFERENCES` on a column definition (SQLite style) is used
    throughout rather than a separate `FOREIGN KEY (...) REFERENCES ...`
    table constraint -- confirm the target engine's equivalent at migration
    time rather than assuming the inline form behaves identically.
  - CHECK constraints are NOT used for enum-like columns (role, status,
    type, ...) even though SQLite supports them -- MySQL silently ignored
    CHECK constraints before 8.0.16, a real historical portability trap.
    Validation for these lives in Python instead, which needed to be true
    everywhere anyway.
  - No SQLite-specific full-text search (FTS5) anywhere -- memory.py's
    `recall()` uses a plain `LIKE` search instead, both because it's
    portable and because it's genuinely sufficient at household scale;
    MySQL's FULLTEXT indexes are a different mechanism/syntax entirely and
    would need real reimplementation, not translation, if search ever
    outgrows LIKE.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path

# NORI_DATA_DIR points the whole app at a different data directory — used to
# run a throwaway test instance without going anywhere near a real one.
# Mirrors the same convention a sibling application uses for its own data
# directory override; the two are unrelated env vars, never shared.
#
# NORI_LIVE (2026-09-13, live-data incident #5 -- a verification script hit
# production a sibling application data because neither this nor NORI_DATA_DIR was set,
# and the old default silently fell through to the real database) is the
# other half of the guard: nori_ctl.ps1's own Start-Nori sets this before
# ever launching the real server, so the actual service is unaffected. A
# bare `python -c` or ad-hoc script started outside that script sets
# neither, and now gets a loud, immediate refusal instead of quietly
# landing on production. Inverts the old default on purpose -- the common
# case (a one-off script) used to be the dangerous one; now it has to
# name what it wants.
# DATA_DIR/DB_PATH are FUNCTIONS/PROPERTIES, re-resolved on every access, never a plain module constant computed
# once at import (2026-09-23, live-data incident #6: the identical bug in promptdoc.py/persona.py let a
# contaminated multi-file test run overwrite the real persona.md for real -- this store.py constant was the
# ORIGINAL instance of the same pattern, already diagnosed in OUTSTANDING.md and deferred; deferring it is what
# let the second one happen). See tests/test_nori_path_safety.py for the adversarial proof this actually works.
_REAL_DATA_DIR = Path(__file__).resolve().parent / "data"   # the live directory itself -- never returned unless NORI_LIVE says so, even if NORI_DATA_DIR happens to name it explicitly


def _resolve_data_dir() -> Path:
    data_env = os.environ.get("NORI_DATA_DIR")
    live = os.environ.get("NORI_LIVE")
    if data_env:
        p = Path(data_env).resolve()
        if p == _REAL_DATA_DIR and not live:
            raise RuntimeError(
                "store.py: NORI_DATA_DIR points at the REAL live data directory, and NORI_LIVE is not set -- "
                "refusing. This is a second, independent check on top of 'unconfigured means refuse': even an "
                "EXPLICIT override cannot land on the real directory by accident (or by stale env-var bleed "
                "across test files sharing one process) without also naming NORI_LIVE=1.")
        return p
    if live:
        return _REAL_DATA_DIR
    raise RuntimeError(
        "store.py: no data directory configured -- refusing to guess. Set one of:\n"
        "  NORI_DATA_DIR=<path>   a throwaway/test instance, your own scratch directory\n"
        "  NORI_LIVE=1            yes, this really is the live production server\n"
        "                         (nori_ctl.ps1 already sets this for you -- if you're not\n"
        "                         going through it, that's usually a sign to stop)\n"
        "This refuses rather than default to the live database: a script run without either "
        "has hit production data before.")


def _db_path() -> Path:
    return _resolve_data_dir() / "nori.db"


# External callers keep writing store.DATA_DIR / store.DB_PATH unchanged -- this module-level __getattr__
# (PEP 562) resolves each fresh on every access instead of freezing it at import. Only affects access from OUTSIDE
# this module; code inside store.py itself calls _resolve_data_dir()/_db_path() directly (see connect() below) --
# a bare global reference here would NOT go through __getattr__.
_MODULE_PATHS = {"DATA_DIR": _resolve_data_dir, "DB_PATH": _db_path}


def __getattr__(name: str):
    if name in _MODULE_PATHS:
        return _MODULE_PATHS[name]()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

_WRITE_LOCK = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS workspaces (
    id         INTEGER PRIMARY KEY,
    created_ts REAL NOT NULL,
    name       TEXT NOT NULL DEFAULT 'My household'
);

-- role: admin | member. status: pending_invite | active | deactivated --
-- see accounts.py for the state machine. totp_secret is schema support for
-- 2FA ("cheap to add the column now, painful to migrate
-- under pressure later") -- unenforced, nothing reads it yet.
CREATE TABLE IF NOT EXISTS users (
    id                 INTEGER PRIMARY KEY,
    workspace_id       INTEGER NOT NULL REFERENCES workspaces(id),
    role               TEXT NOT NULL DEFAULT 'member',
    display_name       TEXT NOT NULL,
    password_hash      TEXT,
    totp_secret        TEXT,
    status             TEXT NOT NULL DEFAULT 'pending_invite',
    created_ts         REAL NOT NULL,
    created_by         INTEGER REFERENCES users(id),
    invite_token_hash  TEXT,
    invite_expires_ts  REAL,
    activated_ts       REAL,
    deactivated_ts     REAL
);
CREATE INDEX IF NOT EXISTS ix_users_workspace ON users(workspace_id, status);

-- Session tokens are stored hashed, same reasoning as a password -- the raw
-- token only ever lives in the cookie and in the moment it's checked, never
-- at rest in a form an attacker with read access to the DB could replay
-- directly.
CREATE TABLE IF NOT EXISTS sessions (
    token_hash   TEXT PRIMARY KEY,
    user_id      INTEGER NOT NULL REFERENCES users(id),
    workspace_id INTEGER NOT NULL REFERENCES workspaces(id),
    role         TEXT NOT NULL,
    created_ts   REAL NOT NULL,
    expires_ts   REAL NOT NULL,
    csrf         TEXT NOT NULL,
    client_ip    TEXT
);
CREATE INDEX IF NOT EXISTS ix_sessions_user ON sessions(user_id);

-- Conversation, per user -- never shared across users even within the same
-- workspace (a deliberate multi-user decision: separate conversations,
-- full stop). role: user | assistant.
CREATE TABLE IF NOT EXISTS messages (
    id      INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    ts      REAL NOT NULL,
    role    TEXT NOT NULL,
    content TEXT NOT NULL,
    emotion TEXT,
    kind    TEXT NOT NULL DEFAULT 'chat',
    meta    TEXT
);
CREATE INDEX IF NOT EXISTS ix_messages_user ON messages(user_id, id);

-- Compaction (2026-09-12, see compaction.py): one row per natural
-- conversation session older than the live raw window, anchored to the
-- real [lo_id, hi_id] message span it covers -- the span is what makes
-- this recoverable (search_history reads straight past it to the real
-- messages) and what lets a segment be regenerated independently of
-- every other one. Never fed forward as input to a later summary --
-- always re-derived from lo_id..hi_id directly -- the fix for a real,
-- observed drift bug (a sibling application's old single-rolling-summary compounded
-- its own prior output every cycle). A CLOSED segment
-- (a newer one already exists beyond it) is immutable and never
-- regenerated; only the most recent, still-growing segment ever gets a
-- fresh summary as its own span extends.
CREATE TABLE IF NOT EXISTS conversation_segments (
    id           INTEGER PRIMARY KEY,
    user_id      INTEGER NOT NULL REFERENCES users(id),
    lo_id        INTEGER NOT NULL,
    hi_id        INTEGER NOT NULL,
    lo_ts        REAL NOT NULL,
    hi_ts        REAL NOT NULL,
    text         TEXT NOT NULL,
    generated_ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_segments_user ON conversation_segments(user_id, hi_id);

-- Generic per-user/per-workspace settings (2026-09-12) -- the only module
-- with raw SQL against this is config.py. Built now because the
-- scheduler needs somewhere to keep per-user ping preferences before a
-- real settings UI exists (Phase 10 reuses this table as-is, doesn't
-- replace it).
CREATE TABLE IF NOT EXISTS settings (
    scope      TEXT NOT NULL,
    scope_id   INTEGER NOT NULL,
    key        TEXT NOT NULL,
    v          TEXT NOT NULL,
    updated_ts REAL NOT NULL,
    PRIMARY KEY (scope, scope_id, key)
);

-- Emotional state (2026-09-12), per user -- see emotion.py. One row per
-- user, overwritten in place by set_emotion; the *effective* current
-- state (with decay applied) is always computed at read time from
-- (state, updated_ts), never written back by a background process --
-- same "derived, not stored" pattern a sibling application uses for overdue tasks.
CREATE TABLE IF NOT EXISTS emotion_state (
    user_id    INTEGER PRIMARY KEY REFERENCES users(id),
    state      TEXT NOT NULL,
    updated_ts REAL NOT NULL
);

-- Sub-agent roster (2026-09-12) -- admin-curated, see sub_agents.py. She
-- picks from this list, never invents an endpoint or model herself.
-- model/base_url/api_key_enc were the original per-agent endpoint+key
-- (api_key_enc encrypted at rest via crypto.py); superseded 2026-09-30 by
-- model_id, a real FK into the models roster (provider + auth already
-- encapsulated there, see providers.py) -- kept as NOT NULL/empty-string
-- columns rather than dropped, same "ALTER-only, never destructive"
-- migration rule as everywhere else in this file.
-- tool_call_limit (2026-09-14, operator's own ask: "one agent might get
-- zero tool calls, another 100 per run") -- 0 means genuinely no tools,
-- the original behaviour, still the default for every new entry so
-- nothing changes for a roster nobody has touched. tool_byte_limit caps
-- cumulative tool-RESULT bytes per job, independent of the call count --
-- see jobs.py's own reasoning for why both matter separately.
CREATE TABLE IF NOT EXISTS sub_agents (
    id              INTEGER PRIMARY KEY,
    label           TEXT NOT NULL UNIQUE,
    model           TEXT NOT NULL,
    base_url        TEXT NOT NULL,
    api_key_enc     TEXT NOT NULL,
    enabled         INTEGER NOT NULL DEFAULT 1,
    tool_call_limit INTEGER NOT NULL DEFAULT 0,
    tool_byte_limit INTEGER NOT NULL DEFAULT 2000000,
    created_ts      REAL NOT NULL,
    created_by      INTEGER NOT NULL REFERENCES users(id),
    -- model_id (2026-09-30, see providers.py/models.py) -- a sub-agent now
    -- points at a roster Model (alias -> provider -> real model name)
    -- instead of free-typing its own model/base_url/api_key. The three
    -- old columns above stay (SQLite can't cheaply drop a column) but are
    -- no longer written by new rows; NULL model_id on an old row means
    -- "still running the pre-migration way until an admin repoints it."
    model_id        INTEGER REFERENCES models(id)
);

-- Providers (2026-09-30, see providers.py) -- a configured connection to
-- an LLM backend: OpenRouter with an API key, or an OAuth-connected
-- subscription (Anthropic/OpenAI/GitHub Copilot). There is deliberately
-- no default/seeded provider -- an instance with nothing configured here
-- has no model access at all until an admin adds one, rather than quietly
-- assuming OpenRouter the way this table's predecessor did.
CREATE TABLE IF NOT EXISTS providers (
    id                 INTEGER PRIMARY KEY,
    workspace_id       INTEGER NOT NULL REFERENCES workspaces(id),
    type               TEXT NOT NULL,
    label              TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'unconfigured',
    api_key_enc        TEXT,
    access_token_enc   TEXT,
    refresh_token_enc  TEXT,
    token_expires_ts   REAL,
    account_meta       TEXT,
    pending_enc        TEXT,
    enabled            INTEGER NOT NULL DEFAULT 1,
    created_ts         REAL NOT NULL,
    created_by         INTEGER NOT NULL REFERENCES users(id)
);

-- Model roster (2026-09-12, reworked 2026-09-30 -- see models.py). Each
-- row is an alias + the provider it runs through + that provider's own
-- model-name string. No more auto-seeded "tested trio" -- those were
-- implicitly OpenRouter-only, which doesn't generalize now that a model
-- can belong to any provider type; an admin adds their own from the roster
-- of providers they've actually configured. Disabling (not deleting)
-- takes a model out of circulation the same config-preserved way the MCP
-- toggles already work. reasoning_effort is nullable -- NULL means "don't
-- send the parameter at all" for a model that doesn't accept it, distinct
-- from an empty string, which would still be sent as a (probably
-- rejected) value.
CREATE TABLE IF NOT EXISTS models (
    id               INTEGER PRIMARY KEY,
    provider_id      INTEGER REFERENCES providers(id),
    model_name       TEXT NOT NULL,
    alias            TEXT NOT NULL,
    reasoning_effort TEXT,
    enabled          INTEGER NOT NULL DEFAULT 1,
    seeded           INTEGER NOT NULL DEFAULT 0,
    created_ts       REAL NOT NULL
);

-- model_chain (primary + fallback model ordering, 2026-09-30) is created
-- further down in init(), AFTER the models-rebuild migration runs, not
-- here -- see that block's own comment for why: creating it here, before
-- an existing install's `models` gets rebuilt, would compile its FK
-- against the OLD `models` table, which SQLite's own RENAME TABLE then
-- silently repoints at the throwaway `models_pre_provider_rework` name
-- (SQLite updates every other table's REFERENCES clause on a rename),
-- permanently broken the moment that throwaway table is dropped.

-- Sub-agent jobs (2026-09-12) -- see jobs.py. Dispatched, run in a
-- background thread, never auto-retried on failure (same standing rule as
-- a sibling application's never-auto-retry-an-orphaned-message). `seen` distinguishes
-- an unread completed result from one already checked, so the digest line
-- can say "N completed, unread" without a viewer table.
-- cost_usd/cost_unavailable/prompt_tokens/completion_tokens (2026-09-14)
-- mirror conversation.py's own cost_meta() shape exactly, so "what is
-- this costing" stays one consistent pattern across real turns and
-- sub-agent jobs -- see jobs.cost_summary(). tool_calls_used/
-- tool_bytes_used are this job's own actual usage against its agent's
-- configured caps, kept for visibility even after the job finishes.
-- woken_ts (2026-09-14) -- NULL until a job's completion has triggered a
-- real turn (jobs._trigger_turn_for_job), then stamped; today's
-- architecture only ever calls that once per job (no retry path exists),
-- so nothing currently depends on this for correctness -- it exists so
-- a future code path can't accidentally re-wake her for the same
-- completion twice, and so the history view can show whether a given
-- job ever actually reached her.
CREATE TABLE IF NOT EXISTS jobs (
    id                 INTEGER PRIMARY KEY,
    user_id            INTEGER NOT NULL REFERENCES users(id),
    sub_agent_id       INTEGER NOT NULL REFERENCES sub_agents(id),
    task               TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'queued',
    result             TEXT,
    error              TEXT,
    seen               INTEGER NOT NULL DEFAULT 0,
    created_ts         REAL NOT NULL,
    started_ts         REAL,
    finished_ts        REAL,
    timeout_s          INTEGER NOT NULL DEFAULT 120,
    cost_usd           REAL,
    cost_unavailable   INTEGER NOT NULL DEFAULT 0,
    prompt_tokens      INTEGER NOT NULL DEFAULT 0,
    completion_tokens  INTEGER NOT NULL DEFAULT 0,
    tool_calls_used    INTEGER NOT NULL DEFAULT 0,
    tool_bytes_used    INTEGER NOT NULL DEFAULT 0,
    woken_ts           REAL,
    raw_file_access    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_jobs_user ON jobs(user_id, status);

-- Connected third-party accounts (2026-09-12) -- see connected_accounts.py
-- (storage) and oauth.py (the actual flow). Tokens encrypted at rest via
-- crypto.py, same as the sub-agent roster's keys -- these are, if
-- anything, more sensitive: the live keys to someone's real mailbox.
-- oauth_state holds the pending CSRF nonce for an in-flight connect
-- attempt; cleared once the callback completes.
CREATE TABLE IF NOT EXISTS connected_accounts (
    id                INTEGER PRIMARY KEY,
    user_id           INTEGER NOT NULL REFERENCES users(id),
    provider          TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'not_connected',
    access_token_enc  TEXT,
    refresh_token_enc TEXT,
    token_expires_ts  REAL,
    connected_ts      REAL,
    meta              TEXT,
    oauth_state       TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_connected_accounts ON connected_accounts(user_id, provider);

-- Household inventory (2026-09-12) -- see household.py. Workspace-scoped
-- (shared across everyone in the household, unlike memory/conversation) --
-- the first real consumer of shared writes that Phase 5 deliberately
-- deferred until a concrete feature actually needed the RBAC decision.
CREATE TABLE IF NOT EXISTS household_items (
    id           INTEGER PRIMARY KEY,
    workspace_id INTEGER NOT NULL REFERENCES workspaces(id),
    name         TEXT NOT NULL,
    quantity     TEXT,
    status       TEXT NOT NULL DEFAULT 'ok',
    updated_ts   REAL NOT NULL,
    updated_by   INTEGER NOT NULL REFERENCES users(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_household_items ON household_items(workspace_id, name);

-- Meal planning (2026-09-12) -- see meals.py. Also workspace-scoped, same
-- reasoning. meal_date is stored as 'YYYY-MM-DD' text -- portable and
-- sortable without needing a real DATE column type.
CREATE TABLE IF NOT EXISTS meal_plan (
    id           INTEGER PRIMARY KEY,
    workspace_id INTEGER NOT NULL REFERENCES workspaces(id),
    meal_date    TEXT NOT NULL,
    meal_type    TEXT NOT NULL DEFAULT 'dinner',
    description  TEXT NOT NULL,
    updated_ts   REAL NOT NULL,
    updated_by   INTEGER NOT NULL REFERENCES users(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_meal_plan ON meal_plan(workspace_id, meal_date, meal_type);

-- Generated tools (2026-09-12) -- see tool_builder.py for the full
-- lifecycle. Append-only per name: an edit is a new row with version+1,
-- never an overwrite -- approved/enabled/available_to all reset to
-- their most-restrictive default on a new version, so an edit to a
-- live tool can never silently inherit its predecessor's trust.
CREATE TABLE IF NOT EXISTS tool_drafts (
    id             INTEGER PRIMARY KEY,
    name           TEXT NOT NULL,
    version        INTEGER NOT NULL,
    description    TEXT NOT NULL,
    schema_json    TEXT NOT NULL,
    code           TEXT NOT NULL,
    risk_tier      TEXT NOT NULL DEFAULT 'C',
    available_to   TEXT NOT NULL DEFAULT 'admin_only',
    approved       INTEGER NOT NULL DEFAULT 0,
    enabled        INTEGER NOT NULL DEFAULT 0,
    static_flags   TEXT,
    dry_run_ok     INTEGER,
    dry_run_output TEXT,
    created_by     INTEGER NOT NULL REFERENCES users(id),
    created_ts     REAL NOT NULL,
    approved_by    INTEGER REFERENCES users(id),
    approved_ts    REAL
);
CREATE INDEX IF NOT EXISTS ix_tool_drafts_name ON tool_drafts(name, version);

-- Own audit table, not the shared events/memory_events tables -- same
-- reasoning as every other subsystem's own event log: a future scope
-- wipe elsewhere shouldn't erase tool-builder provenance.
CREATE TABLE IF NOT EXISTS tool_audit_events (
    id       INTEGER PRIMARY KEY,
    ts       REAL NOT NULL,
    draft_id INTEGER NOT NULL,
    action   TEXT NOT NULL,
    actor    TEXT NOT NULL,
    note     TEXT
);
CREATE INDEX IF NOT EXISTS ix_tool_audit_draft ON tool_audit_events(draft_id, ts);

-- Typed memory (2026-09-11) -- see memory.py for the fixed type taxonomy
-- and the query interface this exists to support. scope is 'user' for
-- everything right now (household-shared writes are a later, deliberate
-- decision, not built yet); the column exists so that
-- doesn't need a migration when it lands. user_id is always the owner
-- (even for a future household-scoped row, provenance of who added it);
-- workspace_id rides along so a workspace-wide query never needs a join
-- through users just to get there.
CREATE TABLE IF NOT EXISTS memory (
    id           INTEGER PRIMARY KEY,
    user_id      INTEGER NOT NULL REFERENCES users(id),
    workspace_id INTEGER NOT NULL REFERENCES workspaces(id),
    scope        TEXT NOT NULL DEFAULT 'user',
    type         TEXT NOT NULL,
    value        TEXT NOT NULL,
    tags         TEXT,
    source       TEXT NOT NULL DEFAULT 'tool',
    pinned       INTEGER NOT NULL DEFAULT 0,
    created_ts   REAL NOT NULL,
    updated_ts   REAL NOT NULL,
    last_used_ts REAL,
    expires_ts   REAL,
    safety_tier  INTEGER NOT NULL DEFAULT 0,
    embedding_json  TEXT,
    embedding_model TEXT
);
CREATE INDEX IF NOT EXISTS ix_memory_user ON memory(user_id, type, pinned);

-- Audit trail for memory.py's writers (remember/update_memory/forget/
-- pin_memory) -- own table, not sharing space with anything a future reset
-- scope might wipe wholesale, same reasoning a sibling application applied to
-- memory_events/media_log/consequence_events.
CREATE TABLE IF NOT EXISTS memory_events (
    id        INTEGER PRIMARY KEY,
    ts        REAL NOT NULL,
    memory_id INTEGER NOT NULL,
    user_id   INTEGER NOT NULL REFERENCES users(id),
    action    TEXT NOT NULL,
    actor     TEXT NOT NULL,
    type      TEXT,
    value     TEXT,
    note      TEXT
);
CREATE INDEX IF NOT EXISTS ix_memory_events_mid ON memory_events(memory_id, ts);

-- Backing store for memory.py's NodryaMemoryBackend (2026-09-25) -- the
-- "memory" table above serves LocalMemoryBackend; this one serves the
-- Nodrya-backed counterpart, same shape as memory's own columns so the
-- two implementations of the pluggable-backend interface stay easy to
-- compare. Off by default: nothing writes here unless a workspace's own
-- memory_backend setting is "nodrya". A row is this backend's local,
-- instantly-readable mirror of a note living in Nodrya (nodrya_note_id) --
-- see memory.py's own NodryaMemoryBackend docstring for why a write-
-- through cache exists at all (instant read-back of her own writes,
-- independent of Nodrya's async embedding cadence).
CREATE TABLE IF NOT EXISTS nodrya_memory (
    id             INTEGER PRIMARY KEY,
    user_id        INTEGER NOT NULL REFERENCES users(id),
    workspace_id   INTEGER NOT NULL REFERENCES workspaces(id),
    nodrya_note_id INTEGER,
    type           TEXT NOT NULL,
    value          TEXT NOT NULL,
    tags           TEXT,
    source         TEXT NOT NULL DEFAULT 'tool',
    pinned         INTEGER NOT NULL DEFAULT 0,
    safety_tier    INTEGER NOT NULL DEFAULT 0,
    created_ts     REAL NOT NULL,
    updated_ts     REAL NOT NULL,
    embedding_json  TEXT,
    embedding_model TEXT
);
CREATE INDEX IF NOT EXISTS ix_nodrya_memory_user ON nodrya_memory(user_id, type, pinned);

-- Ping-signal bookkeeping (2026-09-26, scheduler.py's per-signal cooldown
-- and rotation, see that module's own docstring). last_checked_ts bounds
-- how often an EXPENSIVE signal's own function is even called, regardless
-- of outcome (the email signal's real cost driver); last_fired_ts is when
-- it last actually won and got said to him, the per-(signal, dedup_token)
-- cooldown and the rotation-fairness ordering both read from it.
-- dedup_token defaults to '' (the signal as a whole); a signal like
-- calendar that needs to dedupe PER EVENT rather than as a whole passes
-- its own token (the event id) instead, so a second, different event
-- isn't silently blocked by the first one's own cooldown.
CREATE TABLE IF NOT EXISTS ping_signal_state (
    user_id         INTEGER NOT NULL REFERENCES users(id),
    signal_key      TEXT NOT NULL,
    dedup_token     TEXT NOT NULL DEFAULT '',
    last_checked_ts REAL,
    last_fired_ts   REAL,
    PRIMARY KEY (user_id, signal_key, dedup_token)
);
CREATE INDEX IF NOT EXISTS ix_ping_signal_state_user ON ping_signal_state(user_id, signal_key);

-- Real, observable spend for ping signals that cost something beyond a
-- local read (today: the email signal's triage calls) -- the operator, 2026-09-26:
-- "he should be able to see what the email signal has cost and whether
-- it's been skipping... a cap that silently disables a feature he
-- enabled is the failure mode we keep fixing." Same shape medialog's own
-- image spend ledger already uses. skipped=1 rows record a check that
-- was refused for being over budget (cost_usd NULL) -- kept, not
-- discarded, so the cost page can show "skipped N times today" rather
-- than the signal just quietly going silent with no visible reason.
CREATE TABLE IF NOT EXISTS ping_signal_spend (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    signal_key TEXT NOT NULL,
    ts         REAL NOT NULL,
    cost_usd   REAL,
    skipped    INTEGER NOT NULL DEFAULT 0,
    note       TEXT
);
CREATE INDEX IF NOT EXISTS ix_ping_signal_spend_user ON ping_signal_spend(user_id, signal_key, ts);

-- Reflection bookkeeping (2026-09-12) -- see memory.py's reflect() and
-- due_for_reflection(). One row per user: when their last successful
-- reflection pass completed. A FAILED attempt never advances this (set
-- only at the end of a successful reflect()), so the same backlog gets
-- retried next cycle rather than silently skipped.
CREATE TABLE IF NOT EXISTS memory_reflection_state (
    user_id            INTEGER PRIMARY KEY REFERENCES users(id),
    last_reflection_ts REAL NOT NULL
);

-- Per-user working folder (2026-09-11) -- see workfiles.py. Deliberately
-- thin: this table tracks only the two things a bare filesystem can't
-- tell you -- who created a file (never the model's word for it; set by
-- the one write path) and a cached vision caption for an image, so a
-- captioning call runs once, not once per read. Everything else (size,
-- whether something's a folder, mtime) is read live off disk, same
-- philosophy /admin/avatars already uses. Folders get no row at all --
-- nothing about "who made this empty folder" needs remembering, since
-- deleting an empty folder destroys no content regardless of who made it.
CREATE TABLE IF NOT EXISTS work_files (
    id                 INTEGER PRIMARY KEY,
    user_id            INTEGER NOT NULL REFERENCES users(id),
    rel_path           TEXT NOT NULL,
    created_by         TEXT NOT NULL DEFAULT 'user',
    created_ts         REAL NOT NULL,
    updated_ts         REAL NOT NULL,
    vision_description TEXT,
    vision_ts          REAL,
    source_url         TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_work_files ON work_files(user_id, rel_path);

-- Image generation (2026-09-14, operator's own ask: mirror a sibling application's
-- send_image, ported as two tools -- see imagegen.py). Workspace-scoped
-- (a sibling application's own media_log has no such column -- single-user app, no
-- household to share a budget across) since the daily spend cap here is
-- shared by the whole household, not per-member. Spend itself is never a
-- maintained running counter (a sibling application's own runtime.img_spend_today/
-- img_spend_total) -- computed live from this table instead, same
-- pattern jobs.cost_summary() already uses, so there's no day-rollover
-- logic to get wrong.
CREATE TABLE IF NOT EXISTS media_log (
    id             INTEGER PRIMARY KEY,
    workspace_id   INTEGER NOT NULL REFERENCES workspaces(id),
    user_id        INTEGER NOT NULL REFERENCES users(id),
    ts             REAL NOT NULL,
    kind           TEXT NOT NULL DEFAULT 'image_gen',
    purpose        TEXT NOT NULL,
    prompt         TEXT,
    final_prompt   TEXT,
    model          TEXT,
    seed           INTEGER,
    used_reference INTEGER,
    ok             INTEGER NOT NULL,
    error          TEXT,
    cost_usd       REAL,
    cost_is_actual INTEGER,
    file_id        TEXT,
    caption        TEXT
);
CREATE INDEX IF NOT EXISTS ix_media_log_workspace ON media_log(workspace_id, ts);

-- MCP client support (2026-09-12) -- see mcp_servers.py, the only module
-- with raw SQL against these two tables. scope/scope_id mirrors config.py's
-- own per-user/per-workspace axis exactly (a server connection is either
-- personal or shared, decided once at connect time, matching how
-- config.py already separates personal from shared settings).
-- credential_enc goes through crypto.py, same
-- treatment as connected_accounts' OAuth tokens and sub_agents' API keys.
CREATE TABLE IF NOT EXISTS mcp_servers (
    id             INTEGER PRIMARY KEY,
    scope          TEXT NOT NULL,
    scope_id       INTEGER NOT NULL,
    name           TEXT NOT NULL,
    url            TEXT NOT NULL,
    auth_type      TEXT NOT NULL DEFAULT 'none',
    credential_enc TEXT,
    -- What the server actually IS, in plain words an admin writes at
    -- connect time (e.g. "the operator's personal notes app") -- a tool
    -- name and a bracketed connection-name tag were found to tell Nori
    -- nothing about what a connected server is FOR: Nodrya's tools live
    -- in her own schema and she still described wanting the capability
    -- they already gave her. context.py surfaces this
    -- via mcp_servers.capability_block(); NULL/blank falls back to just
    -- the connection's own name.
    purpose        TEXT,
    enabled        INTEGER NOT NULL DEFAULT 1,
    created_ts     REAL NOT NULL,
    created_by     INTEGER NOT NULL REFERENCES users(id)
);

-- One row per tool a server has ever advertised, discovered via "sync" --
-- never auto-created live mid-conversation. A newly-discovered tool lands
-- enabled but locked to risk_tier='D' (admin-only, hard-floored by
-- tools.py itself) until an admin reviews it and deliberately widens
-- min_role/data_scope/risk_tier -- the remote server's own description of
-- a tool is never trusted to set its own blast radius.
CREATE TABLE IF NOT EXISTS mcp_server_tools (
    id            INTEGER PRIMARY KEY,
    server_id     INTEGER NOT NULL REFERENCES mcp_servers(id),
    tool_name     TEXT NOT NULL,
    description   TEXT,
    input_schema  TEXT NOT NULL,
    min_role      TEXT NOT NULL DEFAULT 'admin',
    data_scope    TEXT NOT NULL DEFAULT 'self',
    risk_tier     TEXT NOT NULL DEFAULT 'D',
    enabled       INTEGER NOT NULL DEFAULT 1,
    discovered_ts REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_mcp_server_tools ON mcp_server_tools(server_id, tool_name);

-- PACI (see the PACI specification) -- an agent-to-agent peer connection. Same
-- ownership shape as mcp_servers above (scope/scope_id, checked by
-- peers.py's own _owner_check_for), because a peer needs the identical
-- "no user identity ever travels on the wire" guarantee an MCP connection
-- already has -- see peers.py's module docstring. `purpose` here is
-- the PACI specification §10's relational prompt: written from Nori's side only,
-- never exchanged with the peer, never required to match whatever that
-- peer's own operator wrote about the same relationship on their end.
-- remote_* columns are learned from the peer's own hello_ack and used to
-- compute the effective (more-conservative-wins) shared limits -- see
-- peers.py's _effective_limits().
CREATE TABLE IF NOT EXISTS peers (
    id                 INTEGER PRIMARY KEY,
    scope              TEXT NOT NULL,
    scope_id           INTEGER NOT NULL,
    name               TEXT NOT NULL,
    purpose            TEXT,
    self_agent_id      TEXT NOT NULL,
    self_agent_name    TEXT NOT NULL DEFAULT 'Nori',
    url                TEXT NOT NULL,
    psk_enc            TEXT NOT NULL,
    turn_limit         INTEGER NOT NULL DEFAULT 10,
    cooldown_minutes   INTEGER NOT NULL DEFAULT 60,
    daily_cap          INTEGER NOT NULL DEFAULT 4,
    expiry_hours       INTEGER NOT NULL DEFAULT 24,
    resend_cap         INTEGER NOT NULL DEFAULT 1,
    -- reply_requested's own per-hour-per-direction cap (2026-09-13,
    -- the PACI specification v0.6 §9.5 -- originally shipped hardcoded at 1 and
    -- explicitly "non-configurable"; the operator reversed that the same
    -- day, same posture as message_user's cap reversal earlier -- he's
    -- tuned essentially every other threshold shipped today, this one is
    -- no different). Local only, never negotiated -- same posture as
    -- expiry_hours/resend_cap, not turn_limit/cooldown_minutes/daily_cap.
    reply_requested_cap INTEGER NOT NULL DEFAULT 3,
    enabled            INTEGER NOT NULL DEFAULT 1,
    last_hello_ts      REAL,
    remote_turn_limit  INTEGER,
    remote_cooldown_minutes INTEGER,
    remote_daily_cap   INTEGER,
    -- Nori's own local policy on what THIS peer may get done via
    -- peer{id}_act (the PACI specification §11.1) -- 'none'/'prompt'/'full'. Never
    -- sent to the peer, never settable by anything the peer says; only
    -- ever changed here, by this side's own admin. Defaults to 'prompt',
    -- not 'full' -- a newly connected peer gets no standing authority
    -- until the operator deliberately grants it.
    trust_level        TEXT NOT NULL DEFAULT 'prompt',
    -- Peer-action advertisement (2026-09-13, the PACI specification v0.6, §11.1) --
    -- the peer's own requestable-action list, learned from their last
    -- hello/hello_ack, JSON-encoded (a plain list of strings, [] if they
    -- have none or haven't told us yet). This is THEIR local registry as
    -- THEY chose to disclose it to us -- never our own _PEER_REQUESTABLE,
    -- and never a trust level (theirs or ours) crossing the wire; see
    -- peers.py's _requestable_actions_for().
    remote_requestable_actions TEXT,
    -- Per-peer screening kill switch (2026-09-15, operator's own ask) --
    -- ON by default for every existing and new peer; only ever turned off
    -- here, deliberately, one peer at a time. With it off, a received
    -- message from this peer skips ingest.summarize_untrusted() entirely
    -- -- see peers.py's _unscreened_result() and handle_inbound(). This is
    -- disabling a prompt-injection defense on a channel, not a
    -- preference, and the settings page labels it that way.
    screening_enabled  INTEGER NOT NULL DEFAULT 1,
    -- Periodic forced check-in (2026-09-13) -- last time the every-4h
    -- automatic status check-in actually ran (or was deliberately
    -- skipped for lack of real user activity) for this peer. NULL means
    -- never yet attempted; see peers.py's forced_checkin_tick().
    last_forced_checkin_ts REAL,
    -- Peer-check ping layer (2026-09-13, operator's own explicit ask: he
    -- does not accept a pending message just waiting) -- last time the
    -- pending-inbound-message check ran (or was deliberately skipped:
    -- nothing pending, or nothing could be sent anyway). A DIFFERENT
    -- clock from last_forced_checkin_ts above -- different purpose
    -- (checking for THEIR message, not sending OUR status), different
    -- interval (derived from this peer's own cooldown_minutes, not a
    -- fixed 4h) -- see peers.py's peer_check_tick().
    last_peer_check_ts REAL,
    created_ts         REAL NOT NULL,
    created_by         INTEGER NOT NULL REFERENCES users(id)
);

-- One row per conversation ever opened with a peer -- turn_count is the
-- shared, per-conversation counter the PACI specification §9.1 caps; ended_ts/
-- end_reason is what both the cooldown check (§9.2) and a resend's "this
-- can't continue the old conversation" rule (§7) read.
CREATE TABLE IF NOT EXISTS peer_conversations (
    id              INTEGER PRIMARY KEY,
    peer_id         INTEGER NOT NULL REFERENCES peers(id),
    conversation_id TEXT NOT NULL,
    started_ts      REAL NOT NULL,
    ended_ts        REAL,
    end_reason      TEXT,
    turn_count      INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_peer_conversations ON peer_conversations(peer_id, conversation_id);

-- The full PACI message log, both directions, AND the outbound retry
-- queue combined -- a 'sent' row's own status/attempts/expires_ts/
-- last_error IS the outbox (see peers.py's docstring on why this isn't a
-- separate table). A 'received' row's body_json is always the SCREENED
-- (ingest.summarize_untrusted) structured result, never the peer's raw
-- text -- see the PACI specification §11 and peers.py's handle_inbound(). This
-- table is also Nori's headless audit trail (no UI renders it, but the
-- operator's own stated requirement was a real record, not a page).
CREATE TABLE IF NOT EXISTS peer_messages (
    id              INTEGER PRIMARY KEY,
    peer_id         INTEGER NOT NULL REFERENCES peers(id),
    conversation_id TEXT NOT NULL,
    message_id      TEXT NOT NULL,
    direction       TEXT NOT NULL,
    type            TEXT NOT NULL,
    seq             INTEGER NOT NULL,
    retry_of        TEXT,
    resend_depth    INTEGER NOT NULL DEFAULT 0,
    body_json       TEXT NOT NULL,
    ts              REAL NOT NULL,
    status          TEXT NOT NULL DEFAULT 'delivered',
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    expires_ts      REAL,
    expiry_notified INTEGER NOT NULL DEFAULT 0,
    -- Passive delivery, two independent dimensions (2026-09-12, the PACI specification
    -- §9.4; split 2026-09-19, operator's own correction -- "read in a peer
    -- initiated turn is not read in a user initiated turn"). Both NULL on a
    -- 'received' row until peers.pending_delivery_messages() has folded it
    -- into a real turn of THAT kind once -- then that ONE column is stamped
    -- and never re-injected for THAT kind again; the other stays NULL, still
    -- pending, until its own kind of turn shows it too. Not meaningful for
    -- 'sent' rows (always NULL on both there). This is what lets the
    -- per-turn check stay a single cheap indexed SELECT: "anything still
    -- NULL (for the relevant column) for peers I own" is the whole query.
    -- presented_peer_ts: shown once in a peer-motivated turn (force_checkin,
    -- forced_checkin_tick, a granted reply_requested, peer_check_tick) --
    -- was named `presented_ts` before the split, when this was the only
    -- dimension that existed.
    -- presented_user_ts: shown once in a turn answering the operator
    -- directly (live send/retry/voice, an orphan-sweep) -- new 2026-09-19.
    presented_peer_ts REAL,
    presented_user_ts REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_peer_messages ON peer_messages(peer_id, direction, message_id);

-- Deferred reply_requested compulsion (2026-09-19, the PACI specification v1.0,
-- see peers.py's own module docstring on the busy path). One row per
-- peer -- collapsing is structural, not a convention a caller has to
-- remember: a second message arriving while one is already queued
-- merges into the SAME row (message_ids grows) rather than creating a
-- second one, so a peer can never run up a backlog of queued turns.
-- Same shape as a sibling application/store.py's identical table.
CREATE TABLE IF NOT EXISTS peer_compulsions (
    peer_id     INTEGER PRIMARY KEY REFERENCES peers(id),
    message_ids TEXT NOT NULL,
    queued_ts   REAL NOT NULL
);

-- Every grant/queue/run/drop decision, real and timestamped -- the
-- record that didn't exist before 2026-09-19, which is the whole
-- reason a real incident (a peer going quiet on several reply_requested
-- asks) couldn't be diagnosed from the data alone. decision is one of
-- 'granted' (ran immediately), 'queued' (busy, deferred), 'run_from_queue'
-- (drained once free), 'dropped_expired' (queued past its own message's
-- expiry_hours window), 'dropped_other' (any other reason, named in detail).
CREATE TABLE IF NOT EXISTS peer_compulsion_log (
    id          INTEGER PRIMARY KEY,
    ts          REAL NOT NULL,
    peer_id     INTEGER NOT NULL REFERENCES peers(id),
    message_ids TEXT NOT NULL,
    decision    TEXT NOT NULL,
    detail      TEXT
);

-- Peer-motivated turns that exhausted their own round budget (2026-09-19,
-- real gap found investigating why a peer sometimes gets no reply at
-- all: hitting the round cap produces a fallback string that, on a
-- peer-motivated turn, is discarded exactly like any other unsent reply
-- -- mechanistically indistinguishable, before this table existed, from
-- a turn that ran fine and genuinely chose silence). A DIFFERENT
-- question from peer_compulsion_log above (that one is §9.5's own
-- grant/queue/drop decision for reply_requested specifically; this is a
-- resource-limit outcome that can happen on ANY peer-motivated trigger
-- -- force_checkin, forced_checkin_tick, a granted reply_requested,
-- peer_check_tick, all funneled through peers._run_prompted_turn) --
-- deliberately a separate table rather than overloading that one's own
-- `decision` enum with a concept it was never about.
CREATE TABLE IF NOT EXISTS peer_turn_limit_log (
    id       INTEGER PRIMARY KEY,
    ts       REAL NOT NULL,
    peer_id  INTEGER NOT NULL REFERENCES peers(id),
    rounds   INTEGER NOT NULL,
    reason   TEXT
);

-- A peer-motivated turn's own model call failing outright, after
-- chat.py's own retries (2026-09-19, real gap found in the same
-- "ensure silence is truly her choice" sweep as peer_turn_limit_log
-- above) -- a DIFFERENT failure mode from that table (this fires from
-- the `except chat.ModelError` branch, before any round loop even ran
-- one to completion; that one fires when the loop ran but exhausted
-- its budget). A sibling application had been overwriting a single non-durable
-- runtime field for this outcome, which the very next scheduler tick
-- could erase -- real, but not durable or peer-attributed. Nori had
-- nothing at all. `error` is chat.ModelError's own message, already
-- bounded (see chat.py); `reason` is the same human "why this turn
-- exists" string every peer-motivated trigger already carries.
CREATE TABLE IF NOT EXISTS peer_model_failure_log (
    id      INTEGER PRIMARY KEY,
    ts      REAL NOT NULL,
    peer_id INTEGER NOT NULL REFERENCES peers(id),
    error   TEXT NOT NULL,
    reason  TEXT
);

-- A peer's request to run a capability-changing action (the PACI specification
-- §11.1), gated by that peer's own trust_level. 'full' still writes a
-- row here (status starts 'approved', resolved immediately) so every
-- outcome -- not just the 'prompt' ones -- has the same visible,
-- queryable record; that's the "logged either way" half of §11.1.
-- Expiry reuses §7's own shape (an expires_ts a background sweep
-- checks, same tick() loop that already ages out the outbox) rather
-- than a second timeout concept. source_message_id points at the
-- peer_messages row this relays, so an approval can be traced back to
-- the actual (already-screened) ask that prompted it.
CREATE TABLE IF NOT EXISTS peer_pending_actions (
    id                 INTEGER PRIMARY KEY,
    peer_id            INTEGER NOT NULL REFERENCES peers(id),
    tool_name          TEXT NOT NULL,
    tool_args_json     TEXT NOT NULL,
    source_message_id  TEXT,
    requested_ts       REAL NOT NULL,
    expires_ts         REAL NOT NULL,
    status             TEXT NOT NULL DEFAULT 'pending',
    resolved_ts        REAL,
    resolved_by        INTEGER REFERENCES users(id),
    result_json        TEXT
);

-- Web search/fetch (2026-09-14, see webtools.py) -- an admin's own domain
-- rules, checked by webtools._host_allowed() before anything is fetched.
-- kind: 'read_block' (GET is open-by-default; this is the exception list)
-- or 'write_allow' (anything else is closed-by-default; this is the
-- opt-in list). pattern is a bare domain or a "*.example.com" wildcard,
-- which also matches the bare domain itself -- see _domain_matches().
CREATE TABLE IF NOT EXISTS web_domain_rules (
    id       INTEGER PRIMARY KEY,
    kind     TEXT NOT NULL,
    pattern  TEXT NOT NULL,
    added_ts REAL NOT NULL,
    UNIQUE(kind, pattern)
);

-- Every web_search/web_fetch attempt, success or failure -- "cost logged
-- as its own line" (operator's own requirement): a real dollar figure
-- when one is known, NULL when it genuinely isn't (Tavily bills in
-- account-level credits, not a per-call invoiced number this app can see
-- -- an honest gap, not a guessed zero, same convention as usage.cost
-- elsewhere in this codebase).
CREATE TABLE IF NOT EXISTS web_tool_log (
    id             INTEGER PRIMARY KEY,
    ts             REAL NOT NULL,
    kind           TEXT NOT NULL,
    target         TEXT NOT NULL,
    ok             INTEGER NOT NULL,
    reason         TEXT,
    cost_usd       REAL,
    cost_is_actual INTEGER
);

-- Out-of-band agent channel (2026-09-14, see agent_channel.py) -- every
-- call's audit trail, entirely separate from `messages`: this is never a
-- real conversation turn, so it must never look like one, including in
-- the log it leaves behind. user_id is whose persona/memory/emotional
-- state the question was asked against (agent_channel.py's --user,
-- default the workspace admin) -- not a real participant in a real
-- exchange, just which context to build from.
CREATE TABLE IF NOT EXISTS agent_channel_log (
    id       INTEGER PRIMARY KEY,
    ts       REAL NOT NULL,
    user_id  INTEGER NOT NULL REFERENCES users(id),
    question TEXT NOT NULL,
    reply    TEXT,
    cost_usd REAL
);

-- Home Assistant (2026-09-15) -- discovery and exposure are deliberately
-- two different things: discover_entities() (admin-only, see
-- homeassistant.py) upserts every entity HA reports into this table on
-- every rediscovery, but NEVER touches enabled/enabled_for_peers on an
-- existing row -- rediscovering the house never silently re-exposes or
-- un-exposes anything an admin already decided. A brand-new entity always
-- lands with both flags 0: invisible to her until an admin explicitly
-- checks it. Two checkboxes, not a read/write split (2026-09-15, the
-- operator's own direct answer, replacing an earlier read/write + device-
-- class-severity design of mine): `enabled` -- she can see and use it, no
-- separate read-only step -- and `enabled_for_peers` -- a connected peer
-- can reach it too, on top of that same peer's own trust level. Peer-
-- enabled implies Nori-enabled, enforced in homeassistant.py's own
-- set_exposure(), not just relied on here.
CREATE TABLE IF NOT EXISTS ha_entities (
    entity_id         TEXT PRIMARY KEY,
    domain            TEXT NOT NULL,
    friendly_name     TEXT,
    enabled           INTEGER NOT NULL DEFAULT 0,
    enabled_for_peers INTEGER NOT NULL DEFAULT 0,
    last_seen_ts      REAL NOT NULL,
    raw_json          TEXT
);

-- Every ha_get_state/ha_control call, same "log every request, complete
-- rather than sampled" discipline web_tool_log already established.
-- service is NULL for a read; domain/service together are what a human
-- scanning this needs to tell "turned on a light" apart from "unlocked a
-- door" at a glance, without parsing payload.
CREATE TABLE IF NOT EXISTS ha_tool_log (
    id        INTEGER PRIMARY KEY,
    ts        REAL NOT NULL,
    kind      TEXT NOT NULL,
    entity_id TEXT,
    domain    TEXT,
    service   TEXT,
    ok        INTEGER NOT NULL,
    reason    TEXT,
    agent     TEXT,
    status    INTEGER,
    payload   TEXT
);

-- General-purpose scheduler (2026-09-15, see schedules.py and
-- scheduler.py's own due-check). user_id is always the ACCOUNT whose turn
-- actually fires -- for a peer-created entry that's the peer's own owning
-- account (session["user_id"] at the moment the peer's request-action ran
-- created it), never the peer itself, since a peer has no turn of its own
-- to run. created_by_type/created_by_peer_name record who AUTHORED it
-- (nori / user / a specific peer, by name -- same convention _peer_context
-- already uses for display everywhere else, not a peers.id FK) purely for
-- display -- the operator's own explicit ask, "he should be able to see which
-- schedules are his, hers, or [a peer]'s" -- and never gate anything at
-- fire time.
--
-- instruction is free text, deliberately no allowlist (operator's own
-- characterization: "stored input that executes later") -- required_tool
-- is the one structural guardrail, checked live against tools.active_
-- schemas() right before a turn fires, never at creation time only (a
-- tool can go from available to not between when a schedule was made and
-- when it's due).
--
-- deliver_to is entirely independent of whether required_tool exists --
-- 'user' persists her reply to his own chat (scheduler._send_proactive's
-- shape), 'peer' instructs her to use that peer's own send tool instead
-- and stays silent to him, 'both' does both, 'none' is "act, don't
-- report" (his own phrase) -- silent everywhere except message_user,
-- which stays reachable as the deliberate-exception escape hatch in every
-- case, same as jobs.py's job-completion trigger.
--
-- next_run_ts is the one field the due-check actually scans -- computed
-- at creation and recomputed AFTER every fire, always relative to the
-- real fire time rather than the missed slot. That's the deliberate
-- catch-up policy for "a missed window while the app was down" (left to
-- this module's own design, per the operator): fire once on the first
-- cycle after recovery, then reschedule forward from now -- never replay
-- a backlog of every interval/day missed while the process was down.
CREATE TABLE IF NOT EXISTS schedules (
    id                 INTEGER PRIMARY KEY,
    user_id            INTEGER NOT NULL REFERENCES users(id),
    name               TEXT NOT NULL,
    instruction        TEXT NOT NULL,
    required_tool      TEXT,
    deliver_to         TEXT NOT NULL DEFAULT 'user',
    deliver_peer_id    INTEGER REFERENCES peers(id),
    schedule_type      TEXT NOT NULL,
    interval_min       INTEGER,
    time_hour          INTEGER,
    time_minute        INTEGER,
    created_by_type      TEXT NOT NULL DEFAULT 'user',
    created_by_peer_name TEXT,
    enabled            INTEGER NOT NULL DEFAULT 1,
    created_ts         REAL NOT NULL,
    last_run_ts        REAL,
    next_run_ts        REAL NOT NULL,
    last_status        TEXT,
    last_error         TEXT
);
CREATE INDEX IF NOT EXISTS ix_schedules_due ON schedules(enabled, next_run_ts);
CREATE INDEX IF NOT EXISTS ix_schedules_user ON schedules(user_id);

-- One row per actual fire (2026-09-15) -- the operator's own explicit
-- requirement: "log the full instruction with every run so what she was
-- told to do is always recoverable from the record." Stores the
-- instruction TEXT itself, not just schedule_id, so the record survives
-- the schedule later being edited or deleted -- what she was actually
-- told at the time stays recoverable regardless of what the row says now.
-- schedule_id deliberately carries NO `REFERENCES schedules(id)` -- same
-- reasoning memory_events.memory_id already established for the identical
-- problem (see that table's own comment): PRAGMA foreign_keys=ON really
-- is enforced here (connect(), below), so a declared FK would make
-- deleting a schedule with any run history raise FOREIGN KEY constraint
-- failed -- exactly backwards from "the record survives deletion." (A
-- version of this table briefly shipped WITH that FK -- init()'s own
-- one-time migration below recreates it without data loss the one time
-- that old shape is still found on disk.)
CREATE TABLE IF NOT EXISTS schedule_runs (
    id          INTEGER PRIMARY KEY,
    schedule_id INTEGER NOT NULL,
    ts          REAL NOT NULL,
    instruction TEXT NOT NULL,
    status      TEXT NOT NULL,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS ix_schedule_runs_schedule ON schedule_runs(schedule_id, ts);

-- Edit history for a schedule (2026-09-15, operator's own follow-up ask:
-- "not just a last-modified field, he wants the trail"). Same provenance
-- shape memory.py's memory_events already established for the identical
-- problem ("who did this, nori/user/peer") -- reused deliberately rather
-- than invented a third time, per the operator's own instruction. One row
-- per create/update/enable/disable/delete, actor + actor_peer_name (the
-- same peer-by-name convention schedules.created_by_peer_name already
-- uses, not a peers.id FK) naming WHO, ts naming WHEN, changes (JSON
-- {field: {old, new}}, only the fields that actually changed) naming
-- WHAT -- so a peer editing an instruction Nori wrote, or the reverse, is
-- recoverable later, not just the current row's own last state.
-- schedule_id has no FK either, same reasoning as schedule_runs just
-- above -- a deleted schedule's own edit trail must survive it.
CREATE TABLE IF NOT EXISTS schedule_events (
    id              INTEGER PRIMARY KEY,
    schedule_id     INTEGER NOT NULL,
    ts              REAL NOT NULL,
    actor           TEXT NOT NULL,
    actor_peer_name TEXT,
    action          TEXT NOT NULL,
    changes         TEXT,
    note            TEXT
);
CREATE INDEX IF NOT EXISTS ix_schedule_events_schedule ON schedule_events(schedule_id, ts);

-- Tasks -- the board's real data (2026-09-16, see tasks.py). The card
-- surface WISHLIST.md sketched (2026-09-11) named several card types
-- (note/task/reminder/running topic); this is the first one actually
-- built, and only that one -- the operator's own scoping. due_ts is a
-- plain epoch (nullable -- a task need not have one); recur_type/
-- recur_interval_min/recur_time_hour/recur_time_minute are the exact
-- same shape schedules' own recurrence fields use (recurrence.py, shared
-- rather than reinvented per the operator's own explicit instruction).
-- A recurring task's own close() advances due_ts to the next occurrence
-- instead of ending it -- see tasks.py's close(). status='closed' means
-- gone from the board, never gone from the record -- close, never
-- delete, is the operator's own explicit rule; there is no DELETE FROM
-- tasks anywhere in this app.
CREATE TABLE IF NOT EXISTS tasks (
    id                 INTEGER PRIMARY KEY,
    user_id            INTEGER NOT NULL REFERENCES users(id),
    name               TEXT NOT NULL,
    body               TEXT,
    priority           TEXT NOT NULL DEFAULT 'normal',
    category           TEXT NOT NULL DEFAULT 'other',
    due_ts             REAL,
    recur_type         TEXT,
    recur_interval_min INTEGER,
    recur_time_hour    INTEGER,
    recur_time_minute  INTEGER,
    status             TEXT NOT NULL DEFAULT 'open',
    created_by_type      TEXT NOT NULL DEFAULT 'user',
    created_by_peer_name TEXT,
    created_ts         REAL NOT NULL,
    closed_ts          REAL,
    needs_attention    INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ix_tasks_user ON tasks(user_id, status);
-- ix_tasks_category is NOT here -- see _MIGRATIONS below. This whole
-- executescript runs BEFORE _MIGRATIONS on every startup, so an index on
-- a column a migration adds can't live here: against a database that
-- already had `tasks` (without `category`) from an earlier deploy,
-- CREATE TABLE IF NOT EXISTS is a no-op and this index would try to
-- reference a column that doesn't exist yet, in the SAME executescript
-- call that's supposed to create it -- confirmed directly, not assumed
-- (this exact ordering already broke ix_users_workspace once before).

-- Task history (2026-09-16) -- same actor-tagged event-log shape
-- schedule_events already established (reused, per the operator's own
-- instruction, rather than a third provenance scheme), extended for the
-- one thing a task's own history needs that a schedule's never did:
-- holding ACTIONS taken against the task, not just edits to its fields.
-- action='reminded' (task_update called with a `note` but no actual
-- field change -- see tasks.py's own update()) is the concrete case
-- that drove this -- "she reminded me about it" is a fact about what
-- happened, not a diff of what changed, and the same `note` column that
-- already carries free context on any event (schedule_events has it
-- too) is exactly where that fact belongs, without a parallel column or
-- a parallel table for "things that happened but weren't edits." No FK
-- on task_id, same reasoning as schedule_events.schedule_id -- a closed
-- (or, in principle, any other future state) task's history must not be
-- blockable by its own audit trail.
CREATE TABLE IF NOT EXISTS task_events (
    id              INTEGER PRIMARY KEY,
    task_id         INTEGER NOT NULL,
    ts              REAL NOT NULL,
    actor           TEXT NOT NULL,
    actor_peer_name TEXT,
    action          TEXT NOT NULL,
    changes         TEXT,
    note            TEXT
);
CREATE INDEX IF NOT EXISTS ix_task_events_task ON task_events(task_id, ts);

-- Notes -- the board's second card type (2026-09-16, see notes.py).
-- Deliberately the SIMPLEST of WISHLIST.md's original card sketch: "a
-- note" with none of a task's due date/priority/recurrence/close
-- lifecycle -- title, body, category (the same fixed five tasks use,
-- shared by direct import of tasks.CATEGORIES rather than a second
-- tuple that could drift). No status column at all -- there's no open/
-- closed distinction for a note, only exists-or-doesn't. Unlike tasks,
-- a note really can be deleted (the operator's own explicit, deliberate
-- distinction -- "delete IS allowed here, unlike tasks where it's
-- close-only"); note_events (below) still survives that delete, same
-- as every other event table in this app.
CREATE TABLE IF NOT EXISTS notes (
    id                   INTEGER PRIMARY KEY,
    user_id              INTEGER NOT NULL REFERENCES users(id),
    title                TEXT NOT NULL,
    body                 TEXT,
    category             TEXT NOT NULL DEFAULT 'other',
    created_by_type      TEXT NOT NULL DEFAULT 'user',
    created_by_peer_name TEXT,
    created_ts           REAL NOT NULL,
    needs_attention      INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ix_notes_user ON notes(user_id);
CREATE INDEX IF NOT EXISTS ix_notes_category ON notes(user_id, category);

-- Same actor-tagged event-log shape as schedule_events/task_events,
-- reused a third time rather than invented again. Included for notes
-- even though the operator didn't explicitly ask for it here (he asked
-- for provenance, which this also carries) -- see notes.py's own module
-- docstring for that call, made explicitly rather than assumed. No FK
-- on note_id, same reasoning as the other two event tables -- a
-- deleted note's own history must survive the delete that created it,
-- which for notes (unlike tasks) is a REAL delete, not just a status
-- flip, making this the one place that guarantee actually gets
-- exercised in earnest.
CREATE TABLE IF NOT EXISTS note_events (
    id              INTEGER PRIMARY KEY,
    note_id         INTEGER NOT NULL,
    ts              REAL NOT NULL,
    actor           TEXT NOT NULL,
    actor_peer_name TEXT,
    action          TEXT NOT NULL,
    changes         TEXT,
    note            TEXT
);
CREATE INDEX IF NOT EXISTS ix_note_events_note ON note_events(note_id, ts);

-- Trackers (2026-09-16, see trackers.py) -- personal data points, of a
-- kind SHE defines, not a fixed list (water, weight, blood pressure,
-- medicine, sleep, mood, and whatever comes after -- the operator's own
-- examples). The real design decision, per his own framing ("a type
-- carrying a declared value-kind is the obvious answer but it's a real
-- design decision"): every type declares value_kind up front, one of
-- 'number' (one value + a unit -- water in oz, weight in lbs),
-- 'pair' (two labeled values sharing a unit -- blood pressure's
-- systolic/diastolic), 'boolean' (took it or didn't -- medicine, stored
-- as 0/1 in value_1 so "adherence rate" falls out of a plain AVG()),
-- 'scale' (an integer within a declared min/max -- mood as a 1-5), or
-- 'text' (mood as a word, or anything that genuinely doesn't reduce to
-- a number). Free-text-only would make history unqueryable; strictly
-- numeric-only would leave medicine and mood with nowhere to go -- this
-- is what actually covers every example he gave without over- or
-- under-constraining the ones after. value_meta (JSON) carries whatever
-- is specific to the kind (pair's two component labels; scale's min/max
-- and endpoint labels; boolean's true/false labels) -- read as a whole
-- to interpret value_1/value_2/value_text, never filtered at the SQL
-- level, so JSON is the right call here the same way schedule_events'
-- own `changes` column already established for this app.
--
-- Disable, never delete, same rule tasks/notes already follow -- a
-- disabled type's existing entries survive and stay queryable exactly
-- as before; enabled=0 only ever gates NEW logging (trackers.py's own
-- log_entry()), never history reads.
CREATE TABLE IF NOT EXISTS tracker_types (
    id                   INTEGER PRIMARY KEY,
    user_id              INTEGER NOT NULL REFERENCES users(id),
    name                 TEXT NOT NULL,
    value_kind           TEXT NOT NULL,
    unit                 TEXT,
    value_meta           TEXT,
    enabled              INTEGER NOT NULL DEFAULT 1,
    created_by_type      TEXT NOT NULL DEFAULT 'user',
    created_by_peer_name TEXT,
    created_ts           REAL NOT NULL
);
-- PARTIAL on enabled=1 (2026-09-16, root cause of a real bug report: a
-- disabled type used to reserve its own name forever, since this was
-- a plain unconditional unique index -- disabling "coffee" then
-- blocked ever creating a fresh "coffee", with no way out. See
-- _MIGRATIONS below for the drop-and-recreate that upgrades an
-- existing database; a brand-new one gets the partial index directly
-- from this CREATE.
CREATE UNIQUE INDEX IF NOT EXISTS ux_tracker_types_name ON tracker_types(user_id, name) WHERE enabled=1;

-- One row per logged data point. type_id DOES carry a real FK here,
-- deliberately unlike every other child-of-a-deletable-parent table in
-- this app (schedule_runs, task_events, etc.) -- the difference is that
-- a tracker_type is NEVER deleted, only disabled (see above), so the
-- "deleting the parent must not be blocked by its own children" problem
-- those tables exist to dodge never actually arises here. user_id is
-- still stored directly rather than only reachable via a join to the
-- type -- same "every row knows its own owner" convention every other
-- table in this app already follows. ts is when the tracked thing
-- actually happened (she can log retroactively -- "took my meds this
-- morning," logged in the evening); created_ts is when the log entry
-- itself was recorded, always now, never backdated.
CREATE TABLE IF NOT EXISTS tracker_entries (
    id                   INTEGER PRIMARY KEY,
    type_id              INTEGER NOT NULL REFERENCES tracker_types(id),
    user_id              INTEGER NOT NULL REFERENCES users(id),
    value_1              REAL,
    value_2              REAL,
    value_text           TEXT,
    note                 TEXT,
    ts                   REAL NOT NULL,
    created_ts           REAL NOT NULL,
    created_by_type      TEXT NOT NULL DEFAULT 'user',
    created_by_peer_name TEXT
);
CREATE INDEX IF NOT EXISTS ix_tracker_entries_type ON tracker_entries(type_id, ts);

-- Provenance/update history (2026-09-16, "consistent with tasks," the
-- operator's own ask) -- one shared table for both subjects a tracker
-- has (the type itself: created/enabled/disabled; an entry: created/
-- updated) rather than two near-identical tables, distinguished by
-- `subject`. Same actor-tagged shape as schedule_events/task_events/
-- note_events, reused a fourth time. No FK on subject_id -- a type
-- subject never needs one (types are never deleted), but an entry
-- subject's own row IS, in principle, still just a row like any other,
-- so this stays consistent with the no-FK convention rather than
-- special-casing per subject.
CREATE TABLE IF NOT EXISTS tracker_events (
    id              INTEGER PRIMARY KEY,
    subject         TEXT NOT NULL,
    subject_id      INTEGER NOT NULL,
    ts              REAL NOT NULL,
    actor           TEXT NOT NULL,
    actor_peer_name TEXT,
    action          TEXT NOT NULL,
    changes         TEXT,
    note            TEXT
);
CREATE INDEX IF NOT EXISTS ix_tracker_events_subject ON tracker_events(subject, subject_id, ts);

-- Categories (2026-09-16, see catalog.py) -- widened from tasks.py's own
-- hardcoded 5-tuple the moment the operator asked for her (and him) to
-- be able to manage the set itself, not just pick from it. Same PATTERN
-- tracker_types already established (declared name, disable never
-- delete, provenance, actor-tagged history) -- reused, not reinvented,
-- for the fifth time this build (schedules/tasks/notes/trackers were
-- the first four instances of this same shape). Deliberately its OWN
-- table rather than tracker_types itself made generic: evaluated a
-- physical merge and chose against it -- tracker_types carries real
-- structured fields (value_kind/unit/value_meta) a bare category never
-- needs, and there was zero live tracker data at the time this was
-- built to justify the migration risk of relocating those into an
-- opaque JSON blob for a merge that would mostly just make the fit
-- worse, not better. `domain` scopes a name to one item type
-- (task/note/reminder) so two domains COULD diverge later without a
-- schema change, even though they're seeded identically today.
CREATE TABLE IF NOT EXISTS categories (
    id                   INTEGER PRIMARY KEY,
    user_id              INTEGER NOT NULL REFERENCES users(id),
    domain               TEXT NOT NULL,
    name                 TEXT NOT NULL,
    enabled              INTEGER NOT NULL DEFAULT 1,
    created_by_type      TEXT NOT NULL DEFAULT 'user',
    created_by_peer_name TEXT,
    created_ts           REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_categories_name ON categories(user_id, domain, name);

CREATE TABLE IF NOT EXISTS category_events (
    id              INTEGER PRIMARY KEY,
    category_id     INTEGER NOT NULL,
    ts              REAL NOT NULL,
    actor           TEXT NOT NULL,
    actor_peer_name TEXT,
    action          TEXT NOT NULL,
    changes         TEXT,
    note            TEXT
);
CREATE INDEX IF NOT EXISTS ix_category_events_category ON category_events(category_id, ts);

-- Reminders (2026-09-16, see reminders.py) -- like tasks, but urgent:
-- presented at a due time and nagged about until closed, rather than
-- sitting quietly on the board. One row per reminder OBJECT, carrying
-- its own CURRENT occurrence's live state (next_due_ts/
-- occurrence_status/nag_count/last_nag_ts) -- closing or missing an
-- occurrence never inserts a new row, it advances these fields in
-- place, exactly the operator's own framing: "one reminder object with
-- a history of occurrences." That history itself lives entirely in
-- reminder_events (nags/progress/completion/missed) -- no separate
-- occurrences table, same "one object, one event log" shape every
-- other feature in this build already settled on.
--
-- recur_type NULL means a one-off (due on `once_date`, closes for good
-- once done); otherwise one of recurrence.TYPES minus 'interval' (see
-- reminders.py's own RECUR_TYPES and recurrence.py's own module
-- docstring for why weekly/monthly_day/monthly_weekday were added
-- there rather than duplicated here). due_hour/due_minute is the daily
-- clock time every recurring pattern shares; recur_weekday/
-- recur_month_day/recur_week_ordinal are populated only for the
-- patterns that actually use them, same "irrelevant fields just sit
-- unused" convention schedules.py's own interval-vs-time columns
-- already established.
--
-- status ('active'|'closed') is the reminder's own overall lifecycle
-- (closed = a one-off that's done, or a recurrence deliberately
-- stopped); occurrence_status ('pending'|'done'|'missed') is the
-- CURRENT cycle alone, reset to 'pending' every time it advances --
-- two different axes, not one, because a still-active recurring
-- reminder can have ALREADY had a done or missed occurrence sitting in
-- its own history while its current one is freshly pending again.
CREATE TABLE IF NOT EXISTS reminders (
    id                   INTEGER PRIMARY KEY,
    user_id              INTEGER NOT NULL REFERENCES users(id),
    name                 TEXT NOT NULL,
    body                 TEXT,
    category             TEXT NOT NULL DEFAULT 'other',
    due_hour             INTEGER NOT NULL,
    due_minute           INTEGER NOT NULL,
    recur_type           TEXT,
    once_date            TEXT,
    recur_weekday        INTEGER,
    recur_month_day      INTEGER,
    recur_week_ordinal   INTEGER,
    nag_interval_min     INTEGER NOT NULL DEFAULT 30,
    nag_max_count        INTEGER NOT NULL DEFAULT 3,
    status               TEXT NOT NULL DEFAULT 'active',
    created_by_type      TEXT NOT NULL DEFAULT 'user',
    created_by_peer_name TEXT,
    created_ts           REAL NOT NULL,
    next_due_ts          REAL NOT NULL,
    occurrence_status    TEXT NOT NULL DEFAULT 'pending',
    nag_count            INTEGER NOT NULL DEFAULT 0,
    last_nag_ts          REAL,
    needs_attention      INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ix_reminders_due ON reminders(user_id, status, occurrence_status, next_due_ts);

-- Same actor-tagged shape as schedule_events/task_events/note_events/
-- tracker_events/category_events, reused a sixth time. action='progress'
-- (reminders.record_progress(), the operator's own "track" clarified to
-- "record progress") carries changes={"status": {"old": null, "new":
-- ...}} when a fixed-set marker was given, `note` for free text, and
-- `ts` for when -- all three of the things he asked about, none of them
-- needing a new column beyond what this shape already has. No FK on
-- reminder_id, same reasoning as every other event table here.
CREATE TABLE IF NOT EXISTS reminder_events (
    id              INTEGER PRIMARY KEY,
    reminder_id     INTEGER NOT NULL,
    ts              REAL NOT NULL,
    actor           TEXT NOT NULL,
    actor_peer_name TEXT,
    action          TEXT NOT NULL,
    changes         TEXT,
    note            TEXT
);
CREATE INDEX IF NOT EXISTS ix_reminder_events_reminder ON reminder_events(reminder_id, ts);

-- the PACI specification v0.9, §4.1: one row per peer, the current/last health-check
-- result. last_status is always one of the five names §4.1 defines
-- (healthy/unreachable/hmac_mismatch/limits_diverged/clock_skew) --
-- never a bare bool. last_detail is the raw evidence behind that status
-- (the actual error, the actual limits diff, the actual skew seconds),
-- JSON-encoded, for the settings-page chip's own expanded view.
-- status_changed_ts is separate from last_checked_ts specifically so a
-- transition can be logged/surfaced -- "still healthy" and "just became
-- healthy again" are different, useful facts, not the same row.
CREATE TABLE IF NOT EXISTS paci_health (
    peer_id           INTEGER PRIMARY KEY,
    last_status       TEXT NOT NULL DEFAULT 'unreachable',
    last_checked_ts   REAL,
    last_detail       TEXT,
    status_changed_ts REAL
);

-- Read receipts (the PACI specification §7.1, v1.3) -- what a PEER told us about a
-- message WE sent them (direction='sent' in peer_messages), never what we
-- know about a message we received. One row per (peer, message, stage) --
-- the unique index is what makes storing an inbound receipt idempotent,
-- since the sender's own emission is already fire-once by construction
-- (see peers.py's own receipt-emission comment) but a receiver shouldn't
-- have to trust that alone. `context` ('peer'/'user') names which of the
-- two independent presented_*_ts dimensions actually fired on the OTHER
-- side -- see peer_messages.presented_peer_ts/presented_user_ts above for
-- the identical two-dimension split this mirrors. `at` is the sending
-- peer's own claimed wire timestamp (ISO 8601, kept as sent, never
-- reparsed against our clock for anything functional); `received_ts` is
-- OUR OWN clock at receipt, which is what "N minutes ago" display and any
-- future windowing actually uses -- deliberately not clock-skew-sensitive.
CREATE TABLE IF NOT EXISTS peer_receipts (
    id          INTEGER PRIMARY KEY,
    peer_id     INTEGER NOT NULL REFERENCES peers(id),
    message_id  TEXT NOT NULL,
    stage       TEXT NOT NULL,
    context     TEXT NOT NULL,
    at          TEXT NOT NULL,
    received_ts REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_peer_receipts ON peer_receipts(peer_id, message_id, stage);

-- custody.py: a sealed result held for a peer's own sealed-outcome game. payload_enc is encrypted at rest and read by NOTHING but custody.py (no tool,
-- context builder, digest or page); it is erased (NULL) once the release is acknowledged. Never surfaced to the model.
CREATE TABLE IF NOT EXISTS custody_holds (
    id           INTEGER PRIMARY KEY,
    peer_id      INTEGER NOT NULL REFERENCES peers(id),
    token        TEXT NOT NULL,
    game_ref     TEXT NOT NULL,
    reveal_ts    REAL NOT NULL,
    commitment   TEXT NOT NULL,
    payload_enc  TEXT,
    status       TEXT NOT NULL,                 -- holding | released
    received_ts  REAL NOT NULL,
    released_ts  REAL,
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_custody_holds ON custody_holds(peer_id, token);

-- integration_health.py: extends the PACI health-check pattern above to
-- every OTHER outward-facing integration (Home Assistant, Tavily, each
-- connected Google/Microsoft account, each connected MCP server). One
-- row per stable string key ("home_assistant", "tavily", "gmail",
-- "mcp:3", ...) rather than a peer_id, since these aren't all peer-
-- shaped -- same five-ish-state vocabulary, same transition-logged
-- status_changed_ts, see that module's own docstring for the full state
-- list (it's wider than PACI's five: also not_configured/scope_missing/
-- api_disabled/rate_limited, since these integrations fail in more
-- specific ways than a peer handshake does).
CREATE TABLE IF NOT EXISTS integration_health (
    key               TEXT PRIMARY KEY,
    last_status       TEXT NOT NULL DEFAULT 'unknown',
    last_checked_ts   REAL,
    last_detail       TEXT,
    last_explain      TEXT,
    status_changed_ts REAL
);

-- Context tuning: the operator's saved "last known good" (tuning_admin.py). Set deliberately, by one explicit action; never inferred from recent edits, so it cannot drift
-- into whatever was typed last. One row per workspace, replaced only by saving a new baseline. It lives in its own table on purpose, away from `settings`: nothing that
-- resets, rewrites or migrates settings can reach it, and the trigger below refuses to delete it.
CREATE TABLE IF NOT EXISTS tuning_baseline (
    workspace_id INTEGER PRIMARY KEY,
    values_json  TEXT NOT NULL,
    saved_ts     REAL NOT NULL,
    saved_by     INTEGER
);
CREATE TRIGGER IF NOT EXISTS trg_tuning_baseline_nodelete BEFORE DELETE ON tuning_baseline
BEGIN SELECT RAISE(ABORT, 'a saved tuning baseline is never deleted; save a new one to replace it'); END;

-- What the tuning values were BEFORE each change (a save, a reset, a restore), so "back one edit" always has somewhere to go. Append-only.
CREATE TABLE IF NOT EXISTS tuning_history (
    id           INTEGER PRIMARY KEY,
    workspace_id INTEGER NOT NULL,
    ts           REAL NOT NULL,
    values_json  TEXT NOT NULL,
    action       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_tuning_history_ws ON tuning_history(workspace_id, id);
CREATE TRIGGER IF NOT EXISTS trg_tuning_history_noupdate BEFORE UPDATE ON tuning_history
BEGIN SELECT RAISE(ABORT, 'tuning history is append-only'); END;
"""

# Empty for now -- first real migration lands with the first schema change
# after initial release. ALTER TABLE statements only, same convention as
# a sibling application/store.py: each one must be safe to re-run (OperationalError on
# "column already exists" is swallowed by init(), same as there).
_MIGRATIONS: list[str] = [
    # Emotional state (2026-09-12) -- messages predating this feature have
    # emotion=NULL, which is correct (nothing to show), not a gap to backfill.
    "ALTER TABLE messages ADD COLUMN emotion TEXT",
    # Proactive scheduler (2026-09-12) -- distinguishes a message she sent
    # unprompted from a reply. Existing rows default to 'chat', correctly --
    # nothing before this feature was ever proactive.
    "ALTER TABLE messages ADD COLUMN kind TEXT NOT NULL DEFAULT 'chat'",
    # MCP capability descriptions (2026-09-12) -- see mcp_servers.py's
    # capability_block(). Existing connections (Nodrya) get NULL here
    # until an admin fills it in via the admin page; the fallback is just
    # the connection's own name, never a crash or a missing block.
    "ALTER TABLE mcp_servers ADD COLUMN purpose TEXT",
    # Per-peer authority level (2026-09-12) -- the PACI specification §11.1. An
    # existing peer connection gets the same 'prompt' default a
    # brand-new one would -- no peer silently inherits standing authority
    # just because it predates this feature.
    "ALTER TABLE peers ADD COLUMN trust_level TEXT NOT NULL DEFAULT 'prompt'",
    # Peer-action advertisement + periodic forced check-in (2026-09-13) --
    # an existing peer connection gets NULL for both, correctly: no
    # advertised actions learned yet (next handshake fills it in) and no
    # forced check-in run yet (the first tick() after upgrade starts the
    # 4h clock fresh rather than assuming one is overdue immediately).
    "ALTER TABLE peers ADD COLUMN remote_requestable_actions TEXT",
    "ALTER TABLE peers ADD COLUMN last_forced_checkin_ts REAL",
    "ALTER TABLE peers ADD COLUMN last_peer_check_ts REAL",
    # Working-memory metadata (2026-09-13, operator's own generalized ask):
    # a JSON blob riding alongside a self-initiated message (kind='proactive'
    # or 'peer_proactive'), carrying a short {"reason": ...} explaining why
    # she said it -- structural, never appended into `content` (that's what
    # the chat page renders and exports, untouched). Existing rows get NULL,
    # correctly: nothing before this feature recorded a reason to recover.
    # See conversation.render_for_model() for where this actually reaches
    # the model, and compaction.py for what happens to it once compacted.
    "ALTER TABLE messages ADD COLUMN meta TEXT",
    # reply_requested's cap, made configurable (2026-09-13) -- an existing
    # peer gets the same default (3/hour) a brand-new one would.
    "ALTER TABLE peers ADD COLUMN reply_requested_cap INTEGER NOT NULL DEFAULT 3",
    # presented_ts (2026-09-12, passive delivery) is deliberately NOT here --
    # it needs a one-time backfill coupled to the ALTER succeeding, which
    # this plain list can't express. See init()'s own dedicated block below.
    # Full web_tool_log audit trail (2026-09-14, operator's own ask: "full
    # visibility... complete rather than sampled") -- method/agent were
    # already implicit in `target`'s own combined string; broken out here
    # so the settings page can filter/column them instead of parsing text.
    # denied_by names the specific rule a request failed (read_block,
    # write_allow, ssrf, method, scheme) -- NULL means it cleared every
    # check. simulated (see webtools.py's write-mode gate) is a SEPARATE
    # axis from denied_by: a simulated write can still have cleared every
    # policy check and simply never been sent, which is exactly the
    # distinction the audit trail needs to make legible. status is the
    # real HTTP response code when a real request actually completed;
    # NULL for anything denied, simulated, or that failed before a
    # response (a network/timeout error, detailed in `reason` instead).
    # payload is the POST body, logged on both a real and a simulated
    # write -- simulated especially, since seeing what would have been
    # sent is the entire point of that mode.
    "ALTER TABLE web_tool_log ADD COLUMN method TEXT",
    "ALTER TABLE web_tool_log ADD COLUMN agent TEXT",
    "ALTER TABLE web_tool_log ADD COLUMN simulated INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE web_tool_log ADD COLUMN denied_by TEXT",
    "ALTER TABLE web_tool_log ADD COLUMN status INTEGER",
    "ALTER TABLE web_tool_log ADD COLUMN payload TEXT",
    # Per-agent tool access for sub-agent dispatch (2026-09-14, operator's
    # own ask). 0 on an existing roster entry means exactly what it always
    # meant -- no tools -- so nothing already configured changes behavior
    # just by upgrading.
    "ALTER TABLE sub_agents ADD COLUMN tool_call_limit INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE sub_agents ADD COLUMN tool_byte_limit INTEGER NOT NULL DEFAULT 2000000",
    # Per-agent file-write and web access (2026-10-02, operator's own ask:
    # "read only vs read write... another checkbox for search/fetch
    # access") -- both 0 on every existing row, so nothing already
    # configured gains a capability just by upgrading. See jobs.py's
    # allowed_tool_names().
    "ALTER TABLE sub_agents ADD COLUMN file_write INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE sub_agents ADD COLUMN web_access INTEGER NOT NULL DEFAULT 0",
    # Sub-agent job cost/usage tracking (2026-09-14) -- see jobs.py's
    # cost_summary(). A job that finished before this migration simply has
    # no cost recorded (NULL/0), same honest gap conversation.cost_meta()
    # already leaves for a turn whose provider didn't report one.
    "ALTER TABLE jobs ADD COLUMN cost_usd REAL",
    "ALTER TABLE jobs ADD COLUMN cost_unavailable INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE jobs ADD COLUMN prompt_tokens INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE jobs ADD COLUMN completion_tokens INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE jobs ADD COLUMN tool_calls_used INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE jobs ADD COLUMN tool_bytes_used INTEGER NOT NULL DEFAULT 0",
    # Sub-agent-completion turn trigger (2026-09-14) -- see jobs.py's own
    # _trigger_turn_for_job. A job that finished before this migration
    # simply has woken_ts=NULL, correctly: nothing before this feature
    # ever triggered a turn for a completion, so there's nothing to
    # backfill.
    "ALTER TABLE jobs ADD COLUMN woken_ts REAL",
    # Raw-content read access for a sub-agent's OWN job (2026-09-14,
    # operator's own ask) -- off by default (0) on every existing and new
    # row, on purpose: the deliberate exception to read_file's normal
    # summary-only containment is per-job and explicit, never automatic.
    # See jobs.py's own module docstring for the full reasoning.
    "ALTER TABLE jobs ADD COLUMN raw_file_access INTEGER NOT NULL DEFAULT 0",
    # Download provenance (2026-09-15, operator's own ask: a downloaded
    # file should read as downloaded, not indistinguishable from
    # something she wrote or generated herself) -- NULL on every
    # existing row, correctly: nothing before this feature was ever
    # fetched from a URL. Set only by workfiles.download_image(), never
    # by write_file/upload_file. See its own module docstring.
    "ALTER TABLE work_files ADD COLUMN source_url TEXT",
    # Per-peer screening kill switch (2026-09-15) -- every existing peer
    # gets the same default (1, screening on) a brand-new one
    # would; nothing already connected loses its injection screening just
    # by upgrading.
    "ALTER TABLE peers ADD COLUMN screening_enabled INTEGER NOT NULL DEFAULT 1",
    # ha_entities' exposure model, redesigned the same day it shipped
    # (2026-09-15, the operator's own direct answer, replacing a read/write +
    # device-class-severity scheme of mine before he'd ever tested it --
    # see homeassistant.py's own module docstring): enabled/
    # enabled_for_peers replace exposed_read/exposed_write. On an
    # instance that already created the table with the old columns
    # (init() swallows "duplicate column" the same as every other
    # migration here, so this is a no-op on a fresh install where
    # CREATE TABLE already has the new names), both new columns land at
    # 0 -- nothing was ever exposed under the old names either, since
    # this shipped and was redesigned the same day before real use.
    "ALTER TABLE ha_entities ADD COLUMN enabled INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE ha_entities ADD COLUMN enabled_for_peers INTEGER NOT NULL DEFAULT 0",
    # Task category (2026-09-16, operator's own addition to the tasks
    # feature the same day it first shipped) -- every task that already
    # exists (there weren't any real ones yet) lands on 'other', the same
    # default a brand-new one gets when a caller genuinely omits it.
    "ALTER TABLE tasks ADD COLUMN category TEXT NOT NULL DEFAULT 'other'",
    # Safe to run every startup regardless of whether the ALTER just above
    # actually fired this time or was already applied in an earlier one --
    # CREATE INDEX IF NOT EXISTS is idempotent the same way ADD COLUMN's
    # own OperationalError-swallow makes IT idempotent; by the time this
    # line runs, `category` exists on `tasks` either way.
    "CREATE INDEX IF NOT EXISTS ix_tasks_category ON tasks(user_id, category)",
    # ux_tracker_types_name widened to a PARTIAL index (2026-09-16, see
    # that index's own comment above for the bug this fixes). The SCHEMA
    # block's own CREATE UNIQUE INDEX IF NOT EXISTS never touches an
    # index that already exists under this name, even with a changed
    # definition -- an existing database is stuck on the old,
    # unconditional version forever without this explicit drop-and-
    # recreate. DROP INDEX IF EXISTS always succeeds (a no-op on a
    # brand-new database that never had the old one); the CREATE right
    # after it then always succeeds too, safe to repeat every startup.
    "DROP INDEX IF EXISTS ux_tracker_types_name",
    "CREATE UNIQUE INDEX ux_tracker_types_name ON tracker_types(user_id, name) WHERE enabled=1",
    # the PACI specification v0.9, §4.1 -- health check, local-only, never negotiated,
    # same posture as expiry_hours/resend_cap/reply_requested_cap above.
    # An existing peer gets the same defaults (5 min interval, 60s skew
    # warning threshold) a brand-new one would.
    "ALTER TABLE peers ADD COLUMN health_check_interval_minutes INTEGER NOT NULL DEFAULT 5",
    "ALTER TABLE peers ADD COLUMN health_check_clock_skew_warn_s INTEGER NOT NULL DEFAULT 60",
    # integration_health.py's own explain field (2026-09-19) -- landed a day
    # after the table itself shipped, so an already-live instance needs the
    # real ALTER, not just the CREATE TABLE's own column list above (which
    # only matters for a brand-new install). last_detail (the raw technical
    # error) already existed and is kept unchanged; this is a SEPARATE,
    # short, relayable "what it means and what would fix it" string Nori's
    # own health tool reads instead of the raw text (see that module's own
    # docstring on why the two are kept distinct).
    "ALTER TABLE integration_health ADD COLUMN last_explain TEXT",
    # Topic-triggered memory activation (2026-09-17) -- see memory.py's
    # own module docstring. safety_tier is a DELIBERATE flag, set only by
    # the model (remember/update_memory's own "safety" argument) or the
    # operator (the memory settings tab) -- never inferred by an algorithm,
    # same reasoning as every other authority-shaped decision in this
    # codebase staying explicit rather than heuristic. embedding_json/
    # embedding_model cache one memory's own embedding vector -- computed
    # lazily, safety_tier rows only (see EMBEDDING_MODEL), and recomputed
    # automatically if the configured model ever changes (embedding_model
    # stops matching, which is treated as a cache miss, not a stale hit).
    "ALTER TABLE memory ADD COLUMN safety_tier INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE memory ADD COLUMN embedding_json TEXT",
    "ALTER TABLE memory ADD COLUMN embedding_model TEXT",
    # Read receipts (the PACI specification §7.1, v1.3) -- local-only, never
    # negotiated, same posture as trust_level/health_check_* above. An
    # existing peer defaults to 'off', same as a brand-new one would --
    # emitting a receipt at all is an explicit opt-in, never something a
    # peer connection acquires silently on upgrade.
    "ALTER TABLE peers ADD COLUMN receipt_granularity TEXT NOT NULL DEFAULT 'off'",
    # sub_agents.model_id is NOT here -- see init()'s own comment on why
    # (same models-rename FK-rewrite hazard as model_chain).
]


def connect() -> sqlite3.Connection:
    _resolve_data_dir().mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_db_path(), timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=8000")
    return conn


def init() -> None:
    """Safe to call against a genuinely empty database -- this is the path
    every fresh `git clone` takes, not just an upgrade path for an existing
    one. Meets this project's shareability bar: migrations run from empty."""
    with _WRITE_LOCK, connect() as conn:
        conn.executescript(SCHEMA)
        for stmt in _MIGRATIONS:
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError:
                pass  # column/table already exists
        # Passive delivery (2026-09-12, the PACI specification §9.4/§13): a plain
        # ALTER-only migration, run the same way as everything in
        # _MIGRATIONS above, would dump every pre-existing 'received' row
        # into the very first turn after upgrade -- exactly the noisy
        # backlog this feature exists to avoid. So the backfill is coupled
        # here to the ALTER actually succeeding (the `else`), which only
        # happens once, the moment the column is first added -- never
        # again on a later restart, where a NULL row is by then
        # legitimately "arrived, not yet shown to her" and must NOT be
        # silently marked presented out from under a turn that hasn't
        # happened yet.
        try:
            conn.execute("ALTER TABLE peer_messages ADD COLUMN presented_ts REAL")
        except sqlite3.OperationalError:
            pass  # column already exists -- already handled, nothing to backfill
        else:
            conn.execute("UPDATE peer_messages SET presented_ts=? WHERE direction='received'",
                        (time.time(),))
        # Split into two independent dimensions (2026-09-19, operator's own
        # correction -- "read in a peer initiated turn is not read in a user
        # initiated turn," so overloading one flag for both was wrong). A
        # database that already ran the migration just above has a real,
        # meaningful `presented_ts` -- rename it rather than add a same-
        # shaped column and leave the old one dangling unused. On a genuinely
        # fresh database, SCHEMA above already created `presented_peer_ts`/
        # `presented_user_ts` directly and never had `presented_ts` at all,
        # so this RENAME correctly no-ops (OperationalError: no such column).
        try:
            conn.execute("ALTER TABLE peer_messages RENAME COLUMN presented_ts TO presented_peer_ts")
        except sqlite3.OperationalError:
            pass  # already renamed, or a fresh schema that never had the old name
        # presented_user_ts is a genuinely new column, coupled to the same
        # "don't dump a noisy backlog on upgrade" backfill precedent as
        # presented_ts's own migration above: a row already shown to her
        # under the old, single-flag model (presented_peer_ts IS NOT NULL,
        # by now renamed from presented_ts above) is treated as already
        # handled under BOTH new dimensions at the moment of this upgrade --
        # reusing that row's own presented_peer_ts value rather than
        # inventing a fresh time.time(), since "at or before this point" is
        # all this backfill needs to claim. A genuinely unread row
        # (presented_peer_ts IS NULL) stays NULL here too, correctly still
        # pending both ways. Only fires once, the moment the column is
        # first added -- never again on a later restart.
        try:
            conn.execute("ALTER TABLE peer_messages ADD COLUMN presented_user_ts REAL")
        except sqlite3.OperationalError:
            pass  # column already exists -- already handled, nothing to backfill
        else:
            conn.execute(
                "UPDATE peer_messages SET presented_user_ts=presented_peer_ts "
                "WHERE direction='received' AND presented_peer_ts IS NOT NULL")
        # needs_attention (2026-09-17, operator's own ask -- a read/needs-
        # attention flag on tasks/notes/reminders that drives the board's
        # own count badge). SCHEMA above gives every NEW row DEFAULT 1
        # directly; the coupled backfill here is the same "avoid dumping a
        # noisy backlog on upgrade" reasoning as presented_ts just above --
        # every task/note/reminder that already existed before this
        # feature shipped gets backfilled to 0 (already seen) the ONE time
        # each ALTER actually fires, so the badge starts clean at zero
        # instead of counting his entire existing backlog as "needing
        # attention" the moment he restarts into this. A later restart,
        # where the column already exists, skips the backfill entirely --
        # by then a 1 is a real, freshly-created item that must NOT be
        # silently marked read out from under him.
        try:
            conn.execute("ALTER TABLE tasks ADD COLUMN needs_attention INTEGER NOT NULL DEFAULT 1")
        except sqlite3.OperationalError:
            pass
        else:
            conn.execute("UPDATE tasks SET needs_attention=0")
        try:
            conn.execute("ALTER TABLE notes ADD COLUMN needs_attention INTEGER NOT NULL DEFAULT 1")
        except sqlite3.OperationalError:
            pass
        else:
            conn.execute("UPDATE notes SET needs_attention=0")
        try:
            conn.execute("ALTER TABLE reminders ADD COLUMN needs_attention INTEGER NOT NULL DEFAULT 1")
        except sqlite3.OperationalError:
            pass
        else:
            conn.execute("UPDATE reminders SET needs_attention=0")
        # Models page confusing-pair fix (2026-09-14, see models.py's own
        # _VALID_EFFORTS comment) -- "none" is no longer a selectable
        # reasoning_effort value, only ever None is; normalize any row an
        # earlier admin action already saved as the literal string "none"
        # down to the same meaning. No ALTER involved (schema unchanged),
        # so this can't be coupled to one succeeding once like the block
        # above -- just an always-safe no-op once there's nothing left to
        # normalize, run every startup regardless.
        conn.execute("UPDATE models SET reasoning_effort=NULL WHERE reasoning_effort='none'")
        # schedule_runs briefly shipped with a `REFERENCES schedules(id)`
        # FK (this feature's very first deploy, before any real schedule
        # had ever fired or been deleted) -- fixed above (see SCHEMA's own
        # comment on this table) once it turned out PRAGMA foreign_keys=ON
        # made that FK block exactly the "the record survives deletion"
        # guarantee this table exists for. CREATE TABLE IF NOT EXISTS just
        # above is a no-op against a table that already exists with the
        # OLD shape, so the fix has to happen here -- detected via PRAGMA
        # foreign_key_list rather than assumed, and only acts (rename,
        # recreate without the FK, copy every row across, drop the old
        # copy) the one time that old shape is still actually found on
        # disk; a fresh database created straight from today's SCHEMA
        # never has the FK in the first place, so this is a no-op there.
        if conn.execute("PRAGMA foreign_key_list(schedule_runs)").fetchall():
            conn.execute("ALTER TABLE schedule_runs RENAME TO schedule_runs_pre_fk_fix")
            conn.execute(
                "CREATE TABLE schedule_runs (id INTEGER PRIMARY KEY, schedule_id INTEGER NOT NULL, "
                "ts REAL NOT NULL, instruction TEXT NOT NULL, status TEXT NOT NULL, error TEXT)")
            conn.execute("INSERT INTO schedule_runs SELECT * FROM schedule_runs_pre_fk_fix")
            conn.execute("DROP TABLE schedule_runs_pre_fk_fix")
            conn.execute("CREATE INDEX IF NOT EXISTS ix_schedule_runs_schedule ON schedule_runs(schedule_id, ts)")
        # Providers/Models rework (2026-09-30) -- `models` moves from a
        # flat OpenRouter-slug list (slug/label, slug UNIQUE) to
        # provider_id/model_name/alias, and the old UNIQUE-on-slug
        # constraint has to go: two different providers can legitimately
        # use the same underlying model_name string. RENAME COLUMN can't
        # drop a constraint, so this is a real rebuild, same shape as the
        # schedule_runs fix just above -- detected via table_info (a
        # fresh database's SCHEMA above already creates the new shape
        # directly, so this is a no-op there), acts once. provider_id is
        # left NULL for every migrated row; providers.migrate_from_env()
        # (called once at startup, after this) links them to an
        # auto-created OpenRouter provider if OPENROUTER_API_KEY is set,
        # or leaves them unlinked -- surfaced honestly in the UI as
        # "needs a provider" -- if it isn't.
        if any(r["name"] == "slug" for r in conn.execute("PRAGMA table_info(models)").fetchall()):
            conn.execute("ALTER TABLE models RENAME TO models_pre_provider_rework")
            conn.execute(
                "CREATE TABLE models (id INTEGER PRIMARY KEY, provider_id INTEGER REFERENCES providers(id), "
                "model_name TEXT NOT NULL, alias TEXT NOT NULL, reasoning_effort TEXT, "
                "enabled INTEGER NOT NULL DEFAULT 1, seeded INTEGER NOT NULL DEFAULT 0, "
                "created_ts REAL NOT NULL)")
            conn.execute(
                "INSERT INTO models(id, provider_id, model_name, alias, reasoning_effort, enabled, "
                "seeded, created_ts) SELECT id, NULL, slug, label, reasoning_effort, enabled, seeded, "
                "created_ts FROM models_pre_provider_rework")
            conn.execute("DROP TABLE models_pre_provider_rework")
        # See the comment where model_chain used to live in SCHEMA above --
        # created here, unconditionally (IF NOT EXISTS -- a no-op on every
        # later startup), always AFTER `models` is guaranteed to already be
        # in its final shape, whether that took the rebuild just above or
        # SCHEMA's own CREATE TABLE IF NOT EXISTS already had it right on a
        # fresh database.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS model_chain (workspace_id INTEGER NOT NULL REFERENCES workspaces(id), "
            "model_id INTEGER NOT NULL REFERENCES models(id), priority INTEGER NOT NULL, "
            "PRIMARY KEY (workspace_id, priority))")
        # sub_agents.model_id -- same reasoning as model_chain just above:
        # added here (never in _MIGRATIONS, which runs before this point)
        # so its FK always compiles against `models` in its FINAL shape.
        try:
            conn.execute("ALTER TABLE sub_agents ADD COLUMN model_id INTEGER REFERENCES models(id)")
        except sqlite3.OperationalError:
            pass  # column already exists


def write(fn):
    with _WRITE_LOCK, connect() as conn:
        return fn(conn)


def read(fn):
    with connect() as conn:
        return fn(conn)


if __name__ == "__main__":
    init()
    print(f"initialised {_db_path()}")
    with connect() as c:
        for (name,) in c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
            n = c.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
            print(f"  {name:16} {n}")
