# Scheduler

Source: `nori/scheduler.py` (the background tick), `nori/schedules.py`
(one-off and recurring scheduled tasks), `nori/reminders.py` (the nag
loop).

## One background thread, one tick, many jobs

A single daemon thread loops every `NORI_SCHEDULER_INTERVAL_MIN`
(default 15) minutes and, per active user, per cycle: checks memory
reflection, checks conversation compaction, fires any due scheduled
task, fires any due reminder, and — only if none of the household-
signal gates below say otherwise — checks for a proactive ping worth
sending. Instance-wide work (backups, integration health checks) is
also folded into this same tick rather than getting its own timer.

## Three different kinds of "she speaks up unprompted," three different gates

**Proactive pings** (household-signal, e.g. inventory or meal
planning noticing something worth mentioning) are the only kind
actually gated by all three of:

- `ping_enabled` — off entirely for this user, or not.
- **Quiet hours** (`ping_window_start`/`ping_window_end`) — computed in
  *that user's own configured timezone*, not the server's — a real bug
  once made this the server process's own local hour instead.
- **A minimum gap since real activity** (`ping_min_gap_min`) — don't
  interrupt someone who was just talking to her.

Only once all three pass does the cycle ask `outstanding_reason(user_id)`
— every registered signal provider in turn, first non-`None` reason
wins — and only a real reason produces an actual turn.

**Scheduled tasks and reminders are deliberately *not* gated by any of
the three above.** An explicit instruction the operator set up to fire
at a specific time isn't a household-signal ping asking "is now a good
moment to interrupt" — it's a due obligation, checked and fired
regardless of the hour or how recently he was active. Quiet hours
still apply somewhere, just not here: `notify_quiet_start`/`_end`
gates only whether the browser shows a local push notification for the
resulting message, never whether the turn runs or the message is
persisted.

## Required-tool preflight — closing a confabulation shape by construction

Before a scheduled task's turn ever starts, `_fire_schedule` checks
whether that task's `required_tool` is actually available and enabled
for this account. A miss skips the turn entirely and records a plain,
honest "skipped — tool unavailable" outcome. This exists specifically
so she never wakes up mid-task, discovers the tool she needs isn't
there, and has to reason her way through that gap in real time — which
is exactly the shape [confabulation](confabulation.md) takes when a
real failure isn't caught before it reaches the model. Checking first
means there's no gap for a fluent, wrong explanation to fill.

## Why every fire routes through `turns.run()`, not `chat.run()` directly

A proactive ping, a scheduled task, and a reminder nag all define a
`_first` (the actual proactive/scheduled turn) and a `_sweep` (what
runs instead if a real user message arrives while this held the
account's turn lock — answered as an ordinary reply, never with the
proactive/scheduled framing). Calling `chat.run()` directly once let a
tick run fully concurrently with a real live turn for the same
account — a genuine collision, found live. `turns.run()` is what
prevents that; see [Architecture](architecture.md) for the general
per-account locking model this leans on.

A tick that finds the account's turn lock already held is never
retried separately — the schedule or reminder's own `next_due_ts`/nag
state is left untouched, so the very next cycle (or, for a schedule,
the next scheduler pass, since the due time is still in the past)
picks up the identical due item on its own, natural cadence. No
separate retry queue exists because none is needed.

## Reminders: a nag loop, not a one-shot

A reminder that's still open when its next nag interval elapses fires
again, up to `nag_max_count` times per day, each attempt worded a
little more insistent than the last (`"contact 2 of 3"`, and so on) —
still in character, never scolding. Past the end of the due day with
no acknowledgment, it's marked **missed**, silently — no turn fires
just to announce a miss; that would spend a real turn on pure
bookkeeping, the opposite of what the nag budget exists to bound. The
model is told explicitly to call `reminder_close` once the operator
confirms it's handled — closing it is a real action, not implied by
merely mentioning it.

## Why a self-initiated message says why

Every proactive/scheduled/reminder turn sets a marker
(`_schedule_context`, or the plain proactive reason) that widens
`message_user`'s own ownership check and, on the stored message
itself, records the real reason it was sent — the same working-memory
mechanism [Peer agents](peer-agents.md#why-a-self-initiated-message-records-its-own-reason)
uses for peer-triggered messages, applied here to every other kind of
self-initiated speech. The point is the same one made there: a later
turn asked "why did you say that" should find the real answer, not
reconstruct a plausible-sounding one.

## Extending it

A new household-signal source registers a function with
`scheduler.register_signal(fn)` at import time — `fn(user_id)` returns
a short reason string or `None`; one provider's own exception is
caught and skipped so it can never take the whole cycle down. See
[Household planning](household-planning.md) for the first two real
signal providers (inventory, meal planning).
