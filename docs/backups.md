# Backups

Source: `nori/backup.py`, `nori/restore_backup.py`. Daily, automatic,
local-first, optionally uploaded to a connected Google Drive/OneDrive/
SharePoint destination once you've [connected one](google-and-microsoft.md).

## Admin-only infrastructure, never a tool

Nothing in `backup.py` is ever passed to `tools.register()` — the
whole backup lifecycle is unreachable by the model or a peer, the same
posture [Google Drive/OneDrive/SharePoint's write access](google-and-microsoft.md)
already established for anything with this much blast radius: a
capability this consequential is a UI action an admin triggers (or a
scheduled daily run), never something a turn can invoke.

## A consistent snapshot, not a raw file copy

The database is backed up through SQLite's own online backup API
(`Connection.backup()`), not a plain file copy — the database is
written to continuously by a live server, and copying its file
directly risks capturing a genuinely corrupt, mid-write snapshot. The
backup API produces a real, consistent point-in-time copy without
stopping anything.

## What travels, and why one specific thing needs a second key

The database, generated images, persona files (and their edit
history), and real working-folder files travel — plus `secret.key`
(the [encryption-at-rest](enforcement-model.md) key), which has to
come along because it's what decrypts connected-account OAuth tokens
stored inside the database this backs up. Including it is safe
specifically because the *entire archive* gets its own, separate
encryption layer before it ever touches disk — the two keys never
travel together in the clear.

**`.env` is deliberately excluded** — credentials are a different risk
class from personal data, and none of it can be re-derived from a
restore anyway; a restore needs a real `.env` supplied separately, by
the operator, the same as a fresh install.

**Any named subfolder under the working folder can be excluded,** via
`NORI_BACKUP_EXCLUDE_DIRS` (comma-separated folder names, matched at
any depth) — for a large, regenerable derived artifact that doesn't
belong in backup-worthy data. Real files sitting alongside an excluded
folder are still included; only the named subfolder itself is skipped.
`backups/` (this app's own output) is always excluded, regardless of
this setting.

## Encryption, and the one truly unrecoverable failure mode

The whole archive is encrypted, keyed from `NORI_BACKUP_KEY` — a
separate secret from `secret.key`, sourced from `.env`, never itself
included in the archive. This can't reuse `secret.key` for this
purpose: that key travels *inside* the archive, so encrypting the
archive with its own contents would be circular — anyone holding the
encrypted file would already hold the key that opens it.
`NORI_BACKUP_KEY` has no such problem, since `.env` is excluded from
every backup. The real, load-bearing consequence: **lose
`NORI_BACKUP_KEY` (or forget the passphrase it holds), and every
backup made with it becomes permanently unreadable — there is no
recovery path.** Local copies get the identical encryption as uploaded
ones — one archive format, one code path, so the local `backups/`
folder is no more exposed than the live data it's a snapshot of.

### A passphrase, not a generated key — deliberately

The **documented default is a passphrase you actually choose and
remember** — something like five or six random, unrelated words, at
least 20 characters. This was a real, deliberate reversal: an earlier
version of this design required a raw, machine-generated key, and the
operator's own objection is exactly why it changed — a string too
complex to remember gets written down somewhere, quite possibly right
next to the backups it's supposed to protect, which defeats the point.
A passphrase you can actually hold in your head has nowhere to leak to.

Under the hood, the passphrase is run through **scrypt** (memory-hard,
so an offline attacker who steals an archive can't just throw cheap
GPU time at it — cracking it costs real RAM, not just CPU cycles) with
parameters matching OWASP's current password-storage minimum for
scrypt (`n=2**17`, `r=8`, `p=1`) and a **fresh random salt generated
for every archive**. That salt is not a secret — it's written in
cleartext into the archive's own small header, specifically so
restoring one needs nothing but the passphrase itself. Nothing else
has to be remembered, backed up, or kept in sync with anything.

A minimum length (20 characters) is enforced and checked *before* any
real backup work starts — a passphrase that's too short is refused
outright, loudly, as a visible error, never silently accepted and
quietly weaker than it looks.

**A raw, generated Fernet key still works too**, for a self-hoster who
would genuinely rather manage real key material directly — detected
automatically by its own exact shape (44 base64url characters
decoding to 32 bytes), so nothing needs to be configured differently
to use one:

```
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Either way, it ships as an empty placeholder in `nori.env.example` —
backups refuse to write unencrypted rather than silently going out in
the clear until something real is set.

## Retention: local is automatic, remote is not

Configurable, default 14 days. Local pruning runs automatically,
age-based, every scheduler cycle — low risk, since it's this app's own
disk. Remote pruning is deliberately **not** automatic: it surfaces as
an explicit prompt in the settings UI ("N backups older than retention
on `<destination>`, clean up now?") rather than an unattended delete
sweep against a third-party cloud store — the same human-in-the-loop
principle [Google Drive/OneDrive/SharePoint](google-and-microsoft.md)
already applies to writes, extended here to delete, which is the more
sensitive of the two.

## Real weight

Measured against a live instance: roughly 15–18MB compressed, mostly
her own generated images and real working-folder content — the
database itself, even with well over a thousand stored messages, is
well under 1MB. Text is cheap; images are what a backup actually
weighs.

## Configuring

Signed in as admin, `/settings?tab=backups` — enable, pick the local
run hour, retention days, and an upload destination (blank means local
only; otherwise whichever of Google Drive, OneDrive work, OneDrive
personal, or SharePoint is connected — see
[Google & Microsoft](google-and-microsoft.md) to connect one). A "back
up now" button runs the full cycle (create, upload, prune) immediately,
the same as the daily schedule. Backups reuse the existing [scheduler](scheduler.md)
tick (a daily hour-match plus "not run in the last ~20h") rather than
a new timer.

## Restoring

A documented, out-of-process command, not a live in-app button.
Swapping a running app's own database out from under itself while it's
still serving requests is a real, common failure mode — this doesn't
attempt to paper over it. Stop the server first:

```
pwsh nori\nori_ctl.ps1 stop
python nori\restore_backup.py <path-to-backup.tar.gz.enc>
```

Prompts for confirmation before touching the live data directory (or
pass `--target <dir>` to restore into a throwaway location instead —
useful for verifying a specific backup restores cleanly before
trusting it). Needs `NORI_BACKUP_KEY` in the environment (loaded
automatically from the real `nori.env` unless already set in your
shell) — the same passphrase (or raw key) the backup was made with;
nothing else, since the salt a passphrase needs travels inside the
archive itself. Checks the restored database with a real `PRAGMA
integrity_check` before reporting success, and refuses cleanly (no
partial extraction) if the passphrase/key is wrong or the archive is
corrupted.

**Verified for real, not assumed:** a throwaway data directory with
real-shaped content was backed up, the resulting encrypted archive was
restored via this exact script into a *different* throwaway directory,
and the restored database was queried directly to confirm real data
survived the round trip — plus a wrong-key restore attempt confirmed
to fail cleanly with no partial write. See `tests/test_nori_backup.py`.

After restoring: start the server back up, and if this is a fresh
machine, supply the real `.env` yourself first — it was never part of
the backup.
