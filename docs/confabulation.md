# The confabulation pattern

This page exists because it should be easy to find, not because it's
comfortable to publish. If you're evaluating whether to trust this
software with something real, this is the most important page in this
folder.

## What it is

Nori runs on a large language model. Like any LLM-based agent, when
asked something whose honest answer requires checking a real source —
a tool's actual result, a job's actual status, a setting's actual
value — she can instead answer fluently with a plausible-sounding
statement that isn't true, rather than checking or admitting she
doesn't know. This isn't a Nori-specific bug and it isn't something
this codebase invented a name for; it's a known property of the
underlying model, sometimes called confabulation or hallucination.
What this page documents is the *specific, real, tracked instances* of
it happening in this app, in production use, and what was actually
done about each one.

## Why it happens

The honest answer, every time this has been caught: it isn't that the
model is being careless. In each real instance, a real, accessible
source existed — a tool result, a job's real status, a setting's real
value, a real page that does or doesn't exist — and nothing in that
turn made checking it feel necessary. Answering fluently without
checking is *cheaper* than checking, in the sense that matters to a
model generating the next token, and nothing about a normal turn
signals that this particular claim is one worth verifying before
saying it.

## Real, tracked instances

Recorded here in generic terms (see the note on naming below), in the
order they were found:

1. **A tool-call failure with no real diagnosis.** Told an image
   generation call had failed, the persona invented a specific,
   plausible-sounding technical reason ("the picture service is
   picky") that had no basis in anything the system actually reported.
2. **A stale reason, reasoned out fresh.** Asked why it had sent a
   particular unprompted message, a persona invented a plausible
   explanation instead of admitting the real reason wasn't visible to
   it anymore — the actual reason existed at send time but wasn't
   recorded anywhere a later turn could look it up.
3. **A stale status, reported as current.** A background job's status
   ("still running") traveled between two connected agents and was
   reported confidently on the receiving side, because nothing
   re-checked whether it was still true by the time it was relayed.
4. **A real capability, misdiagnosed.** Told a photo "didn't come
   through," Nori agreed and invented a specific false cause ("it
   didn't come through on my side") plus a useless remedy ("try
   resending"). The upload had actually succeeded; the real cause was
   an unrelated setting being switched off, reachable via a tool she
   already had and didn't use until told exactly where to look.
5. **A plausible feature, asserted as real.** Nori stated that a
   specific settings page existed before it did — and, separately, did
   not reliably know that a real page she already had existed either.
   Distinct from 1–4: those hid ignorance of a *status*; this one
   *invented a feature* she inferred should exist from her own general
   shape, not from checking what actually does. Notably, this was also
   the second time (after instance 4) that the invented thing turned
   out to be a good idea — see below.

## What actually reduces it

Nothing here makes confabulation structurally impossible — see **What
this doesn't fix**, below. What's been built are real, specific tools
that make the honest answer *cheaper to reach for* than a guess, for
the particular classes of question that have actually gone wrong:

- **`explain_self`** ([Tools](tools.md)) — a tool whose entire purpose
  is "look this up instead of guessing": her real tool list and what
  each is for, how memory/compaction/PACI actually work, her own real
  address, and every real page that currently exists. Every answer
  reads from the real, live source (the tool registry, the page list,
  the environment variable) rather than being a second, hand-written
  copy that could itself drift from the truth.
- **`check_integration_health`** ([Integration health](integration-health.md)) —
  built directly in response to instance 4. Before telling anyone a
  capability isn't working, this reads a real, recently-checked status
  for every outward connection, with a short, pre-written explanation
  of what the status actually means and what would fix it — so even
  once she checks, she isn't left to improvise the sentence.
- **Working-memory metadata** — built directly in response to instance
  2. A self-initiated message now records *why* it happened,
  structurally, alongside the message — so a later turn asked "why did
  you say that" can look the real reason up instead of reasoning out a
  new one.
- **A tool's own purpose text names the trigger.** Where a specific
  failure mode is known, the fix isn't just "give her a tool that
  could answer this" — it's writing that tool's description to say,
  explicitly, *when* to reach for it ("call this BEFORE telling anyone
  a capability isn't working"), because a tool that exists but isn't
  reached for at the right moment doesn't close the gap.

Twice now (instances 4 and 5), the *invented* thing turned out to be a
genuinely good idea — a real toggle worth having, a real page worth
building. That's a real, repeatable signal, not a coincidence: her
guesses about her own design tend to track what a well-designed version
of her would actually have. The practical response, both times, was to
build the real thing rather than only suppress the guess.

## What this doesn't fix

There is no general mechanism in this codebase — and, as far as is
known, none published anywhere — that makes a model check a source
before asserting something, in general, every time. Every fix above is
narrow: built for a specific class of question, after a specific real
instance of getting it wrong. A new capability added to this app
without an equivalent "check first" tool and without that tool's
purpose text naming the trigger is exactly as exposed to this pattern
as everything was before instance 1. If you extend this codebase,
assume this can happen again in your own new code, and read
[Contributing](contributing.md)'s note on it before assuming a new
capability is safe by default.

## A note on how the instances above are described

The real record of these instances (dates, exact wording, which
specific tool or setting was involved) lives in this project's internal
design history, not reproduced here — this page documents the pattern
honestly without requiring a stranger to know this project's own
runtime history to understand it. Nothing about the *shape* of any
instance above is softened or invented for this page; only identifying
specifics are omitted.
