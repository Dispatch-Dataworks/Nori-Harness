# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Admin-managed sub-agent roster -- the only module with raw SQL against
`sub_agents`. She picks a labeled entry from this list, never invents an
endpoint or key herself -- adding or
disabling an entry is an admin-only HTTP action, not something a tool call
can do. The API key is encrypted at rest via crypto.py.
"""
from __future__ import annotations

import os
import time

import crypto
import store

# Sane bounds for the two per-agent tool caps (2026-09-14, operator's own
# ask: "one agent might get zero tool calls, another 100 per run"). Zero
# is always valid and means genuinely no tools -- these are ceilings on
# the nonzero case, not a floor that forces every agent to have tools.
TOOL_CALL_LIMIT_MAX = 1000
TOOL_BYTE_LIMIT_MIN = 10_000       # 10 KB -- technically valid, practically useless
TOOL_BYTE_LIMIT_MAX = 50_000_000   # 50 MB
TOOL_BYTE_LIMIT_DEFAULT = 2_000_000  # 2 MB -- roughly ten average-sized file reads


def _validate_limits(tool_call_limit: int, tool_byte_limit: int) -> str | None:
    if tool_call_limit < 0 or tool_call_limit > TOOL_CALL_LIMIT_MAX:
        return f"tool-call limit must be between 0 and {TOOL_CALL_LIMIT_MAX}"
    if tool_call_limit > 0 and not (TOOL_BYTE_LIMIT_MIN <= tool_byte_limit <= TOOL_BYTE_LIMIT_MAX):
        return (f"byte limit must be between {TOOL_BYTE_LIMIT_MIN:,} and "
                f"{TOOL_BYTE_LIMIT_MAX:,} when tools are enabled")
    return None


def create(created_by: int, label: str, model: str, base_url: str, api_key: str,
          tool_call_limit: int = 0, tool_byte_limit: int = TOOL_BYTE_LIMIT_DEFAULT) -> tuple[bool, str | int]:
    """api_key may be blank -- means "use the operator's own
    OPENROUTER_API_KEY" (2026-09-12), made explicit rather than the
    previous behavior (required, so a blank field just errored and had to
    be re-entered every time). Stored as a literal empty string, not
    encrypted-empty -- real_api_key() below checks for exactly that.

    tool_call_limit=0 (the default) means exactly what it always meant
    before tools existed at all: no tools, one plain completion, nothing
    else to configure. See jobs.py for what a nonzero limit actually buys."""
    label = label.strip()
    if not label or not model.strip() or not base_url.strip():
        return False, "label, model, and base_url are all required"
    if get_by_label(label) is not None:
        return False, f"a sub-agent named {label!r} already exists"
    err = _validate_limits(tool_call_limit, tool_byte_limit)
    if err:
        return False, err
    now = time.time()
    key_enc = crypto.encrypt(api_key) if api_key.strip() else ""
    sid = store.write(lambda c: c.execute(
        "INSERT INTO sub_agents(label, model, base_url, api_key_enc, enabled, "
        "tool_call_limit, tool_byte_limit, created_ts, created_by) VALUES (?,?,?,?,1,?,?,?,?)",
        (label, model.strip(), base_url.strip(), key_enc, tool_call_limit, tool_byte_limit,
         now, created_by)).lastrowid)
    return True, sid


def set_limits(sub_agent_id: int, tool_call_limit: int, tool_byte_limit: int) -> str | None:
    """Returns an error string, or None on success -- same shape as
    create()'s own validation, reused rather than re-implemented."""
    err = _validate_limits(tool_call_limit, tool_byte_limit)
    if err:
        return err
    store.write(lambda c: c.execute(
        "UPDATE sub_agents SET tool_call_limit=?, tool_byte_limit=? WHERE id=?",
        (tool_call_limit, tool_byte_limit, sub_agent_id)))
    return None


def list_all() -> list[dict]:
    # api_key_enc included (2026-09-12) -- uses_default_key() needs it for
    # the roster's own "default key" vs "own key" display; the real
    # decrypted key itself is never exposed by this function regardless.
    rows = store.read(lambda c: c.execute(
        "SELECT id, label, model, base_url, api_key_enc, enabled, tool_call_limit, "
        "tool_byte_limit, created_ts FROM sub_agents ORDER BY id").fetchall())
    return [dict(r) for r in rows]


def get(sub_agent_id: int) -> dict | None:
    r = store.read(lambda c: c.execute("SELECT * FROM sub_agents WHERE id=?", (sub_agent_id,)).fetchone())
    return dict(r) if r else None


def get_by_label(label: str) -> dict | None:
    r = store.read(lambda c: c.execute("SELECT * FROM sub_agents WHERE label=?", (label,)).fetchone())
    return dict(r) if r else None


def get_enabled_by_label(label: str) -> dict | None:
    row = get_by_label(label)
    return row if row and row["enabled"] else None


def set_enabled(sub_agent_id: int, enabled: bool) -> None:
    store.write(lambda c: c.execute(
        "UPDATE sub_agents SET enabled=? WHERE id=?", (1 if enabled else 0, sub_agent_id)))


def uses_default_key(row: dict) -> bool:
    return not row["api_key_enc"]


def real_api_key(row: dict) -> str:
    if uses_default_key(row):
        return os.environ.get("OPENROUTER_API_KEY", "")
    return crypto.decrypt(row["api_key_enc"])
