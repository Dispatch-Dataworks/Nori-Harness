# Setup

Configuration in full, once [Quick start](quickstart.md) has you
running. Every setting mentioned here has a comment in the real source
file explaining it — this page is oriented around *what you're trying
to do*, not a re-listing of every line.

## Python dependencies

Nori is stdlib-only for everything *except* two real, required
packages: `pip install cryptography` (secrets-at-rest — see
[Auth & roles](auth-and-roles.md#secrets-at-rest)) and
`pip install tzdata` (real IANA timezone data — needed on Windows and
anywhere else without a built-in tz database; per-user local time
depends on it). `Pillow` is optional, for photo-grid thumbnails only —
without it, `/photos` serves full-size originals as grid tiles
instead, silently, no missing-feature error.

## Environment variables (`.env`)

`nori.env` lives **outside the repo entirely** — a sibling directory
named `<repo-folder-name>-env`, next to the repo, not inside it.
That's deliberate, not incidental: it means a real key is never one
`.gitignore` mistake away from being committed. `server.py`'s own
`ENV_PATH` constant derives this path from the repo's own location, so
it's portable across machines rather than hardcoded. (Running this
alongside another one of your own apps on the same host? Give it its
own equivalent env file in its own directory — nothing here is meant
to be shared between unrelated apps.) Running in Docker instead? See
[Deployment & supervision](deployment-and-watchdog.md) — the container
takes its environment from `docker-compose.yml`/an env file passed to
`docker compose`, not this sibling-directory convention.

The authoritative, always-current list of keys is `.env.example` (repo
root) — copy it to `nori.env` in that sibling directory and fill in
what you need. It's organized into the same sections as the settings below:

- **Server** — port, bind host, cookie security.
- **Model provider** — your OpenRouter key, default model/reasoning
  effort, timeouts.
- **Voice layer** — a separate OpenAI key, only needed if you turn
  voice on (see [Voice](voice.md)).
- **Connected accounts** — Google/Microsoft credential pairs (see
  [Google & Microsoft](google-and-microsoft.md) for the full Cloud-
  console walkthrough), Home Assistant's URL and token (see
  [Home Assistant](home-assistant.md)).
- **Tool-calling / rate limits** — per-call and per-window caps.
- **Web search / fetch** — a Tavily key (see
  [Web search & fetch](web-search-and-fetch.md)).
- **Working folder** — where per-user files live (see
  [The working folder](working-folder.md)).
- **Testing isolation** — `NORI_DATA_DIR`/`NORI_PROMPTS_DIR`/
  `NORI_NO_LOGFILE`, for a throwaway instance. Never point these at
  data you'd mind losing; a real deployment leaves them unset.

`NORI_PUBLIC_URL`, once set, is what she can tell you her own address
is if asked directly, and is required for any OAuth-based integration
(Google/Microsoft) to build a working redirect URI at all.

**An env-file change needs a restart to take effect.** Every key is
read once into the process's own memory at startup — editing
`nori.env` on disk does nothing to an already-running instance. See
[Deployment & supervision](deployment-and-watchdog.md) for restarting.

## Accounts, roles, and inviting the household

The first account created on a fresh instance becomes the workspace
admin automatically ([Auth & roles](auth-and-roles.md)). From
**Settings → Household** (admin only), invite the rest of the
household — each invite generates a one-time link; whoever opens it
sets their own password, never shared with or visible to the admin. A
member can do everything day-to-day (chat, tasks, the working folder,
their own memory/schedules); admin-only surfaces are the ones that
affect the whole instance or another integration's credentials — see
[Auth & roles](auth-and-roles.md) for the exact boundary.

## Customizing her

**Persona** (character/voice) and **guidance** (tool-usage mechanics,
kept deliberately separate — see [Architecture](architecture.md)) are
each a small versioned text file. The persona has an editor in the
settings pages — **Settings → Administration → Persona** — with
validation, a version history, a baseline of your own, and a reset to
the shipped default; see [The persona editor](persona.md). A fresh
install works with no editing at all: the shipped default is written
to suit anyone, and ends with a short section telling her that the
specifics of your relationship are yours to add.

Guidance has the same versioned-file mechanics (`guidance.py`) but no
settings page yet: to change it, edit `prompts/tool-usage.md` on disk,
or see [Contributing](contributing.md) if you would like to build its
editor. The persona editor is a small, self-contained module
(`persona_admin.py`) that is a good template for that.

## What lives where on disk

Under your configured data directory (`NORI_DATA_DIR`, or a default
`data/` next to the code if unset — see
[Deployment](deployment-and-watchdog.md)):

- `nori.db` — the one SQLite database (see [Architecture](architecture.md)).
- `secret.key` — the encryption key for everything Fernet-encrypted at
  rest (see [Auth & roles](auth-and-roles.md#secrets-at-rest)). **Back
  this up** — see [Backups](backups.md); losing it makes every stored
  OAuth token and peer secret permanently unreadable, not just
  inconvenient to replace.
- `generated/` — images she's made (see [Image generation](image-generation.md)).
- `prompts/` — the persona/guidance text files above, plus their edit
  history.
- `workfiles/` — the per-user working folder (see
  [The working folder](working-folder.md)).

## Where to go from here

Every integration is optional and off until you connect it — pick the
page for whichever one you actually want: [Home Assistant](home-assistant.md),
[Google & Microsoft](google-and-microsoft.md),
[Web search & fetch](web-search-and-fetch.md),
[MCP client](mcp.md) for a third-party tool server, or
[Peer agents](peer-agents.md) to connect another running agent.
