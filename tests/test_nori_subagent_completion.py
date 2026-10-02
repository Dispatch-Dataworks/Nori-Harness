# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Sub-agent exact-text access and completion checks (2026-10-02), after a
manuscript review ran on 200-character gists and a raw read stopped dead at
the first 4,000 characters, and the job was reported done either way.

  1. Raw reads are PAGED: offset / total_chars / next_offset, contiguous,
     exact at EOF, an error past it, never "content" for a page whose
     screening failed.
  2. A gist read says plainly that it's a gist -- with wording that depends
     on whether exact access was granted.
  3. Completion is checked by the harness from what it observed: an
     expected output not written, or a file only partly read, makes the job
     'incomplete' instead of 'done'.
  4. Exact text is a per-agent default that a dispatch can override either
     way.

Real store and filesystem; only the screening model call
(ingest.summarize_untrusted) and the sub-agent's own model call
(jobs._call) are scripted.
"""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_SCRATCH = tempfile.mkdtemp(prefix="nori_test_subagent_completion_")
os.environ["NORI_DATA_DIR"] = _SCRATCH
os.environ["NORI_PROMPTS_DIR"] = tempfile.mkdtemp(prefix="nori_test_subagent_completion_prompts_")
os.environ["NORI_NO_LOGFILE"] = "1"

import accounts  # noqa: E402
import ingest  # noqa: E402
import jobs  # noqa: E402
import server  # noqa: E402
import store  # noqa: E402
import sub_agents  # noqa: E402
import workfiles  # noqa: E402

store.init()
_user = accounts.bootstrap_admin("Tester", "testpass123")
_UID = _user["id"]
_SESSION = {"user_id": _UID, "workspace_id": _user["workspace_id"], "role": "admin", "csrf": "test-csrf"}

_OK_SCREEN = {"category": "informational", "priority": "normal", "suggested_action": "", "suspicious": False}


def _screen(content, *, kind="email", preserve_content=False, cost_sink=None):
    if preserve_content:
        return {**_OK_SCREEN, "content": content[:4000], "truncated": len(content) > 4000}
    return {**_OK_SCREEN, "summary": "a short gist"}


def _root():
    return workfiles.WORKFILES_DIR / str(_UID)


def _text(n_words=400):
    """Words and newlines, long enough for many small pages."""
    return "\n".join(" ".join(f"word{w}" for w in range(i, i + 12)) for i in range(0, n_words, 12)) + "\n"


class _Base(unittest.TestCase):
    def setUp(self):
        import shutil
        shutil.rmtree(_root(), ignore_errors=True)
        store.write(lambda c: c.execute("DELETE FROM work_files WHERE user_id=?", (_UID,)))
        self._saved_page_chars = workfiles.RAW_PAGE_CHARS
        workfiles.RAW_PAGE_CHARS = 100  # small pages: many of them, fast
        self._p = patch.object(ingest, "summarize_untrusted", side_effect=_screen)
        self.screen = self._p.start()

    def tearDown(self):
        self._p.stop()
        workfiles.RAW_PAGE_CHARS = self._saved_page_chars

    def put(self, name, text):
        assert workfiles.upload_file(_SESSION, name, text.encode("utf-8")).get("ok")


class PagedRawReads(_Base):
    def read(self, path, offset=0):
        return workfiles.read_file(_SESSION, path, preserve_content=True, offset=offset)

    def test_first_page(self):
        text = _text()
        self.put("c.md", text)
        r = self.read("c.md")
        self.assertEqual((r["offset"], r["total_chars"], r["path"]), (0, len(text), "c.md"))
        self.assertTrue(r["truncated"])
        self.assertEqual(r["content"], text[:r["next_offset"]])
        self.assertLessEqual(len(r["content"]), workfiles.RAW_PAGE_CHARS)

    def test_middle_of_file_is_the_exact_slice(self):
        text = _text()
        self.put("c.md", text)
        r = self.read("c.md", 1000)
        self.assertEqual(r["content"], text[1000:r["next_offset"]])
        self.assertEqual(r["offset"], 1000)

    def test_walking_next_offset_reassembles_the_file_exactly(self):
        text = _text(2000) + "no-spaces-at-all-" * 40 + "\n" + "tail"
        self.put("c.md", text)
        got, offset, pages = "", 0, 0
        while offset is not None:
            r = self.read("c.md", offset)
            self.assertEqual(r["offset"], len(got))
            got += r["content"]
            offset = r["next_offset"]
            pages += 1
            self.assertLess(pages, 1000)
        self.assertEqual(got, text)
        self.assertGreater(pages, 5)
        self.assertFalse(r["truncated"])

    def test_the_last_page_has_no_next_offset(self):
        text = _text()
        self.put("c.md", text)
        r = self.read("c.md", len(text) - 10)
        self.assertEqual((r["next_offset"], r["truncated"]), (None, False))
        self.assertEqual(r["content"], text[-10:])

    def test_exact_eof_is_a_valid_empty_final_page(self):
        text = _text()
        self.put("c.md", text)
        r = self.read("c.md", len(text))
        self.assertNotIn("error", r)
        self.assertEqual((r["content"], r["next_offset"], r["truncated"], r["total_chars"]),
                         ("", None, False, len(text)))

    def test_past_eof_is_an_error(self):
        text = _text()
        self.put("c.md", text)
        r = self.read("c.md", len(text) + 1)
        self.assertIn("past the end", r["error"])
        self.assertNotIn("content", r)

    def test_bad_offsets_are_errors(self):
        self.put("c.md", _text())
        self.assertIn("negative", self.read("c.md", -1)["error"])
        self.assertIn("whole number", self.read("c.md", "abc")["error"])

    def test_a_file_smaller_than_a_page_is_one_page(self):
        self.put("s.md", "short file")
        r = self.read("s.md")
        self.assertEqual((r["content"], r["next_offset"], r["truncated"], r["total_chars"]),
                         ("short file", None, False, 10))

    def test_an_empty_file(self):
        self.put("e.md", "")
        r = self.read("e.md")
        self.assertEqual((r["content"], r["next_offset"], r["total_chars"]), ("", None, 0))

    def test_pages_break_at_whitespace_not_mid_word(self):
        self.put("c.md", _text())
        r = self.read("c.md")
        self.assertTrue(r["content"][-1] in " \n", repr(r["content"][-10:]))

    def test_only_the_page_goes_to_the_screening_model(self):
        self.put("big.md", _text(5000))
        self.read("big.md", 500)
        sent = self.screen.call_args.args[0]
        self.assertLessEqual(len(sent), workfiles.RAW_PAGE_CHARS)

    def test_content_is_the_files_own_text_not_the_screeners(self):
        self.put("c.md", _text())
        self.screen.side_effect = lambda content, **kw: {**_OK_SCREEN, "content": "MODEL PARAPHRASE",
                                                         "truncated": False}
        self.assertNotEqual(self.read("c.md")["content"], "MODEL PARAPHRASE")

    def test_a_page_whose_screening_failed_is_an_error_not_content(self):
        self.put("c.md", _text())
        self.screen.side_effect = lambda content, **kw: {**_OK_SCREEN, "suspicious": True,
                                                         "screening_failed": True,
                                                         "content": "(could not be read safely)", "truncated": False}
        r = self.read("c.md", 100)
        self.assertIn("retry", r["error"])
        self.assertEqual((r["offset"], r["total_chars"]) , (100, len(_text())))
        self.assertNotIn("content", r)

    def test_real_fallback_carries_the_failure_marker(self):
        self.assertTrue(ingest._fallback(True)["screening_failed"])
        self.assertTrue(ingest._fallback(False)["screening_failed"])

    def test_summary_mode_is_unchanged_apart_from_total_chars_and_path(self):
        text = _text()
        self.put("c.md", text)
        r = workfiles.read_file(_SESSION, "c.md")
        self.assertEqual((r["summary"], r["total_chars"], r["path"]), ("a short gist", len(text), "c.md"))
        self.assertNotIn("content", r)
        # offset means nothing without preserve_content
        self.assertEqual(workfiles.read_file(_SESSION, "c.md", offset=999)["summary"], "a short gist")


def _agent(**kw) -> dict:
    kw.setdefault("tool_call_limit", 50)
    ok, sid = sub_agents.create(_UID, f"agent-{len(sub_agents.list_all())}", None, **kw)
    assert ok, sid
    return sub_agents.get(sid)


def _tc(name, args, call_id="1"):
    return {"id": call_id, "function": {"name": name, "arguments": json.dumps(args)}}


def _round(*calls):
    return {"content": "", "tool_calls": [_tc(n, a, str(i)) for i, (n, a) in enumerate(calls)], "usage": {}}


_FINAL = {"content": "final report", "tool_calls": [], "usage": {}}


def _run(agent, rounds, *, raw=False, expected=None):
    job_id = store.write(lambda c: c.execute(
        "INSERT INTO jobs(user_id, sub_agent_id, task, status, created_ts, timeout_s, raw_file_access) "
        "VALUES (?,?,?,'queued',?,?,?)", (_UID, agent["id"], "t", 0, 30, 1 if raw else 0)).lastrowid)
    seen = []

    def fake(a, messages, tool_schema, timeout_s):
        seen.append({"schema": [s["function"]["name"] for s in (tool_schema or [])], "messages": list(messages)})
        return rounds[len(seen) - 1]

    with patch.object(jobs, "_call", side_effect=fake):
        jobs._run_job_with_tools(job_id, agent, "t", 30, _SESSION, raw, expected or [])
    row = dict(store.read(lambda c: c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()))
    tool_msgs = [json.loads(m["content"]) for m in seen[-1]["messages"] if m.get("role") == "tool"]
    return row, tool_msgs, seen


class SummaryWarning(_Base):
    def _gist_job(self, raw):
        self.put("c.md", _text())
        a = _agent(file_write=False)
        return _run(a, [_round(("read_file", {"path": "c.md"})), _FINAL], raw=raw)

    def test_without_exact_access_the_gist_says_so_and_says_to_stop(self):
        _, msgs, _ = self._gist_job(raw=False)
        self.assertEqual(msgs[0]["access"], "summary_only")
        self.assertIn("NOT the file's text", msgs[0]["warning"])
        self.assertIn("exact text is NOT available", msgs[0]["warning"])
        self.assertIn("stop and report", msgs[0]["warning"])
        self.assertEqual(msgs[0]["summary"], "a short gist")  # the gist itself still comes through

    def test_with_exact_access_the_warning_points_at_read_file_full(self):
        _, msgs, _ = self._gist_job(raw=True)
        self.assertIn("NOT the file's text", msgs[0]["warning"])
        self.assertIn("read_file_full", msgs[0]["warning"])
        self.assertIn("next_offset", msgs[0]["warning"])
        self.assertNotIn("is NOT available", msgs[0]["warning"])

    def test_a_failed_read_gets_no_warning(self):
        a = _agent()
        _, msgs, _ = _run(a, [_round(("read_file", {"path": "missing.md"})), _FINAL])
        self.assertIn("error", msgs[0])
        self.assertNotIn("warning", msgs[0])

    def test_the_registered_read_file_tool_itself_is_unchanged_for_nori(self):
        # The warning is added by the sub-agent loop, not the shared tool.
        self.put("c.md", _text())
        import tools
        r = tools.dispatch("read_file", {"path": "c.md"}, _SESSION)
        self.assertNotIn("warning", r)

    def test_the_raw_tool_schema_documents_paging(self):
        props = jobs._RAW_READ_SCHEMA["function"]["parameters"]["properties"]
        self.assertIn("offset", props)
        self.assertIn("next_offset", jobs._RAW_READ_SCHEMA["function"]["description"])


class CompletionChecks(_Base):
    def _read_all(self, path, text, *, page_calls=None):
        calls = []
        off = 0
        # same paging the workfiles code will do
        while off is not None:
            r = workfiles.read_file(_SESSION, path, preserve_content=True, offset=off)
            calls.append(("read_file_full", {"path": path, "offset": off}))
            off = r["next_offset"]
        return calls

    def test_missing_expected_output_makes_the_job_incomplete(self):
        a = _agent(file_write=True)
        row, _, _ = _run(a, [_FINAL], expected=["reports/review.md"])
        self.assertEqual(row["status"], "incomplete")
        self.assertIn("expected output reports/review.md was not written by this job", row["error"])
        self.assertEqual(row["result"], "final report")  # what it did produce is kept

    def test_written_expected_output_is_done(self):
        a = _agent(file_write=True)
        row, msgs, _ = _run(a, [_round(("write_file", {"path": "out/review.md", "content": "x"})), _FINAL],
                            expected=["out/review.md"])
        self.assertEqual((row["status"], row["error"]), ("done", None))
        self.assertTrue(msgs[0]["ok"])

    def test_expected_path_matching_ignores_case_and_slash_style(self):
        a = _agent(file_write=True)
        row, _, _ = _run(a, [_round(("write_file", {"path": "Out/Review.md", "content": "x"})), _FINAL],
                         expected=["out\\review.md"])
        self.assertEqual(row["status"], "done", row["error"])

    def test_an_output_that_only_pre_exists_does_not_count(self):
        self.put("old/report.md", "from an earlier job")
        a = _agent(file_write=True)
        row, _, _ = _run(a, [_FINAL], expected=["old/report.md"])
        self.assertEqual(row["status"], "incomplete")

    def test_a_versioned_write_satisfies_the_expected_path(self):
        self.put("report.md", "operator's own file")
        a = _agent(file_write=True)
        row, msgs, _ = _run(a, [_round(("write_file", {"path": "report.md", "content": "new"})), _FINAL],
                            expected=["report.md"])
        self.assertEqual(msgs[0]["path"], "report.v2.md")
        self.assertEqual((row["status"], row["error"]), ("done", None))

    def test_multiple_expected_outputs_all_must_be_written(self):
        a = _agent(file_write=True)
        row, _, _ = _run(a, [_round(("write_file", {"path": "a.md", "content": "x"})), _FINAL],
                         expected=["a.md", "b.md", "c.md"])
        self.assertEqual(row["status"], "incomplete")
        self.assertNotIn("a.md was not", row["error"])
        self.assertIn("b.md was not", row["error"])
        self.assertIn("c.md was not", row["error"])

    def test_unread_trailing_pages_make_the_job_incomplete(self):
        text = _text()
        self.put("c.md", text)
        a = _agent()
        row, _, _ = _run(a, [_round(("read_file_full", {"path": "c.md"})), _FINAL], raw=True)
        self.assertEqual(row["status"], "incomplete")
        self.assertRegex(row["error"], r"c\.md was only partly read \(\d+ of %s characters\)" % f"{len(text):,}")
        self.assertEqual(row["partial_reads"], 1)

    def test_reading_every_page_is_done(self):
        text = _text()
        self.put("c.md", text)
        a = _agent()
        calls = self._read_all("c.md", text)
        self.assertGreater(len(calls), 3)
        row, msgs, _ = _run(a, [_round(*calls), _FINAL], raw=True)
        self.assertEqual((row["status"], row["error"], row["partial_reads"]), ("done", None, 0))
        self.assertEqual("".join(m["content"] for m in msgs), text)

    def test_reading_only_a_later_page_is_still_partial(self):
        text = _text()
        self.put("c.md", text)
        a = _agent()
        row, _, _ = _run(a, [_round(("read_file_full", {"path": "c.md", "offset": 200})), _FINAL], raw=True)
        self.assertEqual(row["status"], "incomplete")

    def test_rereading_one_page_does_not_cover_the_rest(self):
        self.put("c.md", _text())
        a = _agent()
        row, _, _ = _run(a, [_round(("read_file_full", {"path": "c.md"}),
                                    ("read_file_full", {"path": "c.md"})), _FINAL], raw=True)
        self.assertEqual(row["status"], "incomplete")

    def test_pages_read_out_of_order_still_cover_the_file(self):
        text = _text(100)
        self.put("c.md", text)
        a = _agent()
        calls = self._read_all("c.md", text)
        row, _, _ = _run(a, [_round(*reversed(calls)), _FINAL], raw=True)
        self.assertEqual(row["status"], "done", row["error"])

    def test_a_page_that_errored_is_not_counted_as_read(self):
        self.put("c.md", _text())
        a = _agent()
        self.screen.side_effect = lambda content, **kw: {**_OK_SCREEN, "screening_failed": True, "suspicious": True}
        row, msgs, _ = _run(a, [_round(("read_file_full", {"path": "c.md"})), _FINAL], raw=True)
        self.assertIn("error", msgs[0])
        # nothing was read at all, so there's no partly-read file to flag
        self.assertEqual(row["partial_reads"], 0)

    def test_running_out_of_budget_with_reads_unfinished_says_so(self):
        self.put("c.md", _text())
        a = _agent(tool_call_limit=1)
        row, _, _ = _run(a, [_round(("read_file_full", {"path": "c.md"}),
                                    ("read_file_full", {"path": "c.md", "offset": 100})), _FINAL], raw=True)
        self.assertEqual(row["status"], "incomplete")
        self.assertIn("ran out of its tool-call/byte budget", row["error"])

    def test_gist_only_despite_exact_access_is_incomplete(self):
        self.put("c.md", _text())
        a = _agent()
        row, _, _ = _run(a, [_round(("read_file", {"path": "c.md"})), _FINAL], raw=True)
        self.assertEqual(row["status"], "incomplete")
        self.assertIn("read as a gist only despite exact access being granted: c.md", row["error"])

    def test_gist_reads_without_exact_access_are_done_but_counted_and_flagged(self):
        self.put("c.md", _text())
        a = _agent()
        row, _, _ = _run(a, [_round(("read_file", {"path": "c.md"})), _FINAL], raw=False)
        self.assertEqual((row["status"], row["summary_reads"]), ("done", 1))
        note = jobs.completion_note(row)
        self.assertIn("exact file text was NOT granted", note)
        self.assertIn("1 file read(s) returned only short gists", note)

    def test_a_plain_job_with_no_requirements_is_unchanged(self):
        a = _agent()
        row, _, _ = _run(a, [_FINAL])
        self.assertEqual((row["status"], row["error"], row["partial_reads"], row["summary_reads"]),
                         ("done", None, 0, 0))
        self.assertEqual(jobs.completion_note(row), "")

    def test_several_problems_are_all_reported(self):
        self.put("c.md", _text())
        a = _agent(file_write=True)
        row, _, _ = _run(a, [_round(("read_file_full", {"path": "c.md"})), _FINAL], raw=True,
                         expected=["out.md"])
        self.assertIn("only partly read", row["error"])
        self.assertIn("out.md was not written", row["error"])


class JobsReadBackAndWakeUp(_Base):
    def _incomplete_job(self):
        a = _agent(file_write=True)
        row, _, _ = _run(a, [_FINAL], expected=["never.md"])
        store.write(lambda c: c.execute("UPDATE jobs SET expected_outputs=?, user_id=? WHERE id=?",
                                        (json.dumps(["never.md"]), _UID, row["id"])))
        return row

    def test_check_job_reports_the_harness_check_and_marks_it_seen(self):
        row = self._incomplete_job()
        r = jobs._check_job_impl(_SESSION, row["id"])
        self.assertEqual(r["status"], "incomplete")
        self.assertIn("INCOMPLETE -- expected output never.md was not written", r["harness_check"])
        self.assertEqual(r["expected_outputs"], ["never.md"])
        self.assertEqual(r["result"], "final report")
        self.assertEqual(store.read(lambda c: c.execute(
            "SELECT seen FROM jobs WHERE id=?", (row["id"],)).fetchone())["seen"], 1)

    def test_check_job_for_a_clean_job_has_no_harness_check(self):
        a = _agent()
        row, _, _ = _run(a, [_FINAL])
        r = jobs._check_job_impl(_SESSION, row["id"])
        self.assertNotIn("harness_check", r)

    def test_check_job_reports_read_counts(self):
        self.put("c.md", _text())
        a = _agent()
        row, _, _ = _run(a, [_round(("read_file", {"path": "c.md"})), _FINAL])
        r = jobs._check_job_impl(_SESSION, row["id"])
        self.assertEqual(r["reads"], {"files_only_partly_read": 0, "gist_only_reads": 1,
                                      "exact_file_text_granted": False})

    def test_digest_counts_an_incomplete_job_as_unread(self):
        store.write(lambda c: c.execute("DELETE FROM jobs WHERE user_id=?", (_UID,)))
        self._incomplete_job()
        self.assertIn("completed and unread", jobs.digest_line(_UID))

    def test_list_jobs_can_filter_to_incomplete(self):
        row = self._incomplete_job()
        listed = jobs._list_jobs_impl(_SESSION, "incomplete")["jobs"]
        self.assertIn(row["id"], [j["id"] for j in listed])
        import tools
        enum = tools.schema_for("list_jobs")["function"]["parameters"]["properties"]["status"]["enum"]
        self.assertIn("incomplete", enum)

    def test_the_wake_up_prompt_carries_the_harness_check_outside_the_screened_result(self):
        row = self._incomplete_job()
        captured = {}

        def fake_chat_run(session, user_id, name, *, extra_message=None, **kw):
            captured["prompt"] = extra_message["content"]
            return {"text": ""}

        import chat
        import turns
        import timing
        with patch.object(turns, "run", side_effect=lambda uid, f, sweep: f()), \
             patch.object(chat, "run", side_effect=fake_chat_run), \
             patch.object(timing, "start", return_value=MagicMock()):
            jobs._trigger_turn_for_job(row["id"], _UID, "agent", "do the thing", "incomplete",
                                       "final report", row["error"])
        p = captured["prompt"]
        self.assertIn("INCOMPLETE", p)
        self.assertIn("HARNESS CHECK", p)
        self.assertIn("expected output never.md was not written", p)
        # and it sits OUTSIDE the screened body: screening was given only the result text
        screened_arg = self.screen.call_args.args[0]
        self.assertNotIn("HARNESS CHECK", screened_arg)

    def test_a_done_job_wakes_nori_without_a_harness_check(self):
        a = _agent()
        row, _, _ = _run(a, [_FINAL])
        captured = {}

        def fake_chat_run(session, user_id, name, *, extra_message=None, **kw):
            captured["prompt"] = extra_message["content"]
            return {"text": ""}

        import chat
        import turns
        import timing
        with patch.object(turns, "run", side_effect=lambda uid, f, sweep: f()), \
             patch.object(chat, "run", side_effect=fake_chat_run), \
             patch.object(timing, "start", return_value=MagicMock()):
            jobs._trigger_turn_for_job(row["id"], _UID, "agent", "do the thing", "done", "final report", None)
        self.assertIn("finished successfully", captured["prompt"])
        self.assertNotIn("HARNESS CHECK", captured["prompt"])


class DispatchDefaultsAndValidation(_Base):
    def _dispatch(self, agent, **kw):
        # dispatch refuses an agent with no model; the job thread is stubbed
        # out below, so a truthy model_id is all that has to be there.
        with patch.object(jobs.threading, "Thread") as thread, \
             patch.object(sub_agents, "get_enabled_by_label",
                          side_effect=lambda label: {**sub_agents.get_by_label(label), "model_id": 1}):
            res = jobs._dispatch_impl(_SESSION, agent["label"], "do it", **kw)
        return res, thread

    def _row(self, res):
        return dict(store.read(lambda c: c.execute("SELECT * FROM jobs WHERE id=?", (res["job_id"],)).fetchone()))

    def test_agent_default_applies_when_the_call_omits_the_flag(self):
        a = _agent(raw_file_access=True)
        res, _ = self._dispatch(a)
        self.assertTrue(res["raw_file_access"])
        self.assertEqual(self._row(res)["raw_file_access"], 1)
        self.assertEqual(res["access"]["exact_file_text_source"], "this sub-agent's default")

    def test_an_agent_without_the_default_stays_gist_only_when_omitted(self):
        a = _agent(raw_file_access=False)
        res, _ = self._dispatch(a)
        self.assertFalse(res["raw_file_access"])
        self.assertEqual(self._row(res)["raw_file_access"], 0)

    def test_a_call_can_opt_out_of_the_agent_default(self):
        a = _agent(raw_file_access=True)
        res, thread = self._dispatch(a, raw_file_access=False)
        self.assertFalse(res["raw_file_access"])
        self.assertEqual(res["access"]["exact_file_text_source"], "this call")
        self.assertEqual(self._row(res)["raw_file_access"], 0)
        self.assertFalse(thread.call_args.kwargs["args"][5])

    def test_a_call_can_opt_in_over_a_gist_only_default(self):
        a = _agent(raw_file_access=False)
        res, thread = self._dispatch(a, raw_file_access=True)
        self.assertTrue(res["raw_file_access"])
        self.assertTrue(thread.call_args.kwargs["args"][5])

    def test_a_tool_less_agent_is_told_it_has_no_file_access(self):
        a = _agent(tool_call_limit=0, raw_file_access=True)
        res, _ = self._dispatch(a)
        self.assertFalse(res["access"]["exact_file_text"])
        self.assertIn("no file access of any kind", res["access"]["note"])

    def test_dispatch_metadata_states_the_real_access(self):
        a = _agent(file_write=True, write_folder="Two Masters/grok", web_access=False, raw_file_access=True)
        res, _ = self._dispatch(a)
        acc = res["access"]
        self.assertEqual(acc["writes"], "only inside Two Masters/grok/")
        self.assertIn("name.v2.ext", acc["originals"])
        self.assertTrue(acc["exact_file_text"])
        self.assertFalse(acc["web"])
        self.assertEqual(acc["tool_call_limit"], 50)
        ro = _agent()
        res2, _ = self._dispatch(ro)
        self.assertEqual((res2["access"]["writes"], res2["access"]["originals"]),
                         ("none (read-only)", "read-only"))

    def test_expected_outputs_are_validated_stored_and_passed_to_the_job(self):
        a = _agent(file_write=True, write_folder="out")
        res, thread = self._dispatch(a, expected_outputs=["out/ch1.md", "out\\ch2.md"])
        self.assertEqual(res["expected_outputs"], ["out/ch1.md", "out/ch2.md"])
        self.assertEqual(json.loads(self._row(res)["expected_outputs"]), ["out/ch1.md", "out/ch2.md"])
        self.assertEqual(thread.call_args.kwargs["args"][6], ["out/ch1.md", "out/ch2.md"])

    def test_a_single_string_is_accepted_as_one_expected_output(self):
        a = _agent(file_write=True)
        res, _ = self._dispatch(a, expected_outputs="report.md")
        self.assertEqual(res["expected_outputs"], ["report.md"])

    def test_expected_outputs_need_an_agent_that_can_write(self):
        for kw in ({"file_write": False}, {"file_write": True, "tool_call_limit": 0}):
            a = _agent(**kw)
            res, thread = self._dispatch(a, expected_outputs=["x.md"])
            self.assertIn("can't write files", res["error"], kw)
            thread.assert_not_called()

    def test_an_expected_output_outside_the_write_folder_is_refused_up_front(self):
        a = _agent(file_write=True, write_folder="Two Masters/grok")
        res, thread = self._dispatch(a, expected_outputs=["Two Masters/other/ch1.md"])
        self.assertIn("outside this sub-agent's write folder", res["error"])
        thread.assert_not_called()

    def test_bad_expected_outputs_are_refused(self):
        a = _agent(file_write=True)
        for bad in (["../escape.md"], ["a/b?c.md"], [123], ["x.md"] * (jobs.MAX_EXPECTED_OUTPUTS + 1), {"a": 1}):
            res, thread = self._dispatch(a, expected_outputs=bad)
            self.assertIn("error", res, bad)
            thread.assert_not_called()

    def test_no_job_row_is_created_when_expected_outputs_are_invalid(self):
        a = _agent(file_write=False)
        before = store.read(lambda c: c.execute("SELECT COUNT(*) n FROM jobs").fetchone())["n"]
        self._dispatch(a, expected_outputs=["x.md"])
        after = store.read(lambda c: c.execute("SELECT COUNT(*) n FROM jobs").fetchone())["n"]
        self.assertEqual(before, after)

    def test_the_dispatch_tool_schema_describes_the_new_parameters(self):
        import tools
        props = tools.schema_for("dispatch_subagent")["function"]["parameters"]["properties"]
        self.assertIn("expected_outputs", props)
        self.assertIn("OMIT", props["raw_file_access"]["description"])
        self.assertEqual(tools.schema_for("dispatch_subagent")["function"]["parameters"]["required"],
                         ["agent_label", "task"])


class AccessPreamble(_Base):
    def test_the_first_message_states_the_real_access(self):
        a = _agent(file_write=True, write_folder="out", raw_file_access=True)
        _, _, seen = _run(a, [_FINAL], raw=True, expected=["out/a.md"])
        first = seen[0]["messages"][0]["content"]
        self.assertTrue(first.startswith("[Job access -- set by the harness"), first[:80])
        self.assertIn("Exact file text: YES", first)
        self.assertIn("only inside out/", first)
        self.assertIn("Required outputs: out/a.md", first)
        self.assertTrue(first.endswith("t"))  # the task itself follows, unchanged

    def test_a_gist_only_job_is_told_to_stop_rather_than_guess(self):
        _, _, seen = _run(_agent(), [_FINAL], raw=False)
        first = seen[0]["messages"][0]["content"]
        self.assertIn("Exact file text: NO", first)
        self.assertIn("stop and say so", first)
        self.assertIn("Writes: none (read-only)", first)

    def test_preamble_text_comes_from_the_roster_not_the_caller(self):
        text = jobs.access_preamble({"file_write": 1, "write_folder": "", "web_access": 1}, False, [])
        self.assertIn("anywhere in the working folder", text)
        self.assertIn("Web search/fetch: yes", text)


class AdminUi(_Base):
    class _H(server.Handler):
        def __init__(self):
            self.headers = {}
            self.sent = []
            self.command = "GET"

        def send(self, code, body=b"", extra=None, *, ctype="text/html; charset=utf-8"):
            self.sent.append(body.decode() if isinstance(body, bytes) else body)

    def _page(self):
        h = self._H()
        h.subagents_admin_form(_SESSION)
        return h.sent[0]

    def test_the_roster_row_and_add_form_have_the_checkbox(self):
        a = _agent(raw_file_access=True)
        page = self._page()
        self.assertIn("name=raw_file_access checked> exact file text by default", page)
        self.assertIn("<input type=checkbox name=raw_file_access> exact file text by default", page)
        self.assertIn("exact file text by default", page)

    def test_saving_the_row_updates_the_default_both_ways(self):
        a = _agent(file_write=True)
        h = self._H()
        h.subagents_limits_post(_SESSION, str(a["id"]), {"tool_call_limit": "50", "tool_byte_limit": "2000000",
                                                         "file_write": "on", "raw_file_access": "on"})
        self.assertEqual(sub_agents.get(a["id"])["raw_file_access"], 1)
        h.subagents_limits_post(_SESSION, str(a["id"]), {"tool_call_limit": "50", "tool_byte_limit": "2000000",
                                                         "file_write": "on"})
        self.assertEqual(sub_agents.get(a["id"])["raw_file_access"], 0)

    def test_the_add_form_creates_an_agent_with_the_default(self):
        h = self._H()
        h.subagents_admin_post(_SESSION, {"label": "ui-raw-agent", "tool_call_limit": "10",
                                          "tool_byte_limit": "2000000", "model_id": "0",
                                          "raw_file_access": "on"})
        self.assertEqual(sub_agents.get_by_label("ui-raw-agent")["raw_file_access"], 1)

    def test_new_and_existing_agents_default_to_gist_only(self):
        self.assertEqual(_agent()["raw_file_access"], 0)

    def test_nori_is_told_each_agents_default(self):
        _agent(raw_file_access=True)
        import self_knowledge
        roster = self_knowledge._sub_agents_summary()["configured_roster"]
        self.assertTrue(any(r["exact_file_text_by_default"] for r in roster))
        self.assertIn("expected_outputs", jobs.SUBAGENT_LIMITS_EXPLAIN)
        self.assertIn("INCOMPLETE", jobs.SUBAGENT_LIMITS_EXPLAIN)


if __name__ == "__main__":
    unittest.main()
