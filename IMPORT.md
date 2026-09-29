# Importing an existing installation

Moving an existing, non-Docker Nori install's real data into this
repository's Docker setup. This has been exercised against a real,
accumulated production database (via a safe, read-only SQLite backup
snapshot — never against the live files directly, and never by
stopping the running instance to do it), not just a fresh/synthetic
one — see **What this was actually tested against**, below.

## What has to move, and why

Everything that matters lives in three directories, all **outside**
version control on the old install (check your old install's
`.gitignore` if unsure):

| From your old install | To | Contains |
|---|---|---|
| `nori/data/` | `nori-data` volume, mounted at `/app/data` | `nori.db` (everything: accounts, messages, memory, tasks, connected-account tokens), `secret.key` (decrypts the tokens in that DB), `workfiles/`, `backups/`, `generated/`, `quarantine/` |
| `nori/prompts/` | `nori-prompts` volume, mounted at `/app/prompts` | `persona.md`, `persona.baseline.md` (+`.meta`), `tool-usage.md`, `*-history/` — your customized persona and tool-usage guidance, if you've ever edited either from the defaults |
| `nori/static/avatars/source/`, `nori/static/avatars/thumbs/`, `nori/static/voice/*.wav`/`*.mp3` | `nori-static` volume, mounted at `/app/static` | Uploaded avatar art and any generated voice audio |

**OAuth credentials specifically:** there is no separate handling for
these, on purpose. A connected account's refresh/access token is
stored Fernet-encrypted *inside* `nori.db`'s `connected_accounts`
table, and the only thing that can decrypt it is `secret.key`, sitting
right next to that database in `data/`. Moving `data/` as one unit
moves a decryptable, working credential — moving the database without
`secret.key` (or vice versa) leaves you with encrypted bytes nothing
can read. **Neither file goes in the repo or a build.** They only ever
move directly into the Docker named volumes below, which live on the
host's own Docker storage — never through a `COPY` in the `Dockerfile`
(which would bake them into an image layer, readable by anyone who
gets the image) and never staged inside this checkout (where a stray
`git add -A` could commit them).

Your `.env` (API keys, OAuth client ID/secret pairs, `NORI_BACKUP_KEY`)
moves too, but separately from the above — see step 3.

## Procedure

Stop the old instance first (`nori_ctl.ps1 stop`, or however you run
it) — copying a live SQLite database with an ordinary file copy, while
it may be mid-write, can produce a corrupt copy. (If you can't take
the old instance down yet and need a copy while it keeps running, use
a **read-only SQLite backup**, not a plain file copy — see the note at
the end of this document; that's how this procedure was tested against
real, live data without ever stopping the source.)

0. **If you don't already have a `.env` next to `docker-compose.yml`, copy
   `.env.example` to `.env` first.** `docker compose up` reads `.env` via
   `env_file` and refuses to run at all if it's missing — an unexplained
   error on the very first command otherwise. You'll overwrite it with
   your real values at step 3; this placeholder just gets the first build
   and empty-volume creation working.

1. **Build the image and create the volumes** (no data yet):
   ```
   docker compose build
   docker compose up -d
   docker compose down
   ```
   This creates the three empty named volumes and populates
   `nori-prompts`/`nori-static` with the shipped defaults (Docker
   copies a named volume's initial content from the image the first
   time it's used) — you're about to overwrite the ones you actually
   customized.

2. **Copy the three directories into the volumes.** Find each
   volume's real path on disk with `docker volume inspect
   <project>_nori-data` (etc.) — `Mountpoint` in the output — then copy
   your old install's directories into it, or use a throwaway
   container as the copy tool if your old install and Docker aren't on
   the same filesystem path space:
   ```
   docker run --rm -v <project>_nori-data:/dst -v /path/to/old/nori/data:/src:ro alpine cp -a /src/. /dst/
   docker run --rm -v <project>_nori-prompts:/dst -v /path/to/old/nori/prompts:/src:ro alpine cp -a /src/. /dst/
   docker run --rm -v <project>_nori-static:/dst -v /path/to/old/nori/static:/src:ro alpine cp -a /src/. /dst/
   ```
   **Running these from Git Bash on Windows (not PowerShell/cmd):** prefix
   each command with `MSYS_NO_PATHCONV=1`, or Git Bash silently rewrites
   the `/dst`, `/src`, and similar in-container paths into Windows paths
   before Docker ever sees them, and the copy either fails oddly or lands
   somewhere unexpected. PowerShell and cmd don't do this rewriting; only
   Git Bash's own path-conversion needs the override.

3. **Copy your `.env` values.** Copy your old install's `nori.env`
   (wherever your old `ENV_PATH` pointed it — see that install's own
   `docs/setup.md`) to `.env` next to this repo's `docker-compose.yml`.
   `docker-compose.yml` reads it via `env_file`; nothing about it goes
   into the image or a volume.

4. **Start it and verify for real** — not just that it starts, that
   your actual data is there:
   ```
   docker compose up -d
   curl http://localhost:8877/healthz
   ```
   Log in with an existing account (not the first-run setup screen —
   seeing that instead means the database didn't actually import).
   Check a connected account still shows connected on the integrations
   page (proves the OAuth token decrypted correctly with the imported
   `secret.key`), and that your persona wasn't reset to the default.

## Dry run: rehearsing this safely before committing

You don't have to trust this procedure on faith, and you don't have to
risk your only working install to try it. The whole thing above can be
rehearsed against a **disposable copy**, on a **scratch port**, in
**throwaway volumes**, while your real instance keeps running,
undisturbed, the entire time. This is exactly how the procedure above
was proven to work — not assumed, run for real against a year-plus of
accumulated production data — and it costs nothing to do the same
before trusting it with your own.

**The four things that make it a rehearsal, not the real thing:**

1. **A read-only copy of your data, never the live files.** Take
   `nori.db` via the SQLite backup API (see the note at the end of this
   document) while your real instance keeps running — never a plain
   file copy of a database that might be mid-write. Everything else
   (`secret.key`, `workfiles/`, `prompts/`, the avatar/voice files) is
   safe to copy directly, live, per the reasoning above.

2. **An isolated compose project name**, so the rehearsal's containers,
   network, and volumes never collide with a real deployment's:
   ```
   docker compose -p nori-dryrun build
   docker compose -p nori-dryrun up -d
   docker compose -p nori-dryrun down
   ```
   Every volume this creates is named `nori-dryrun_nori-data` etc. —
   distinct from a real `nori_nori-data`, and easy to confirm with
   `docker volume ls --filter name=nori-dryrun` before you delete
   anything.

3. **A scratch host port, if your real instance is still running.** The
   shipped `docker-compose.yml` binds host port 8877 — the same port a
   real running instance already owns, so starting the rehearsal
   without changing this **will fail outright** (Docker refuses to bind
   a port already in use, loudly, before anything starts — it will not
   silently share or steal it). Override it with a
   `docker-compose.override.yml` next to (not committed with, and not
   part of) your checkout:
   ```yaml
   services:
     nori:
       ports: !override
         - "127.0.0.1:18877:8877"
   ```
   (The `!override` tag matters — without it, Compose *adds* this port
   mapping alongside the original 8877 one rather than replacing it,
   and you're back to the same conflict.) Delete this file once the
   rehearsal is done; it has no place in a real deployment.

4. **Full teardown afterward** — this is disposable, so leave nothing
   behind:
   ```
   docker compose -p nori-dryrun down -v
   docker rmi nori-dryrun-nori
   ```
   plus deleting your scratch copy of the data and the scratch `.env`
   you made for it. Confirm your real instance was never touched: check
   its process is still the one you started with, its own port still
   answers, and the *original* data files' modification times only ever
   moved from its own real activity, never from anything you just ran.

Steps 1–4 above run exactly as written against this rehearsal setup —
nothing about the procedure itself changes, only the project name, the
port, and the fact that everything gets deleted afterward rather than
kept.

## What this was actually tested against

This procedure was verified against a real copy of a year-plus of
accumulated production data — not a fresh database, not a synthetic
fixture — taken with SQLite's own online backup API
(`sqlite3.connect("file:<path>?mode=ro", uri=True).backup(dest)`)
against the live database file while its real server kept running
throughout, then pointed at with `NORI_DATA_DIR`/`NORI_PROMPTS_DIR`
(the same pair of environment variables that gate a throwaway/test
instance from ever touching live data by accident — see
`docs/deployment-and-watchdog.md`). Confirmed against that copy:
`store.init()` opens the real schema with real rows (real user
accounts, thousands of real messages, multiple real connected
accounts); the real, customized persona (not the shipped default) and
its baseline both load correctly through `promptdoc.PromptDoc`; and a
real Gmail connected account's encrypted refresh token decrypted
successfully with the copied `secret.key`, proving the credential
travels as a working, usable credential and not just as opaque bytes.
The temporary copy used for this test was deleted immediately after —
it existed only long enough to prove the procedure above, not as a
standing copy of anyone's real data.

If you need to do the same — pull a safe copy while the source stays
running, rather than stopping it — use that same backup-API approach
for `nori.db` specifically (it is the one file actually unsafe to copy
live); `secret.key`, `prompts/`, and `static/` are static files, not
live database connections, so an ordinary file copy of those is fine
even while the old instance is running.
