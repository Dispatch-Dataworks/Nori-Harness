# Deployment & supervision

For getting a first instance running at all, see
[Quick start](quickstart.md) and [Setup](setup.md); this page covers
running it for real, long-term, unattended.

## Docker (recommended)

The simplest way to keep this running unattended: the container
runtime's own restart policy replaces everything below. See the
`Dockerfile` and `docker-compose.yml` at the repository root, and
[Importing existing data](../IMPORT.md) if you're moving an existing
non-Docker install in.

```
docker compose up -d
```

`restart: unless-stopped` in `docker-compose.yml` means Docker itself
brings the container back after a crash or a host reboot — there is no
separate watchdog to configure, and none of the SYSTEM-vs-interactive-
user site-packages problem described below applies, because a
container image installs its own dependencies into its own,
consistent environment. Check health with:

```
docker compose logs -f
curl http://localhost:8877/healthz
```

## Upgrading

Take a backup first (see [Backups](backups.md)) — not because this
procedure is expected to fail, but because "I can get back to where I
was" should be true before changing anything that touches real data,
not assumed. See [Compatibility](compatibility.md) for what's actually
promised to survive an upgrade (config keys, the database schema) and
what a version number means for whether something's allowed to break.

**Docker:**
```
git pull
docker compose build
docker compose up -d
```
The three named volumes (`nori-data`, `nori-prompts`, `nori-static`)
are untouched by a rebuild — they're not part of the image, so a newer
image attaches to the exact same data a fresh `docker compose up -d`
would otherwise create empty. Schema migrations run automatically on
every startup (`store.init()`, called from `server.py`'s own `main()`)
against whatever
database is already there — this isn't a separate step you have to
remember or trigger; the same code path that sets up a brand-new
database also brings an existing one forward, every time the process
starts. `docker compose logs -f` shows the startup; look for the
server's own "on http://0.0.0.0:8877" line, then `curl
http://localhost:8877/healthz`.

**Without Docker (`nori_ctl.ps1`):**
```
git pull
pwsh nori_ctl.ps1 restart
```
Same migration behavior — `store.init()` runs at process start
regardless of how the process was launched. `data/`, `prompts/`, and
`static/` live on disk under this checkout (or wherever `NORI_DATA_DIR`
points), not inside anything `git pull` touches, so pulling new source
doesn't disturb them either.

**What you should see:** a normal startup (the same health check as
any other restart — `nori_ctl.ps1 status`, or `/healthz` for Docker),
your existing account and data intact on login, and nothing resembling
the first-run setup screen (seeing that instead means the database
didn't actually carry over — see [Importing existing
data](../IMPORT.md)'s own troubleshooting for what that looks like and
why).

**If it goes wrong:** stop, and restore the backup you just took
before troubleshooting further ([Backups](backups.md) has the exact
restore command) — a database that failed to come up correctly after
a migration is not a database to keep experimenting against. Then
report what broke: which version you upgraded from, what the logs
showed, and at which of the steps above it diverged.

## Running it directly (no Docker): `nori_ctl.ps1`

```
pwsh nori_ctl.ps1 start
pwsh nori_ctl.ps1 stop
pwsh nori_ctl.ps1 status
pwsh nori_ctl.ps1 restart
pwsh nori_ctl.ps1 ensure     # start/restart the server if unhealthy
```

The server runs as a detached background process that survives
closing the terminal that launched it — `start` isn't a foreground
command you leave a window open for.

**`NORI_LIVE=1` is set in exactly one place: the line in this script
that actually launches the real server.** `store.py` refuses to open a
data directory at all without either that variable or `NORI_DATA_DIR`
set — the guard that keeps a throwaway test run from ever touching
real, live data by accident. If you're scripting your own launch
instead of using this control script, that variable (or a real,
intentional `NORI_DATA_DIR`) has to be set explicitly; nothing here
defaults to live data quietly.

### A known limitation of this path (why Docker is recommended instead)

On Windows, launching this from a SYSTEM-context Scheduled Task (rather
than a logged-in session) can fail at import: if `cryptography` was
installed into a specific user's own per-user site-packages rather
than a system-wide location, SYSTEM's Python doesn't see it, and the
process dies before it ever binds a socket. This is a property of how
Python dependencies were installed on that specific host, not of this
app — a container's own dependency install doesn't have an
"interactive user vs. SYSTEM" split at all, which is the real reason
Docker is the recommended path for anything unattended.

If you hit this: install dependencies into a system-wide location, or
run the scheduled task under a specific logged-in user's account
instead of SYSTEM, or switch to Docker.

### Real reboot-survival without Docker

For unattended, long-term operation on Windows without a container,
register a genuine Scheduled Task — a real OS-level process, never a
child of anything this repo starts — that runs `nori_ctl.ps1 ensure`
on a short interval (a 60-second repetition has no real overhead: one
short-lived process per tick, a fast no-op when healthy). On Linux
(including WSL), the equivalent is a `systemd` unit with
`Restart=always` calling the same `nori_ctl.ps1`/`server.py` entry
points.

**Run exactly one supervisor.** Two independent supervisors calling
`ensure`/`stop`/`start` on the same instance, uncoordinated, can each
force-kill whatever the other just started — a real, sustained
crash-loop was traced to exactly this in this project's own history,
not a hypothetical. If you add a Scheduled Task or systemd unit, don't
also run an in-process watchdog alongside it for the same instance.

## What "healthy" means

A **listening socket on the port and a real `/healthz` 200** — never
just "a process exists." A process that's alive but never finished
starting up, or one that's been killed and left a stale pidfile behind,
is not healthy.

- **`start` / `restart`** wait (up to `NORI_START_TIMEOUT_S`, default
  90) for the server to actually serve. Exit `0` only when it does;
  exit `1` when the process died, or stayed alive without ever serving.
  A dead launch removes its stale pidfile.
- **`ensure`** exits `0` only if the server is serving *afterwards*;
  `1` if the restart didn't bring it up; `2` in crash-loop backoff; `3`
  if it couldn't get the control lock.
- **`.ctl.lock`** serialises start/stop/ensure across callers, so an
  overlapping automated and manual invocation can't kill what the
  other just started.
- **`boot.py`** launches `server.py` in-process (same PID) and writes
  any import-time crash to `.nori.boot.err` — `server.py` only
  redirects its own logs after every import, so a crash before that
  point would otherwise vanish with no trace.

**Where the record is (readable without elevation):**
`.supervision.jsonl` (one JSON event per start/ensure/sweep, with
caller, before-state, reason, crash text; bounded), `.nori.boot.err`,
`.nori.log`/`.nori.err` (every line stamped with its own timestamp).
The same events appear in the settings page's "why was there no reply
— or why was it down?" timeline (see [Peer agents](peer-agents.md)). A
healthy no-op sweep writes nothing, so the record isn't drowned.

## A real bug this project found: don't trust a bare PID

A pidfile check that trusts a bare recorded PID with no cross-check can
be fooled after a reboot: the OS can reuse that exact PID for a
completely unrelated process, and a naive `status`/`ensure` reads that
as "still running," masking a real outage. The fix: record each
process's own start-time alongside its PID and trust a match only when
both agree. A related gap: not checking whether something else already
has the port before launching can let two server processes both bind
and listen on the same port at once instead of the second one being
refused. Both are fixed in `nori_ctl.ps1`; if you're scripting your own
launch/supervision instead of using it, both are worth replicating.

## Before restarting a live instance, check for an in-flight turn

`nori_ctl.ps1 restart` (and `docker compose restart`) stops the server
unconditionally — neither waits for or checks an in-progress
conversation turn. Restarting a server actively answering someone
mid-turn drops that turn's own response. For anything other than an
automated crash-recovery restart (where there's nothing graceful to
wait for anyway, since the server already isn't healthy), check the
server's own log for recent activity before restarting by hand.

## After a restart, verify for real

`/healthz` returning 200 means the process is up and answering HTTP —
it does not by itself confirm every configured integration still
works. Beyond process-level health, the
[Integration health](integration-health.md) page is the real answer to
"is everything actually connected," not an assumption from the process
being alive.
