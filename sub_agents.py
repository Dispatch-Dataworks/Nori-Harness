# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Admin-managed sub-agent roster -- the only module with raw SQL against
`sub_agents`. She picks a labeled entry from this list, never invents an
endpoint or model herself -- adding or disabling an entry is an admin-only
HTTP action, not something a tool call can do. Each entry points at a
models.py roster Model (provider + auth already encapsulated there, see
providers.py) via model_id, rather than carrying its own endpoint/key.
"""
from __future__ import annotations

import time

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


def create(created_by: int, label: str, model_id: int | None,
          tool_call_limit: int = 0, tool_byte_limit: int = TOOL_BYTE_LIMIT_DEFAULT,
          file_write: bool = False, web_access: bool = False,
          write_folder: str = "", raw_file_access: bool = False) -> tuple[bool, str | int]:
    """model_id (2026-09-30, see models.py/providers.py) -- a sub-agent
    now picks a roster Model (alias -> provider -> real model name)
    instead of free-typing its own model/base_url/api key; jobs.py
    resolves and dispatches through it exactly like the primary chat turn.
    May be None/0 -- the admin form always requires picking one, but the
    data layer itself allows a sub-agent to exist unconfigured (same state
    an old pre-migration row is left in), rather than making "has no
    model yet" an error instead of a fact jobs.py can just report cleanly
    when someone tries to actually dispatch to it.

    tool_call_limit=0 (the default) means exactly what it always meant
    before tools existed at all: no tools, one plain completion, nothing
    else to configure. See jobs.py for what a nonzero limit actually buys.

    file_write / web_access (2026-10-02) -- per-agent opt-ins on top of
    the read-only default; both only matter when tool_call_limit > 0, and
    both default off. See jobs.allowed_tool_names()."""
    label = label.strip()
    if not label:
        return False, "a label is required"
    if get_by_label(label) is not None:
        return False, f"a sub-agent named {label!r} already exists"
    err = _validate_limits(tool_call_limit, tool_byte_limit)
    if err:
        return False, err
    import workfiles  # local: same reasoning as every other subsystem module
    write_folder, ferr = workfiles.validate_write_folder(write_folder)
    if ferr:
        return False, ferr
    now = time.time()
    # model/base_url/api_key_enc are legacy NOT NULL columns kept for old
    # rows (see store.py's schema comment) -- new rows just satisfy the
    # NOT NULL constraint with empty placeholders; model_id is what
    # jobs.py actually reads.
    sid = store.write(lambda c: c.execute(
        "INSERT INTO sub_agents(label, model, base_url, api_key_enc, enabled, "
        "tool_call_limit, tool_byte_limit, created_ts, created_by, model_id, file_write, web_access, "
        "write_folder, raw_file_access) VALUES (?,'','','',1,?,?,?,?,?,?,?,?,?)",
        (label, tool_call_limit, tool_byte_limit, now, created_by, model_id or None,
         1 if file_write else 0, 1 if web_access else 0, write_folder,
         1 if raw_file_access else 0)).lastrowid)
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


def set_access(sub_agent_id: int, file_write: bool, web_access: bool,
              write_folder: str = "", raw_file_access: bool = False) -> str | None:
    """Returns an error string, or None on success (same shape as
    set_limits). write_folder is validated and normalized first; nothing
    is saved if it's invalid."""
    import workfiles  # local: same reasoning as every other subsystem module
    write_folder, ferr = workfiles.validate_write_folder(write_folder)
    if ferr:
        return ferr
    store.write(lambda c: c.execute(
        "UPDATE sub_agents SET file_write=?, web_access=?, write_folder=?, raw_file_access=? WHERE id=?",
        (1 if file_write else 0, 1 if web_access else 0, write_folder,
         1 if raw_file_access else 0, sub_agent_id)))
    return None


def list_all() -> list[dict]:
    rows = store.read(lambda c: c.execute(
        "SELECT id, label, model_id, enabled, tool_call_limit, "
        "tool_byte_limit, file_write, web_access, write_folder, raw_file_access, created_ts "
        "FROM sub_agents ORDER BY id").fetchall())
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


def job_counts() -> dict[int, int]:
    """{sub_agent_id: job count}, every agent that has run at least one --
    server.py's roster row uses this to word the delete confirmation
    accurately per agent instead of a generic warning."""
    rows = store.read(lambda c: c.execute(
        "SELECT sub_agent_id, COUNT(*) AS n FROM jobs GROUP BY sub_agent_id").fetchall())
    return {r["sub_agent_id"]: r["n"] for r in rows}


def delete(sub_agent_id: int, force: bool = False) -> tuple[bool, str]:
    """jobs.sub_agent_id is a real, NOT NULL FK (unlike models.provider_id/
    sub_agents.model_id, both nullable) -- a sub-agent with real job/cost
    history can't be deleted without also deleting that history, no
    middle ground the schema allows. Without force, that's refused with a
    clear count rather than silently destroying an audit trail (disable
    stays the non-destructive option). With force=True (2026-09-30,
    operator's own explicit call: "if that means deleting the history
    then we can do that") -- the caller (server.py's confirm() dialog) is
    responsible for making that consequence clear before this is ever
    invoked; this function trusts force and doesn't ask a second time."""
    count = store.read(lambda c: c.execute(
        "SELECT COUNT(*) AS n FROM jobs WHERE sub_agent_id=?", (sub_agent_id,)).fetchone())["n"]
    if count and not force:
        return False, (f"this agent has {count} job(s) in its history -- disable it instead to keep "
                       f"that history intact, or delete again to also delete that history")
    if count:
        store.write(lambda c: c.execute("DELETE FROM jobs WHERE sub_agent_id=?", (sub_agent_id,)))
    store.write(lambda c: c.execute("DELETE FROM sub_agents WHERE id=?", (sub_agent_id,)))
    return True, f"deleted (including {count} job(s) of history)" if count else "deleted"
