# The debug endpoint

A read-only door for the person developing Nori, so a running agent can be asked how it is doing without getting at its container: its health,
recent errors and logs, conversations, memory and settings, as JSON. (`debug_api.py`, `logbuf.py`, `scripts/debug_query.py`; added 2026-10-30.)

**It exposes private data** — the household's conversations and memory — to anyone holding the token. That is why it is built the way it is.

## Turning it on

Settings → **Debug endpoint** (admins only):

1. **Token**: enter one of at least 24 characters. Use the same one on every agent you want to query; the tab shows an 8-character *fingerprint* so you can
   see that two agents have the same token without ever displaying it. Only the token's SHA-256 is stored.
2. **Switch**: turn it on — until you turn it off, or for 1 hour, 8 hours, 24 hours or 7 days, after which it switches itself off and stays off.

While it is on, every page shows a red **debug on** chip to admins. The tab lists the last requests (time, address, path, outcome).

## What it promises

| | |
|---|---|
| **Off by default** | Until an admin turns it on, `/debug` is not served: it is handled exactly like any other path that does not exist (a signed-out visitor gets the login redirect, a signed-in one the same not-found page). The right token does nothing while it is off. |
| **Token required** | `Authorization: Bearer <token>`, compared in constant time. A token in the URL is never accepted (URLs end up in logs). A session cookie is not a token. Failures count toward the login form's per-address lockout (then HTTP 429). |
| **Read-only** | GET only. Nothing in it writes, and there is no way to run SQL, a shell, or a tool through it. |
| **No secrets** | Every section is built from explicit column lists, then scrubbed: any field *named* like a secret (`key`, `token`, `password`, `psk`, `*_enc`, `*_hash`, `cookie`, `csrf`, …) is `[redacted]`, and any string that *looks* like one (a bearer header, an `sk-…` key, a `paci1:` connection string, a session cookie, a JWT, `?token=` in a URL) is masked — including inside conversation text and log lines. URLs lose credentials and query strings. Token *counts* (`prompt_tokens`) are not secrets and are kept. Where it can't be sure it over-redacts. |
| **The agent can't touch it** | The switch, the timer and the token hash are config entries marked secret, so `get_settings` cannot read them and `update_settings` (or a peer's request) cannot change them. A prompt-injected message cannot turn the endpoint on. |
| **Audited** | Each answered request is recorded (last 1000) and printed to the log. A request made while it is off is not recorded: nothing answered it. |
| **Bounded** | Row limits per section, and a 4 MB cap on a response (HTTP 413 with advice to ask for less). A section that breaks reports its own error instead of blanking the response. |

## Asking

```
curl -H "Authorization: Bearer $NORI_DEBUG_TOKEN" https://<agent>/debug                 # the list of sections
curl -H "Authorization: Bearer $NORI_DEBUG_TOKEN" "https://<agent>/debug/errors?limit=50"
```

or, for several agents at once (the token comes from the environment, never the command line):

```
NORI_DEBUG_TOKEN=... python scripts/debug_query.py --agent nori=https://nori.example.com --agent iron=https://iron.example.com errors
NORI_DEBUG_TOKEN=... NORI_DEBUG_AGENTS="nori=https://a,iron=https://b" python scripts/debug_query.py conversation user=Tester limit=20
```

| section | what it shows |
|---|---|
| `overview` | version, uptime, users, the model chain, providers and their status, message counts, disk, which `NORI_*` variables are set (names only) |
| `health` | integration health, peer (PACI) reachability, paired devices, connected accounts, the scheduler's last cycle and any error it hit, live threads, update check, phone-app fetch, sandbox, MCP servers, running jobs |
| `errors` | the diagnostics events, recent error lines from the log, counts of *screening failed* / *reply cut off* / tracebacks, failed tool calls, job problems, failed or errored peer messages, model failures and turn-limit hits, schedule and image failures |
| `logs` | the last ~4000 log lines of this run (in memory): `lines=`, `grep=`, `level=error`, `stream=out\|err`, `since=<epoch>` |
| `conversation` | one user's messages with their metadata: `user=<id or name>`, `limit=`, `before=`/`after=` (paging), `kinds=chat,tool,…`, `grep=`; plus the latest conversation summaries |
| `turns` | recent replies and how each was made: model, provider, fallbacks tried, latency, tokens, cost, tool calls, whether it was cut off |
| `settings` | every non-secret setting, workspace and per user, flagged where it differs from the default; the safe environment values |
| `peers` | each PACI peer's configuration and health, its recent messages **with the screening result of each** (so "could not be screened" and "screened and flagged" can be told apart), compulsion log |
| `memory` | counts by type, recent items, memory events |
| `jobs`, `schedules`, `pings`, `tools`, `db` | sub-agent jobs and roster; schedules and their runs; proactive-ping signals, their state and recent unprompted messages; the tool registry; row counts per table |

The log ring (`logbuf.py`) is a copy of what the server prints, kept in memory only and lost on restart; nothing about the logging itself changes.

## Using it well

- Prefer the narrow sections first (`errors`, `health`) and page conversations with `limit=` and `before=`.
- The same token on each agent means one `NORI_DEBUG_TOKEN` queries all of them. Turn the switch off, or use a timer, when development is done.
- Use it only over **https** (behind your reverse proxy): the token is sent with every request, and so is everything it returns.
- Treat the token like a password: anyone holding it can read the household's conversations while the switch is on. To rotate it, enter a new one on each agent (the old one stops working at once); to cut access immediately, turn the switch off or remove the token.

## Tests

`tests/test_nori_debug_endpoint.py` (52): off by default and indistinguishable from a missing page, the token rules (header only, wrong/partial/extended tokens, cookies, lockout, only the hash stored, weak tokens refused, rotation, removal), unreachable by the agent's tools, **no planted secret in any section**, what each section shows, the log ring, the admin tab (admin only, CSRF, never echoes the token, header chip), and the access record.
