# Auth & roles

Source: `nori/accounts.py` (the only module running raw SQL against
users/sessions/invites — see [Architecture](architecture.md)'s note on
that convention) and `nori/crypto.py` (secrets at rest).

## Workspaces, users, roles

One workspace per instance — the household. Every user belongs to
exactly one workspace and has exactly one of two roles: **admin** or
**member**. There's no third role and no per-feature permission grid;
[Tools & the dispatch model](tools.md) covers the one place role
actually gates something narrower than "admin vs. everyone."

The **first account created on a fresh instance becomes admin
automatically** (`bootstrap_admin`) — there's no one else yet to grant
that role, and no separate "make me admin" step. Every account after
that is created via an invite, and an invite states its role
explicitly at creation time; there's no self-service role upgrade.

A deactivated user (`deactivate_user`) is disabled, not deleted
(`reactivate_user` reverses it) — consistent with this codebase's own
disable-rather-than-delete convention (see
[Contributing](contributing.md)'s note on provenance).

## Invites

An admin creates an invite (`create_invite`) naming a display name and
a role; this generates a one-time token (hashed before storage — the
raw token is never written to the database, so a copy of the database
alone can't be used to redeem a pending invite) with a 7-day expiry.
Whoever holds the real link sets their own password directly — an
admin never sees or sets it on someone else's behalf.

## Sessions

A session token is a random value, hashed before storage the same way
an invite token is, with a 24-hour expiry from creation (not
sliding — logging in again after expiry is a normal, expected event,
not a bug). Every session also carries its own CSRF token, checked on
every state-changing request.

## Login throttling

`accounts.py` rate-limits repeated failed login attempts per source IP
(`rate_limited`/`record_fail`) — a real, if basic, brake against
password guessing. This is process-local (an in-memory counter, not a
persisted table) — a restart clears it.

## Secrets at rest

Anything genuinely sensitive stored in the database — a connected
account's OAuth refresh token, a peer's shared secret — is encrypted
with `cryptography`'s Fernet (authenticated symmetric encryption, not
a hand-rolled cipher) before it's written to any column, via
`crypto.py`. The key (`secret.key`) lives on disk **next to the
database, not inside it** — see [Setup](setup.md#where-things-live-on-disk).

**What this protects against:** someone obtaining a copy of the
database file alone (a backup left somewhere it shouldn't be, disk
access without the whole machine) without also having the separate key
file. **What it does not protect against:** the operator who controls
the machine both files live on — this is disk-theft protection, not a
claim of protection from whoever is actually running the instance. See
[The enforcement model](enforcement-model.md) for how this fits the
rest of this app's own code-enforced-vs-convention distinctions —
encryption-at-rest is squarely in the "code enforced" column; nothing
writes an unencrypted token to that column.

Backups add a **second**, separate encryption layer with its own key —
see [Backups](backups.md) for why reusing `secret.key` for that would
be circular (it has to travel *inside* the backup archive to make the
restored data usable again).

## What role actually gates, precisely

"Admin" covers: household management (inviting/deactivating members),
every third-party integration's connection/credentials (Google,
Microsoft, Home Assistant, MCP servers, a peer agent), the tool builder,
model configuration, backups, and integration health. "Member" covers
everything personal — chat, memory, tasks/notes/reminders/trackers, the
working folder, their own scheduled tasks and settings. A few pages are
visible to every role regardless (Active tools, the Settings → About
page) because what they show isn't sensitive to anyone in the
household. See `_settings_groups` in `server.py` for the exact,
current, authoritative split — it's a short function, worth reading
directly rather than trusting a summary that could drift from it.
