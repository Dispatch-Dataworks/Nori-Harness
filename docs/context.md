# Context & tuning

Source: `nori/context.py`. What's actually in the prompt on any given
turn, and the settings that control how much.

## What's in it

[Architecture](architecture.md#how-the-prompt-is-assembled) has the
full, ordered list — this page is about the *tuning*, not a second
copy of that list (see `context._system_parts()` for the one real
source of the order; it's a short function, read it directly rather
than trust a paraphrase that could drift). In short: a fixed operating
header, persona, tool-usage guidance, her current emotional state, the
always-loaded [memory](memory.md) slice, a sub-agent jobs digest, and
capability summaries for connected MCP servers and peer agents — then,
after the system prompt, any [compacted](compaction.md) older sessions
followed by the live raw message window, oldest first.

## Two things deliberately proximate, not standing

Per-turn nudges — [the emotion precheck](emotions.md#the-precheck-mechanism)
and (when applicable) a duration-tracker digest — are injected as the
*last* message before the model replies, not folded into the standing
system prompt above. Pending content from a connected peer agent
travels the same way: its own late message per peer with something
unread, not string-concatenated into the standing block. Both are real,
measured fixes (see [Architecture](architecture.md) and
[Compaction](compaction.md) for the two independent times this same
lesson was found), not stylistic choices — if you add a new kind of
per-turn nudge, put it here, not in `_system_parts()`.

## Seeing the real, current effect

The context-tuning settings page (admin) shows a **live composition
breakdown** — real measured token counts for the actual assembled
prompt for your own next reply, computed just now, not estimated —
broken down by the same sections `_system_parts()` defines, so the page
can never describe a different set of sections than what's actually
sent. Reload the tab after changing a value to see the real effect.

## The tunable numbers

Workspace-scoped (shared by the whole household — see
[Settings model](settings.md)), live, no restart required:

- `context_window_msgs` — how many of the most recent raw messages
  ride along verbatim.
- `compaction_enabled`, `compaction_max_segments`,
  `compaction_budget_tokens`, `compaction_session_gap_hours` — see
  [Compaction](compaction.md).
- `memory_max_tokens`, `memory_pinned_max_tokens` — separate budgets
  for ordinary memory and pinned memory (see [Memory](memory.md)) —
  kept separate on purpose: a pin is the explicit "this always
  matters" signal and shouldn't have to compete with the ordinary
  budget for space.
- `peer_recent_cap`, `peer_recent_window_hours` — how much recent,
  already-handled peer exchange history rides along as background
  awareness (see [Peer agents](peer-agents.md)) — distinct from
  *pending* peer content, which isn't capped the same way since it's
  unread, not background.

## Baseline and going back

The same three layers as the [persona editor](persona.md#three-layers-and-the-ways-back), for the nine
numbers above (Settings → Administration → Context tuning, under "going back"):

- **Shipped defaults**: the values a fresh install runs with. They never change and are always
  available.
- **Your baseline**: a last-known-good you mark with **Save the current values as my baseline**. It is
  set by that action alone, never by an edit, a reset or a restore, and it carries its date so you can
  see how old it is. It lives in its own database table, apart from the settings themselves, so
  nothing that resets, rewrites or migrates settings can reach it, and the database refuses to delete
  it; only saving a new baseline replaces it.
- **The live values**: freely edited.

Three ways back, each shown as a **preview of exactly which values would change** (old to new) before
anything happens: back to your baseline, back to the shipped defaults, and back one edit (or any
earlier version from the history list). Every change records the values it replaces, so a restore can
be undone. A restore is applied all-or-nothing: if any value in it no longer fits its allowed range,
nothing is changed.

Persona and context tuning are independent: restoring one never touches the other, so a bad persona
edit doesn't cost you good tuning. Only administrators can use any of this, and no tool she has can
reach it.

## Her own past output is a template

Anything malformed that gets *stored* and later *re-rendered* into her prompt becomes an example she
imitates: the model sees its own earlier turns, and copies them. Two real cases in this app's own
history: a bracketed annotation the app once rendered for a photo she had sent
(`[you sent him a photo] with: ...`), which she then wrote out as if it were her reply, and a tool call
typed out as prose (`set_emotion(state="happy")`) instead of being called. There were also app-written
fallback lines ("(hit the 6-step tool-call limit ...)") stored as though she had said them.

So the database keeps exactly what happened, and every place that shows her her own past words goes
through `own_output.scrub()` first: the conversation window, compaction input, `search_history`
results, the memory-reflection prompt, and the recent-peer-exchanges block (what she sent to a peer).
It cuts prose tool calls and the old annotation form, and drops app-written fallback lines. What the
app itself needs to tell her (for example that a picture she generated was sent) is added as a clearly
marked `(app note, not part of what you said: ...)` line, never as a bracketed form she could type.
The same pattern is used to catch a leaked call as it is generated (`chat.py`), so what is caught going
in and what is scrubbed coming back cannot drift apart. `tests/test_nori_own_output.py` fails if a new
module starts reading stored messages without being triaged.

Honest scope: the record of a real leak is kept as it happened, and on the current default model this
exposure did not cause a measurable difference in a 10-trial before/after run; the scrub closes a
deterministic path, it is not a fix for a behaviour that was observed to recur today.

The core trade-off running through all of it, worth internalizing
before tuning any one number in isolation: more raw history or more
summarized history both mean less room for persona and memory, which
is exactly the influence most worth protecting. There's no setting
that gives you more of everything at once.
