# Quick start

The fastest path to a running instance and a first reply.

## 0. Prerequisites

- **PowerShell 7+** (`pwsh`) and **Python 3.10+**.
- Two real pip packages: `pip install cryptography tzdata` — Nori
  isn't stdlib-only despite appearances; see
  [Setup](setup.md#python-dependencies) for exactly why each is needed.
- The repo, cloned.

## 1. Get an OpenRouter key

Nori talks to models through [OpenRouter](https://openrouter.ai/keys),
not directly to any one provider — one key, your choice of model, your
own billing. This is the one thing you genuinely can't skip; everything
else in this section has a working default.

## 2. Write `nori.env`

Copy `.env.example` to `nori.env` in a sibling directory next to
the repo (see [Setup](setup.md#environment-variables-env) for exactly
where and why), and fill in `OPENROUTER_API_KEY`. Everything else in
that file has a comment explaining its own default and can wait — see
[Setup](setup.md) when you're ready to go past the minimum.

## 3. Start it

```
pwsh nori_ctl.ps1 start
```

(This page is the non-Docker path. Running in Docker instead? Its
`.env` setup differs from step 2 above — see the
[README](../README.md#running-it) for the Docker quick start instead
of mixing the two.)

See [Deployment & supervision](deployment-and-watchdog.md) for what
this actually does and the other subcommands (`status`, `stop`,
`restart`, `ensure`).

## 4. Create the first account

Open `http://127.0.0.1:8877` (or whatever `NORI_PORT` you set). With no
account on the instance yet, you'll land on a setup screen — the first
account you create becomes the workspace's admin automatically; there's
no one else yet to grant that role. See [Auth & roles](auth-and-roles.md)
for what admin actually means day to day, and how to invite the rest of
the household once you have more than one person using this.

## 5. Say something

That's it — a real chat turn, no other configuration required. She has
a persona and basic tool-usage guidance out of the box
([Setup](setup.md#customizing-her) if you want to change either), and a
starter set of native tools (memory, tasks/notes/reminders, the working
folder) with nothing external connected yet.

## Where to go next

- Want her to read your email/calendar, control smart-home devices, or
  search the web? See [Setup](setup.md)'s own links to each
  integration's page — every one of them is optional and off until you
  connect it.
- Want to understand what you just started, in depth, before connecting
  anything real to it? Start with [Architecture](architecture.md) and
  [The enforcement model](enforcement-model.md).
- Running this for real, long-term, unattended? See
  [Deployment & supervision](deployment-and-watchdog.md) and
  [Backups](backups.md) before you have real data you'd mind losing.
