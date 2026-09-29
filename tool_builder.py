# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""The generated-tool lifecycle: draft -> static checks -> sandboxed dry-
run -> approve (typed confirmation) -> enable -> widen. Every step here is
admin-only HTTP action, not a model-callable tool -- who can call an
approved tool is a separate question, answered by the tool itself once
it's registered.

This is deliberately the most conservative module in the app, per this
run's standing instruction: default-deny imports, a genuinely separate
subprocess with a scrubbed environment, a fresh per-run scratch
directory, a hard wall-clock timeout.

Honest limits, not papered over -- favoring conservative means saying
plainly what ISN'T covered rather than faking coverage: on Windows,
without Docker or a firewall API, there is no real OS-level network-
egress sandbox and no enforced CPU/memory cap. The static import-
allowlist blocking every network-capable module is the actual mitigation
for network egress -- refused at review time, since it can't be enforced
at the OS level here. The wall-clock timeout (subprocess.run(timeout=))
is the one resource limit that IS genuinely enforced. A generated tool is
a pure function -- JSON args in, JSON result out, no database or memory
access of its own -- a capability API for tools that need that is a
deliberate future enhancement, not built now.

Versioning: append-only per name. An edit is a new row (version+1), never
an overwrite; approved/enabled/available_to all start over at their most
restrictive default on the new version, so an edit to a live tool can
never silently inherit its predecessor's trust.

Registration into the live tool registry happens once, at server startup
(load_approved_tools()) -- a newly approved+enabled tool needs a restart
to actually go live. No hot-reload: loading arbitrary newly-approved code
into an already-running process on the fly is a materially bigger, riskier
feature than this build calls for.
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import store

APPROVE_PHRASE = "I have reviewed and approve this tool"
SANDBOX_TIMEOUT_S = int(os.environ.get("NORI_TOOLBUILDER_TIMEOUT_S", "10"))
_SCRATCH_ROOT = store.DATA_DIR / "tool_sandbox"

# Default-deny: nothing reaches the network or the filesystem from inside
# generated code. Widening this list is a code change to THIS file, not
# something a draft or an admin form can request -- it's the one place in
# the whole tool-builder feature that's genuinely not configurable.
ALLOWED_IMPORTS = frozenset({"json", "re", "math", "datetime", "time", "collections",
                            "itertools", "string", "statistics", "textwrap", "random"})
BANNED_NAMES = frozenset({"eval", "exec", "compile", "open", "__import__", "globals",
                          "locals", "vars", "input", "exit", "quit", "breakpoint"})
BANNED_ATTRS = frozenset({"system", "popen", "fork", "spawn", "remove", "unlink", "rmtree"})

_RUNNER_TEMPLATE = """\
import json
with open({args_path!r}, encoding="utf-8") as f:
    _args = json.load(f)

{code}

try:
    _result = run(_args)
    if not isinstance(_result, dict):
        _result = {{"error": "run() must return a dict"}}
except Exception as exc:
    _result = {{"error": f"tool raised: {{exc}}"}}
with open({result_path!r}, "w", encoding="utf-8") as f:
    json.dump(_result, f, default=str)
"""


def static_check(code: str) -> list[str]:
    """Returns a list of flags -- empty means clean, and clean is required
    (not merely advisory) before a dry-run or an approval can happen.
    A generated tool must define a top-level `def run(args): -> dict` --
    that's the only entry point the sandbox ever calls."""
    flags: list[str] = []
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return [f"does not parse: {exc}"]
    if not any(isinstance(n, ast.FunctionDef) and n.name == "run" for n in ast.walk(tree)):
        flags.append("must define a top-level function named 'run(args)'")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top not in ALLOWED_IMPORTS:
                    flags.append(f"disallowed import: {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            top = (node.module or "").split(".")[0]
            if top not in ALLOWED_IMPORTS:
                flags.append(f"disallowed import: {node.module}")
        elif isinstance(node, ast.Name) and node.id in BANNED_NAMES:
            flags.append(f"disallowed name used: {node.id}")
        elif isinstance(node, ast.Attribute) and node.attr in BANNED_ATTRS:
            flags.append(f"disallowed attribute access: .{node.attr}")
    return flags


def run_sandboxed(code: str, args: dict) -> dict:
    """Real subprocess isolation: fresh scratch dir, scrubbed environment
    (no secrets -- not even OPENROUTER_API_KEY), hard timeout. Always
    returns a dict -- a crash, a timeout, and a clean result all produce
    one; nothing here lets an exception from generated code escape into
    the caller."""
    _SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(dir=str(_SCRATCH_ROOT)))
    try:
        args_path = run_dir / "args.json"
        result_path = run_dir / "result.json"
        script_path = run_dir / "tool_code.py"
        args_path.write_text(json.dumps(args), encoding="utf-8")
        script_path.write_text(
            _RUNNER_TEMPLATE.format(args_path=str(args_path), result_path=str(result_path), code=code),
            encoding="utf-8")
        env = {"PATH": os.environ.get("PATH", "")}
        if os.name == "nt":
            env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", "")
        try:
            proc = subprocess.run(
                [sys.executable, str(script_path)], cwd=str(run_dir), env=env,
                timeout=SANDBOX_TIMEOUT_S, capture_output=True, text=True)
        except subprocess.TimeoutExpired:
            return {"error": f"timed out after {SANDBOX_TIMEOUT_S}s"}
        if not result_path.is_file():
            return {"error": f"produced no result -- crashed before writing one. stderr: {(proc.stderr or '')[:500]}"}
        try:
            return json.loads(result_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {"error": "result file was not valid JSON"}
    finally:
        import shutil
        shutil.rmtree(run_dir, ignore_errors=True)


# ── lifecycle ─────────────────────────────────────────────────────────────
def _log(draft_id: int, action: str, actor: int, *, note: str = "") -> None:
    store.write(lambda c: c.execute(
        "INSERT INTO tool_audit_events(ts, draft_id, action, actor, note) VALUES (?,?,?,?,?)",
        (time.time(), draft_id, action, str(actor), note[:1000])))


def get(draft_id: int) -> dict | None:
    r = store.read(lambda c: c.execute("SELECT * FROM tool_drafts WHERE id=?", (draft_id,)).fetchone())
    return dict(r) if r else None


def list_drafts() -> list[dict]:
    return [dict(r) for r in store.read(lambda c: c.execute(
        "SELECT * FROM tool_drafts ORDER BY name, version DESC").fetchall())]


def audit_events(draft_id: int) -> list[dict]:
    return [dict(r) for r in store.read(lambda c: c.execute(
        "SELECT * FROM tool_audit_events WHERE draft_id=? ORDER BY ts", (draft_id,)).fetchall())]


def create_draft(created_by: int, name: str, description: str, params: dict, code: str) -> dict:
    """params is a plain JSON-schema `properties` dict (+ 'required' list
    folded in by the caller) -- the admin form builds this from a simple
    per-line spec; a future model-driven `propose_tool` tool could call
    this exact function too, since it doesn't care who authored the code."""
    name = name.strip()
    flags = static_check(code)
    now = time.time()
    existing = store.read(lambda c: c.execute(
        "SELECT max(version) AS v FROM tool_drafts WHERE name=?", (name,)).fetchone())
    version = (existing["v"] or 0) + 1
    schema = {"type": "function", "function": {"name": name, "description": description, "parameters": params}}
    draft_id = store.write(lambda c: c.execute(
        "INSERT INTO tool_drafts(name, version, description, schema_json, code, risk_tier, "
        "available_to, approved, enabled, static_flags, created_by, created_ts) "
        "VALUES (?,?,?,?,?,'C','admin_only',0,0,?,?,?)",
        (name, version, description, json.dumps(schema), code, json.dumps(flags), created_by, now)
    ).lastrowid)
    _log(draft_id, "created", created_by, note=f"v{version}")
    _log(draft_id, "static_checked", created_by, note=("clean" if not flags else f"flags: {flags}"))
    return {"id": draft_id, "name": name, "version": version, "static_flags": flags}


def _sample_args(params_schema: dict) -> dict:
    """Placeholder values derived from the schema so a dry-run actually
    exercises the tool's logic instead of guaranteeing a KeyError for any
    tool with required parameters -- {} would do exactly that."""
    placeholders = {"string": "sample", "number": 1, "integer": 1, "boolean": True, "array": []}
    props = (params_schema or {}).get("properties", {})
    return {name: placeholders.get(spec.get("type", "string"), "sample") for name, spec in props.items()}


def dry_run(draft_id: int, actor: int) -> dict:
    row = get(draft_id)
    if row is None:
        return {"error": "no such draft"}
    flags = json.loads(row["static_flags"] or "[]")
    if flags:
        return {"error": f"cannot dry-run -- static checks flagged: {flags}"}
    schema = json.loads(row["schema_json"])
    sample = _sample_args(schema.get("function", {}).get("parameters", {}))
    result = run_sandboxed(row["code"], sample)
    ok = "error" not in result
    store.write(lambda c: c.execute(
        "UPDATE tool_drafts SET dry_run_ok=?, dry_run_output=? WHERE id=?",
        (1 if ok else 0, json.dumps(result), draft_id)))
    _log(draft_id, "dry_run", actor, note=json.dumps(result)[:500])
    return result


def approve(draft_id: int, actor: int, confirm_phrase: str) -> tuple[bool, str]:
    if confirm_phrase != APPROVE_PHRASE:
        return False, f"type exactly: {APPROVE_PHRASE!r}"
    row = get(draft_id)
    if row is None:
        return False, "no such draft"
    flags = json.loads(row["static_flags"] or "[]")
    if flags:
        return False, f"cannot approve -- static checks flagged: {flags}"
    if not row["dry_run_ok"]:
        return False, "run a successful dry-run first"
    store.write(lambda c: c.execute(
        "UPDATE tool_drafts SET approved=1, approved_by=?, approved_ts=? WHERE id=?",
        (actor, time.time(), draft_id)))
    _log(draft_id, "approved", actor)
    return True, "approved -- enable it to make it live"


def enable(draft_id: int, actor: int) -> tuple[bool, str]:
    row = get(draft_id)
    if row is None:
        return False, "no such draft"
    if not row["approved"]:
        return False, "not approved yet"
    store.write(lambda c: c.execute("UPDATE tool_drafts SET enabled=1 WHERE id=?", (draft_id,)))
    _log(draft_id, "enabled", actor)
    return True, "enabled -- restart the server to activate it (no hot-reload, by design)"


def disable(draft_id: int, actor: int) -> tuple[bool, str]:
    store.write(lambda c: c.execute("UPDATE tool_drafts SET enabled=0 WHERE id=?", (draft_id,)))
    _log(draft_id, "disabled", actor)
    return True, "disabled -- restart the server to actually remove it from the live registry"


def widen(draft_id: int, actor: int) -> tuple[bool, str]:
    row = get(draft_id)
    if row is None:
        return False, "no such draft"
    if not row["approved"]:
        return False, "not approved yet"
    store.write(lambda c: c.execute("UPDATE tool_drafts SET available_to='all_members' WHERE id=?", (draft_id,)))
    _log(draft_id, "widened", actor, note="admin_only -> all_members")
    return True, "widened to all_members -- restart to apply"


# ── registration into the live tool registry (startup only) ─────────────
def _register_generated_tool(row: dict) -> None:
    import tools  # local: tools.py never needs to know this module exists
    schema = json.loads(row["schema_json"])
    min_role = "member" if row["available_to"] == "all_members" else "admin"
    code = row["code"]

    def _impl(session, **kwargs):  # noqa: ARG001 -- session unused: generated tools are pure functions
        return run_sandboxed(code, kwargs)

    tools.register(tools.Tool(row["name"], schema, _impl, min_role=min_role,
                              data_scope="self", risk_tier=row["risk_tier"]))


def load_approved_tools() -> int:
    """Call once at server startup. Returns how many were loaded."""
    rows = store.read(lambda c: c.execute(
        "SELECT t.* FROM tool_drafts t JOIN "
        "(SELECT name, max(version) AS mv FROM tool_drafts WHERE approved=1 AND enabled=1 GROUP BY name) latest "
        "ON t.name = latest.name AND t.version = latest.mv "
        "WHERE t.approved=1 AND t.enabled=1").fetchall())
    for row in rows:
        _register_generated_tool(dict(row))
    return len(rows)
