# Contributing

## How to contribute

This project intends to accept contributions through the normal
GitHub flow — issues for bugs and proposals, pull requests for code.
This section is a placeholder until that's set up somewhere with a
real issue tracker — don't take the absence of a link as a dead end;
reach the project through whatever channel led you to this
documentation in the first place. Every commit in a pull request
needs a DCO sign-off — see [Sign-off](#sign-off-dco-not-a-cla) below —
and the whole contribution is offered under this project's own license,
see [Licensing](#licensing) below.

## Running the tests

Each `tests/test_nori_*.py` file is self-contained and meant to be run
directly, as its own process, from inside `tests/`:

```
cd tests
python3 test_nori_backup.py -v
```

Running a file as `python3 -m unittest tests.test_nori_whatever` (or
bulk-discovering the whole directory in one process) is **not**
supported: several files set environment variables and import their
own module dependencies at *import time*, before `unittest` even
starts collecting — correct the first time any given module is
imported in a process, but wrong for every test file after the first
if they all share one process with a module cache. Running each file
as its own script keeps that guarantee.

## Licensing

**Decided (2026-09-17):** this harness (everything in this repository)
is licensed under the **GNU Affero General Public License, version 3 or
any later version (AGPL-3.0-or-later)**. Full, unmodified text: `../LICENSE`.
Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin
Townsend.

AGPL was chosen as the option that actually fits this project's real
shape — a self-hosted web application people run as a service rather
than receive as a copy. Plain GPL's reciprocity obligation triggers on
*distributing* software; running a modified version privately as a
service was never required to share anything under GPL alone (the
so-called "ASP loophole"). AGPL closes exactly that gap: **if you run
a modified version of this harness as a network service that others
interact with, you must offer them the modified source** (AGPL §13).
That's a real, binding obligation, not a suggestion — read the license
itself, not this summary, before relying on it.

**What this means for a contribution:**

- **Every source file carries a short header** pointing
  back to `../LICENSE` (see any existing file for the exact wording).
  A new file you add needs the same header — copy it from an existing
  file rather than reinventing the wording.
- **A pull request is offered under the same AGPL-3.0-or-later terms**
  as the rest of the project — there is no separate contributor
  license or copyright assignment yet (see below).
- **PACI (the protocol this app implements) is a separate specification,
  maintained in its own repository, under its own license** (Creative
  Commons Attribution 4.0 International) — not AGPL, and not part of
  this repository at all. See `NOTICE` at the repository root.

## Sign-off (DCO, not a CLA)

**Decided (2026-09-17):** no CLA (Contributor License Agreement) —
instead, every commit in a pull request needs a **DCO sign-off**. The
DCO (Developer Certificate of Origin 1.1, full text at `../DCO`,
unmodified) is a statement about *provenance* — "I wrote this, or I
have the right to submit it under this project's license" — not a
copyright assignment or a grant of any additional rights beyond the
license itself. That's the whole point of choosing it over a CLA: it
adds a paper trail, not paperwork.

Add it with `git commit -s` (or `--signoff`) — this appends a line
like:

```
Signed-off-by: Jane Doe <jane@example.com>
```

using the name and email from your own `git config`. That line *is*
the certification — reading `../DCO` and putting your real name on
that line is agreeing to what it says, nothing more is asked of you.
Forgot to sign off? `git commit --amend -s` before pushing, or add it
on a later commit — there's no other process around this.

## Extension points

The practical question a contributor actually has: "I want to add
[a tool / a connector / an integration] — where does that go?"

- **A new native tool** — the most common case. Write a function
  taking `session` (and whatever real parameters it needs), register it
  with `tools.register(tools.Tool(name, schema, impl, min_role=...,
  data_scope=..., risk_tier=...))` (see [Tools](tools.md) for the
  registry, the tiers, and `owner_check`). Read [Tools](tools.md) and
  [Content screening](content-screening.md) *before* writing one if it
  touches anything outside this app's own database — see **Disciplines
  that matter**, below.
- **Connecting an existing third-party tool server without writing
  Python** — if the thing you want to connect already speaks MCP
  (Model Context Protocol), no code change is needed at all: connect
  it as an MCP server (see [MCP client](mcp.md)). It lands admin-only
  by default and has to be deliberately widened — a real human
  reviewing a real discovered tool, not a description trusted on its
  own.
- **A new bespoke integration** (something with no existing protocol
  to speak, or where you need finer-grained control than "expose this
  whole tool" — an entity-level allowlist, say) — write a native
  module the way [Home Assistant](home-assistant.md) does. That page's
  own module docstring explains directly why MCP was rejected for that
  specific case (no way to express "expose this ENTITY, not that
  tool" without bespoke code on top of the MCP layer anyway, at which
  point MCP is pure overhead) — read it before assuming MCP is always
  the lighter path; it usually is, but not always.
- **A tool built without touching the Python source at all** — there's
  a live, in-app tool-builder pipeline (draft → static safety check →
  dry run → a typed, explicit approval phrase → enable → widen to
  every household member), admin-only, restart-required to actually go
  live. See [Tools](tools.md#the-tool-builder). Useful for a household
  admin without repo access; a contributor sending a PR should
  generally prefer a real native tool instead, reviewed the normal way.

## Disciplines that matter

These aren't style preferences — each one exists because leaving it out
once already caused a real problem in this codebase's own history.
A pull request that adds a capability without respecting the relevant
one of these should expect to be asked to fix it before it's merged.

- **Go through the dispatch chokepoint.** Every tool call must run
  through `tools.dispatch()` — never call a tool's implementation
  function directly from anywhere else, and never build a second way
  to invoke a capability that bypasses role/scope checking. See
  [Tools](tools.md).
- **Screen untrusted content before it reaches a model with tools.**
  Anything read from outside this app's own control — an email body, a
  fetched web page, a calendar description, device state from Home
  Assistant — goes through `ingest.py`'s reader/actor split before it
  reaches a turn that has tool access. See
  [Content screening](content-screening.md). This is the second real
  security chokepoint in this app, after tool dispatch, and it's easy
  to forget precisely because a new connector's happy path doesn't
  need it to *work* — only to be *safe*.
- **New settings default to secret-excluded, on purpose.** Adding a
  key to `config.py`'s `_SPEC` requires explicitly saying
  `secret=False` before her own `get_settings` tool can read it back
  to her. If you're adding a setting and unsure, leave it excluded —
  that fails safe; the reverse doesn't. See [Settings model](settings.md).
- **Provenance and actor tagging.** Anything a person or the model can
  create, rename, or disable — a category, a tracker type, a memory —
  follows the same established shape: a declared name, disable rather
  than delete, and a history table recording *who* (which actor —
  human or model) did *what*, when. `catalog.py`'s own categories and
  `trackers.py`'s tracker types are the reference implementations of
  this pattern; a new similarly-shaped feature should reuse it, not
  invent a third variant.
- **Say plainly whether a new safety property is enforced or
  conventional.** See [the enforcement model](enforcement-model.md) in
  full — and update that page if what you're adding introduces a new
  boundary worth naming, of either kind.
- **Her own past output is a template.** Anything malformed that gets
  stored and later re-rendered into her prompt becomes something she
  imitates. Any feature that shows her her own earlier words (a
  summary, a search result, a transcript, a digest) must render them
  through `own_output.scrub()`, or render a clearly marked
  `(app note, not part of what you said: ...)` line, never a bracketed
  form she could type herself. `tests/test_nori_own_output.py` fails
  until a new module that reads stored messages is triaged. See
  [Context & tuning](context.md#her-own-past-output-is-a-template).
- **Documentation is part of the change, not a follow-up.** A feature
  isn't finished until the relevant page in this folder reflects it —
  see [the confabulation pattern](confabulation.md)'s closing note:
  assume a new capability is exposed to the same failure mode as
  everything documented there until you've deliberately made checking
  it cheaper than guessing about it.
