# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Secrets-at-rest encryption — built now, before anything uses it.

The highest-value thing Nori will ever store is a connected-account OAuth
token (a real Gmail/Outlook refresh token — the live keys to someone's
actual mailbox, not just Nori's copy of their conversation about it). That
lands in Phase 10. This helper exists now because retrofitting encryption
onto live token columns later is painful, and the primitive itself is small
enough to build and test standalone before anything depends on it.

What this protects against: someone getting a copy of the database file
(backup theft, disk access) without also having the separate key file.
What it does NOT protect against: the operator themselves, who controls
the machine both files live on — that is the honest limit of app-level
isolation. This is disk-theft protection, not a claim of protection from
the person running the instance.

Uses `cryptography`'s Fernet (authenticated symmetric encryption — nonce,
versioning, and integrity-checking all handled by the library rather than
hand-rolled). The key lives under DATA_DIR, same directory as the database
and gitignored the same wholesale way (`nori/data/`) — so a throwaway test
instance (NORI_DATA_DIR) gets its own key and never touches a real one.
"""
from __future__ import annotations

import os
import threading

from cryptography.fernet import Fernet, InvalidToken

import store

_KEY_PATH_NAME = "secret.key"
_lock = threading.Lock()
_cached_fernet: Fernet | None = None


def _key_path():
    return store.DATA_DIR / _KEY_PATH_NAME


def _load_or_create_key() -> bytes:
    store.DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = _key_path()
    if path.exists():
        return path.read_bytes()
    key = Fernet.generate_key()
    path.write_bytes(key)
    try:
        os.chmod(path, 0o600)  # best-effort; Windows ACLs don't map onto this directly
    except OSError:
        pass
    return key


def _fernet() -> Fernet:
    global _cached_fernet
    if _cached_fernet is None:
        with _lock:
            if _cached_fernet is None:
                _cached_fernet = Fernet(_load_or_create_key())
    return _cached_fernet


def encrypt(plaintext: str) -> str:
    """str in, str out (urlsafe base64 token) -- safe to store directly in a
    TEXT column."""
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(token: str) -> str:
    """Raises ValueError on a token that doesn't decrypt with the current
    key -- wrong key file, corrupted value, or not actually a Fernet token.
    Callers decide what that means for them; this module doesn't guess."""
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken as e:
        raise ValueError("could not decrypt — wrong key or corrupted value") from e


if __name__ == "__main__":
    store.init()
    sample = "round-trip test value, not a real secret"
    token = encrypt(sample)
    assert token != sample
    assert decrypt(token) == sample
    print(f"key file: {_key_path()}")
    print("round-trip OK")
