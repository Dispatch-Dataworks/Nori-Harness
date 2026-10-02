# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Per-agent sub-agent tool access (2026-10-02, operator's own ask: "read
only vs read write... another checkbox for search/fetch access") --
sub_agents.file_write / web_access and jobs.allowed_tool_names. Real
store and real tools.dispatch (so write_file's own provenance rule is
exercised for real, not assumed); only the model call (jobs._call) is
scripted and the one network-touching web function is patched.
"""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_SCRATCH = tempfile.mkdtemp(prefix="nori_test_subagent_access_")
os.environ["NORI_DATA_DIR"] = _SCRATCH
os.environ["NORI_PROMPTS_DIR"] = tempfile.mkdtemp(prefix="nori_test_subagent_access_prompts_")
os.environ["NORI_NO_LOGFILE"] = "1"

import accounts  # noqa: E402
import jobs  # noqa: E402
import store  # noqa: E402
import sub_agents  # noqa: E402
import tools  # noqa: E402
import webtools  # noqa: E402
import workfiles  # noqa: E402

store.init()
_user = accounts.bootstrap_admin("Tester", "testpass123")
_SESSION = {"user_id": _user["id"], "workspace_id": _user["workspace_id"], "role": "admin"}


def _agent(**kw) -> dict:
    ok, sid = sub_agents.create(_user["id"], f"agent-{len(sub_agents.list_all())}", None,
                                tool_call_limit=10, **kw)
    assert ok, sid
    return sub_agents.get(sid)


def _tc(name: str, args: dict, call_id: str = "1") -> dict:
    return {"id": call_id, "function": {"name": name, "arguments": json.dumps(args)}}


def _run(agent: dict, scripted_rounds: list[dict]) -> tuple[dict, list]:
    """Run one job through the real tool loop with a scripted model, and
    return (job row, every tool-role message the loop fed back)."""
    job_id = store.write(lambda c: c.execute(
        "INSERT INTO jobs(user_id, sub_agent_id, task, status, created_ts, timeout_s, raw_file_access) "
        "VALUES (?,?,?,'queued',?,?,0)",
        (_user["id"], agent["id"], "t", 0, 30)).lastrowid)
    seen = []

    def _fake_call(a, messages, tool_schema, timeout_s):
        seen.append({"schema": [s["function"]["name"] for s in (tool_schema or [])],
                    "messages": list(messages)})
        return scripted_rounds[len(seen) - 1]

    with patch.object(jobs, "_call", side_effect=_fake_call):
        jobs._run_job_with_tools(job_id, agent, "t", 30, _SESSION)
    row = dict(store.read(lambda c: c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()))
    tool_results = [json.loads(m["content"]) for m in seen[-1]["messages"] if m.get("role") == "tool"]
    return row, tool_results, seen


class AllowedToolNames(unittest.TestCase):
    def test_default_is_read_only(self):
        a = _agent()
        self.assertEqual(jobs.allowed_tool_names(a), ("list_files", "read_file", "search_files"))

    def test_file_write_adds_only_write_file_and_create_folder(self):
        a = _agent(file_write=True)
        names = jobs.allowed_tool_names(a)
        self.assertIn("write_file", names)
        self.assertIn("create_folder", names)
        # Deliberately not offered: move_file has no provenance protection,
        # delete_file isn't needed for "write".
        self.assertNotIn("move_file", names)
        self.assertNotIn("delete_file", names)
        self.assertNotIn("web_search", names)

    def test_web_access_adds_search_and_fetch_only(self):
        a = _agent(web_access=True)
        names = jobs.allowed_tool_names(a)
        self.assertIn("web_search", names)
        self.assertIn("web_fetch", names)
        self.assertNotIn("write_file", names)

    def test_set_access_round_trips_and_defaults_off(self):
        a = _agent()
        self.assertEqual((a["file_write"], a["web_access"]), (0, 0))
        sub_agents.set_access(a["id"], True, False)
        a2 = sub_agents.get(a["id"])
        self.assertEqual((a2["file_write"], a2["web_access"]), (1, 0))
        sub_agents.set_access(a["id"], False, True)
        a3 = sub_agents.get(a["id"])
        self.assertEqual((a3["file_write"], a3["web_access"]), (0, 1))

    def test_list_all_exposes_the_flags(self):
        _agent(file_write=True, web_access=True)
        row = sub_agents.list_all()[-1]
        self.assertEqual((row["file_write"], row["web_access"]), (1, 1))


class JobLoopEnforcesPerAgentAccess(unittest.TestCase):
    def test_write_file_refused_for_a_read_only_agent_and_nothing_written(self):
        a = _agent()
        rounds = [{"content": "", "tool_calls": [_tc("write_file", {"path": "ro.txt", "content": "x"})],
                  "usage": {}},
                 {"content": "done", "tool_calls": [], "usage": {}}]
        row, results, seen = _run(a, rounds)
        self.assertNotIn("write_file", seen[0]["schema"])
        self.assertIn("not available to sub-agents", results[0]["error"])
        self.assertFalse((workfiles.WORKFILES_DIR / str(_user["id"]) / "ro.txt").exists())

    def test_write_file_works_for_a_write_enabled_agent(self):
        a = _agent(file_write=True)
        rounds = [{"content": "", "tool_calls": [_tc("write_file", {"path": "rw.txt", "content": "hello"})],
                  "usage": {}},
                 {"content": "done", "tool_calls": [], "usage": {}}]
        row, results, seen = _run(a, rounds)
        self.assertIn("write_file", seen[0]["schema"])
        self.assertTrue(results[0].get("ok"), results)
        self.assertEqual(row["status"], "done")
        self.assertEqual((workfiles.WORKFILES_DIR / str(_user["id"]) / "rw.txt").read_bytes(), b"hello")

    def test_write_to_a_users_file_is_saved_as_v2_and_the_original_is_untouched(self):
        workfiles.upload_file(_SESSION, "mine.txt", b"original")
        a = _agent(file_write=True)
        rounds = [{"content": "", "tool_calls": [_tc("write_file", {"path": "mine.txt", "content": "revised"})],
                  "usage": {}},
                 {"content": "done", "tool_calls": [], "usage": {}}]
        _, results, _ = _run(a, rounds)
        root = workfiles.WORKFILES_DIR / str(_user["id"])
        self.assertTrue(results[0].get("ok"), results)
        self.assertEqual(results[0]["path"], "mine.v2.txt")
        self.assertEqual(results[0]["versioned_from"], "mine.txt")
        self.assertIn("original is untouched", results[0]["note"])
        self.assertEqual((root / "mine.txt").read_bytes(), b"original")
        self.assertEqual((root / "mine.v2.txt").read_bytes(), b"revised")

    def test_repeat_writes_to_the_same_original_go_v3_v4_never_overwriting_a_version(self):
        workfiles.upload_file(_SESSION, "rep.txt", b"original")
        a = _agent(file_write=True)
        rounds = [{"content": "", "tool_calls": [
                      _tc("write_file", {"path": "rep.txt", "content": "one"}, "1"),
                      _tc("write_file", {"path": "rep.txt", "content": "two"}, "2"),
                      _tc("write_file", {"path": "rep.txt", "content": "three"}, "3")], "usage": {}},
                  {"content": "done", "tool_calls": [], "usage": {}}]
        _, results, _ = _run(a, rounds)
        self.assertEqual([r["path"] for r in results], ["rep.v2.txt", "rep.v3.txt", "rep.v4.txt"])
        root = workfiles.WORKFILES_DIR / str(_user["id"])
        self.assertEqual([(root / n).read_bytes() for n in ("rep.txt", "rep.v2.txt", "rep.v3.txt", "rep.v4.txt")],
                         [b"original", b"one", b"two", b"three"])

    def test_extensionless_and_multi_dot_names_version_sensibly(self):
        workfiles.upload_file(_SESSION, "README", b"x")
        workfiles.upload_file(_SESSION, "a.tar.gz", b"x")
        a = _agent(file_write=True)
        rounds = [{"content": "", "tool_calls": [
                      _tc("write_file", {"path": "README", "content": "n"}, "1"),
                      _tc("write_file", {"path": "a.tar.gz", "content": "n"}, "2")], "usage": {}},
                  {"content": "done", "tool_calls": [], "usage": {}}]
        _, results, _ = _run(a, rounds)
        self.assertEqual([r["path"] for r in results], ["README.v2", "a.tar.v2.gz"])

    def test_a_nonexistent_path_is_just_a_normal_write_not_versioned(self):
        a = _agent(file_write=True)
        rounds = [{"content": "", "tool_calls": [_tc("write_file", {"path": "fresh.txt", "content": "n"})],
                  "usage": {}},
                 {"content": "done", "tool_calls": [], "usage": {}}]
        _, results, _ = _run(a, rounds)
        self.assertEqual(results[0]["path"], "fresh.txt")
        self.assertNotIn("versioned_from", results[0])

    def test_nori_own_live_turn_write_still_refuses_rather_than_versioning(self):
        # The versioning is a sub-agent-job behavior only -- her own
        # session carries no _versioned_writes flag, so nothing changed.
        workfiles.upload_file(_SESSION, "live.txt", b"original")
        result = workfiles.write_file(_SESSION, "live.txt", "clobber")
        self.assertIn("error", result)
        root = workfiles.WORKFILES_DIR / str(_user["id"])
        self.assertEqual((root / "live.txt").read_bytes(), b"original")
        self.assertFalse((root / "live.v2.txt").exists())

    def test_the_model_cannot_turn_versioning_on_through_tool_args(self):
        # Tool arguments never reach the session -- a hallucinated or
        # injected "_versioned_writes" arg must not change anything for
        # a caller that didn't already have it.
        workfiles.upload_file(_SESSION, "arg.txt", b"original")
        with self.assertRaises(TypeError):
            workfiles.write_file(_SESSION, "arg.txt", "x", _versioned_writes=True)

    def test_write_enabled_agent_can_write_a_versioned_copy_next_to_a_users_file(self):
        workfiles.upload_file(_SESSION, "notes.txt", b"original")
        a = _agent(file_write=True)
        rounds = [{"content": "", "tool_calls": [
                      _tc("write_file", {"path": "notes.v2.txt", "content": "revised"}, "1"),
                      _tc("write_file", {"path": "notes.v2.txt", "content": "revised again"}, "2")],
                   "usage": {}},
                  {"content": "done", "tool_calls": [], "usage": {}}]
        _, results, _ = _run(a, rounds)
        self.assertTrue(results[0].get("ok"), results)
        # Its own earlier file is fair game to overwrite -- provenance is
        # "created by Nori/a sub-agent", not "never touched before".
        self.assertTrue(results[1].get("ok"), results)
        root = workfiles.WORKFILES_DIR / str(_user["id"])
        self.assertEqual((root / "notes.txt").read_bytes(), b"original")
        self.assertEqual((root / "notes.v2.txt").read_bytes(), b"revised again")

    def test_write_enabled_agent_cannot_move_or_delete(self):
        a = _agent(file_write=True)
        rounds = [{"content": "", "tool_calls": [
                      _tc("move_file", {"from_path": "a", "to_path": "b"}, "1"),
                      _tc("delete_file", {"path": "a"}, "2")], "usage": {}},
                 {"content": "done", "tool_calls": [], "usage": {}}]
        _, results, _ = _run(a, rounds)
        self.assertTrue(all("not available to sub-agents" in r["error"] for r in results), results)

    def test_web_search_refused_without_web_access_and_allowed_with_it(self):
        off = _agent()
        rounds = [{"content": "", "tool_calls": [_tc("web_search", {"query": "q"})], "usage": {}},
                 {"content": "done", "tool_calls": [], "usage": {}}]
        _, results, seen = _run(off, rounds)
        self.assertNotIn("web_search", seen[0]["schema"])
        self.assertIn("not available to sub-agents", results[0]["error"])

        on = _agent(web_access=True)
        with patch.object(webtools, "search", return_value={"ok": True, "results": []}) as spy:
            _, results2, seen2 = _run(on, rounds)
        self.assertIn("web_search", seen2[0]["schema"])
        spy.assert_called_once()
        self.assertTrue(results2[0].get("ok"), results2)


class WriteFolderScope(unittest.TestCase):
    """Per-agent write_folder (2026-10-02, operator's own ask: "let an
    agent write only inside a designated manuscript/output folder").
    Enforced in workfiles._check_write_scope, so these go through the real
    job loop AND real tools.dispatch -- not just the helper."""

    def _root(self):
        return workfiles.WORKFILES_DIR / str(_user["id"])

    def _write_round(self, calls):
        return [{"content": "", "tool_calls": [
                    _tc("write_file", {"path": p, "content": c}, str(i)) for i, (p, c) in enumerate(calls)],
                 "usage": {}},
                {"content": "done", "tool_calls": [], "usage": {}}]

    def test_write_inside_the_folder_works_and_creates_it(self):
        a = _agent(file_write=True, write_folder="manuscript/revised")
        _, results, _ = _run(a, self._write_round([("manuscript/revised/ch10.md", "new")]))
        self.assertTrue(results[0].get("ok"), results)
        self.assertEqual((self._root() / "manuscript/revised/ch10.md").read_bytes(), b"new")

    def test_write_outside_the_folder_is_refused_and_names_the_folder(self):
        a = _agent(file_write=True, write_folder="manuscript/revised")
        _, results, _ = _run(a, self._write_round([("elsewhere.md", "x")]))
        self.assertIn("manuscript/revised/", results[0]["error"])
        self.assertEqual(results[0]["write_folder"], "manuscript/revised")
        self.assertFalse((self._root() / "elsewhere.md").exists())

    def test_users_original_outside_the_folder_is_refused_not_versioned(self):
        workfiles.upload_file(_SESSION, "manuscript/ch11.md", b"original")
        a = _agent(file_write=True, write_folder="manuscript/revised")
        _, results, _ = _run(a, self._write_round([("manuscript/ch11.md", "edit")]))
        self.assertIn("error", results[0])
        self.assertFalse((self._root() / "manuscript/ch11.v2.md").exists())
        self.assertEqual((self._root() / "manuscript/ch11.md").read_bytes(), b"original")

    def test_versioning_still_applies_to_a_users_file_inside_the_folder(self):
        workfiles.upload_file(_SESSION, "scoped/placed.md", b"original")
        a = _agent(file_write=True, write_folder="scoped")
        _, results, _ = _run(a, self._write_round([("scoped/placed.md", "edit")]))
        self.assertEqual(results[0]["path"], "scoped/placed.v2.md")
        self.assertEqual((self._root() / "scoped/placed.md").read_bytes(), b"original")

    def test_dot_dot_traversal_out_of_the_folder_is_refused(self):
        a = _agent(file_write=True, write_folder="manuscript/revised")
        _, results, _ = _run(a, self._write_round([("manuscript/revised/../../escape.md", "x")]))
        self.assertIn("error", results[0])
        self.assertFalse((self._root() / "escape.md").exists())

    def test_create_folder_is_scoped_too(self):
        a = _agent(file_write=True, write_folder="manuscript/revised")
        rounds = [{"content": "", "tool_calls": [
                      _tc("create_folder", {"path": "manuscript/revised/drafts"}, "1"),
                      _tc("create_folder", {"path": "other"}, "2")], "usage": {}},
                  {"content": "done", "tool_calls": [], "usage": {}}]
        _, results, _ = _run(a, rounds)
        self.assertTrue(results[0].get("ok"), results)
        self.assertIn("error", results[1])
        self.assertFalse((self._root() / "other").exists())

    def test_folder_casing_does_not_cause_a_false_refusal(self):
        a = _agent(file_write=True, write_folder="Casey/Out")
        _, results, _ = _run(a, self._write_round([("casey/out/x.md", "x")]))
        self.assertTrue(results[0].get("ok"), results)

    def test_unscoped_agent_still_writes_anywhere_and_nori_is_unaffected(self):
        a = _agent(file_write=True)
        _, results, _ = _run(a, self._write_round([("anywhere.md", "x")]))
        self.assertTrue(results[0].get("ok"), results)
        self.assertTrue(workfiles.write_file(_SESSION, "nori-own.md", "x").get("ok"))

    def test_the_scope_cannot_be_set_through_tool_args(self):
        with self.assertRaises(TypeError):
            workfiles.write_file(_SESSION, "x.md", "x", _write_root="somewhere")


class WriteFolderConfig(unittest.TestCase):
    def test_validate_normalizes_and_rejects_unsafe_paths(self):
        v = workfiles.validate_write_folder
        self.assertEqual(v(""), ("", ""))
        self.assertEqual(v("  "), ("", ""))
        self.assertEqual(v(r"a\b/"), ("a/b", ""))
        self.assertEqual(v("./a//b"), ("a/b", ""))
        for bad in ("/abs", "C:/x", "a/../b", "a/b?c", "a/CON"):
            self.assertIsNone(v(bad)[0], bad)
            self.assertTrue(v(bad)[1], bad)

    def test_create_rejects_a_bad_folder_and_stores_a_good_one_normalized(self):
        ok, msg = sub_agents.create(_user["id"], "bad-folder-agent", None, tool_call_limit=5,
                                    file_write=True, write_folder="../up")
        self.assertFalse(ok)
        ok, sid = sub_agents.create(_user["id"], "good-folder-agent", None, tool_call_limit=5,
                                    file_write=True, write_folder=r"m\out/")
        self.assertTrue(ok, sid)
        self.assertEqual(sub_agents.get(sid)["write_folder"], "m/out")

    def test_set_access_validates_and_saves_nothing_on_error(self):
        a = _agent(file_write=True, write_folder="keep")
        err = sub_agents.set_access(a["id"], True, True, "/abs")
        self.assertTrue(err)
        after = sub_agents.get(a["id"])
        self.assertEqual((after["write_folder"], after["web_access"]), ("keep", 0))
        self.assertIsNone(sub_agents.set_access(a["id"], True, False, "new/spot"))
        self.assertEqual(sub_agents.get(a["id"])["write_folder"], "new/spot")
        self.assertEqual(sub_agents.list_all()[-1]["write_folder"], sub_agents.get(sub_agents.list_all()[-1]["id"])["write_folder"])

    def test_default_is_unscoped(self):
        self.assertEqual(_agent()["write_folder"], "")


class ExplainTextMentionsPerAgentAccess(unittest.TestCase):
    def test_explain_text_is_no_longer_an_unconditional_no_writes_claim(self):
        self.assertNotIn("no writes, no web", jobs.SUBAGENT_LIMITS_EXPLAIN)
        self.assertIn("file_write", jobs.SUBAGENT_LIMITS_EXPLAIN)
        self.assertIn("web_access", jobs.SUBAGENT_LIMITS_EXPLAIN)


if __name__ == "__main__":
    unittest.main()
