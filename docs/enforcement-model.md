# Enforcement model: what's structural, and what's a convention

This app ships open source and makes no promises about what it will or
won't do in someone else's deployment. What this page *does* do is say
plainly, for every safety-relevant property this codebase relies on,
whether that property is actually **enforced** — by this app's own code
structure, or by an external provider's permission system, such that a
bug elsewhere in the codebase couldn't violate it even if it tried — or
is a **convention**: true today because no code path currently does the
thing, not because one couldn't. A convention is one line change away
from no longer holding. An enforced property isn't.

Read this before trusting any one system with something sensitive. Each
system's own page states its enforcement points too; this page is the
index and the shared vocabulary.

## The two kinds of enforcement in this codebase

**Code-enforced** — a structural chokepoint that every real call
actually passes through, checked here once rather than trusted to be
checked correctly at every call site:

- **Tool dispatch** (`tools.dispatch()`, see [Tools](tools.md)) — the
  *only* place a tool call runs. Role (`min_role`) and risk tier are
  checked here, on every call, regardless of which tool or who's
  calling; a tool a session can't use is structurally absent from what
  the model is even told exists (`active_schemas()`), not merely
  refused if attempted. No tool schema is ever allowed to declare a
  `user_id`/`workspace_id` parameter, and dispatch strips one out of
  the model's own arguments if it somehow appeared — an implementation
  reads identity from the session it was given, never from anything
  the model supplied.
- **Content screening** (`ingest.py`, see [Content screening](content-screening.md)) —
  the *only* path untrusted external content (an email body, a web
  page, Home Assistant device state) takes on its way to a model,
  structurally split from anything with tool access — the reading pass
  has no tools at all, not merely "none happen to be given."
- **Secret exclusion** (`config.py`'s `_SPEC`, see [Settings model](settings.md)) —
  deny-by-default: every setting must explicitly declare `secret=False`
  before her own `get_settings` tool can read it. A new setting added
  without thinking about this is excluded automatically, not exposed by
  omission.
- **Peer trust levels** (`peers.py`, see [Peer agents](peer-agents.md)) —
  what a connected peer agent can ask for is checked against a
  per-peer trust setting on every request, not assumed from how the
  connection was set up.
- **Encryption at rest** (`crypto.py`, see [Auth & roles](auth-and-roles.md#secrets-at-rest)) —
  every OAuth token and peer shared-secret is Fernet-encrypted before
  it touches a database column; there's no code path that writes one in
  plaintext.
- **Persona editing is operator-only** (`persona_admin.py`, see
  [The persona editor](persona.md)) — reachable only from the
  administrator-gated settings routes behind the CSRF check, and not
  reachable from any tool: no module that registers a model-callable
  tool imports the persona code, and no setting the settings tool can
  read or write names a prompt. A test parses the source to hold that
  line. The same holds for the context-tuning baseline and history (`tuning_admin.py`), which also live in tables the settings code cannot reach, with a database trigger against deleting a baseline. It is a structural property of what imports what, so a future
  change that widens it fails the suite rather than relying on review.
- **A provider's own permission scope** — when the *provider* (Google,
  Microsoft) enforces a boundary at the token level, no bug in this
  codebase can cross it. Example: Outlook's `Mail.ReadWrite` scope
  genuinely does not include `Mail.Send` — that's a different
  permission, never requested, so a bug that tried to send mail would
  be rejected by Microsoft's own servers regardless of what this code
  does.

**Convention-only** — true because no tool or code path currently does
the thing, not because the underlying access doesn't permit it:

- Most "can't write/delete X" properties for a connected Google/
  Microsoft account. The OAuth scopes requested are usually broader
  than the tools built on top of them (see
  [Google & Microsoft](google-and-microsoft.md)'s own full table) —
  deliberately: the scope is wide enough that adding a write tool later
  is "register a tool," not "request a new grant and rebuild a
  subsystem." Until that tool is registered, the only thing preventing
  the write is that the code to do it doesn't exist yet.
- Home Assistant entity exposure ([Home Assistant](home-assistant.md))
  is enforced in code (a fixed `enabled`/`enabled_for_peers` check
  before any real request), but *which* entities are exposed is purely
  the operator's own choice, re-checked nowhere else — there's no
  external system limiting it the way a Google scope does.
- SharePoint's site reach: `Sites.ReadWrite.All` is genuinely
  tenant-wide at the provider level — every site the connected account
  can see, every SharePoint tool can reach. Nothing in this codebase
  narrows that further; the only real per-site boundary Microsoft
  offers (`Sites.Selected`) was traded away for reaching every site
  without an allowlist. This is the one case in this app where a
  property is **not enforced at all**, at either level — worth naming
  as its own category, not folded into "convention."

## Why the distinction matters more than it sounds like it should

A convention holding today says nothing about whether it holds after
the next change. If you're extending this codebase (see
[Contributing](contributing.md)), treat "no write tool exists for this
scope yet" as an invitation to think about whether adding one is safe
under this document, not as a property you're preserving by leaving it
alone. And if you're deciding whether to connect an integration or a
peer with real access to something sensitive, the question worth
asking about every boundary you're relying on is exactly this one:
enforced, or just nobody's built the thing yet.

## Where the detail actually lives

This page is the index, not the exhaustive table. For the full,
per-provider scope breakdown (which exact Google/Microsoft permission,
what it grants, what's code-only), see
[Google & Microsoft](google-and-microsoft.md#whats-actually-enforced-and-by-whom).
For PACI's own trust-level semantics, see [Peer agents](peer-agents.md).
For the tool risk-tier system in full, see [Tools](tools.md).
