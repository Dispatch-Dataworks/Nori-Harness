# Memory

Source: `nori/memory.py` (the only module running raw SQL against
`memory`/`memory_events`).

## Why typed, not one flat list

An earlier design (used in this project's other, separate persona app)
injects one flat facts list into every turn. That doesn't scale: the
whole store rides along on every call regardless of relevance. Nori's
memory instead carries a `type` from a **fixed, code-level taxonomy** —
the model can add new *values* of an existing type, never a new type
itself, so this can't sprawl into an unbounded ad hoc tag system. Only
a small, always-loaded slice rides on every turn; everything else is
available on demand.

## The taxonomy

Nine fixed types, each with a one-line disambiguation the model is
shown (see `TYPE_HELP` in `memory.py`): `identity`, `routine`,
`people`, `email`, `calendar`, `household`, `meals`, `task`,
`preference`. Several of these are deliberately narrow to avoid
duplicating a system that already exists: `household`/`meals`/`task`
memory means a *durable fact* ("recycling is collected Tuesdays," "he
tends to forget dentist appointments unless reminded twice") — never
the live inventory, meal plan, or an actual tracked task with a due
date, which are each their own dedicated system (see
[Household planning](household-planning.md) and
[Tasks, notes, reminders, trackers](tasks-notes-reminders-trackers.md)).
Getting that boundary wrong wouldn't just misfile a fact, it would
duplicate a system that's already there for exactly that.

`identity` and `preference` are **always loaded** — every turn, capped,
same discipline as everything else in [Context & tuning](context.md).
Every other type is only in context when `recall(type=...)` actually
pulls it; a fact stored under `people` doesn't appear on its own just
because it exists.

## The tools

`remember`, `recall`, `forget` (soft — see below), `update_memory`, and
pinning (`set_pinned`) — all member-level, self-scoped. Recall accepts
a type and/or tag filter; search is a plain SQL `LIKE`, not SQLite's
FTS5 extension, deliberately — this keeps the database file portable
(no extension-specific index to lose if it's opened elsewhere) and is
genuinely enough at household scale. Tag filtering happens in Python
after fetching by type/user, same reasoning.

## Pinning

A pinned memory gets **proximate** treatment, not standing-context
treatment — injected as its own line near the end of the prompt (see
[Context & tuning](context.md) and [Emotions](emotions.md)'s precheck
mechanism for why proximity, not just presence, is what makes content
actually get used) rather than folded into the same buried,
position-zero block as `identity`/`preference`. A pin is the explicit
"this always matters" signal; it earns a different position in the
prompt for that reason, not just a flag on the same row.

## Reflection

A periodic, automatic consolidation pass (`reflect(user_id)`), not
triggered by a single message — due after either a message-count
threshold or a real gap in conversation since the user was last active
(`due_for_reflection`; both thresholds configurable via environment
variables, `NORI_REFLECTION_EVERY_MSGS`/`NORI_REFLECTION_GAP_HOURS`,
defaults 25 messages / 18 hours). Reads a real recent window of
conversation and proposes additions/updates to typed memory. **Never
raises on a model or JSON failure** — a failed pass returns an error
result and deliberately does *not* advance its own "last reflected"
timestamp, so the same backlog is retried on the next due cycle rather
than silently dropped.

## Review, not silent deletion

A memory reflection can flag an existing row for review rather than
deleting it outright — `removal_candidates(user_id)` surfaces these on
a settings page for a human to actually decide (`resolve_removal_flag`,
keep or remove), same disable/review-before-destroy discipline as
elsewhere in this codebase (see [Contributing](contributing.md)'s note
on provenance). Nothing in the reflection pass itself deletes a memory
unilaterally.

## Topic-triggered activation — she doesn't have to remember to call `recall`

Instead of relying on her to think to call `recall()`, an incoming
message's own topics are matched against stored memory automatically,
and a strong match is injected into context **before** she decides what
to say or do — the same proximate, end-of-context treatment pins already
get (see [Emotions](emotions.md)'s precheck mechanism for why position,
not wording, is what makes injected content actually used). This fires
at most once per new real message, not once per turn — a scheduler tick
or peer-motivated turn that runs with nothing new said since adds
nothing.

**Matching**: topics are extracted from the message (tokenize, drop
stopwords, stem — see `topic_match.py`) and scored against each memory's
tags and content: a tag hit counts for more than a content hit, so an
exact named entity or category the memory was explicitly tagged with
outranks the same word merely appearing somewhere in the fact's own
text — matching the operator's own framing (exact entity: strong,
multiple tags: stronger, broad category alone: weaker).

**Why not keyword matching alone**: measured, not assumed, against
Nori's own real memory and message history (2026-09-17) before this was
built. A naive keyword scorer recalled 2 of 7 real safety/constraint
memories that should have activated; adding stemming raised that to 5
of 7. The one clean remaining miss was a genuine paraphrase with zero
shared words between the message and the memory's own tags/content ("is
the cage locked" vs a memory that only ever says "enclosure stays
latched") — exactly the failure mode worth worrying about, since a
safety memory that silently fails to fire is worse than not having this
feature at all. **This is the known weak spot of the keyword tier**:
a fact whose wording never overlaps the message that should trigger it
can still be missed, on any tier, if the semantic pass isn't reached. To
close that gap for what matters most, a memory can be flagged
`safety_tier` (the model's own deliberate `safety=true` argument to
`remember`/`update_memory`, or the operator marking one retroactively on
the memory settings tab — **never inferred from content by an
algorithm**) and that tier gets a second, embedding-based semantic pass
(cosine similarity, OpenRouter's `/api/v1/embeddings`) on top of the
keyword one. Ordinary (non-safety) memory relies on keyword+stemming
only — this is an enforcement/convention distinction worth stating
plainly: the safety tier is checked two ways because missing it is
worse, not because the mechanism is a hard guarantee against every
possible paraphrase.

**Before a consequential action**, the semantic pass over the safety
tier runs **unconditionally** — never gated on how confident the keyword
pass was. A confident keyword match can still be the *wrong* memory;
gating the semantic pass on that confidence would let a wrong-but-
confident hit hide the right one, right when it matters most. "Before a
consequential action" means: right after `disable_mcp_connection`,
`enable_mcp_connection`, `ha_control`, `update_settings`, `message_user`,
or a connected peer's own `send`/`act` tool returns its result inside
the same turn — `tools.py`'s `Tool.consequential` is a deliberate,
per-tool flag set only at registration, not inferred from `risk_tier` or
anything else. This can't roll back an action that already ran (these
are single-call, immediately-executed tools) — what it does is make sure
the relevant safety memory is in front of her for whatever she does
next in that same turn (report back plainly, apologize, undo, ask for
confirmation), which is a real but bounded guarantee: convention-level
context, not a code-enforced block on the action itself.

**If the embedding provider is unreachable**, this never silently
degrades to "checked, nothing relevant" — the injected context (or, if
nothing else needs saying, a short standalone note) says plainly that
the safety-tier check is degraded to keyword-only this turn, and a log
line records it. An outage must never look like an all-clear.

## Peers and memory

Memory tools are exposed to a connected peer agent's own requests only
through the peer-action gating described in
[Peer agents](peer-agents.md) — check that page and `register_peer_actions()`
in `memory.py` for exactly which memory operations a peer can request
today, since this is exactly the kind of boundary worth reading
directly rather than trusting a summary.
