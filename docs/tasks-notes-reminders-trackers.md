# Tasks, notes, reminders, trackers

Source: `nori/tasks.py`, `nori/notes.py`, `nori/reminders.py`,
`nori/trackers.py`, `nori/catalog.py` (shared category management).
The board itself is `/board` in `server.py`.

## Four types, one pattern

Task, note, reminder, and tracker each get their own table plus their
own event-history table — one object, one append-only log, the same
shape reused four times rather than a generic "item" table with a type
column. What differs is lifecycle:

- **Task** — a due date, priority, an optional recurrence, and a real
  close/reopen lifecycle. Distinct from a `type='task'` [memory](memory.md)
  entry, which is a free-text fact that something task-shaped is worth
  remembering — a task is the structured, board-visible object that
  kind of memory entry always stood in for before this existed.
- **Note** — the simplest of the four: title, body, category. No due
  date, no close lifecycle, and **delete is real** — the one
  deliberate difference from a task, where the equivalent operation is
  close-only. A note's history survives its own deletion (no foreign
  key ties an event row to the note it describes), so the record that
  it existed and what it said isn't erased along with it.
- **Reminder** — presented at a due time, then nagged up to
  `nag_max_count` times (default 3) at `nag_interval_min` intervals
  (default 30) until acknowledged, missed at end-of-day, or exhausted.
  One reminder row persists across every occurrence of a recurring
  reminder — closing or missing one logs an event against the same
  row and advances `next_due_ts` in place, never a fresh row per
  occurrence. See [Scheduler](scheduler.md#reminders-a-nag-loop-not-a-one-shot)
  for the actual firing loop — this module owns storage and state,
  not the tick.
- **Tracker** — not board-visible at all (a time series doesn't fit a
  static card someone glances at once) but reachable from the board
  as its own page. A tracker *type* declares a `value_kind` up front
  (numeric, boolean, or text) and every logged entry is validated
  against what that type promised — never floated on trust. Disabling
  a type is reversible and only blocks new entries; deleting one only
  succeeds when it has zero entries — once anything's been logged
  against it, disable is the only way to retire it. History returns
  both the raw entries and a kind-appropriate summary (sum only makes
  sense for a plain count like ounces of water, never for something
  like a mood scale or blood pressure).

## Categories are user-manageable, not a hardcoded list

`catalog.py` generalizes what started as a fixed five-category tuple
into named category sets either the operator or she can create, rename,
or disable — scoped per item domain (task/note/reminder), seeded
identically today but free to diverge later since each domain's own
rows are independent. A category is validated against the *live* set
at the moment a task or note is created, never against a list frozen
into a tool schema at registration time — the schema takes a plain
string, `is_valid()` checks it at call time, so a category added after
the model's tools were last described is still usable immediately.

## History is provenance, not just an edit log

Every event row is actor-tagged (user / her / a specific peer) — see
[Peer agents](peer-agents.md), which wires all four types into the
standard trust ladder. A field edit is logged as `updated` with a diff;
a pure act with nothing to change but something worth recording (a
reminder nag, a reminder of a task without editing it) gets its own
action label and a free-text note instead. One tool call can produce
either outcome depending on whether anything actually changed —
"reminding" was deliberately never given a fourth verb of its own.

## Extending it

A fifth board item type, or a new tracker value kind, should follow
the existing pattern: one table, one actor-tagged event-history table,
provenance from creation, and a peer-trust registration decision made
deliberately rather than defaulted — see [Contributing](contributing.md).
