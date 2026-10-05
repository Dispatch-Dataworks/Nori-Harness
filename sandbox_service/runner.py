# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""The code-sandbox runner -- the ONLY thing running inside the `sandbox`
container (see docker-compose.yml and docs/sandbox.md).

What makes this safe is the container around it, not this script: no
network at all (network_mode: none), no volumes except the one tiny socket
volume, no env_file/secrets, read-only root filesystem, a size-capped tmpfs
for scratch space, dropped capabilities, memory/pid/cpu caps. Nothing in the
container is worth stealing and nothing outside it is reachable. This file
adds the per-run discipline on top:

  * one run at a time (connections are served serially);
  * the command is an argv whose first word must be on a short allowlist
    (python3 by default) -- never a shell string;
  * each run gets a fresh directory; the files Nori sent are written into
    it, owned by an unprivileged uid; everything the run leaves behind is
    deleted afterwards, and every process still running as that uid is
    killed;
  * the child runs as that unprivileged uid with no supplementary groups,
    in its own session, with CPU / memory / process / file-size / open-file
    limits, a scrubbed environment, and a hard wall-clock kill of the whole
    process group;
  * the reply carries stdout, stderr, the exit status, and a manifest of
    which files the run created, changed or deleted (contents included for
    created/changed files, within caps). The runner never touches Nori's
    files -- Nori decides what, if anything, to save, under its own rules.

Protocol: a 4-byte big-endian length, then that many bytes of JSON; the reply
is framed the same way. Over a unix socket in a volume only Nori's container
mounts. Stdlib only. POSIX-only modules are imported inside the functions
that need them, so the pure helpers can be unit-tested on any platform."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import signal
import socket
import stat
import struct
import sys
import time

SOCK_DIR = os.environ.get("SANDBOX_SOCK_DIR", "/run/sandbox")
SOCK_PATH = os.path.join(SOCK_DIR, "runner.sock")
WORK_DIR = os.environ.get("SANDBOX_WORK_DIR", "/work")
RUN_UID = int(os.environ.get("SANDBOX_RUN_UID", "53417"))
RUN_GID = int(os.environ.get("SANDBOX_RUN_GID", "53417"))
ALLOWED_COMMANDS = tuple(c.strip() for c in os.environ.get("SANDBOX_ALLOWED", "python3").split(",") if c.strip())

MAX_REQUEST_BYTES = 80 * 1024 * 1024
MAX_INPUT_FILES = 1000
MAX_INPUT_BYTES = 64 * 1024 * 1024
MAX_TIMEOUT_S = int(os.environ.get("SANDBOX_MAX_TIMEOUT_S", "300"))
MEM_LIMIT_MB = int(os.environ.get("SANDBOX_MEM_MB", "512"))
MAX_PROCS = int(os.environ.get("SANDBOX_MAX_PROCS", "64"))
MAX_FILE_WRITE_BYTES = 50 * 1024 * 1024   # RLIMIT_FSIZE: one file can't grow past this
MAX_OPEN_FILES = 256
OUTPUT_CAP = 64 * 1024                      # per stream returned to Nori
OUTPUT_FILE_CAP = 8 * 1024 * 1024           # per stream the child may even write
RETURN_FILE_CAP = 5 * 1024 * 1024           # one returned file
RETURN_TOTAL_CAP = 20 * 1024 * 1024         # all returned files together
RETURN_FILE_COUNT = 200
SCAN_FILE_CAP = 5000                        # files inspected after a run


class RunError(Exception):
    """A request the runner refuses -- the message goes straight back."""


# ── pure helpers (unit-tested on any platform) ───────────────────────────
def safe_rel(path: str) -> str:
    """A relative path that stays inside its root: no absolute paths, no
    '..', no empty parts, no NUL. Returns the normalized path ('' = root)."""
    if not isinstance(path, str) or "\x00" in path:
        raise RunError("bad path")
    p = path.replace("\\", "/")
    if p.startswith("/") or (len(p) > 1 and p[1] == ":"):
        raise RunError(f"path must be relative: {path!r}")
    parts = [x for x in p.split("/") if x not in ("", ".")]
    if ".." in parts:
        raise RunError(f"path escapes the working directory: {path!r}")
    return "/".join(parts)


def split_command(command) -> list[str]:
    """argv from the request. A list is taken as-is; a string is split with
    shlex (still no shell). First word must be allowlisted."""
    import shlex
    if isinstance(command, str):
        try:
            argv = shlex.split(command)
        except ValueError as exc:
            raise RunError(f"couldn't parse the command: {exc}")
    elif isinstance(command, list) and all(isinstance(a, str) for a in command):
        argv = list(command)
    else:
        raise RunError("command must be a string or a list of strings")
    if not argv:
        raise RunError("empty command")
    if len(" ".join(argv)) > 20_000:
        raise RunError("command is too long")
    if argv[0] not in ALLOWED_COMMANDS:
        raise RunError(f"{argv[0]!r} isn't an allowed command here -- allowed: {', '.join(ALLOWED_COMMANDS)}")
    return argv


def cap_text(data: bytes, cap: int = OUTPUT_CAP) -> tuple[str, bool]:
    """(text, truncated): decoded leniently, keeping the head and the tail
    when too long -- a traceback is at the END of stderr."""
    if len(data) <= cap:
        return data.decode("utf-8", "replace"), False
    head, tail = cap * 2 // 3, cap // 3
    mid = f"\n...[{len(data) - head - tail} bytes omitted]...\n".encode()
    return (data[:head] + mid + data[-tail:]).decode("utf-8", "replace"), True


def diff_manifest(before: dict[str, str], after: dict[str, tuple[str, int, str]]) -> tuple[list[dict], list[str]]:
    """before: rel -> sha256 of what Nori sent. after: rel -> (sha256, size,
    abs path) of what's there now. Returns (created/modified entries, deleted
    rels), each sorted by path."""
    changed = []
    for rel in sorted(after):
        sha, size, _ = after[rel]
        if rel not in before:
            changed.append({"path": rel, "status": "created", "size": size, "sha256": sha})
        elif before[rel] != sha:
            changed.append({"path": rel, "status": "modified", "size": size, "sha256": sha})
    deleted = sorted(rel for rel in before if rel not in after)
    return changed, deleted


# ── one run ───────────────────────────────────────────────────────────────
def _write_inputs(ws: str, files: dict) -> dict[str, str]:
    if not isinstance(files, dict):
        raise RunError("files must be an object")
    if len(files) > MAX_INPUT_FILES:
        raise RunError(f"too many files ({len(files)} > {MAX_INPUT_FILES})")
    sha_by_rel: dict[str, str] = {}
    total = 0
    for raw_rel, b64 in files.items():
        rel = safe_rel(raw_rel)
        if not rel:
            raise RunError("a file needs a name")
        try:
            data = base64.b64decode(b64 or "", validate=True)
        except Exception:
            raise RunError(f"{raw_rel!r}: bad content encoding")
        total += len(data)
        if total > MAX_INPUT_BYTES:
            raise RunError("input files are too large")
        dest = os.path.join(ws, *rel.split("/"))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as f:
            f.write(data)
        sha_by_rel[rel] = hashlib.sha256(data).hexdigest()
    # Hand the whole tree to the unprivileged user.
    for dirpath, dirnames, filenames in os.walk(ws):
        os.chown(dirpath, RUN_UID, RUN_GID)
        for name in filenames:
            os.chown(os.path.join(dirpath, name), RUN_UID, RUN_GID)
    return sha_by_rel


def _scan(ws: str) -> tuple[dict[str, tuple[str, int, str]], bool]:
    """Regular files under ws, never following a symlink (the run could have
    made one pointing anywhere in this container)."""
    out: dict[str, tuple[str, int, str]] = {}
    truncated = False
    for dirpath, dirnames, filenames in os.walk(ws, followlinks=False):
        for name in filenames:
            full = os.path.join(dirpath, name)
            try:
                st = os.lstat(full)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            if len(out) >= SCAN_FILE_CAP:
                truncated = True
                return out, truncated
            h = hashlib.sha256()
            try:
                with open(full, "rb") as f:
                    for chunk in iter(lambda: f.read(1 << 20), b""):
                        h.update(chunk)
            except OSError:
                continue
            rel = os.path.relpath(full, ws).replace(os.sep, "/")
            out[rel] = (h.hexdigest(), st.st_size, full)
    return out, truncated


def _limits(timeout_s: int):
    def apply():  # runs in the child between fork and exec
        import resource
        os.umask(0o022)
        mem = MEM_LIMIT_MB * 1024 * 1024
        for res, val in ((resource.RLIMIT_CPU, timeout_s + 2), (resource.RLIMIT_AS, mem),
                         (resource.RLIMIT_NPROC, MAX_PROCS), (resource.RLIMIT_FSIZE, MAX_FILE_WRITE_BYTES),
                         (resource.RLIMIT_NOFILE, MAX_OPEN_FILES), (resource.RLIMIT_CORE, 0)):
            resource.setrlimit(res, (val, val))
    return apply


def _reap() -> None:
    try:
        while os.waitpid(-1, os.WNOHANG)[0]:
            pass
    except ChildProcessError:
        pass


def _pids_of_uid(uid: int, me: int) -> list[int]:
    found = []
    try:
        pids = [int(p) for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return found
    for pid in pids:
        if pid == me:
            continue
        try:
            with open(f"/proc/{pid}/status") as f:
                for line in f:
                    if line.startswith("Uid:"):
                        if int(line.split()[1]) == uid:
                            found.append(pid)
                        break
        except (OSError, ValueError):
            continue
    return found


def kill_everything_of_uid(uid: int, *, except_pid: int | None = None) -> None:
    """Whatever a run left running -- a daemonized child that escaped the
    process-group kill -- dies with the run. Scans /proc for the run uid, kills
    every match, then REAPS: this runner is PID 1, so orphaned children are
    reparented to it, and a killed-but-unreaped zombie still counts against
    the uid's process limit -- left alone they pile up until nothing can fork."""
    me = except_pid or os.getpid()
    for _ in range(40):                       # up to ~2s for kills to land
        pids = _pids_of_uid(uid, me)
        if not pids:
            return
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        time.sleep(0.05)
        _reap()


def run(req: dict) -> dict:
    import subprocess
    t0 = time.time()
    argv = split_command(req.get("command"))
    timeout_s = int(req.get("timeout_s") or 30)
    if timeout_s < 1 or timeout_s > MAX_TIMEOUT_S:
        raise RunError(f"timeout must be between 1 and {MAX_TIMEOUT_S} seconds")
    cwd_rel = safe_rel(req.get("cwd") or "")
    exe = shutil.which(argv[0], path="/usr/local/bin:/usr/bin:/bin")
    if not exe:
        raise RunError(f"{argv[0]!r} isn't installed in the sandbox")
    argv[0] = exe

    os.makedirs(WORK_DIR, exist_ok=True)
    run_dir = os.path.join(WORK_DIR, f"run-{os.getpid()}-{int(t0 * 1000)}")
    ws, io_dir = os.path.join(run_dir, "ws"), os.path.join(run_dir, "io")
    os.makedirs(ws)
    os.makedirs(io_dir, mode=0o700)
    os.chmod(run_dir, 0o711)          # the run user can traverse to ws, nothing else
    os.chown(ws, RUN_UID, RUN_GID)
    timed_out = False
    rc = None
    out_b = err_b = b""
    out_size = err_size = 0
    try:
        before = _write_inputs(ws, req.get("files") or {})
        cwd = os.path.join(ws, *cwd_rel.split("/")) if cwd_rel else ws
        if not os.path.isdir(cwd):
            os.makedirs(cwd)
            os.chown(cwd, RUN_UID, RUN_GID)
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": ws, "TMPDIR": ws, "LANG": "C.UTF-8",
               "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1", "PYTHONIOENCODING": "utf-8"}
        out_path, err_path = os.path.join(io_dir, "stdout"), os.path.join(io_dir, "stderr")
        with open(out_path, "wb") as fo, open(err_path, "wb") as fe:
            proc = subprocess.Popen(
                argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=fo, stderr=fe,
                user=RUN_UID, group=RUN_GID, extra_groups=[], start_new_session=True,
                preexec_fn=_limits(timeout_s), close_fds=True)
            try:
                rc = proc.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                timed_out = True
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    pass
                proc.wait()
        # Whatever else is still running as that uid dies BEFORE the files are read.
        kill_everything_of_uid(RUN_UID)
        out_size, err_size = os.path.getsize(out_path), os.path.getsize(err_path)
        with open(out_path, "rb") as f:
            out_b = f.read(OUTPUT_FILE_CAP)
        with open(err_path, "rb") as f:
            err_b = f.read(OUTPUT_FILE_CAP)
        after, scan_truncated = _scan(ws)
        changed, deleted = diff_manifest(before, after)
        returned_bytes = 0
        for i, ent in enumerate(changed):
            full = after[ent["path"]][2]
            if i >= RETURN_FILE_COUNT:
                ent["omitted"] = f"more than {RETURN_FILE_COUNT} changed files"
            elif ent["size"] > RETURN_FILE_CAP:
                ent["omitted"] = f"larger than {RETURN_FILE_CAP // (1024 * 1024)} MB"
            elif returned_bytes + ent["size"] > RETURN_TOTAL_CAP:
                ent["omitted"] = "total size of changed files is over the limit"
            else:
                with open(full, "rb") as f:
                    ent["content_b64"] = base64.b64encode(f.read()).decode()
                returned_bytes += ent["size"]
    finally:
        kill_everything_of_uid(RUN_UID)
        shutil.rmtree(run_dir, ignore_errors=True)
    sig = None
    if rc is not None and rc < 0:
        try:
            sig = signal.Signals(-rc).name
        except ValueError:
            sig = str(-rc)
    stdout, out_trunc = cap_text(out_b)
    stderr, err_trunc = cap_text(err_b)
    return {"ok": True, "exit_code": None if timed_out else rc, "signal": sig, "timed_out": timed_out,
            "duration_ms": round((time.time() - t0) * 1000),
            "stdout": stdout, "stderr": stderr,
            "stdout_truncated": out_trunc or out_size > OUTPUT_FILE_CAP,
            "stderr_truncated": err_trunc or err_size > OUTPUT_FILE_CAP,
            "changed": changed, "deleted": deleted, "scan_truncated": scan_truncated}


# ── the socket loop ───────────────────────────────────────────────────────
def _recv_exact(conn: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = conn.recv(min(1 << 20, n - len(buf)))
        if not chunk:
            raise RunError("connection closed mid-request")
        buf += chunk
    return bytes(buf)


def _send(conn: socket.socket, obj: dict) -> None:
    body = json.dumps(obj, separators=(",", ":")).encode()
    conn.sendall(struct.pack(">I", len(body)) + body)


def handle(conn: socket.socket) -> None:
    conn.settimeout(60)
    try:
        (length,) = struct.unpack(">I", _recv_exact(conn, 4))
        if length > MAX_REQUEST_BYTES:
            raise RunError("request too large")
        req = json.loads(_recv_exact(conn, length))
        if not isinstance(req, dict):
            raise RunError("bad request")
        op = req.get("op")
        if op == "ping":
            _send(conn, {"ok": True, "allowed": list(ALLOWED_COMMANDS), "max_timeout_s": MAX_TIMEOUT_S,
                         "mem_mb": MEM_LIMIT_MB})
        elif op == "run":
            conn.settimeout(None)   # a long run must not trip the read timeout while it executes
            _send(conn, run(req))
        else:
            raise RunError("unknown op")
    except RunError as exc:
        try:
            _send(conn, {"ok": False, "error": str(exc)})
        except OSError:
            pass
    except Exception as exc:  # noqa: BLE001 -- the loop must survive anything a request does
        print(f"runner: {type(exc).__name__}: {exc}", flush=True)
        try:
            _send(conn, {"ok": False, "error": f"sandbox runner failed: {type(exc).__name__}"})
        except OSError:
            pass
    finally:
        try:
            conn.close()
        except OSError:
            pass


def main() -> None:
    os.makedirs(SOCK_DIR, exist_ok=True)
    os.chmod(SOCK_DIR, 0o755)
    try:
        os.unlink(SOCK_PATH)
    except FileNotFoundError:
        pass
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCK_PATH)
    os.chmod(SOCK_PATH, 0o600)       # root only: the run uid can't talk to the runner
    srv.listen(8)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    print(f"sandbox runner listening on {SOCK_PATH}; allowed: {', '.join(ALLOWED_COMMANDS)}", flush=True)
    srv.settimeout(1.0)
    while True:
        # Idle ticks reap too: as PID 1 this process inherits every orphan, and
        # an unreaped zombie still counts against the run uid's process limit.
        try:
            conn, _ = srv.accept()
        except socket.timeout:
            _reap()
            continue
        conn.settimeout(None)
        handle(conn)
        _reap()


if __name__ == "__main__":
    main()
