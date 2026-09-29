# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Restore a nori backup archive -- an out-of-process, command-line
procedure, not a live in-app button. Run with the server STOPPED
(pwsh nori_ctl.ps1 stop first) -- this writes directly into a data
directory nori's own process would otherwise have open.

Usage:
    python restore_backup.py <path-to-backup.tar.gz.enc> [--target DIR] [--yes]

--target defaults to this app's own live data directory -- almost
always what you want, but pointing it at a throwaway directory lets you
verify a backup restores cleanly before trusting it, without touching
real data.

Needs NORI_BACKUP_KEY in the environment -- the same passphrase (or raw
Fernet key) the backup was encrypted with. Loaded automatically from the
same nori.env the server itself reads, unless already set in your shell.

Needs the operator's OWN .env supplied separately afterward if this is a
fresh machine -- .env was never part of the backup (see backup.py's own
module docstring for why). Without it, a restored instance has its
database and files back, but no OPENROUTER_API_KEY/OAuth credentials/etc.
until you put those back yourself.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import io
import os
import sqlite3
import sys
import tarfile
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

REPO_ROOT = Path(__file__).resolve().parent
ENV_PATH = REPO_ROOT.parent / (REPO_ROOT.name + "-env") / "nori.env"

# Kept deliberately self-contained rather than importing backup.py -- this
# script is meant to run with as few real dependencies as possible (a
# recovery tool shouldn't need the rest of the app importable to work),
# same reasoning it already didn't import backup.py before this change.
# See backup.py's own module docstring for the full design; these five
# names and the two functions below must stay byte-for-byte compatible
# with backup.py's own copy, or an archive encrypted by one won't decrypt
# with the other.
_ARCHIVE_MAGIC = b"BKV1"
_MODE_RAW_KEY = 0
_MODE_PASSPHRASE = 1
_SALT_LEN = 16
_SCRYPT_N = 2 ** 17
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32


def _looks_like_raw_fernet_key(value: str) -> bool:
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


def _decrypt_archive(data: bytes, secret: str) -> bytes:
    if not data.startswith(_ARCHIVE_MAGIC):
        raise ValueError("not a recognized backup archive -- wrong file, or corrupted")
    mode = data[len(_ARCHIVE_MAGIC)]
    rest = data[len(_ARCHIVE_MAGIC) + 1:]
    if mode == _MODE_RAW_KEY:
        key, token = secret.encode("ascii"), rest
    elif mode == _MODE_PASSPHRASE:
        salt, token = rest[:_SALT_LEN], rest[_SALT_LEN:]
        key = _derive_key_from_passphrase(secret, salt)
    else:
        raise ValueError(f"unrecognized backup format mode: {mode}")
    return Fernet(key).decrypt(token)


def _load_env(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def restore(archive_path: Path, target_dir: Path) -> dict:
    _load_env(ENV_PATH)
    secret = os.environ.get("NORI_BACKUP_KEY")
    if not secret:
        return {"error": "NORI_BACKUP_KEY isn't set -- can't decrypt this backup"}
    data = archive_path.read_bytes()
    try:
        raw = _decrypt_archive(data, secret)
    except InvalidToken:
        return {"error": "couldn't decrypt -- wrong NORI_BACKUP_KEY, or a corrupted/non-backup file"}
    except ValueError as exc:
        if str(exc).startswith(("not a recognized", "unrecognized backup format")):
            return {"error": str(exc)}
        # A raw-key-mode archive against a passphrase-shaped current
        # secret (or vice versa) fails inside Fernet's own key-format
        # check, not InvalidToken -- from the operator's side this is
        # the identical actionable fact as InvalidToken: the current
        # NORI_BACKUP_KEY doesn't match what this archive was made with.
        return {"error": "couldn't decrypt -- wrong NORI_BACKUP_KEY, or a corrupted/non-backup file"}
    target_dir.mkdir(parents=True, exist_ok=True)
    target_resolved = target_dir.resolve()
    extracted = []
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
        for member in tar.getmembers():
            # Refuse anything trying to escape target_dir -- a defensive
            # check against a maliciously crafted archive, not something
            # this system's own create_backup() would ever produce.
            dest = (target_dir / member.name).resolve()
            if not str(dest).startswith(str(target_resolved)):
                return {"error": f"refusing to extract '{member.name}' -- escapes the target directory"}
            extracted.append(member.name)
        tar.extractall(path=target_dir)
    # Sanity check: the restored db actually opens and passes an integrity
    # check -- catching a truncated/corrupt archive here, not after nori's
    # own server tries to use it.
    db_path = target_dir / "nori.db"
    if db_path.is_file():
        conn = sqlite3.connect(str(db_path))
        try:
            result = conn.execute("PRAGMA integrity_check").fetchone()
            if result[0] != "ok":
                return {"error": f"restored database failed integrity check: {result[0]}"}
        finally:
            conn.close()
    return {"ok": True, "target_dir": str(target_dir), "files_extracted": len(extracted)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("archive", help="path to a *.tar.gz.enc backup file")
    ap.add_argument("--target", default=None, help="directory to restore into (default: this app's live data dir)")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = ap.parse_args()

    archive_path = Path(args.archive)
    if not archive_path.is_file():
        print(f"no such file: {archive_path}", file=sys.stderr)
        return 1
    if args.target:
        target_dir = Path(args.target)
    else:
        target_dir = REPO_ROOT / "data"
        if not args.yes:
            resp = input(f"This will overwrite files under the LIVE data directory ({target_dir}). "
                        f"Make sure the server is stopped first. Type 'yes' to continue: ")
            if resp.strip().lower() != "yes":
                print("aborted")
                return 1

    result = restore(archive_path, target_dir)
    if "error" in result:
        print(f"restore failed: {result['error']}", file=sys.stderr)
        return 1
    print(f"restored {result['files_extracted']} files into {result['target_dir']}")
    print("Remember: .env was never part of the backup -- supply your real nori.env separately "
         "before starting the server, if this is a fresh machine.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
