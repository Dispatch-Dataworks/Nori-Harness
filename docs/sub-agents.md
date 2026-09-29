# Sub-agents

Source: `nori/sub_agents.py` (the configured roster) and `nori/jobs.py`
(dispatching and running a job).

## What a sub-agent is

A background job, run under a **separate, deliberately limited**
model/tool roster — not Nori herself doing something in the
background, a different, sandboxed agent she can hand a bounded task
to. Configured per label (admin only): its own model, base URL, and
API key (blank uses the operator's own default OpenRouter key), plus
two real limits — a tool-call cap per job (0 to 1000, 0 meaning a
plain completion with no tools at all) and a cumulative byte cap across
every tool result in that job (10KB–50MB, default 2MB — roughly ten
average file reads). Both are checked before dispatch, not after.

## What a job can and can't do, precisely

Read-only by default: only `list_files`, `read_file` (summary-only —
see below), and `search_files` are available — no writes, no web, no
email, no peers, no other tool of any kind. A task description is
capped at a fixed character limit. The whole job has **one hard
wall-clock deadline** with no retry or resumption past it — a job that
times out is done, not queued for another attempt.

## The one deliberate, gated hole — and why it's actually safe

A sub-agent's own `read_file` **always** calls the underlying function
with `preserve_content=False` — a summary, never raw content, no
matter what the job asks for. That boundary holds regardless of
anything below.

Separately, and only when a job is explicitly dispatched with
`raw_file_access=True`, one additional tool (`read_file_full`) is
made available — **hand-built, and deliberately never passed through
`tools.register()`**, so it can never appear in the main tool registry
and a live chat turn can never obtain it, regardless of session or
role. It exists only inside that one job's own offered tool schema.

**Why granting raw content to a sandboxed sub-agent is safe on its
own:** the caller reading it is read-only and side-effect-free by
construction — no email, no peers, no web, no write tool of any kind —
so reading untrusted content into that specific model can't be
leveraged into a real action by that model itself.

**Why that alone doesn't make the whole path closed:** the sub-agent's
own final text — which could echo raw file content verbatim, including
anything adversarial embedded in it — flows back into **Nori's own**
context once the job finishes, and she has real tools. That's exactly
why the job's result is screened (the same [content-screening](content-screening.md)
discipline used everywhere else untrusted content crosses into a
tool-capable turn) before it ever reaches her prompt — the second
layer this design actually depends on. Removing that screening step
would reopen the exact gap `raw_file_access` is built to keep
contained. If you're touching either `read_file_full` or the result
hand-off in `jobs.py`, both halves have to move together.

## Crash recovery

A job interrupted by a server restart is swept and marked
**interrupted**, not silently left showing "still running" forever —
`sweep_orphaned()` runs once at startup. `recently_interrupted()` and
`running_jobs()` back the admin page that shows this — a stale-looking
row here is worth a look, but a restart doesn't leave one sitting
indefinitely by itself. This is also the fix for confabulation instance
3 in [the confabulation pattern](confabulation.md) — a stale "still
running" status relayed confidently is exactly the failure this sweep
exists to prevent from persisting.
