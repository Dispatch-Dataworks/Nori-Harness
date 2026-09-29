# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Daily backups (2026-09-18, operator's own ask, both apps). Local disk
first, then optionally uploaded to a connected Google Drive/OneDrive/
SharePoint destination -- his choice, configured below. Admin-only,
system infrastructure, never a tool: nothing here is ever
tools.register()'d, and nothing about it is reachable by the model or a
peer -- same reasoning Drive/OneDrive/SharePoint write already
established for this codebase, applied to the whole backup lifecycle.

WHAT'S IN A BACKUP:
  - store.DB_PATH -- via sqlite3's own online backup API (Connection.
    backup()), not a raw file copy. The db is written to continuously by
    a live server; a raw copy mid-write risks a corrupt snapshot. The
    backup API produces a real, consistent point-in-time copy without
    stopping anything.
  - generated/ (her own generated images) and prompts/ (persona.md +
    persona-history/, tool-usage.md + tool-usage-history/ don't exist in
    nori -- persona only here) -- real, live, operator-specific content
    that isn't in git and can't be regenerated.
  - workfiles/ -- EXCEPT "backups" (always) and any subfolder named in
    NORI_BACKUP_EXCLUDE_DIRS (see _exclude_dir_names(), below), at any
    depth. An operator-configurable exclusion, for a large, derived,
    regenerable artifact that isn't real backup-worthy data. Real files
    alongside it (e.g. a character-sheet image, a
    downloaded photo) ARE included -- this excludes one named subfolder,
    not workfiles wholesale.
  - secret.key (crypto.py's Fernet key) -- has to travel with the backup:
    it's what decrypts connected_accounts' own OAuth token columns
    inside the db this backs up. Safe to include here specifically
    BECAUSE the whole archive gets a second, independent encryption
    layer before it ever reaches disk (see below) -- the two keys never
    travel together in the clear.

WHAT'S NOT INCLUDED, AND WHY:
  - .env -- credentials (OPENROUTER_API_KEY, OAI_API_KEY, the OAuth
    client secrets, NORI_BACKUP_KEY itself). Pushing those to a cloud
    drive is a different risk class than pushing personal data there,
    and none of it can be re-derived from a restore anyway -- a restore
    needs a real .env supplied separately, by the operator, same as a
    fresh install. Documented in restore_backup.py's own instructions,
    not left implicit.
  - workfiles/.../<excluded snapshot folder>/ -- see above.
  - quarantine/ -- currently always empty in the real instance (nothing
    exercises this path yet); included if it ever isn't, cost is zero.

ENCRYPTION: the whole archive (db snapshot + every file above) is
Fernet-encrypted, keyed from NORI_BACKUP_KEY -- a SEPARATE secret from
crypto.py's secret.key, sourced from .env, never included in the
archive itself. This can't reuse secret.key: that key travels INSIDE
this same archive (see above), so encrypting the archive with it would
be circular -- anyone who has the encrypted file already has the key
that opens it. NORI_BACKUP_KEY has no such problem: it never leaves
.env, which is excluded from every backup. Real, load-bearing
consequence: lose NORI_BACKUP_KEY (or forget the passphrase it holds)
and every backup made with it becomes permanently unreadable, no
recovery path -- store it somewhere durable outside this machine, same
as any other credential. Local copies get this same encryption, not
just uploaded ones -- one archive format, one code path, and it means
the local backups/ folder itself is no more exposed than the live data
it's a snapshot of.

NORI_BACKUP_KEY, TWO ACCEPTED SHAPES (2026-09-19, revised after a real
operator objection to the original design -- "a backup key needs to be
something I know; forcing a large complex string means I'll end up
writing it down next to the backups themselves, which is the actual
weak point"). A raw generated Fernet key (44 base64url chars) is
detected by its own exact shape (_looks_like_raw_fernet_key below) and
used directly, unchanged from the original design -- for a self-hoster
who'd rather manage real key material. The DOCUMENTED DEFAULT is a
human passphrase: anything that isn't shaped like a raw Fernet key is
treated as one, checked against MIN_PASSPHRASE_LEN, then run through
scrypt (memory-hard, deliberately -- makes an offline brute-force
attempt against a stolen archive expensive in RAM as well as time, not
just CPU cycles) with a fresh random salt PER ARCHIVE to derive the
actual 32-byte Fernet key. The salt is not a secret -- it's written in
cleartext into the archive's own small header (see _ARCHIVE_MAGIC
below) specifically so restoring needs nothing but the passphrase
itself; nothing else has to be remembered, backed up, or kept in sync.
Parameters (n=2**17, r=8, p=1) match OWASP's password-storage cheat
sheet's current scrypt minimum -- ~128MiB working set, roughly 1-2s on
ordinary hardware, entirely fine for something that runs once a day
(or once, for a manual restore), never on any request-serving path.
MIN_PASSPHRASE_LEN=20 is a length floor, not a real entropy guarantee
by itself -- the settings-page copy recommends a multi-word passphrase
(5-6 random words) rather than a padded-out single word, since that's
what actually clears the floor with real entropy behind it rather than
by accident.

RETENTION: local pruning is automatic (age-based, runs every scheduler
cycle -- our own disk, low risk, see prune_local()). Remote pruning is
NOT automatic -- see remote_prune_candidates()/remote_prune_execute():
surfaced as an explicit prompt in the settings UI ("N backups older than
retention on <destination>, clean up now?") instead of an unattended
delete sweep against a third-party cloud store. Same human-in-the-loop
principle as Drive/OneDrive/SharePoint's write-is-a-UI-action precedent,
just applied to delete -- a more sensitive action than write, so at
least as much caution, not less.

RESTORE is a documented, out-of-process procedure -- see
restore_backup.py, run from the command line with the server STOPPED --
not a live in-app button. Swapping a running app's own database out
from under itself while it's still serving requests is a real, common
failure mode this doesn't attempt to paper over."""
from __future__ import annotations

import base64
import binascii
import gzip
import io
import json
import os
import sqlite3
import tarfile
import time
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

import accounts
import connected_accounts
import store

# FUNCTIONS, not module constants (2026-09-23: store.DATA_DIR is now re-resolved live on every access -- freezing
# its value into a plain attribute here at import time would defeat that fix just the same way persona.py/
# promptdoc.py did -- see store.py's own comment for the incident).
def _backup_dir() -> Path:
    return store.DATA_DIR / "backups"


def _history_path() -> Path:
    return _backup_dir() / "history.json"


# External callers (tests) keep writing backup.BACKUP_DIR unchanged -- __getattr__ resolves it fresh each time.
def __getattr__(name: str):
    if name == "BACKUP_DIR":
        return _backup_dir()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _exclude_dir_names() -> set[str]:
    """"backups" always excludes itself -- never archive our own output.
    NORI_BACKUP_EXCLUDE_DIRS (comma-separated) adds any other named
    subfolder under workfiles/ an operator wants skipped -- e.g. a large,
    regenerable derived artifact an older install happens to have sitting
    there. A FUNCTION, re-read on every call, not a module-level constant
    computed once at import -- same reasoning store.DATA_DIR's own history
    already established for this codebase (a value read once at import
    can't pick up a later-set env var, and testing an override would need
    a fresh process instead of just setting the variable)."""
    extra = os.environ.get("NORI_BACKUP_EXCLUDE_DIRS", "")
    return {"backups"} | {name.strip() for name in extra.split(",") if name.strip()}
_REMOTE_FOLDER_NAME = "NoriBackups"
_MAX_HISTORY_ROWS = 500  # trimmed on write -- an operational log, not meant to grow forever itself

# ── NORI_BACKUP_KEY: a passphrase (documented default) or a raw Fernet
# key, see module docstring's "TWO ACCEPTED SHAPES" section ─────────────
_ARCHIVE_MAGIC = b"BKV1"
_MODE_RAW_KEY = 0
_MODE_PASSPHRASE = 1
_SALT_LEN = 16
_SCRYPT_N = 2 ** 17  # OWASP password-storage cheat sheet's current scrypt minimum
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
MIN_PASSPHRASE_LEN = 20


def _backup_secret() -> str | None:
    raw = os.environ.get("NORI_BACKUP_KEY")
    return raw if raw else None


def _looks_like_raw_fernet_key(value: str) -> bool:
    """A real Fernet key is exactly 44 base64url characters decoding to
    32 raw bytes -- checked structurally, not just by length, so an
    ordinary passphrase that happens to be 44 characters long is never
    misdetected as one (it won't be valid base64url with correct
    padding by accident)."""
    if len(value) != 44:
        return False
    try:
        decoded = base64.urlsafe_b64decode(value.encode("ascii"))
    except (ValueError, binascii.Error, UnicodeEncodeError):
        return False
    return len(decoded) == 32


def _derive_key_from_passphrase(passphrase: str, salt: bytes) -> bytes:
    kdf = Scrypt(salt=salt, length=_SCRYPT_DKLEN, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))


def is_configured() -> bool:
    return _backup_secret() is not None


def key_status() -> dict:
    """For the settings page and create_backup()'s own preflight check --
    never raises. {"configured": False} if unset; otherwise {"configured":
    True, "ok": bool, "mode": "raw_key"|"passphrase", "reason": str|None}
    -- ok=False with a reason is the "set, but a real problem" state
    (currently just: too short to be a passphrase, and not a real Fernet
    key either), surfaced distinctly so a weak configuration fails loud
    rather than quietly encrypting with something too weak to matter."""
    secret = _backup_secret()
    if secret is None:
        return {"configured": False, "ok": False, "mode": None, "reason": None}
    if _looks_like_raw_fernet_key(secret):
        return {"configured": True, "ok": True, "mode": "raw_key", "reason": None}
    if len(secret) < MIN_PASSPHRASE_LEN:
        return {"configured": True, "ok": False, "mode": "passphrase",
                "reason": f"only {len(secret)} characters -- a passphrase needs at least "
                         f"{MIN_PASSPHRASE_LEN} (or use a generated Fernet key instead)"}
    return {"configured": True, "ok": True, "mode": "passphrase", "reason": None}


def _encrypt_archive(raw: bytes) -> bytes:
    """Prepends a small cleartext header (magic + mode byte, plus a
    fresh random salt for passphrase mode) to the real Fernet token --
    see module docstring. Raises RuntimeError on anything key_status()
    would call not ok; the caller (create_backup) checks that first so
    this is a defensive second check, not the only one."""
    secret = _backup_secret()
    if secret is None:
        raise RuntimeError("NORI_BACKUP_KEY isn't set -- see docs/backups.md")
    if _looks_like_raw_fernet_key(secret):
        token = Fernet(secret.encode("ascii")).encrypt(raw)
        return _ARCHIVE_MAGIC + bytes([_MODE_RAW_KEY]) + token
    if len(secret) < MIN_PASSPHRASE_LEN:
        raise RuntimeError(f"NORI_BACKUP_KEY is only {len(secret)} characters -- a passphrase needs at "
                           f"least {MIN_PASSPHRASE_LEN} (or use a generated Fernet key instead)")
    salt = os.urandom(_SALT_LEN)
    key = _derive_key_from_passphrase(secret, salt)
    token = Fernet(key).encrypt(raw)
    return _ARCHIVE_MAGIC + bytes([_MODE_PASSPHRASE]) + salt + token


def _now() -> float:
    return time.time()


def _snapshot_db(tmp_dir: Path) -> Path:
    """A real, consistent point-in-time copy via sqlite3's own online
    backup API -- not a raw file read, which risks catching the file
    mid-write from the live server."""
    dest = tmp_dir / "nori.db"
    src_conn = sqlite3.connect(str(store.DB_PATH))
    dest_conn = sqlite3.connect(str(dest))
    try:
        src_conn.backup(dest_conn)
    finally:
        dest_conn.close()
        src_conn.close()
    return dest


def _iter_include_paths():
    """Every real file to include, as (arcname, real_path) pairs.
    Skips _exclude_dir_names() at any depth, and any dangling
    symlink -- os.walk with followlinks=False (the default) already
    doesn't descend into a symlinked dir, this just also skips a
    symlink file entry that no longer resolves."""
    exclude = _exclude_dir_names()
    for top in ("generated", "prompts", "quarantine", "workfiles"):
        base = store.DATA_DIR / top
        if not base.is_dir():
            continue
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in exclude]
            for name in files:
                p = Path(root) / name
                if not p.is_file():
                    continue
                yield str(p.relative_to(store.DATA_DIR)).replace("\\", "/"), p
    key_path = store.DATA_DIR / "secret.key"
    if key_path.is_file():
        yield "secret.key", key_path


def _build_archive_bytes() -> bytes:
    import tempfile
    with tempfile.TemporaryDirectory(prefix="nori_backup_") as tmp:
        tmp_path = Path(tmp)
        db_snapshot = _snapshot_db(tmp_path)
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            tar.add(str(db_snapshot), arcname="nori.db")
            for arcname, real_path in _iter_include_paths():
                tar.add(str(real_path), arcname=arcname)
        return buf.getvalue()


def _load_history() -> list[dict]:
    if not _history_path().is_file():
        return []
    try:
        return json.loads(_history_path().read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return []  # a corrupted history log must never block a real backup from running


def _append_history(record: dict) -> None:
    _backup_dir().mkdir(parents=True, exist_ok=True)
    rows = _load_history()
    rows.append(record)
    rows = rows[-_MAX_HISTORY_ROWS:]
    _history_path().write_text(json.dumps(rows, indent=2), encoding="utf-8")


def history(limit: int = 30) -> list[dict]:
    return list(reversed(_load_history()))[:limit]


def last_run_ts(app: str = "nori") -> float | None:
    """Most recent successful create_backup() timestamp for `app` -- the
    scheduler's own "did today's backup already happen" gate reads this
    rather than tracking a separate date itself."""
    rows = [r for r in _load_history() if r.get("action") == "create" and r.get("app") == app
           and r.get("status") == "ok"]
    return max((r["ts"] for r in rows), default=None)


def create_backup(*, app: str = "nori") -> dict:
    """Builds one encrypted local archive. Never raises -- a failure is a
    real, visible history row (status='error'), not a silent skip; the
    caller (scheduler tick or the settings-page button) doesn't need its
    own try/except to make a failure visible. Refuses outright, before
    doing any of the real archive-building work, on anything key_status()
    flags as not ok -- including a too-short passphrase, per the operator's
    own explicit instruction: fail loudly rather than silently writing a
    weakly-protected backup."""
    started = _now()
    status = key_status()
    if not status["ok"]:
        reason = ("NORI_BACKUP_KEY isn't set -- can't encrypt a backup, refusing to write one unencrypted"
                  if not status["configured"] else
                  f"NORI_BACKUP_KEY is {status['reason']} -- refusing to write a weakly-protected backup")
        rec = {"ts": started, "app": app, "action": "create", "status": "error", "error": reason}
        _append_history(rec)
        return rec
    try:
        raw = _build_archive_bytes()
        token = _encrypt_archive(raw)
        _backup_dir().mkdir(parents=True, exist_ok=True)
        name = f"{app}-backup-{time.strftime('%Y%m%d-%H%M%S', time.gmtime(started))}.tar.gz.enc"
        path = _backup_dir() / name
        path.write_bytes(token)
        rec = {"ts": started, "app": app, "action": "create", "status": "ok", "path": str(path), "name": name,
              "size_bytes": len(token), "duration_s": round(_now() - started, 1)}
    except Exception as exc:  # noqa: BLE001 -- a backup bug must be a visible history row, never a crash
        rec = {"ts": started, "app": app, "action": "create", "status": "error",
               "error": f"{type(exc).__name__}: {exc}"[:400]}
    _append_history(rec)
    return rec


def prune_local(retention_days: int) -> int:
    """Automatic, our own disk -- deletes local *.tar.gz.enc backups
    older than retention_days. Returns how many were removed. Never
    touches history.json itself (the log outlives the files it
    describes -- a pruned entry still shows when/whether that day's
    backup succeeded, just without a local file to point at)."""
    if not _backup_dir().is_dir():
        return 0
    cutoff = _now() - retention_days * 86400
    removed = 0
    for p in _backup_dir().glob("*.tar.gz.enc"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                removed += 1
        except OSError:
            continue
    if removed:
        _append_history({"ts": _now(), "app": "local", "action": "prune", "status": "ok", "removed": removed})
    return removed


# ── remote upload/prune ────────────────────────────────────────────────
_REMOTE_PROVIDERS = ("google_drive", "onedrive_work", "onedrive_personal", "sharepoint")


def _backup_owner_session() -> dict | None:
    """Whose connected account backups upload through -- the primary
    admin user (lowest user id, role=admin). A single-workspace, single-
    admin household is what this was built for; a real multi-workspace
    deployment isn't a scenario this considers, not asked for."""
    admins = sorted((u for u in accounts.all_active_users() if u["role"] == "admin"), key=lambda u: u["id"])
    if not admins:
        return None
    u = admins[0]
    return {"user_id": u["id"], "workspace_id": u["workspace_id"], "role": "admin"}


def _find_or_create_remote_folder(session: dict, provider: str, site_id: str | None) -> dict:
    import drive
    import sharepoint
    if provider == "google_drive":
        return drive.find_or_create_folder(session, _REMOTE_FOLDER_NAME)
    if provider in ("onedrive_work", "onedrive_personal"):
        return drive.find_or_create_folder_onedrive(session, _REMOTE_FOLDER_NAME, provider=provider)
    if provider == "sharepoint":
        if not site_id:
            return {"error": "a SharePoint site id is required for SharePoint backups"}
        return sharepoint.find_or_create_folder(session, site_id, _REMOTE_FOLDER_NAME)
    return {"error": f"unknown backup destination: {provider}"}


def upload_one(path: Path, *, provider: str, site_id: str | None = None, app: str = "nori") -> dict:
    """Uploads one already-encrypted local archive to the configured
    destination. Reuses the exact same upload/dedup functions the files
    page's own move-to-Drive/OneDrive/SharePoint actions use -- backups
    get unique timestamped names, so the "refuses a same-name collision"
    behavior those already have is harmless here, not something this
    needs to work around."""
    import drive
    import sharepoint
    if provider not in _REMOTE_PROVIDERS:
        return {"error": f"unknown backup destination: {provider}"}
    session = _backup_owner_session()
    if session is None:
        return {"error": "no admin account exists to own the remote connection"}
    folder = _find_or_create_remote_folder(session, provider, site_id)
    if "error" in folder:
        return folder
    content = path.read_bytes()
    if provider == "google_drive":
        mime = "application/octet-stream"
        result = drive.upload_bytes_to_drive(session, name=path.name, content=content, mime_type=mime,
                                             folder_id=folder["folder_id"])
    elif provider in ("onedrive_work", "onedrive_personal"):
        result = drive.upload_bytes_to_onedrive(session, name=path.name, content=content,
                                                folder_id=folder["folder_id"], provider=provider)
    else:
        result = sharepoint.upload_bytes_to_sharepoint(session, site_id=site_id, name=path.name,
                                                       content=content, folder_id=folder["folder_id"])
    return result


def _list_remote_backup_files(provider: str, site_id: str | None) -> dict:
    """Read-only -- reuses each module's own read tool implementation
    directly (not through tools.dispatch; this isn't a tool call, just
    calling the same function), scoped to the one folder backups live
    in."""
    import drive
    import sharepoint
    session = _backup_owner_session()
    if session is None:
        return {"error": "no admin account exists to own the remote connection"}
    folder = _find_or_create_remote_folder(session, provider, site_id)
    if "error" in folder:
        return folder
    if provider == "google_drive":
        params = {"q": f"'{folder['folder_id']}' in parents and trashed = false"}
        import urllib.parse
        url = f"https://www.googleapis.com/drive/v3/files?{urllib.parse.urlencode(params)}&fields=files(id,name,modifiedTime,size)"
        result = connected_accounts.authed_request(session["user_id"], provider, url)
        if not result.get("ok"):
            return result
        return {"ok": True, "files": [{"id": f["id"], "name": f.get("name", ""),
                                       "modified": f.get("modifiedTime")} for f in result["data"].get("files", [])]}
    if provider in ("onedrive_work", "onedrive_personal"):
        url = f"https://graph.microsoft.com/v1.0/me/drive/items/{folder['folder_id']}/children?$top=200"
        result = connected_accounts.authed_request(session["user_id"], provider, url)
        if not result.get("ok"):
            return result
        return {"ok": True, "files": [{"id": f["id"], "name": f.get("name", ""),
                                       "modified": f.get("lastModifiedDateTime")}
                                      for f in result["data"].get("value", [])]}
    # sharepoint
    url = f"https://graph.microsoft.com/v1.0/sites/{site_id}/drive/items/{folder['folder_id']}/children?$top=200"
    result = connected_accounts.authed_request(session["user_id"], "sharepoint", url)
    if not result.get("ok"):
        return result
    return {"ok": True, "files": [{"id": f["id"], "name": f.get("name", ""),
                                   "modified": f.get("lastModifiedDateTime")}
                                  for f in result["data"].get("value", [])]}


def remote_prune_candidates(provider: str, retention_days: int, site_id: str | None = None) -> dict:
    """What COULD be deleted, for the settings-page prompt -- never
    deletes anything itself. Matches only this system's own naming
    (*-backup-*.tar.gz.enc) and only files inside our own dedicated
    backup folder -- see module docstring for why this is deliberately
    NOT automatic."""
    listed = _list_remote_backup_files(provider, site_id)
    if "error" in listed:
        return listed
    cutoff = _now() - retention_days * 86400
    old = []
    for f in listed["files"]:
        if not f["name"].endswith(".tar.gz.enc"):
            continue
        try:
            from datetime import datetime, timezone
            mod = datetime.fromisoformat((f.get("modified") or "").replace("Z", "+00:00"))
            if mod.timestamp() < cutoff:
                old.append(f)
        except (ValueError, TypeError):
            continue
    return {"ok": True, "candidates": old}


def remote_prune_execute(provider: str, file_ids: list, site_id: str | None = None) -> dict:
    """The one deliberate delete this whole backup system performs --
    ONLY ever called from a human clicking "clean up now" on the
    settings page with an explicit list of file ids that same page just
    showed him (from remote_prune_candidates), never automatically, and
    never exposed as a tool. Every deletion is logged (history()) --
    visible after the fact, not silent."""
    session = _backup_owner_session()
    if session is None:
        return {"error": "no admin account exists to own the remote connection"}
    if provider == "google_drive":
        url_tmpl = "https://www.googleapis.com/drive/v3/files/{id}"
    elif provider in ("onedrive_work", "onedrive_personal"):
        url_tmpl = "https://graph.microsoft.com/v1.0/me/drive/items/{id}"
    elif provider == "sharepoint":
        url_tmpl = "https://graph.microsoft.com/v1.0/sites/" + (site_id or "") + "/drive/items/{id}"
    else:
        return {"error": f"unknown backup destination: {provider}"}
    deleted, failed = [], []
    for fid in file_ids:
        result = connected_accounts.authed_request(
            session["user_id"], provider, url_tmpl.format(id=fid), method="DELETE", raw_response=True)
        (deleted if result.get("ok") else failed).append(fid)
    _append_history({"ts": _now(), "app": "remote", "action": "prune",
                     "status": "ok" if not failed else "partial",
                     "provider": provider, "deleted": deleted, "failed": failed})
    return {"ok": not failed, "deleted": deleted, "failed": failed}


def _uploaded_names() -> set:
    return {r["name"] for r in _load_history()
           if r.get("action") == "upload" and r.get("status") == "ok" and r.get("name")}


def run_daily(*, provider: str | None, site_id: str | None = None, retention_days: int = 14) -> dict:
    """The one function scheduler.py's tick calls, once a day. Creates
    today's nori backup, uploads it if a destination is configured, then
    prunes local files past retention. Always returns a summary; never
    raises -- every real failure is a history() row with status='error',
    visible on the settings page, never silent."""
    rec = create_backup(app="nori")
    results = {"nori_backup": rec}
    if provider and rec.get("status") == "ok":
        up = upload_one(Path(rec["path"]), provider=provider, site_id=site_id)
        _append_history({"ts": _now(), "app": "nori", "action": "upload",
                         "status": "ok" if up.get("ok") else "error", "provider": provider,
                         "error": up.get("error"), "name": rec.get("name")})
        results["nori_upload"] = up
    results["pruned_local"] = prune_local(retention_days)
    return results
