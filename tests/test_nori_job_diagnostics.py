# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Sub-agent job diagnostics, search caps, time limits and concurrency
(2026-10-02). From an investigation of 45 real jobs: 17 of 19 "failures" were
a 120-second wall clock cutting a slow model call, recorded as `failed` with a
BLANK error (str(TimeoutError()) is ""), no usage, no log line; 26 slow
`search_files` calls (a 4.7-million-token screening prompt, 17-32s each) ate
482 of 517 total search-seconds; and every model call in the app shared one
8-thread pool, so a burst of 16 jobs failed about half.

  1. Diagnostics: a timeout is a timeout (ModelTimeout), an error is never
     blank and says where the job stopped, usage/cost are saved on failure,
     every job leaves a bounded trace, a log line, and check_job diagnostics.
  2. search_files is bounded: long lines are windowed, the whole result is
     capped, the screener can't be handed millions of tokens.
  3. Time limit: a larger default, per-agent override, queue time not counted.
  4. Concurrency: at most N jobs run, the rest wait queued; model calls from
     job threads use their own pool; queue wait is not request time.

Real store and filesystem; only model calls are scripted.
"""
import concurrent.futures
import contextlib
import io
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_SCRATCH = tempfile.mkdtemp(prefix="nori_test_job_diagnostics_")
os.environ["NORI_DATA_DIR"] = _SCRATCH
os.environ["NORI_PROMPTS_DIR"] = tempfile.mkdtemp(prefix="nori_test_job_diagnostics_prompts_")
os.environ["NORI_NO_LOGFILE"] = "1"
os.environ.pop("NORI_SUBAGENT_TIMEOUT_S", None)

import accounts  # noqa: E402
import chat  # noqa: E402
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
_OK = {"category": "informational", "priority": "normal", "suggested_action": "", "suspicious": False}


def _screen(content, *, kind="email", preserve_content=False, cost_sink=None):
    return {**_OK, "content": content, "truncated": False} if preserve_content else {**_OK, "summary": "gist"}


# ── 1a. chat: timeouts, pools ────────────────────────────────────────────
class ModelTimeoutType(unittest.TestCase):
    def test_a_timeout_is_both_a_model_error_and_a_timeout_error(self):
        e = chat.ModelTimeout("took too long", kind="blocked_upstream")
        self.assertIsInstance(e, chat.ModelError)
        self.assertIsInstance(e, TimeoutError)
        self.assertEqual((str(e), e.kind, e.transient), ("took too long", "blocked_upstream", False))

    def test_net_error_is_never_blank_and_keeps_a_timeout_a_timeout(self):
        for exc in (TimeoutError(), socket.timeout(), concurrent.futures.TimeoutError()):
            err = chat._net_error("xAI /responses", exc)
            self.assertIsInstance(err, chat.ModelTimeout, repr(exc))
            self.assertIn("xAI /responses", str(err))
            self.assertIn("TimeoutError", str(err))
            self.assertNotEqual(str(err).strip(), "xAI /responses call failed:")

    def test_other_network_errors_keep_their_type_and_text(self):
        err = chat._net_error("Anthropic", urllib.error.URLError("connection refused"))
        self.assertNotIsInstance(err, TimeoutError)
        self.assertIn("URLError", str(err))
        self.assertIn("connection refused", str(err))

    def test_the_blank_message_bug_is_gone_end_to_end(self):
        with patch.object(chat, "_urlopen_json", side_effect=TimeoutError()):
            with self.assertRaises(chat.ModelTimeout) as cm:
                chat._plain_responses_call("http://x", {}, "m", [{"role": "user", "content": "hi"}],
                                           None, None, 5, "xAI")
        self.assertTrue(str(cm.exception).strip().endswith(")"), str(cm.exception))
        self.assertIn("xAI /responses", str(cm.exception))

    def test_a_provider_http_error_still_reports_its_status_and_body(self):
        err = urllib.error.HTTPError("http://x", 426, "Upgrade", {}, io.BytesIO(b'{"error":"outdated"}'))
        with patch.object(chat, "_urlopen_json", side_effect=err):
            with self.assertRaises(chat.ModelError) as cm:
                chat._plain_responses_call("http://x", {}, "m", [{"role": "user", "content": "hi"}],
                                           None, None, 5, "xAI")
        self.assertIn("(426)", str(cm.exception))
        self.assertIn("outdated", str(cm.exception))
        self.assertNotIsInstance(cm.exception, TimeoutError)

    def test_nothing_submits_to_the_shared_pool_directly_any_more(self):
        src = open(os.path.join(ROOT, "chat.py"), encoding="utf-8").read()
        self.assertEqual(src.count("_CALL_EXECUTOR.submit("), 0)
        self.assertEqual(src.count("pool.submit(_wrapped)"), 1)
        self.assertEqual(src.count("_run_call("), 5)  # the definition + 4 call sites

    def test_call_keeps_a_timeout_a_timeout_after_its_retry_loop(self):
        with patch.object(chat, "_run_call", side_effect=chat.ModelTimeout("slow", kind="blocked_upstream")):
            with self.assertRaises(chat.ModelTimeout) as cm:
                chat.call([{"role": "user", "content": "hi"}], api_key="k", timeout=1)
        self.assertEqual(cm.exception.kind, "blocked_upstream")
        self.assertIn("model call failed after 1 attempt(s)", str(cm.exception))


class WorkerPools(unittest.TestCase):
    def setUp(self):
        self._live, self._job = chat._CALL_EXECUTOR, chat._JOB_EXECUTOR
        self._queue_wait = chat.QUEUE_WAIT_S
        chat._CALL_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        chat._JOB_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=1)

    def tearDown(self):
        chat._CALL_EXECUTOR.shutdown(wait=False, cancel_futures=True)
        chat._JOB_EXECUTOR.shutdown(wait=False, cancel_futures=True)
        chat._CALL_EXECUTOR, chat._JOB_EXECUTOR = self._live, self._job
        chat.QUEUE_WAIT_S = self._queue_wait
        chat.use_job_pool(False)

    def test_time_spent_waiting_for_a_worker_is_not_request_time(self):
        chat._CALL_EXECUTOR.submit(time.sleep, 0.25)          # the one worker is busy
        t0 = time.monotonic()
        out = chat._run_call(lambda req, to: "ok", None, 1.0, what="test call")
        self.assertEqual(out, "ok")
        self.assertGreaterEqual(time.monotonic() - t0, 0.2)   # it did wait...
        self.assertGreaterEqual(chat.last_call_stats()["queue_ms"], 150)   # ...and says so
        self.assertEqual(chat.last_call_stats()["pool"], "live")

    def test_waiting_for_a_worker_is_bounded_and_the_call_never_runs(self):
        chat._CALL_EXECUTOR.submit(time.sleep, 0.6)
        ran = []
        with self.assertRaises(chat.ModelTimeout) as cm:
            chat._run_call(lambda req, to: ran.append(1), None, 0.15, what="test call")
        self.assertEqual(cm.exception.kind, "pool_saturated")
        self.assertIn("no model-call worker became free", str(cm.exception))
        time.sleep(0.7)
        self.assertEqual(ran, [], "a call abandoned in the queue must be cancelled, not run later")

    def test_a_slow_request_times_out_with_a_message_that_says_so(self):
        with self.assertRaises(chat.ModelTimeout) as cm:
            chat._run_call(lambda req, to: time.sleep(0.5), None, 0.1, what="xAI /responses call")
        self.assertEqual(cm.exception.kind, "blocked_upstream")
        self.assertIn("xAI /responses call", str(cm.exception))
        self.assertIn("no response from the provider within 0.1s", str(cm.exception))

    def test_exceptions_from_the_call_itself_pass_through_unchanged(self):
        boom = urllib.error.URLError("nope")
        with self.assertRaises(urllib.error.URLError) as cm:
            chat._run_call(lambda req, to: (_ for _ in ()).throw(boom), None, 1.0, what="x")
        self.assertIs(cm.exception, boom)

    def test_job_threads_use_the_job_pool_and_live_threads_do_not(self):
        seen = {}

        def work(name, job):
            if job:
                chat.use_job_pool(True)
            chat._run_call(lambda req, to: None, None, 1.0, what="x")
            seen[name] = chat.last_call_stats()["pool"]

        for name, job in (("job", True), ("live", False)):
            t = threading.Thread(target=work, args=(name, job))
            t.start()
            t.join(5)
        self.assertEqual(seen, {"job": "job", "live": "live"})
        self.assertFalse(getattr(chat._pool_ctx, "job", False), "the flag must not leak across threads")

    def test_a_saturated_live_pool_does_not_block_job_calls(self):
        chat._CALL_EXECUTOR.submit(time.sleep, 0.5)
        chat.use_job_pool(True)
        t0 = time.monotonic()
        chat._run_call(lambda req, to: None, None, 1.0, what="x")
        self.assertLess(time.monotonic() - t0, 0.3)

    def test_the_job_pool_is_sized_for_the_concurrency_cap(self):
        self.assertGreaterEqual(self._job._max_workers, jobs.MAX_CONCURRENT_JOBS)
        self.assertGreater(self._live._max_workers, 8)


# ── 2. search_files caps ─────────────────────────────────────────────────
class _Base(unittest.TestCase):
    def setUp(self):
        import shutil
        shutil.rmtree(workfiles.WORKFILES_DIR / str(_UID), ignore_errors=True)
        store.write(lambda c: c.execute("DELETE FROM work_files WHERE user_id=?", (_UID,)))
        self.sent = []

        def screen(content, **kw):
            self.sent.append(content)
            return _screen(content, **kw)
        self._p = patch.object(ingest, "summarize_untrusted", side_effect=screen)
        self.screen = self._p.start()

    def tearDown(self):
        self._p.stop()

    def put(self, name, text):
        assert workfiles.upload_file(_SESSION, name, text.encode("utf-8")).get("ok")


class SearchIsBounded(_Base):
    def search(self, pattern, path=""):
        return workfiles.search_files(_SESSION, pattern, path)

    def test_a_match_inside_a_multi_megabyte_line_is_a_window_and_fast(self):
        self.put("book.md", "x" * 1_500_000 + "NEEDLE" + "y" * 1_500_000)
        t0 = time.monotonic()
        r = self.search("NEEDLE")
        self.assertLess(time.monotonic() - t0, 5)
        self.assertEqual(r["match_count"], 1)
        self.assertIn("NEEDLE", r["content"])
        self.assertIn("…", r["content"])
        self.assertLess(len(r["content"]), workfiles.SEARCH_LINE_CHARS + 120)
        self.assertLessEqual(len(self.sent[0]), workfiles.SEARCH_MAX_BLOB_CHARS)

    def test_the_screener_is_never_handed_more_than_the_cap(self):
        for i in range(workfiles.SEARCH_MAX_MATCHES):
            self.put(f"f{i}.md", ("long line " * 20000 + " NEEDLE " + "tail " * 20000 + "\n") * 5)
        self.search("NEEDLE")
        self.assertLessEqual(len(self.sent[0]), workfiles.SEARCH_MAX_BLOB_CHARS)

    def test_context_lines_are_clipped_and_the_match_line_is_a_window(self):
        before, after = "B" * 5000, "A" * 5000
        self.put("c.md", before + "\n" + "z" * 400 + " HIT " + "w" * 400 + "\n" + after + "\n")
        r = self.search("HIT")
        lines = r["content"].split("\n")
        self.assertLessEqual(max(len(l) for l in lines), workfiles.SEARCH_LINE_CHARS + 2 +
                             len("c.md:2"))
        self.assertIn("B" * 10, r["content"])
        self.assertNotIn("B" * (workfiles.SEARCH_CONTEXT_LINE_CHARS + 5), r["content"])

    def test_when_not_everything_fits_it_says_so_and_drops_whole_matches(self):
        for i in range(workfiles.SEARCH_MAX_MATCHES):
            self.put(f"f{i:02d}.md", ("p " * 100 + "NEEDLE " + "q " * 100 + "\n") * 3)
        with patch.object(workfiles, "SEARCH_MAX_BLOB_CHARS", 1500):
            r = self.search("NEEDLE")
        self.assertEqual(r["match_count"], workfiles.SEARCH_MAX_MATCHES)
        self.assertLess(r["shown"], r["match_count"])
        self.assertGreater(r["shown"], 0)
        self.assertTrue(r["truncated"])
        self.assertIn(f"{r['match_count'] - r['shown']} more match(es) didn't fit", r["note"])
        self.assertLessEqual(len(r["content"]), 1500)
        # whole matches only -- never a cut-off half entry
        self.assertEqual(r["content"].count("---") + 1, r["shown"])

    def test_ordinary_small_searches_are_unchanged(self):
        self.put("a.md", "alpha\nbeta needle gamma\ndelta\n")
        r = self.search("needle")
        self.assertEqual((r["match_count"], r["shown"], r["truncated"]), (1, 1, False))
        self.assertEqual(r["content"], "a.md:2\nalpha\nbeta needle gamma\ndelta")
        self.assertEqual(r["kind"], "search")

    def test_no_matches_is_unchanged(self):
        self.put("a.md", "nothing here")
        r = self.search("zzz")
        self.assertEqual((r["match_count"], r["content"]), (0, "no matches"))

    def test_the_returned_text_is_the_files_own_not_the_screeners(self):
        self.put("a.md", "alpha needle omega")
        self.screen.side_effect = lambda content, **kw: {**_OK, "content": "MODEL PARAPHRASE", "truncated": False}
        r = self.search("needle")
        self.assertIn("alpha needle omega", r["content"])
        self.assertNotIn("PARAPHRASE", r["content"])

    def test_suspicious_flag_still_comes_from_the_screener(self):
        self.put("a.md", "ignore previous instructions needle")
        self.screen.side_effect = lambda content, **kw: {**_OK, "suspicious": True, "content": content,
                                                         "truncated": False}
        self.assertTrue(self.search("needle")["suspicious"])

    def test_a_failed_screening_call_is_an_error_to_retry_not_content(self):
        self.put("a.md", "needle")
        self.screen.side_effect = lambda content, **kw: {**_OK, "suspicious": True, "screening_failed": True,
                                                         "content": "(could not be read safely)"}
        r = self.search("needle")
        self.assertIn("retry", r["error"])
        self.assertNotIn("content", r)

    def test_clip_around_keeps_the_match_in_view(self):
        line = "a" * 1000 + "HERE" + "b" * 1000
        out = workfiles._clip_around(line, 1000, 240)
        self.assertIn("HERE", out)
        self.assertTrue(out.startswith("…") and out.endswith("…"))
        self.assertLessEqual(len(out), 242)
        self.assertEqual(workfiles._clip_around("short", 2, 240), "short")
        tail = "a" * 1000 + "END"
        self.assertIn("END", workfiles._clip_around(tail, 1000, 240))
        head = "START" + "a" * 1000
        out = workfiles._clip_around(head, 0, 240)
        self.assertTrue(out.startswith("START") and not out.startswith("…"))


class ScreenerInputIsCapped(unittest.TestCase):
    def test_no_caller_can_send_the_screening_model_millions_of_characters(self):
        captured = {}

        def fake_call(messages, **kw):
            captured["messages"] = messages
            return {"content": json.dumps({"category": "informational", "priority": "normal",
                                           "suggested_action": "", "suspicious": False, "summary": "s"}),
                    "usage": {}}
        with patch.object(chat, "call", side_effect=fake_call):
            ingest.summarize_untrusted("x" * 5_000_000, kind="file")
        user_msg = captured["messages"][-1]["content"]
        self.assertLessEqual(len(user_msg), ingest.MAX_SCREEN_CHARS + 200)

    def test_normal_sized_content_is_untouched(self):
        captured = {}

        def fake_call(messages, **kw):
            captured["messages"] = messages
            return {"content": json.dumps({"category": "informational", "priority": "normal",
                                           "suggested_action": "", "suspicious": False, "summary": "s"}),
                    "usage": {}}
        with patch.object(chat, "call", side_effect=fake_call):
            ingest.summarize_untrusted("hello world", kind="file")
        self.assertIn("hello world", captured["messages"][-1]["content"])


# ── 1b/3/4. jobs ─────────────────────────────────────────────────────────
def _agent(**kw) -> dict:
    kw.setdefault("tool_call_limit", 50)
    ok, sid = sub_agents.create(_UID, f"agent-{len(sub_agents.list_all())}", None, **kw)
    assert ok, sid
    return sub_agents.get(sid)


def _tc(name, args, call_id="1"):
    return {"id": call_id, "function": {"name": name, "arguments": json.dumps(args)}}


def _round(*calls, usage=None):
    return {"content": "", "tool_calls": [_tc(n, a, str(i)) for i, (n, a) in enumerate(calls)],
            "usage": usage or {}}


_FINAL = {"content": "final report", "tool_calls": [], "usage": {}}


def _insert_job(agent):
    return store.write(lambda c: c.execute(
        "INSERT INTO jobs(user_id, sub_agent_id, task, status, created_ts, timeout_s) VALUES (?,?,?,?,?,?)",
        (_UID, agent["id"], "t", "queued", time.time(), 30)).lastrowid)


def _row(job_id):
    return dict(store.read(lambda c: c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()))


def _run(agent, rounds, *, timeout_s=30, raw=False, expected=None, capture=False):
    """Run one job through the real loop with a scripted model. A round that
    is an Exception instance is raised instead of returned."""
    job_id = _insert_job(agent)
    n = {"i": 0}

    def fake(a, messages, tool_schema, to):
        r = rounds[min(n["i"], len(rounds) - 1)]
        n["i"] += 1
        if isinstance(r, BaseException):
            raise r
        if callable(r):
            return r()
        return r

    buf = io.StringIO()
    with patch.object(jobs, "_call", side_effect=fake), contextlib.redirect_stdout(buf):
        jobs._run_job_with_tools(job_id, agent, "t", timeout_s, dict(_SESSION), raw, expected or [])
    row = _row(job_id)
    return (row, buf.getvalue()) if capture else row


class _JobBase(_Base):
    def setUp(self):
        super().setUp()
        store.write(lambda c: c.execute("DELETE FROM jobs"))
        self._p2 = patch.object(jobs, "_trigger_turn_for_job")
        self.trigger = self._p2.start()

    def tearDown(self):
        self._p2.stop()
        super().tearDown()


class FailuresSayWhatHappenedAndWhere(_JobBase):
    def test_a_model_timeout_is_timed_out_not_failed_and_not_blank(self):
        row = _run(_agent(), [_round(("list_files", {})),
                              chat.ModelTimeout("xAI /responses call: no response from the provider "
                                                "within 24s (request sent 24s ago)", kind="blocked_upstream")])
        self.assertEqual(row["status"], "timed_out")
        e = row["error"]
        self.assertIn("ModelTimeout", e)
        self.assertIn("xAI /responses call: no response from the provider within 24s", e)
        self.assertIn("during model call (round 2)", e)
        self.assertIn("of the 30s limit", e)
        self.assertIn("1 tool call(s) made", e)

    def test_a_bare_timeouterror_with_no_message_still_explains_itself(self):
        row = _run(_agent(), [TimeoutError()])
        self.assertEqual(row["status"], "timed_out")
        self.assertIn("TimeoutError: (the exception carried no message)", row["error"])
        self.assertIn("during model call (round 1)", row["error"])

    def test_a_blank_exception_names_its_type(self):
        row = _run(_agent(), [RuntimeError()])
        self.assertEqual(row["status"], "failed")
        self.assertIn("RuntimeError: (the exception carried no message)", row["error"])

    def test_a_provider_error_keeps_its_status_and_body_and_gets_context(self):
        row = _run(_agent(), [chat.ModelError('xAI /responses call failed (426): {"error":"outdated"}')])
        self.assertEqual(row["status"], "failed")
        self.assertIn("(426)", row["error"])
        self.assertIn("outdated", row["error"])
        self.assertIn("during model call (round 1)", row["error"])

    def test_a_failure_inside_a_tool_says_which_tool(self):
        with patch("tools.dispatch", side_effect=ValueError("bad regex")):
            row = _run(_agent(), [_round(("search_files", {"pattern": "("}))])
        self.assertEqual(row["status"], "failed")
        self.assertIn("ValueError: bad regex", row["error"])
        self.assertIn("during tool search_files (round 1)", row["error"])

    def test_usage_and_cost_are_saved_even_when_the_job_fails(self):
        row = _run(_agent(), [_round(("list_files", {}), usage={"prompt_tokens": 100, "completion_tokens": 10,
                                                                  "cost": 0.5}),
                              RuntimeError("boom")])
        self.assertEqual((row["prompt_tokens"], row["completion_tokens"], row["cost_usd"]), (100, 10, 0.5))
        self.assertEqual(row["tool_calls_used"], 1)

    def test_the_harness_counters_are_saved_on_failure_too(self):
        self.put("c.md", "word " * 5000)
        a = _agent()
        with patch.object(workfiles, "RAW_PAGE_CHARS", 100):
            row = _run(a, [_round(("read_file_full", {"path": "c.md"})), RuntimeError("x")], raw=True)
        self.assertEqual((row["status"], row["partial_reads"], row["tool_calls_used"]), ("failed", 1, 1))

    def test_hitting_the_job_deadline_between_rounds_is_a_timeout_with_the_reason(self):
        slow = lambda: (time.sleep(1.2), _round(("list_files", {})))[1]  # noqa: E731
        row = _run(_agent(), [slow, _FINAL], timeout_s=1)
        self.assertEqual(row["status"], "timed_out")
        self.assertIn("job time limit reached: 1s elapsed before round 2", row["error"])
        self.assertIn("1 tool call(s) made", row["error"])

    def test_running_out_of_rounds_names_the_round_limit(self):
        # The call budget ends a job before the round limit if real calls are being made (a
        # budget hit forces a final answer). The way to hit the round limit is a model that
        # keeps asking for a tool it doesn't have: refused calls aren't counted.
        a = _agent(tool_call_limit=1)
        with patch.object(jobs, "_MAX_ROUNDS_SAFETY", 5):
            row = _run(a, [_round(("web_search", {"query": "x"}))] * 50)
        self.assertEqual(row["status"], "failed")
        self.assertIn("round limit reached (11 rounds)", row["error"])
        self.assertEqual(row["tool_calls_used"], 0)
        self.assertEqual(json.loads(row["trace"])["stage"], "tool web_search (round 11)")

    def test_the_old_blank_error_can_not_happen(self):
        for exc in (TimeoutError(), RuntimeError(), chat.ModelTimeout("x"), KeyError()):
            row = _run(_agent(), [exc])
            self.assertGreater(len((row["error"] or "").strip()), 30, repr(exc))

    def put(self, name, text):
        assert workfiles.upload_file(_SESSION, name, text.encode("utf-8")).get("ok")


class EveryJobLeavesATrace(_JobBase):
    def put(self, name, text):
        assert workfiles.upload_file(_SESSION, name, text.encode("utf-8")).get("ok")

    def test_a_done_job_has_a_trace_with_model_and_tool_events(self):
        self.put("a.md", "hello")
        row = _run(_agent(), [_round(("list_files", {}), ("read_file", {"path": "a.md"}),
                                     usage={"prompt_tokens": 7, "completion_tokens": 3}), _FINAL])
        tr = json.loads(row["trace"])
        kinds = [(e["k"], e.get("name"), e.get("r")) for e in tr["events"]]
        self.assertEqual(kinds, [("model", None, 1), ("tool", "list_files", 1), ("tool", "read_file", 1),
                                 ("model", None, 2)])
        self.assertEqual(tr["stage"], "finished")
        self.assertEqual(tr["timeout_s"], 30)
        self.assertEqual(tr["events"][0]["pt"], 7)
        self.assertTrue(all("ms" in e for e in tr["events"]))
        self.assertGreater(tr["events"][2]["bytes"], 0)

    def test_the_stage_it_died_in_is_recorded(self):
        row = _run(_agent(), [_round(("list_files", {})), RuntimeError("x")])
        self.assertEqual(json.loads(row["trace"])["stage"], "model call (round 2)")
        self.assertEqual(jobs.diagnostics(row)["stopped_during"], "model call (round 2)")

    def test_a_failed_model_call_is_in_the_trace_with_its_error(self):
        row = _run(_agent(), [RuntimeError("kaboom")])
        ev = json.loads(row["trace"])["events"][0]
        self.assertEqual(ev["k"], "model")
        self.assertIn("RuntimeError: kaboom", ev["err"])

    def test_a_refused_tool_call_is_traced_with_its_error(self):
        row = _run(_agent(), [_round(("web_search", {"query": "x"})), _FINAL])
        tool = [e for e in json.loads(row["trace"])["events"] if e["k"] == "tool"][0]
        self.assertIn("not available to sub-agents", tool["err"])

    def test_diagnostics_summarize_where_the_time_went(self):
        def slow_round():
            time.sleep(0.05)
            return _round(("list_files", {}))
        row = _run(_agent(), [slow_round, _FINAL])
        d = jobs.diagnostics(row)
        self.assertEqual((d["rounds"], d["tool_calls"], d["time_limit_s"]), (2, 1, 30))
        self.assertEqual(d["slowest_tool"]["name"], "list_files")
        self.assertGreaterEqual(d["model_time_s"], 0)
        self.assertEqual(d["stopped_during"], "finished")
        self.assertIn("elapsed_s", d)

    def test_worker_queue_wait_is_surfaced_when_it_was_significant(self):
        with patch.object(chat, "last_call_stats", return_value={"queue_ms": 4200, "pool": "job"}):
            row = _run(_agent(), [_FINAL])
        self.assertEqual(jobs.diagnostics(row)["worker_queue_wait_ms_max"], 4200)
        row2 = _run(_agent(), [_FINAL])
        self.assertNotIn("worker_queue_wait_ms_max", jobs.diagnostics(row2))

    def test_a_job_with_no_trace_has_empty_diagnostics(self):
        self.assertEqual(jobs.diagnostics({"trace": None}), {})
        self.assertEqual(jobs.diagnostics({"trace": "not json"}), {})

    def test_the_trace_is_bounded(self):
        tr = jobs._Trace(30)
        for i in range(1000):
            tr.tool(i, "read_file_full", 5, 9000, error="e" * 500)
        out = tr.to_json()
        self.assertLessEqual(len(out), jobs._TRACE_MAX_CHARS)
        parsed = json.loads(out)
        self.assertTrue(any(e["k"] == "gap" for e in parsed["events"]))
        self.assertEqual(parsed["events"][0]["r"], 0)       # the start survives
        self.assertEqual(parsed["events"][-1]["r"], 999)    # and so does the end

    def test_the_trace_does_not_lose_events_under_the_limit(self):
        tr = jobs._Trace(30)
        for i in range(50):
            tr.tool(i, "t", 1, 1)
        self.assertEqual(len(json.loads(tr.to_json())["events"]), 50)

    def test_the_no_tools_path_records_usage_trace_and_specific_errors_too(self):
        a = _agent(tool_call_limit=0)
        for exc, status in ((chat.ModelTimeout("slow"), "timed_out"), (RuntimeError("boom"), "failed")):
            job_id = _insert_job(a)
            with patch.object(jobs, "_call", side_effect=exc), contextlib.redirect_stdout(io.StringIO()):
                jobs._run_job_no_tools(job_id, a, "t", 30)
            row = _row(job_id)
            self.assertEqual(row["status"], status)
            self.assertIn(type(exc).__name__, row["error"])
            self.assertIn("during model call (round 1)", row["error"])
            self.assertTrue(json.loads(row["trace"])["events"])
        job_id = _insert_job(a)
        ok = {"content": "text", "usage": {"prompt_tokens": 11, "completion_tokens": 2, "cost": 0.01}}
        with patch.object(jobs, "_call", return_value=ok), contextlib.redirect_stdout(io.StringIO()):
            jobs._run_job_no_tools(job_id, a, "t", 30)
        row = _row(job_id)
        self.assertEqual((row["status"], row["prompt_tokens"], row["result"]), ("done", 11, "text"))
        self.assertEqual(json.loads(row["trace"])["stage"], "finished")


class JobsReachTheContainerLog(_JobBase):
    def test_a_failed_job_logs_one_line_with_the_cause_and_a_traceback(self):
        row, out = _run(_agent(), [RuntimeError("kaboom")], capture=True)
        self.assertIn(f"jobs: job #{row['id']} ", out)
        self.assertIn("failed after", out)
        self.assertIn("RuntimeError: kaboom", out)
        self.assertIn("Traceback (most recent call last)", out)

    def test_a_timeout_logs_its_line_without_a_noisy_traceback(self):
        row, out = _run(_agent(), [chat.ModelTimeout("provider silent")], capture=True)
        self.assertIn("timed_out after", out)
        self.assertIn("provider silent", out)
        self.assertNotIn("Traceback", out)

    def test_a_clean_job_logs_a_short_summary_with_no_error_text(self):
        row, out = _run(_agent(), [_round(("list_files", {})), _FINAL], capture=True)
        self.assertIn(f"jobs: job #{row['id']} ", out)
        self.assertIn("done after", out)
        self.assertIn("2 round(s), 1 tool call(s)", out)
        self.assertNotIn("--", out)

    def test_a_slow_tool_is_called_out_in_the_log_line(self):
        tr = jobs._Trace(30)
        tr.rounds = 2
        tr.tool(1, "search_files", 18568, 100)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            jobs._log_job_end(9, "Grok", "timed_out", "x", tr, 3)
        self.assertIn("slowest tool search_files 19s", buf.getvalue())


class CheckJobExplains(_JobBase):
    def test_a_failed_job_includes_diagnostics_by_default(self):
        row = _run(_agent(), [_round(("list_files", {})), RuntimeError("x")])
        r = jobs._check_job_impl(_SESSION, row["id"])
        self.assertEqual(r["status"], "failed")
        self.assertEqual(r["diagnostics"]["stopped_during"], "model call (round 2)")
        self.assertIn("RuntimeError", r["error"])
        self.assertNotIn("trace", r)

    def test_a_clean_job_is_quiet_unless_asked(self):
        row = _run(_agent(), [_FINAL])
        self.assertNotIn("diagnostics", jobs._check_job_impl(_SESSION, row["id"]))
        verbose = jobs._check_job_impl(_SESSION, row["id"], verbose=True)
        self.assertIn("diagnostics", verbose)
        self.assertEqual(verbose["trace"]["stage"], "finished")

    def test_verbose_adds_the_round_by_round_trace(self):
        row = _run(_agent(), [_round(("list_files", {})), RuntimeError("x")])
        r = jobs._check_job_impl(_SESSION, row["id"], verbose=True)
        self.assertEqual([e["k"] for e in r["trace"]["events"]], ["model", "tool", "model"])

    def test_the_tool_schema_exposes_verbose_and_tells_her_to_explain_why(self):
        import tools
        fn = tools.schema_for("check_job")["function"]
        self.assertIn("verbose", fn["parameters"]["properties"])
        self.assertEqual(fn["parameters"]["required"], ["job_id"])
        self.assertIn("diagnostics", fn["description"])

    def test_the_wake_up_prompt_gets_the_richer_error(self):
        row = _run(_agent(), [chat.ModelTimeout("provider silent")])
        prompts = []
        import conversation
        import timing
        import turns
        with patch.object(jobs, "_trigger_turn_for_job", self._p2.temp_original), \
             patch.object(ingest, "summarize_untrusted", side_effect=_screen), \
             patch.object(timing, "start", return_value=MagicMock()), \
             patch.object(turns, "run", side_effect=lambda uid, f, sweep: f()), \
             patch.object(chat, "run", side_effect=lambda s, u, n, *, extra_message=None, **kw:
                          (prompts.append(extra_message["content"]), {"text": "x", "usage": {}})[1]):
            jobs._trigger_turn_for_job(row["id"], _UID, "agent", "t", row["status"], row["result"], row["error"])
        self.assertIn("What went wrong: ModelTimeout: provider silent", prompts[0])
        self.assertIn("during model call (round 1)", prompts[0])


class RecentProblemsOnTheAdminPage(_JobBase):
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

    def test_recent_problems_lists_only_bad_endings_newest_first(self):
        a = _agent()
        ok = _run(a, [_FINAL])
        bad1 = _run(a, [RuntimeError("one")])
        bad2 = _run(a, [chat.ModelTimeout("two")])
        rows = jobs.recent_problems()
        self.assertEqual([r["id"] for r in rows], [bad2["id"], bad1["id"]])
        self.assertNotIn(ok["id"], [r["id"] for r in rows])
        self.assertEqual(rows[0]["label"], a["label"])
        self.assertTrue(rows[0]["diagnostics"])
        self.assertEqual(len(jobs.recent_problems(limit=1)), 1)

    def test_the_page_shows_where_each_job_stopped_and_escapes_the_error(self):
        a = _agent()
        _run(a, [_round(("list_files", {})), RuntimeError("<script>alert(1)</script>")])
        page = self._page()
        self.assertIn("recent job problems", page)
        self.assertIn("stopped during: model call (round 2)", page)
        self.assertIn("1 tool call(s)", page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)
        self.assertNotIn("<script>alert(1)</script>", page)

    def test_the_section_is_absent_when_nothing_has_gone_wrong(self):
        store.write(lambda c: c.execute("DELETE FROM jobs"))
        self.assertNotIn("recent job problems", self._page())

    def test_a_job_from_before_tracing_still_renders(self):
        a = _agent()
        job_id = store.write(lambda c: c.execute(
            "INSERT INTO jobs(user_id, sub_agent_id, task, status, error, created_ts, finished_ts, timeout_s) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (_UID, a["id"], "t", "failed", "xAI /responses call failed: ", 1.0, 2.0, 120)).lastrowid)
        self.assertIn("ran before tracing existed", self._page())


class TimeLimits(_JobBase):
    def test_default_is_much_higher_than_the_old_120(self):
        self.assertEqual(jobs.DEFAULT_TIMEOUT_S, 600)

    def test_env_overrides_the_default(self):
        env = {**os.environ, "NORI_DATA_DIR": _SCRATCH, "NORI_SUBAGENT_TIMEOUT_S": "900",
               "NORI_SUBAGENT_MAX_CONCURRENT": "7"}
        import subprocess
        out = subprocess.run([sys.executable, "-c", "import jobs, chat; print(jobs.DEFAULT_TIMEOUT_S, "
                              "jobs.MAX_CONCURRENT_JOBS, chat.JOB_POOL_WORKERS)"],
                             cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(out.stdout.split()[-3:], ["900", "7", "11"], out.stderr)

    def test_agents_default_to_the_global_limit_and_can_set_their_own(self):
        self.assertEqual(_agent()["timeout_s"], 0)
        self.assertEqual(_agent(timeout_s=900)["timeout_s"], 900)

    def test_bounds_are_enforced_on_create_and_on_save(self):
        for bad in (1, 29, 3601, 99999, -5):
            ok, msg = sub_agents.create(_UID, f"bad-{bad}", None, timeout_s=bad)
            self.assertFalse(ok, bad)
            self.assertIn("time limit", msg)
        a = _agent()
        self.assertIn("time limit", sub_agents.set_limits(a["id"], 5, 100_000, 10))
        self.assertEqual(sub_agents.get(a["id"])["timeout_s"], 0)  # nothing saved on error
        self.assertIsNone(sub_agents.set_limits(a["id"], 5, 100_000, 300))
        self.assertEqual(sub_agents.get(a["id"])["timeout_s"], 300)
        self.assertIsNone(sub_agents.set_limits(a["id"], 5, 100_000, 0))
        self.assertEqual(sub_agents.get(a["id"])["timeout_s"], 0)

    def test_saving_limits_without_a_time_limit_leaves_it_alone(self):
        a = _agent(timeout_s=450)
        self.assertIsNone(sub_agents.set_limits(a["id"], 7, 100_000))
        self.assertEqual(sub_agents.get(a["id"])["timeout_s"], 450)

    def _dispatch(self, agent, **kw):
        with patch.object(jobs.threading, "Thread") as thread, \
             patch.object(sub_agents, "get_enabled_by_label",
                          side_effect=lambda label: {**sub_agents.get_by_label(label), "model_id": 1}):
            res = jobs._dispatch_impl(_SESSION, agent["label"], "do it", **kw)
        return res, thread

    def test_dispatch_uses_the_agents_limit_else_the_default(self):
        res, thread = self._dispatch(_agent(timeout_s=1200))
        self.assertEqual(_row(res["job_id"])["timeout_s"], 1200)
        self.assertEqual(res["access"]["time_limit_s"], 1200)
        self.assertEqual(res["access"]["time_limit_source"], "this sub-agent's own limit")
        self.assertEqual(thread.call_args.kwargs["args"][3], 1200)
        res2, thread2 = self._dispatch(_agent())
        self.assertEqual(_row(res2["job_id"])["timeout_s"], jobs.DEFAULT_TIMEOUT_S)
        self.assertEqual(res2["access"]["time_limit_source"], "the default")
        self.assertEqual(thread2.call_args.kwargs["args"][3], jobs.DEFAULT_TIMEOUT_S)

    def test_the_agent_is_told_its_time_limit_so_it_can_pace_itself(self):
        text = jobs.access_preamble({"file_write": 0}, False, [], 600)
        self.assertIn("Time limit: 600 seconds for the whole job", text)
        self.assertIn("write results as you go", text)
        self.assertNotIn("Time limit", jobs.access_preamble({"file_write": 0}, False, []))

    def test_the_running_job_receives_its_time_limit_in_its_first_message(self):
        seen = []

        def fake(a, messages, schema, to):
            seen.append(messages[0]["content"])
            return _FINAL
        a = _agent()
        job_id = _insert_job(a)
        with patch.object(jobs, "_call", side_effect=fake), contextlib.redirect_stdout(io.StringIO()):
            jobs._run_job_with_tools(job_id, a, "t", 77, dict(_SESSION))
        self.assertIn("Time limit: 77 seconds", seen[0])

    def test_the_explain_text_states_the_new_limits(self):
        self.assertIn("600 seconds by default", jobs.SUBAGENT_LIMITS_EXPLAIN)
        self.assertIn("can set its own", jobs.SUBAGENT_LIMITS_EXPLAIN)
        self.assertIn(f"at most {jobs.MAX_CONCURRENT_JOBS} jobs run at once", jobs.SUBAGENT_LIMITS_EXPLAIN)

    def test_the_admin_form_and_handlers(self):
        h = RecentProblemsOnTheAdminPage._H()
        a = _agent(timeout_s=450)
        h.subagents_admin_form(_SESSION)
        page = h.sent[0]
        self.assertIn("name=timeout_s", page)
        self.assertIn("value=450", page)
        self.assertIn("450s limit", page)
        self.assertIn(f"placeholder='{jobs.DEFAULT_TIMEOUT_S}'", page)
        h.subagents_limits_post(_SESSION, str(a["id"]), {"tool_call_limit": "50", "tool_byte_limit": "2000000",
                                                         "timeout_s": "1500"})
        self.assertEqual(sub_agents.get(a["id"])["timeout_s"], 1500)
        h.subagents_limits_post(_SESSION, str(a["id"]), {"tool_call_limit": "50", "tool_byte_limit": "2000000",
                                                         "timeout_s": "5"})
        self.assertEqual(sub_agents.get(a["id"])["timeout_s"], 1500)   # rejected, unchanged
        self.assertIn("time limit", h.sent[-1])
        h.subagents_admin_post(_SESSION, {"label": "ui-timeout-agent", "tool_call_limit": "10",
                                          "tool_byte_limit": "2000000", "model_id": "0", "timeout_s": "700"})
        self.assertEqual(sub_agents.get_by_label("ui-timeout-agent")["timeout_s"], 700)


# ── 4. concurrency slots ─────────────────────────────────────────────────
class JobSlots(_JobBase):
    """Several jobs run on real threads, so jobs._call is patched ONCE here and
    dispatches on the agent's label -- patching it per thread would have the
    threads patch over each other."""

    def setUp(self):
        super().setUp()
        self._slots = jobs._JOB_SLOTS
        jobs._JOB_SLOTS = threading.BoundedSemaphore(1)
        self.behaviors = {}
        self._pc = patch.object(jobs, "_call", side_effect=lambda agent, *a: self.behaviors[agent["label"]](*a))
        self._pc.start()
        self._out = contextlib.redirect_stdout(io.StringIO())
        self._out.__enter__()

    def tearDown(self):
        self._out.__exit__(None, None, None)
        self._pc.stop()
        jobs._JOB_SLOTS = self._slots
        super().tearDown()

    def _start(self, agent, fake, timeout_s=30):
        self.behaviors[agent["label"]] = fake
        job_id = _insert_job(agent)
        t = threading.Thread(target=jobs._run_job, args=(job_id, agent, "t", timeout_s, dict(_SESSION)))
        t.start()
        return job_id, t

    def test_a_second_job_waits_queued_with_its_clock_not_running(self):
        release = threading.Event()
        first, t1 = self._start(_agent(), lambda *a: (release.wait(10), _FINAL)[1])
        for _ in range(100):                    # wait until it holds the slot
            if _row(first)["status"] == "running":
                break
            time.sleep(0.02)
        second, t2 = self._start(_agent(), lambda *a: _FINAL, timeout_s=2)
        time.sleep(0.5)
        row = _row(second)
        self.assertEqual(row["status"], "queued")
        self.assertIsNone(row["started_ts"])
        time.sleep(1.8)                         # longer than its own 2s limit, still just waiting
        self.assertEqual(_row(second)["status"], "queued")
        release.set()
        t1.join(10)
        t2.join(10)
        done = _row(second)
        self.assertEqual(done["status"], "done", done["error"])       # the wait did not eat its limit
        ev = json.loads(done["trace"])["events"]
        self.assertEqual(ev[0]["k"], "queued")
        self.assertGreater(ev[0]["ms"], 1500)
        self.assertGreater(jobs.diagnostics(done)["waited_for_a_job_slot_ms"], 1500)

    def test_the_limit_is_how_many_run_at_once(self):
        jobs._JOB_SLOTS = threading.BoundedSemaphore(2)
        running, peak, lock = [0], [0], threading.Lock()

        def fake(*a):
            with lock:
                running[0] += 1
                peak[0] = max(peak[0], running[0])
            time.sleep(0.15)
            with lock:
                running[0] -= 1
            return _FINAL
        started = [self._start(_agent(), fake) for _ in range(6)]
        for _, t in started:
            t.join(15)
        self.assertEqual(peak[0], 2)
        self.assertTrue(all(_row(j)["status"] == "done" for j, _ in started))

    def test_a_failing_job_releases_its_slot(self):
        job_id, t = self._start(_agent(), lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
        t.join(10)
        self.assertEqual(_row(job_id)["status"], "failed")
        self.assertTrue(jobs._JOB_SLOTS.acquire(blocking=False))
        jobs._JOB_SLOTS.release()

    def test_the_slot_is_free_while_the_announcement_runs(self):
        free = []
        self.trigger.side_effect = lambda *a, **k: free.append(
            (jobs._JOB_SLOTS.acquire(blocking=False), jobs._JOB_SLOTS.release())[0])
        job_id, t = self._start(_agent(), lambda *a: _FINAL)
        t.join(10)
        self.assertEqual(free, [True], "an announcement waiting on a busy user must not hold up the next job")

    def test_job_threads_are_marked_for_the_job_pool(self):
        flags = []
        job_id, t = self._start(_agent(), lambda *a: (flags.append(getattr(chat._pool_ctx, "job", False)),
                                                      _FINAL)[1])
        t.join(10)
        self.assertEqual(flags, [True])

    def test_dispatch_says_when_a_job_will_have_to_wait(self):
        a = _agent()
        for _ in range(jobs.MAX_CONCURRENT_JOBS):
            store.write(lambda c: c.execute(
                "INSERT INTO jobs(user_id, sub_agent_id, task, status, created_ts, timeout_s) VALUES (?,?,?,?,?,?)",
                (_UID, a["id"], "t", "running", time.time(), 30)))
        with patch.object(jobs.threading, "Thread"), \
             patch.object(sub_agents, "get_enabled_by_label",
                          side_effect=lambda label: {**sub_agents.get_by_label(label), "model_id": 1}):
            res = jobs._dispatch_impl(_SESSION, a["label"], "do it")
        self.assertIn("already running", res["access"]["queue"])
        self.assertIn("its", res["access"]["queue"])
        store.write(lambda c: c.execute("DELETE FROM jobs WHERE status='running'"))
        with patch.object(jobs.threading, "Thread"), \
             patch.object(sub_agents, "get_enabled_by_label",
                          side_effect=lambda label: {**sub_agents.get_by_label(label), "model_id": 1}):
            res2 = jobs._dispatch_impl(_SESSION, a["label"], "do it")
        self.assertNotIn("queue", res2["access"])


if __name__ == "__main__":
    unittest.main()
