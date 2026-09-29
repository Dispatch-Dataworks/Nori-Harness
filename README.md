# Nori

A self-hosted household assistant: a single Python process (stdlib
`http.server`, no framework), one SQLite database, no other services
required to start. Bring your own [OpenRouter](https://openrouter.ai/keys)
key and run her for your own household — memory, tasks/notes/reminders,
a working folder, and (once you connect them) email/calendar, smart-home
control, and web search, all through one persistent chat.

![Nori](Nori_character_sheet.png)

## Running it

**Docker (recommended):**

```
git clone https://github.com/Dispatch-Dataworks/Nori-Harness.git nori
cd nori
cp .env.example .env   # fill in OPENROUTER_API_KEY at minimum
docker compose up -d
```

Open `http://localhost:8877` — the first account you create becomes
the household's admin.

**Exposing this to the internet.** `docker-compose.yml` publishes the port
loopback-only (`127.0.0.1:8877:8877`) by default, on purpose: the first
account created on a fresh instance becomes admin automatically, with
nothing else gating that, so publishing to every network interface before
that account exists would mean whoever reaches the port first — on
whatever network can reach it — owns the instance. The deliberate
sequence:

1. Run it loopback-only (the default above) and create the admin account
   over `http://localhost:8877` from the machine it's running on.
2. Only then, if you want it reachable from elsewhere, put it behind your
   own reverse proxy or tunnel (a Cloudflare Tunnel, Tailscale, nginx with
   TLS — whatever you already trust) and set `NORI_PUBLIC_URL` in `.env`
   to that hostname. Do not simply change the compose port mapping to
   `0.0.0.0` — that publishes the raw HTTP port with no TLS and no
   additional access control, straight to whatever network can reach the
   host.

**What shows up on disk, and what matters for backup:** `docker-compose.yml` mounts three named
volumes. `nori-data` gets `nori.db` (created immediately, at first boot) and `secret.key` (created
the first time anything needs it — a connected account, certain settings overrides; a brand-new
instance may not have one yet) — **back these two up together, always**, since `secret.key` is what
decrypts everything sensitive `nori.db` holds (see [What's genuinely hard](#whats-genuinely-hard-about-self-hosting-this)
below). `nori-prompts` and `nori-static` start populated with the image's own shipped defaults
(Docker's own behavior for a fresh named volume) — `nori-prompts` only gains a real `persona.md` once
you actually edit the persona; until then she runs on the shipped default.

**Without Docker:** see [Quick start](docs/quickstart.md).

**Bringing an existing install's data in?** See [IMPORT.md](IMPORT.md).

## What's genuinely hard about self-hosting this

- **The OAuth token is the actual credential, not the database
  password.** A connected Google/Microsoft account's refresh token is
  encrypted at rest, but the key that decrypts it (`secret.key`) sits
  right next to the database. Losing either one independently is
  recoverable in different ways (re-auth the account, or nothing
  decrypts); losing both together means starting over. Back both up
  together, always — see [Backups](docs/backups.md).
- **A self-hosted assistant with tool access is a real security
  surface**, not a toy. Read [The enforcement model](docs/enforcement-model.md)
  before connecting anything to real accounts — it says plainly, for
  every safety property this app claims, whether it's actually
  enforced in code or just a convention nobody's broken yet.
- **She sometimes states a plausible-sounding thing that isn't true**,
  instead of checking or admitting she doesn't know. This is tracked,
  not hidden — see [The confabulation pattern](docs/confabulation.md)
  for what causes it and what this harness does (and doesn't) do about
  it.
- **Unattended operation needs exactly one supervisor.** Docker's own
  restart policy is that supervisor if you use Docker; if you don't,
  see [Deployment & supervision](docs/deployment-and-watchdog.md) for
  a real, traced incident about what happens when two run at once.

## Admin pages

Once you've created the first (admin) account: Settings →
Administration has the persona editor, context tuning, integration
health, backups, and every per-household limit and toggle. Settings →
Peers is where you'd connect another PACI-speaking agent, if you have
one — see the PACI specification, maintained in its own repository
(a separate, non-Nori-specific protocol), for the wire format.

## Documentation

[docs/README.md](docs/README.md) is the full index — one page per
subsystem, written for a stranger standing up their own instance with
no assumed context.

## License

AGPL-3.0-or-later for this code. See [NOTICE](NOTICE) and
[LICENSE](LICENSE). Contributions: [docs/contributing.md](docs/contributing.md).

**If you run a modified version of this as a network service other people
use, AGPL §13 requires you to offer them the modified source** — this is
a real, binding condition of the license, not a suggestion. It doesn't
apply to running Nori unmodified for yourself or your own household; it
applies once you change the code and let others interact with your
running instance. Read [the license itself](LICENSE) before relying on
a summary, this one included.
