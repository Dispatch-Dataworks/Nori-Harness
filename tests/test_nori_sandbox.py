# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Nori's side of the code sandbox (sandbox.py, 2026-10-04): gating,
validation, what gets copied in, how results are saved back, the audit
log, and the sub-agent wiring (code_exec / allowed_tool_names / preamble /
the real job loop). Real SQLite + real filesystem + real tools.dispatch +
real workfiles write rules; the ONLY fake is the socket to the runner
(sandbox._rpc), replaced by a scripted stand-in that returns what the real
runner would. The runner itself -- the isolation -- is tested separately, for
real, inside its container (tests/test_nori_sandbox_runner.py)."""
import base64
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

os.environ["NORI_DATA_DIR"] = tempfile.mkdtemp(prefix="nori_test_sandbox_")
os.environ["NORI_PROMPTS_DIR"] = tempfile.mkdtemp(prefix="nori_test_sandbox_prompts_")
os.environ["NORI_NO_LOGFILE"] = "1"

import accounts  # noqa: E402
import config  # noqa: E402
import jobs  # noqa: E402
import sandbox  # noqa: E402
import store  # noqa: E402
import sub_agents  # noqa: E402
import tools  # noqa: E402
import workfiles  # noqa: E402

store.init()
_user = accounts.bootstrap_admin("Tester", "testpass123")
WS = _user["workspace_id"]


def _session(**extra) -> dict:
    return {"user_id": _user["id"], "workspace_id": WS, "role": "admin", **extra}


def _b64(data) -> str:
    return base64.b64encode(data.encode() if isinstance(data, str) else data).decode()


def _read(path: str) -> bytes:
    return workfiles._resolve(_user["id"], path).read_bytes()


def _exists(path: str) -> bool:
    return workfiles._resolve(_user["id"], path).exists()


class FakeRunner:
    """Stands in for the socket. `behavior(files: {rel: bytes}) -> dict` returns
    any of stdout/stderr/exit_code/timed_out/created/modified/deleted/extra."""

    def __init__(self, behavior=None):
        self.behavior = behavior or (lambda files: {"stdout": "ok\n"})
        self.requests: list[dict] = []

    def __call__(self, payload: dict, timeout: float) -> dict:
        if payload.get("op") == "ping":
            return {"ok": True, "allowed": ["python3"], "max_timeout_s": 300, "mem_mb": 512}
        self.requests.append({**payload, "_timeout": timeout})
        files = {k: base64.b64decode(v) for k, v in payload["files"].items()}
        b = self.behavior(files)
        changed = []
        for rel, data in (b.get("created") or {}).items():
            changed.append({"path": rel, "status": "created", "size": len(data), "content_b64": _b64(data)})
        for rel, data in (b.get("modified") or {}).items():
            changed.append({"path": rel, "status": "modified", "size": len(data), "content_b64": _b64(data)})
        changed += b.get("extra_changed") or []
        return {"ok": True, "exit_code": None if b.get("timed_out") else b.get("exit_code", 0),
                "signal": None, "timed_out": bool(b.get("timed_out")), "duration_ms": 12,
                "stdout": b.get("stdout", ""), "stderr": b.get("stderr", ""),
                "stdout_truncated": False, "stderr_truncated": False,
                "changed": changed, "deleted": b.get("deleted", []), "scan_truncated": False}


def _enable(on=True):
    config.set("workspace", WS, "sandbox_enabled", on)


def _run(fake, session=None, **args):
    with patch.object(sandbox, "_rpc", fake):
        return sandbox.run_sandboxed_command(session or _session(), **args)


def _last_audit() -> dict:
    return sandbox.recent_runs(1)[0]


class Gating(unittest.TestCase):
    def test_off_by_default_and_refused_when_off(self):
        self.assertFalse(config.get("workspace", WS, "sandbox_enabled"))
        fake = FakeRunner()
        r = _run(fake, command="python3 -c 1")
        self.assertIn("turned off", r["error"])
        self.assertEqual(fake.requests, [], "nothing may be sent to the runner while it's off")
        self.assertEqual(_last_audit()["outcome"], "refused")

    def test_tool_is_hidden_and_undispatchable_until_enabled(self):
        names = lambda: [s["function"]["name"] for s in tools.active_schemas(_session())]  # noqa: E731
        _enable(False)
        self.assertNotIn("run_sandboxed_command", names())
        self.assertIn("no such tool", tools.dispatch("run_sandboxed_command", {"command": "python3 -c 1"},
                                                     _session())["error"])
        _enable(True)
        try:
            self.assertIn("run_sandboxed_command", names())
            with patch.object(sandbox, "_rpc", FakeRunner(lambda f: {"stdout": "4\n"})):
                r = tools.dispatch("run_sandboxed_command", {"command": 'python3 -c "print(2+2)"'}, _session())
            self.assertEqual(r["stdout"], "4\n")
            self.assertEqual(r["exit_code"], 0)
        finally:
            _enable(False)

    def test_no_user_or_workspace_override_reaches_the_tool(self):
        # tools.dispatch strips these before the implementation sees them.
        _enable(True)
        try:
            with patch.object(sandbox, "_rpc", FakeRunner()):
                r = tools.dispatch("run_sandboxed_command",
                                   {"command": "python3 -c 1", "user_id": 999, "workspace_id": 999}, _session())
            self.assertNotIn("error", r)
        finally:
            _enable(False)


class Validation(unittest.TestCase):
    def setUp(self):
        _enable(True)
        self.addCleanup(_enable, False)

    def test_command_rules(self):
        fake = FakeRunner()
        for cmd in ("", "   ", "sh -c ls", "bash", "/bin/sh x", "curl http://example.com", "rm -rf /",
                    "python3x", "python3 'unterminated"):
            r = _run(fake, command=cmd)
            self.assertIn("error", r, cmd)
        self.assertEqual(fake.requests, [])
        self.assertNotIn("error", _run(fake, command="python3 -c 'print(1)'"))
        self.assertEqual(fake.requests[-1]["command"], ["python3", "-c", "print(1)"], "argv, never a shell string")

    def test_non_string_command(self):
        self.assertIn("error", _run(FakeRunner(), command=None))
        self.assertIn("error", _run(FakeRunner(), command=["python3", "x"]))

    def test_timeout_default_clamp_and_bad_values(self):
        fake = FakeRunner()
        _run(fake, command="python3 -c 1")
        self.assertEqual(fake.requests[-1]["timeout_s"], sandbox.DEFAULT_TIMEOUT_S)
        _run(fake, command="python3 -c 1", timeout_seconds=100000)
        self.assertEqual(fake.requests[-1]["timeout_s"], sandbox.MAX_TIMEOUT_S)
        self.assertIn("error", _run(fake, command="python3 -c 1", timeout_seconds=0))
        self.assertIn("error", _run(fake, command="python3 -c 1", timeout_seconds="soon"))

    def test_cwd_rules(self):
        workfiles.create_folder(_session(), "cwdtest")
        workfiles.write_file(_session(), "cwdtest/a.txt", "a")
        fake = FakeRunner()
        self.assertIn("error", _run(fake, command="python3 -c 1", cwd="../outside"))
        self.assertIn("error", _run(fake, command="python3 -c 1", cwd="nope"))
        self.assertIn("error", _run(fake, command="python3 -c 1", cwd="cwdtest/a.txt"))
        self.assertIn("error", _run(fake, command="python3 -c 1", cwd="/etc"))
        self.assertEqual(fake.requests, [])


class Mounting(unittest.TestCase):
    def setUp(self):
        _enable(True)
        self.addCleanup(_enable, False)
        s = _session()
        workfiles.create_folder(s, "m")
        workfiles.create_folder(s, "m/inner")
        workfiles.create_folder(s, "elsewhere")
        workfiles.write_file(s, "m/top.txt", "top")
        workfiles.write_file(s, "m/inner/deep.txt", "deep")
        workfiles.write_file(s, "elsewhere/secret.txt", "not for the sandbox")
        workfiles.write_file(s, "rootfile.txt", "root")

    def test_only_the_cwd_subtree_is_copied_with_relative_names(self):
        fake = FakeRunner()
        _run(fake, command="python3 -c 1", cwd="m")
        sent = {k: base64.b64decode(v) for k, v in fake.requests[-1]["files"].items()}
        self.assertEqual(sent, {"top.txt": b"top", "inner/deep.txt": b"deep"})
        self.assertEqual(fake.requests[-1]["cwd"], "", "the run starts at the root of the copy")

    def test_oversize_file_is_skipped_and_reported(self):
        fake = FakeRunner()
        with patch.object(sandbox, "MOUNT_MAX_FILE_BYTES", 3):
            r = _run(fake, command="python3 -c 1", cwd="m")
        sent = fake.requests[-1]["files"]
        self.assertNotIn("inner/deep.txt", sent)
        self.assertTrue(any("deep.txt" in s for s in r["not_copied_into_sandbox"]))

    def test_too_many_files_or_bytes_refuses_with_advice(self):
        fake = FakeRunner()
        with patch.object(sandbox, "MOUNT_MAX_FILES", 1):
            r = _run(fake, command="python3 -c 1", cwd="m")
        self.assertIn("narrower cwd", r["error"])
        with patch.object(sandbox, "MOUNT_MAX_TOTAL_BYTES", 3):
            self.assertIn("narrower cwd", _run(fake, command="python3 -c 1", cwd="m")["error"])
        self.assertEqual(fake.requests, [])


class SavingResults(unittest.TestCase):
    def setUp(self):
        _enable(True)
        self.addCleanup(_enable, False)

    def test_new_file_is_saved_as_hers_and_reported(self):
        r = _run(FakeRunner(lambda f: {"created": {"out/result.txt": b"42"}}), command="python3 x.py")
        self.assertEqual(_read("out/result.txt"), b"42")
        self.assertEqual(r["files"], [{"path": "out/result.txt", "status": "created", "size": 2,
                                       "saved": True, "saved_as": "out/result.txt"}])
        self.assertEqual(workfiles._meta_row(_user["id"], "out/result.txt")["created_by"], "nori")

    def test_binary_output_survives(self):
        blob = bytes(range(256)) * 4
        _run(FakeRunner(lambda f: {"created": {"bin/data.bin": blob}}), command="python3 x.py")
        self.assertEqual(_read("bin/data.bin"), blob)

    def test_file_she_created_is_updated_in_place(self):
        workfiles.write_file(_session(), "mine/notes.txt", "v1")
        r = _run(FakeRunner(lambda f: {"modified": {"notes.txt": b"v1 + more"}}), command="python3 x.py", cwd="mine")
        self.assertEqual(_read("mine/notes.txt"), b"v1 + more")
        self.assertEqual(r["files"][0]["saved_as"], "mine/notes.txt")
        self.assertFalse(_exists("mine/notes.v2.txt"))

    def test_users_file_is_never_overwritten_it_is_versioned(self):
        workfiles.upload_file(_session(), "theirs/data.csv", b"a,b\n1,2\n")
        r = _run(FakeRunner(lambda f: {"modified": {"data.csv": b"a,b\n1,2\n3,4\n"}}), command="python3 x.py", cwd="theirs")
        self.assertEqual(_read("theirs/data.csv"), b"a,b\n1,2\n", "the original must be untouched")
        self.assertEqual(_read("theirs/data.v2.csv"), b"a,b\n1,2\n3,4\n")
        item = r["files"][0]
        self.assertEqual((item["saved"], item["saved_as"]), (True, "theirs/data.v2.csv"))
        self.assertIn("already exists", item["note"])
        # a second run never overwrites the first version either
        _run(FakeRunner(lambda f: {"modified": {"data.csv": b"third"}}), command="python3 x.py", cwd="theirs")
        self.assertEqual(_read("theirs/data.v2.csv"), b"a,b\n1,2\n3,4\n")
        self.assertEqual(_read("theirs/data.v3.csv"), b"third")

    def test_deletion_is_reported_and_never_applied(self):
        workfiles.upload_file(_session(), "del/keep.txt", b"precious")
        workfiles.write_file(_session(), "del/hers.txt", "also kept")
        r = _run(FakeRunner(lambda f: {"deleted": ["keep.txt", "hers.txt"]}), command="python3 x.py", cwd="del")
        self.assertEqual(r["deleted_in_sandbox_not_applied"], ["del/keep.txt", "del/hers.txt"])
        self.assertEqual(_read("del/keep.txt"), b"precious")
        self.assertEqual(_read("del/hers.txt"), b"also kept")

    def test_omitted_oversize_output_is_reported_not_saved(self):
        ent = {"path": "big.bin", "status": "created", "size": 99_000_000, "omitted": "larger than 5 MB"}
        r = _run(FakeRunner(lambda f: {"extra_changed": [ent]}), command="python3 x.py", cwd="")
        self.assertFalse(r["files"][0]["saved"])
        self.assertIn("larger than 5 MB", r["files"][0]["note"])
        self.assertFalse(_exists("big.bin"))

    def test_a_hostile_returned_path_cannot_escape_the_working_folder(self):
        for evil in ("../../escape.txt", "/abs/escape.txt", "a/../../escape.txt"):
            r = _run(FakeRunner(lambda f, evil=evil: {"created": {evil: b"x"}}), command="python3 x.py")
            self.assertFalse(r["files"][0]["saved"], evil)
            self.assertIn("error", r["files"][0], evil)
        self.assertFalse(os.path.exists(os.path.join(os.environ["NORI_DATA_DIR"], "escape.txt")))

    def test_non_zero_exit_timeout_and_streams(self):
        r = _run(FakeRunner(lambda f: {"exit_code": 2, "stdout": "partial", "stderr": "Traceback..."}),
                 command="python3 x.py")
        self.assertEqual((r["exit_code"], r["stdout"], r["stderr"]), (2, "partial", "Traceback..."))
        self.assertEqual(_last_audit()["outcome"], "nonzero")
        r = _run(FakeRunner(lambda f: {"timed_out": True}), command="python3 x.py", timeout_seconds=5)
        self.assertTrue(r["timed_out"])
        self.assertIn("5s", r["note"])
        self.assertEqual(_last_audit()["outcome"], "timeout")

    def test_long_output_is_clipped_for_the_model_with_head_and_tail(self):
        big = "H" * 20_000 + "T" * 20_000
        r = _run(FakeRunner(lambda f: {"stdout": big, "stderr": big}), command="python3 x.py")
        self.assertLessEqual(len(r["stdout"]), sandbox.MODEL_STDOUT_CHARS + 80)
        self.assertTrue(r["stdout"].startswith("HHH") and r["stdout"].endswith("TTT"))
        self.assertIn("omitted", r["stdout"])
        self.assertLessEqual(len(r["stderr"]), sandbox.MODEL_STDERR_CHARS + 80)


class SubAgentScopes(unittest.TestCase):
    def setUp(self):
        _enable(True)
        self.addCleanup(_enable, False)

    def test_write_folder_agent_can_only_run_inside_it(self):
        workfiles.create_folder(_session(), "scoped")
        workfiles.create_folder(_session(), "scoped/sub")
        workfiles.create_folder(_session(), "other")
        s = _session(_write_root="scoped")
        fake = FakeRunner()
        for cwd in ("", "other"):
            self.assertIn("can only run code inside scoped/", _run(fake, s, command="python3 -c 1", cwd=cwd)["error"], cwd)
        self.assertIn("error", _run(fake, s, command="python3 -c 1", cwd="scoped/../other"))  # traversal: refused outright
        self.assertEqual(fake.requests, [])
        r = _run(FakeRunner(lambda f: {"created": {"o.txt": b"1"}}), s, command="python3 x.py", cwd="scoped/sub")
        self.assertTrue(r["files"][0]["saved"])
        self.assertTrue(_exists("scoped/sub/o.txt"))

    def test_without_file_write_nothing_is_saved_but_output_comes_back(self):
        s = _session(_sandbox_no_apply=True)
        r = _run(FakeRunner(lambda f: {"stdout": "computed 7", "created": {"nowrite/x.txt": b"1"}}), s,
                 command="python3 x.py")
        self.assertEqual(r["stdout"], "computed 7")
        self.assertFalse(r["files"][0]["saved"])
        self.assertIn("isn't allowed to write", r["files"][0]["note"])
        self.assertFalse(_exists("nowrite/x.txt"))


class AuditLog(unittest.TestCase):
    def setUp(self):
        _enable(True)
        self.addCleanup(_enable, False)

    def test_every_outcome_is_logged_with_who_and_what(self):
        before = len(sandbox.recent_runs(500))
        _run(FakeRunner(lambda f: {"stdout": "x", "created": {"audit/new.txt": b"1"}, "deleted": ["gone.txt"]}),
             command="python3 -c 'print(1)'")
        _run(FakeRunner(), command="sh -c id")                                   # refused
        _run(FakeRunner(), _session(_job_id=77, _job_agent="coder"), command="python3 -c 1")  # from a job
        self.assertEqual(len(sandbox.recent_runs(500)) - before, 3)
        job_row, refused_row, ok_row = sandbox.recent_runs(3)
        self.assertEqual((job_row["source"], job_row["job_id"], job_row["agent"]), ("job", 77, "coder"))
        self.assertEqual(refused_row["outcome"], "refused")
        self.assertIn("allowed", refused_row["error"])
        self.assertEqual(ok_row["outcome"], "ok")
        self.assertEqual(ok_row["source"], "chat")
        self.assertEqual(ok_row["changes"][0]["saved_as"], "audit/new.txt")
        self.assertEqual(ok_row["deleted"], ["gone.txt"])
        self.assertEqual(ok_row["stdout_chars"], 1)

    def test_unreachable_runner_is_a_clear_error_and_logged(self):
        def down(payload, timeout):
            raise sandbox.SandboxError("the sandbox container isn't running")
        with patch.object(sandbox, "_rpc", down):
            r = sandbox.run_sandboxed_command(_session(), command="python3 -c 1")
        self.assertIn("isn't running", r["error"])
        self.assertEqual(_last_audit()["outcome"], "unavailable")

    def test_runner_refusal_is_passed_back(self):
        with patch.object(sandbox, "_rpc", lambda p, t: {"ok": False, "error": "request too large"}):
            r = sandbox.run_sandboxed_command(_session(), command="python3 -c 1")
        self.assertEqual(r["error"], "request too large")
        self.assertEqual(_last_audit()["outcome"], "refused")

    def test_an_audit_failure_does_not_fail_the_run(self):
        with patch.object(sandbox.store, "write", side_effect=RuntimeError("db locked")):
            r = _run(FakeRunner(lambda f: {"stdout": "still ran"}), command="python3 -c 1")
        self.assertEqual(r["stdout"], "still ran")


class Status(unittest.TestCase):
    def test_status_shapes(self):
        with patch.object(sandbox, "SOCKET_PATH", ""):
            self.assertEqual(sandbox.status()["configured"], False)
        with patch.object(sandbox, "SOCKET_PATH", "/x/y.sock"), patch.object(sandbox, "_rpc", FakeRunner()):
            st = sandbox.status()
            self.assertTrue(st["reachable"])
            self.assertEqual(st["allowed"], ["python3"])

        def down(p, t):
            raise sandbox.SandboxError("not running")
        with patch.object(sandbox, "SOCKET_PATH", "/x/y.sock"), patch.object(sandbox, "_rpc", down):
            st = sandbox.status()
            self.assertFalse(st["reachable"])
            self.assertIn("not running", st["detail"])

    def test_rpc_without_a_socket_path_says_so(self):
        with patch.object(sandbox, "SOCKET_PATH", ""):
            with self.assertRaises(sandbox.SandboxError):
                sandbox._rpc({"op": "ping"}, 1)


class SubAgentWiring(unittest.TestCase):
    def _agent(self, **kw) -> dict:
        ok, sid = sub_agents.create(_user["id"], f"coder-{len(sub_agents.list_all())}", None,
                                    tool_call_limit=10, **kw)
        assert ok, sid
        return sub_agents.get(sid)

    def test_column_default_create_and_set_access(self):
        a = self._agent()
        self.assertEqual(a["code_exec"], 0)
        b = self._agent(code_exec=True)
        self.assertEqual(b["code_exec"], 1)
        sub_agents.set_access(a["id"], False, False, "", False, code_exec=True)
        self.assertEqual(sub_agents.get(a["id"])["code_exec"], 1)
        sub_agents.set_access(a["id"], True, False, "", False)          # code_exec omitted = unchanged
        self.assertEqual(sub_agents.get(a["id"])["code_exec"], 1)
        sub_agents.set_access(a["id"], True, False, "", False, code_exec=False)
        self.assertEqual(sub_agents.get(a["id"])["code_exec"], 0)
        self.assertIn("code_exec", sub_agents.list_all()[0])

    def test_allowlist_and_workspace_switch(self):
        a, b = self._agent(), self._agent(code_exec=True)
        self.assertNotIn("run_sandboxed_command", jobs.allowed_tool_names(a))
        self.assertIn("run_sandboxed_command", jobs.allowed_tool_names(b))
        _enable(False)
        self.assertNotIn("run_sandboxed_command", jobs._job_tool_names(b, _session()))
        schema = [s["function"]["name"] for s in jobs._subagent_tools_schema(b, False, _session())]
        self.assertNotIn("run_sandboxed_command", schema)
        _enable(True)
        try:
            self.assertIn("run_sandboxed_command", jobs._job_tool_names(b, _session()))
            schema = [s["function"]["name"] for s in jobs._subagent_tools_schema(b, False, _session())]
            self.assertIn("run_sandboxed_command", schema)
            self.assertNotIn("run_sandboxed_command", [s["function"]["name"]
                                                       for s in jobs._subagent_tools_schema(a, False, _session())])
        finally:
            _enable(False)

    def test_preamble_states_the_real_access(self):
        a, b = self._agent(), self._agent(code_exec=True)
        c = self._agent(code_exec=True, file_write=True)
        self.assertNotIn("Code execution", jobs.access_preamble(a, False, []))
        self.assertIn("nothing it creates is saved", jobs.access_preamble(b, False, []))
        self.assertIn("saved back", jobs.access_preamble(c, False, []))
        self.assertIn("run_sandboxed_command", jobs.SUBAGENT_LIMITS_EXPLAIN)

    def _job(self, agent, rounds, expected=None):
        job_id = store.write(lambda c: c.execute(
            "INSERT INTO jobs(user_id, sub_agent_id, task, status, created_ts, timeout_s, raw_file_access, "
            "expected_outputs) VALUES (?,?,?,'queued',?,?,0,?)",
            (_user["id"], agent["id"], "t", 0, 60, json.dumps(expected) if expected else None)).lastrowid)
        seen = []

        def fake_call(a, messages, tool_schema, timeout_s):
            seen.append({"schema": [s["function"]["name"] for s in (tool_schema or [])], "messages": list(messages)})
            return rounds[len(seen) - 1]
        with patch.object(jobs, "_call", side_effect=fake_call):
            jobs._run_job_with_tools(job_id, agent, "t", 60, _session(), expected_outputs=expected or [])
        row = dict(store.read(lambda c: c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()))
        results = [json.loads(m["content"]) for m in seen[-1]["messages"] if m.get("role") == "tool"]
        return row, results, seen

    @staticmethod
    def _tc(args: dict) -> dict:
        return {"id": "1", "function": {"name": "run_sandboxed_command", "arguments": json.dumps(args)}}

    def test_job_loop_runs_code_saves_output_and_counts_it_as_written(self):
        _enable(True)
        self.addCleanup(_enable, False)
        agent = self._agent(code_exec=True, file_write=True)
        rounds = [{"content": None, "usage": {}, "tool_calls": [self._tc({"command": "python3 gen.py", "cwd": "jobout"})]},
                  {"content": "done", "usage": {}, "tool_calls": []}]
        workfiles.create_folder(_session(), "jobout")
        with patch.object(sandbox, "_rpc", FakeRunner(lambda f: {"stdout": "made it", "created": {"report.txt": b"R"}})):
            row, results, seen = self._job(agent, rounds, expected=["jobout/report.txt"])
        self.assertIn("run_sandboxed_command", seen[0]["schema"])
        self.assertEqual(results[0]["stdout"], "made it")
        self.assertEqual(_read("jobout/report.txt"), b"R")
        self.assertEqual(row["status"], "done", row["error"])
        audit = sandbox.recent_runs(1)[0]
        self.assertEqual((audit["source"], audit["job_id"], audit["agent"]), ("job", row["id"], agent["label"]))

    def test_job_without_file_write_computes_but_saves_nothing(self):
        _enable(True)
        self.addCleanup(_enable, False)
        agent = self._agent(code_exec=True)
        rounds = [{"content": None, "usage": {}, "tool_calls": [self._tc({"command": "python3 gen.py", "cwd": "jobro"})]},
                  {"content": "done", "usage": {}, "tool_calls": []}]
        workfiles.create_folder(_session(), "jobro")
        with patch.object(sandbox, "_rpc", FakeRunner(lambda f: {"stdout": "7", "created": {"nope.txt": b"R"}})):
            row, results, _ = self._job(agent, rounds)
        self.assertEqual(results[0]["stdout"], "7")
        self.assertFalse(results[0]["files"][0]["saved"])
        self.assertFalse(_exists("jobro/nope.txt"))

    def test_job_is_refused_the_tool_when_agent_has_no_code_exec(self):
        _enable(True)
        self.addCleanup(_enable, False)
        agent = self._agent(file_write=True)
        rounds = [{"content": None, "usage": {}, "tool_calls": [self._tc({"command": "python3 -c 1"})]},
                  {"content": "done", "usage": {}, "tool_calls": []}]
        fake = FakeRunner()
        with patch.object(sandbox, "_rpc", fake):
            _, results, seen = self._job(agent, rounds)
        self.assertNotIn("run_sandboxed_command", seen[0]["schema"])
        self.assertIn("not available to sub-agents", results[0]["error"])
        self.assertEqual(fake.requests, [])

    def test_model_supplied_session_flags_cannot_reach_the_tool(self):
        # _write_root / _sandbox_no_apply live in the SESSION; tool args are filtered to the schema
        # parameters by the implementation's own signature.
        _enable(True)
        self.addCleanup(_enable, False)
        r = tools.dispatch("run_sandboxed_command",
                           {"command": "python3 -c 1", "_sandbox_no_apply": False, "_write_root": ""}, _session())
        self.assertIn("bad arguments", r["error"])


if __name__ == "__main__":
    unittest.main()
