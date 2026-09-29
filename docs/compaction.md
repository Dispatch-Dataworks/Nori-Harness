# Conversation compaction

Source: `nori/compaction.py`. Replaces an earlier, simpler design
("older than N messages simply doesn't exist") with tiered context:
recent messages ride along verbatim; everything older is partitioned
into real conversational sessions and summarized, one segment at a
time, anchored to the exact message-id span it covers.

## The two invariants that actually matter

Both come from a real, observed bug, not from theory:

1. **A segment is never regenerated from a prior segment's own summary
   text** — only ever from the original raw messages it spans. The bug
   this fixes: an earlier rolling-summarizer design (in this project's
   other, separate persona app) fed its own prior output back in as
   input on every cycle, and a real spot-check found it had genuinely
   misattributed a theme from one time window into the next, plus
   boilerplate that survived unchanged across four regenerations
   regardless of what actually happened. Re-deriving every segment
   from its own untouched raw span makes that class of drift
   structurally impossible, not just less likely.
2. **A segment summary is injected inline, chronologically, where the
   messages it replaces used to sit** — never folded into the
   standing, position-zero system prompt. The same lesson [the
   emotion precheck](emotions.md) demonstrated independently: what
   actually gets used by the model is what's positioned right, not
   just worded right. Found twice, in two unrelated systems, which is
   why it's stated as a real, load-bearing rule here rather than a
   stylistic preference.

## Session boundaries

Older messages are partitioned into segments at real time gaps — a
long-enough silence means a different sitting, not more context on the
same one. The threshold (`compaction_session_gap_hours`) is
deliberately looser than [history search](tasks-notes-reminders-trackers.md)'s
own 30-minute gap for "is this the same conversation" — that number was
tuned to avoid contaminating one specific search match's own context, a
tighter bar than "was this a new sitting" actually needs.

## The tunable numbers

Workspace-scoped, live (no restart needed — see
[Settings model](settings.md)): how many raw messages ride along
verbatim before compaction starts (`context_window_msgs`), whether
compaction runs at all (`compaction_enabled`), how many compacted
segments are kept live at once (`compaction_max_segments`), a token
budget for the compacted total (`compaction_budget_tokens`), and the
session-gap threshold above. [Context & tuning](context.md) covers
where these actually land in the assembled prompt and how to see the
real, current effect for your own account before changing anything.

## Working-memory "why," through compaction

A self-initiated message (a proactive ping, a scheduled task firing)
can carry a short, structural note explaining *why* it happened — see
[the confabulation pattern](confabulation.md) for the real bug this
closes. That note is **not** preserved as a separate field once a
message folds into a compacted segment; instead, the summarizer prompt
is instructed to fold a message's "why" into the segment's own prose
rather than silently dropping it, since the note is present in the raw
text the summarizer is given. A deliberate choice over adding a second
raw column: the reason is exactly the kind of fact worth carrying
forward through the same accuracy-preserving path everything else in a
segment already goes through, not a special case bypassing it.

## Failure handling

A summarization call that fails doesn't corrupt or skip anything
silently — the span it would have covered is retried on the next
compaction pass rather than left half-done. `compact_user()` is called
from the same background tick everything else in [the
scheduler](scheduler.md) uses, not a separate timer.
