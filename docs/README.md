# Nori documentation

Nori is a self-hosted household assistant: a small Python web app (stdlib
only, no framework, one SQLite database) that runs as a persistent chat
companion for the people in one household. This folder documents Nori in
full, for a stranger standing up their own instance — no assumed context,
no assumed familiarity with this project's own history.

If you just want it running, start with **[Quick start](quickstart.md)**.
Everything else here goes deeper once it's up.

## How to read this

- **Orientation** — read these in order if you're new here.
  - [Quick start](quickstart.md) — the fastest path to a running instance and a first reply.
  - [Setup](setup.md) — configuration in full: `.env`, accounts and roles, customizing her persona, what lives where on disk.
  - [Architecture](architecture.md) — the shape of the whole app: the request/turn lifecycle, storage, the tool-dispatch chokepoint, the module map.
  - [Enforcement model](enforcement-model.md) — for every safety property this app claims, whether it's actually enforced (by code or by a provider's own permission system) or just a convention nobody's broken yet. Read this before trusting any of the systems below with something sensitive.
  - [The confabulation pattern](confabulation.md) — an honest account of a real, tracked failure mode: she sometimes states a plausible-sounding thing that isn't true instead of checking or admitting she doesn't know. What causes it, the real instances, what this harness does about it, and what it doesn't fix.
  - [Changelog](changelog.md) — what shipped, in order, at a skim-able grain.
  - [Contributing](contributing.md) — if you want to send code back: how, the extension points, the disciplines a change needs to respect, the project's license (AGPL-3.0-or-later), and the DCO sign-off (`git commit -s`) every commit needs.

- **Systems** — one page per subsystem, each documenting that system in
  full. Read the ones relevant to what you're configuring or extending;
  they cross-link each other and [architecture.md](architecture.md)
  rather than repeat it.

  | System | Covers |
  |---|---|
  | [Memory](memory.md) | Typed long-term memory, reflection, pinning |
  | [Conversation compaction](compaction.md) | How older conversation gets summarized instead of dropped |
  | [Context & tuning](context.md) | What actually rides in the prompt every turn, and the knobs that control it |
  | [The persona editor](persona.md) | Editing who she is from the settings pages, and the ways back from a bad edit |
  | [Tools & the dispatch model](tools.md) | How a capability becomes a callable tool, and the one chokepoint every call goes through |
  | [Content screening](content-screening.md) | The other real security boundary: how untrusted external content (email, calendar, web pages, HA state) is kept from injecting instructions |
  | [The working folder](working-folder.md) | Files she can read, write, and download into, and what's scanned |
  | [Sub-agents](sub-agents.md) | Background jobs run under a separate, limited roster |
  | [MCP client](mcp.md) | Connecting a third-party MCP tool server |
  | [Web search & fetch](web-search-and-fetch.md) | Tavily search and the SSRF-hardened fetch tool |
  | [Home Assistant](home-assistant.md) | Smart-home read/control, entity exposure |
  | [Google & Microsoft](google-and-microsoft.md) | Gmail/Calendar/Contacts/Drive, Outlook/OneDrive/SharePoint |
  | [Peer agents (PACI)](peer-agents.md) | Connecting Nori to another agent as an ongoing peer |
  | [Scheduler](scheduler.md) | The background tick, proactive pings, one-off and recurring scheduled tasks |
  | [Tasks, notes, reminders, trackers](tasks-notes-reminders-trackers.md) | The board and its four item types |
  | [Household planning](household-planning.md) | Inventory and meal planning |
  | [Voice](voice.md) | Speech-to-text input, text-to-speech replies |
  | [Image generation](image-generation.md) | Selfies and imagined images, spend tracking |
  | [Emotions](emotions.md) | The emotional-state system and the precheck injection mechanism |
  | [Backups](backups.md) | What's included, encryption/key management, retention, and the restore procedure |
  | [Integration health](integration-health.md) | The liveness/auth check surfaced on every outward connection |
  | [Settings model](settings.md) | How a setting is declared, scoped, and kept secret-safe by construction |
  | [Auth & roles](auth-and-roles.md) | Accounts, sessions, invites, admin vs. member, secrets-at-rest |
  | [Deployment & supervision](deployment-and-watchdog.md) | Running this for real, `nori_ctl.ps1`, crash recovery |
  | [Compatibility](compatibility.md) | What's promised to survive an upgrade: config keys and the `nori.db` schema, by version type |

## The PACI specification

The full peer-agent wire protocol Nori implements, PACI, is not part
of this repository — it's maintained in its own separate repository,
under its own license (see [NOTICE](../NOTICE)). [Peer agents](peer-agents.md)
here explains what connecting one gets you; the specification itself
lives elsewhere.

## A note on accuracy

Every page here says, where it matters, which real module or constant a
claim is derived from — a tool list, a default value, a scope string.
Prose drifts from code; a file path doesn't. If something in these docs
disagrees with what you see running, the code is correct and this is a
bug in the docs — worth reporting (see [Contributing](contributing.md)).

**A standing rule for anyone changing this codebase, including future
work by an AI agent:** a change to a system documented here isn't done
until the relevant page is updated to match.
