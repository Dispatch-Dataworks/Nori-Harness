# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""run_sandboxed_command -- Nori's code-execution tool (2026-10-04).

Where it actually runs: NOT in this container. A separate `sandbox`
container (docker-compose.yml, sandbox_service/runner.py) with no network, no
secrets, no access to this container's data volume; the only thing shared
is a socket. This module is the client: it checks that the call is
allowed, copies the chosen working-folder subtree out, asks the runner to
run the command against that COPY, and brings the results back.

What that buys, concretely (nothing here is a promise about "the model
being careful"):
  * the code can't reach the network, .env, secret.key, the database, or
    any file outside the copy it was handed;
  * it can't change the real working folder: it gets a copy. Files it
    creates or changes are saved back by THIS module through
    workfiles.write_bytes -- the same write path every model-driven write
    uses, so the write-folder scope, the "never overwrite a file you didn't
    create (it lands as name.v2.ext instead)" rule, and the quotas all
    apply unchanged. A deletion inside the sandbox is reported and never
    applied. So "confirm before it touches the user's files" is structural:
    there is no way for a run to overwrite or delete one;
  * only allowlisted interpreters (python3) run; there's no shell;
  * every call -- including refused ones -- is written to sandbox_runs.

Off until an admin enables the "sandbox_enabled" workspace setting. A
sub-agent additionally needs its own code_exec checkbox (see
jobs.allowed_tool_names), and without file_write its runs can still compute
and print but nothing is saved.
"""
from __future__ import annotations

import base64
import json
import os
import shlex
import socket
import struct
import time
from pathlib import Path

import config
import store

SOCKET_PATH = os.environ.get("NORI_SANDBOX_SOCKET", "")
ALLOWED_COMMANDS = tuple(c.strip() for c in os.environ.get("NORI_SANDBOX_ALLOWED", "python3").split(",") if c.strip())
DEFAULT_TIMEOUT_S = 30
MAX_TIMEOUT_S = int(os.environ.get("NORI_SANDBOX_MAX_TIMEOUT_S", "120"))
MAX_COMMAND_CHARS = 20_000
# What gets copied into the sandbox for a run.
MOUNT_MAX_FILES = 300
MOUNT_MAX_FILE_BYTES = 5 * 1024 * 1024
MOUNT_MAX_TOTAL_BYTES = 20 * 1024 * 1024
# What a model sees of the output (the audit row keeps only sizes).
MODEL_STDOUT_CHARS = 6000
MODEL_STDERR_CHARS = 4000
RESPONSE_MAX_BYTES = 120 * 1024 * 1024
CONNECT_TIMEOUT_S = 5
RECENT_RUNS_DEFAULT = 25


class SandboxError(Exception):
    pass


def is_enabled(session: dict) -> bool:
    return bool(config.get("workspace", session["workspace_id"], "sandbox_enabled"))


# ── the socket client ────────────────────────────────────────────────────
def _recv_exact(s: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = s.recv(min(1 << 20, n - len(buf)))
        if not chunk:
            raise SandboxError("the sandbox closed the connection before replying")
        buf += chunk
    return bytes(buf)


def _rpc(payload: dict, timeout: float) -> dict:
    if not SOCKET_PATH:
        raise SandboxError("the sandbox isn't configured on this instance (NORI_SANDBOX_SOCKET is unset)")
    if not hasattr(socket, "AF_UNIX"):
        raise SandboxError("this platform can't talk to the sandbox (no unix sockets)")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(CONNECT_TIMEOUT_S)
        try:
            s.connect(SOCKET_PATH)
        except (FileNotFoundError, ConnectionRefusedError):
            raise SandboxError("the sandbox container isn't running (an admin can start it with "
                               "`docker compose up -d sandbox`)")
        except OSError as exc:
            raise SandboxError(f"couldn't reach the sandbox: {exc}")
        s.settimeout(timeout)
        body = json.dumps(payload, separators=(",", ":")).encode()
        s.sendall(struct.pack(">I", len(body)) + body)
        (length,) = struct.unpack(">I", _recv_exact(s, 4))
        if length > RESPONSE_MAX_BYTES:
            raise SandboxError("the sandbox's reply was unreasonably large")
        return json.loads(_recv_exact(s, length))
    except socket.timeout:
        raise SandboxError("the sandbox didn't answer in time (it may be busy with another run) -- try again")
    except (ValueError, struct.error):
        raise SandboxError("the sandbox sent a reply that couldn't be understood")
    finally:
        s.close()


def status() -> dict:
    """For the admin page: {"configured", "reachable", "detail", ...}. Never raises."""
    if not SOCKET_PATH:
        return {"configured": False, "reachable": False,
                "detail": "NORI_SANDBOX_SOCKET isn't set -- this instance has no sandbox service"}
    try:
        r = _rpc({"op": "ping"}, 5)
    except SandboxError as exc:
        return {"configured": True, "reachable": False, "detail": str(exc)}
    if not r.get("ok"):
        return {"configured": True, "reachable": False, "detail": str(r.get("error") or "unknown error")}
    return {"configured": True, "reachable": True, "detail": "running",
            "allowed": r.get("allowed"), "max_timeout_s": r.get("max_timeout_s"), "mem_mb": r.get("mem_mb")}


# ── validation ────────────────────────────────────────────────────────────
def _parse_command(command) -> list[str]:
    if not isinstance(command, str) or not command.strip():
        raise SandboxError("command is required, e.g. python3 script.py")
    if len(command) > MAX_COMMAND_CHARS:
        raise SandboxError(f"command is too long (limit {MAX_COMMAND_CHARS} characters)")
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise SandboxError(f"couldn't parse the command: {exc}")
    if not argv:
        raise SandboxError("command is required, e.g. python3 script.py")
    if argv[0] not in ALLOWED_COMMANDS:
        raise SandboxError(f"only {', '.join(ALLOWED_COMMANDS)} can be run here -- {argv[0]!r} isn't allowed "
                           f"(there is no shell, so no pipes or redirects either)")
    return argv


def _clamp_timeout(timeout_seconds) -> int:
    try:
        t = int(timeout_seconds) if timeout_seconds not in (None, "") else DEFAULT_TIMEOUT_S
    except (TypeError, ValueError):
        raise SandboxError("timeout_seconds must be a whole number")
    if t < 1:
        raise SandboxError("timeout_seconds must be at least 1")
    return min(t, MAX_TIMEOUT_S)


def _resolve_cwd(session: dict, cwd: str) -> tuple[Path, str]:
    """(absolute dir, working-folder-relative key). A sub-agent confined to a
    write_folder can only run in it (so what a run can see and what it can
    change coincide)."""
    import workfiles
    user_id = session["user_id"]
    try:
        target = workfiles._resolve(user_id, cwd or "")
    except workfiles.WorkfileError as exc:
        raise SandboxError(str(exc))
    if not target.is_dir():
        raise SandboxError(f"{cwd!r} isn't a folder in the working folder")
    rel = "" if target == workfiles._user_root(user_id) else workfiles._rel_key(user_id, target)
    scope = session.get("_write_root")
    if scope:
        try:
            root = workfiles._resolve(user_id, scope)
        except workfiles.WorkfileError as exc:
            raise SandboxError(str(exc))
        if not (target == root or root in target.parents):
            raise SandboxError(f"this sub-agent can only run code inside {scope}/ -- pass a cwd under it")
    return target, rel


def _collect_inputs(target: Path) -> tuple[dict[str, str], list[str]]:
    """The copy handed to the sandbox: every regular file under `target`
    (no symlinks) within the size caps. Returns ({rel: base64}, [skipped])."""
    files: dict[str, str] = {}
    skipped: list[str] = []
    total = 0
    for dirpath, dirnames, filenames in os.walk(target, followlinks=False):
        dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]
        for name in sorted(filenames):
            full = Path(dirpath) / name
            rel = full.relative_to(target).as_posix()
            if full.is_symlink() or not full.is_file():
                continue
            size = full.stat().st_size
            if size > MOUNT_MAX_FILE_BYTES:
                skipped.append(f"{rel} (over {MOUNT_MAX_FILE_BYTES // (1024 * 1024)} MB)")
                continue
            if len(files) >= MOUNT_MAX_FILES or total + size > MOUNT_MAX_TOTAL_BYTES:
                raise SandboxError(
                    f"that folder is too big to copy into the sandbox (limit {MOUNT_MAX_FILES} files / "
                    f"{MOUNT_MAX_TOTAL_BYTES // (1024 * 1024)} MB) -- pass a narrower cwd")
            total += size
            files[rel] = base64.b64encode(full.read_bytes()).decode()
    return files, skipped


def _clip(text: str, cap: int) -> str:
    if len(text) <= cap:
        return text
    head, tail = cap * 2 // 3, cap // 3
    return text[:head] + f"\n...[{len(text) - head - tail} characters omitted]...\n" + text[-tail:]


# ── the audit log ─────────────────────────────────────────────────────────
def _log(session: dict, command: str, cwd: str, outcome: str, *, exit_code=None, duration_ms=None,
         error: str | None = None, stdout_chars=0, stderr_chars=0, changes=None, deleted=None) -> None:
    job_id = session.get("_job_id")
    try:
        store.write(lambda c: c.execute(
            "INSERT INTO sandbox_runs(ts, user_id, source, job_id, agent, command, cwd, outcome, exit_code, "
            "duration_ms, error, stdout_chars, stderr_chars, changes, deleted) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), session["user_id"], "job" if job_id else "chat", job_id, session.get("_job_agent"),
             command[:2000], cwd, outcome, exit_code, duration_ms, (error or None) and error[:500],
             stdout_chars, stderr_chars, json.dumps(changes) if changes else None,
             json.dumps(deleted) if deleted else None)))
    except Exception as exc:  # noqa: BLE001 -- an audit failure must not turn into a failed run
        print(f"sandbox: couldn't write the audit row: {type(exc).__name__}: {exc}", flush=True)


def recent_runs(limit: int = RECENT_RUNS_DEFAULT) -> list[dict]:
    rows = store.read(lambda c: c.execute(
        "SELECT * FROM sandbox_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall())
    out = []
    for r in rows:
        d = dict(r)
        for k in ("changes", "deleted"):
            try:
                d[k] = json.loads(d[k]) if d[k] else []
            except ValueError:
                d[k] = []
        out.append(d)
    return out


# ── the tool ──────────────────────────────────────────────────────────────
def _apply_changes(session: dict, cwd_rel: str, changed: list[dict]) -> list[dict]:
    """Save what the run created/changed, through workfiles' own write path.
    versioned_writes is forced on: a changed copy of a file she didn't create
    is saved beside it as name.v2.ext, never over it."""
    import workfiles
    apply_session = {**session, "_versioned_writes": True}
    out = []
    for ent in changed:
        rel = ent["path"]
        full = f"{cwd_rel}/{rel}" if cwd_rel else rel
        item = {"path": full, "status": ent["status"], "size": ent.get("size")}
        if session.get("_sandbox_no_apply"):
            item["saved"] = False
            item["note"] = "not saved: this sub-agent isn't allowed to write files"
        elif ent.get("omitted"):
            item["saved"] = False
            item["note"] = f"not saved: {ent['omitted']}"
        else:
            try:
                data = base64.b64decode(ent.get("content_b64") or "")
            except Exception:  # noqa: BLE001
                data = None
            if data is None:
                item["saved"] = False
                item["error"] = "couldn't decode the file the sandbox returned"
            else:
                res = workfiles.write_bytes(apply_session, full, data)
                if res.get("ok"):
                    item["saved"] = True
                    item["saved_as"] = res["path"]
                    if res.get("note"):
                        item["note"] = res["note"]
                else:
                    item["saved"] = False
                    item["error"] = res.get("error", "write refused")
        out.append(item)
    return out


def run_sandboxed_command(session: dict, command: str = "", cwd: str = "", timeout_seconds=None) -> dict:
    cmd_text = command if isinstance(command, str) else ""
    cwd = cwd if isinstance(cwd, str) else ""
    if not is_enabled(session):
        _log(session, cmd_text, cwd, "refused", error="sandbox is turned off")
        return {"error": "the code sandbox is turned off -- an admin can enable it in settings"}
    try:
        argv = _parse_command(command)
        timeout_s = _clamp_timeout(timeout_seconds)
        target, cwd_rel = _resolve_cwd(session, cwd)
        files, skipped = _collect_inputs(target)
    except SandboxError as exc:
        _log(session, cmd_text, cwd, "refused", error=str(exc))
        return {"error": str(exc)}
    try:
        reply = _rpc({"op": "run", "command": argv, "cwd": "", "timeout_s": timeout_s, "files": files},
                     timeout_s + 90)
    except SandboxError as exc:
        _log(session, cmd_text, cwd_rel, "unavailable", error=str(exc))
        return {"error": str(exc)}
    if not reply.get("ok"):
        err = str(reply.get("error") or "the sandbox refused the run")
        _log(session, cmd_text, cwd_rel, "refused", error=err)
        return {"error": err}

    changed = reply.get("changed") or []
    deleted = [f"{cwd_rel}/{d}" if cwd_rel else d for d in (reply.get("deleted") or [])]
    files_out = _apply_changes(session, cwd_rel, changed)
    stdout = _clip(reply.get("stdout") or "", MODEL_STDOUT_CHARS)
    stderr = _clip(reply.get("stderr") or "", MODEL_STDERR_CHARS)
    outcome = "timeout" if reply.get("timed_out") else ("ok" if reply.get("exit_code") == 0 else "nonzero")
    _log(session, cmd_text, cwd_rel, outcome, exit_code=reply.get("exit_code"),
         duration_ms=reply.get("duration_ms"), stdout_chars=len(reply.get("stdout") or ""),
         stderr_chars=len(reply.get("stderr") or ""),
         changes=[{k: v for k, v in f.items() if k in ("path", "status", "saved", "saved_as", "note", "error")}
                  for f in files_out],
         deleted=deleted)
    result: dict = {"exit_code": reply.get("exit_code"), "timed_out": bool(reply.get("timed_out")),
                    "duration_ms": reply.get("duration_ms"), "stdout": stdout, "stderr": stderr}
    if reply.get("signal"):
        result["killed_by_signal"] = reply["signal"]
    if reply.get("timed_out"):
        result["note"] = f"killed after the {timeout_s}s time limit"
    if reply.get("stdout_truncated") or reply.get("stderr_truncated"):
        result["output_truncated"] = True
    if files_out:
        result["files"] = files_out
    if deleted:
        result["deleted_in_sandbox_not_applied"] = deleted
    if skipped:
        result["not_copied_into_sandbox"] = skipped[:20]
    if reply.get("scan_truncated"):
        result["warning"] = "the run left so many files that not all were checked for changes"
    return result


def _register_tools() -> None:
    import tools  # local: same reasoning as every other subsystem module

    tools.register(tools.Tool(
        "run_sandboxed_command",
        {"type": "function", "function": {
            "name": "run_sandboxed_command",
            "description": (
                "Run python3 in an isolated sandbox -- for computing, parsing, converting or checking "
                "something that needs real code rather than guessing. The sandbox has NO network, no "
                "secrets and no packages beyond the Python standard library, and it runs against a COPY "
                "of a folder in the working folder (cwd), not the real files: your command's working "
                "directory is that copy. Anything it prints comes back as stdout/stderr, with the exit "
                "code. Files it creates or changes are saved back into the working folder under the "
                "usual rules -- a file you created yourself is updated in place; a file someone else "
                "placed is NEVER overwritten, the new version is saved beside it as name.v2.ext -- and "
                "a file the code deletes is reported but never deleted for real. Only python3 is "
                "allowed (no shell, so no pipes or redirects): use e.g. command='python3 script.py' "
                "after writing script.py with write_file, or command='python3 -c \"print(2+2)\"'. Hard "
                "time limit, default 30 s. Prefer a narrow cwd: the whole subfolder is copied in."),
            "parameters": {"type": "object", "properties": {
                "command": {"type": "string",
                            "description": "python3 followed by its arguments, e.g. python3 analyze.py data.csv"},
                "cwd": {"type": "string",
                        "description": "folder in the working folder to run in (default: the top level). "
                                       "Only this folder and below is copied in."},
                "timeout_seconds": {"type": "integer", "default": DEFAULT_TIMEOUT_S,
                                    "description": f"1-{MAX_TIMEOUT_S}; the run is killed at the limit"}},
                "required": ["command"]}}},
        run_sandboxed_command, min_role="member", data_scope="self", risk_tier="C",
        enabled=is_enabled))


_register_tools()
