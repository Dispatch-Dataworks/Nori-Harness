# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Custody of a sealed result for a peer (2026-09-19) -- a PACI control exchange, code to code.

A peer's own game or feature can draw a result now and reveal it hours later. When the operator has chosen Nori as the holder, the peer hands the result to
this module at the moment it is sealed, and this module hands it back at the reveal time -- on its own clock.

WHAT THIS IS NOT: it is not text in Nori's context that he has been asked not to mention. The operator's worry was Nori leaking a secret, and a standing
instruction not to say something is exactly what gets discounted, so it is built so there is nothing to discount:
  * The result is stored ENCRYPTED AT REST (crypto.py's Fernet, the same key the peer secrets use) in `custody_holds`, a table NOTHING but this
    module reads. No tool, no context builder, no digest, no search, no summary, no page touches it (tests/test_nori_custody.py scans the source
    to prove that, and mutation-checks the scan). He, the model, has no path to it and is not told it exists: the exchange is handled in
    peers.handle_inbound BEFORE any conversation is created, so it never becomes a message he reads.
  * Release is by the CLOCK, in `tick()`: at `reveal_ts` this module posts the result back. Nori's judgement is not involved and there is no
    way to ask for it sooner.
The honest limit: whoever can release on a clock must be able to open what it releases, so this module's CODE can read it. A holder that could release
but never read would need a third party or a time-lock; neither exists here. The threat being defended is the model talking, and that is closed.

WIRE (both are PACI control types, HMAC-signed like every other request, never reaching a model on either side):
  custody_hold     peer -> Nori   {token, game_ref, reveal_ts, commitment, payload}   ->  custody_hold_ack {token, commitment}
                   `commitment` = sha256("<game_ref>|<token>|<payload>"): Nori verifies the payload against it, so a garbled hold is refused, and
                   it is what the peer checks the release against later. Idempotent on (peer, token): a retried hold is acknowledged, never doubled.
  custody_release  Nori -> peer   {token, payload}                                   ->  custody_release_ack {token}
                   Sent when reveal_ts has passed, and RETRIED EVERY TICK until acknowledged -- forever. If the peer is down, or unreachable, it
                   simply waits; nothing expires. Once acknowledged the payload is erased from this side (status 'released', payload_enc NULL).

If Nori is the one that is unreachable at the appointed moment, a well-behaved peer holds a fallback of its own: after a
grace it reveals from its own record, marked as such. So the result is never lost to either side being down, and never depends on this one.
"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.error

import crypto
import store

MAX_PAYLOAD = 2000


def commitment_of(game_ref: str, token: str, payload: str) -> str:
    return hashlib.sha256(f"{game_ref}|{token}|{payload}".encode("utf-8")).hexdigest()


def handle_hold(peer: dict, envelope: dict) -> tuple[int, dict]:
    """Inbound custody_hold (signature already verified by peers.handle_inbound). Stores the payload encrypted; acknowledges. Idempotent."""
    body = envelope.get("body") if isinstance(envelope.get("body"), dict) else envelope
    token, game_ref, payload = body.get("token"), body.get("game_ref"), body.get("payload")
    commitment, reveal_ts = body.get("commitment"), body.get("reveal_ts")
    if not (isinstance(token, str) and 8 <= len(token) <= 128 and isinstance(game_ref, str) and 0 < len(game_ref) <= 64
            and isinstance(payload, str) and 0 < len(payload) <= MAX_PAYLOAD and isinstance(commitment, str)):
        return 400, {"type": "error", "body": {"reason": "malformed custody_hold"}}
    try:
        reveal_ts = float(reveal_ts)
    except (TypeError, ValueError):
        return 400, {"type": "error", "body": {"reason": "malformed reveal_ts"}}
    if commitment_of(game_ref, token, payload) != commitment:
        return 400, {"type": "error", "body": {"reason": "the payload does not match its commitment"}}

    def _w(c):
        row = c.execute("SELECT commitment FROM custody_holds WHERE peer_id=? AND token=?", (peer["id"], token)).fetchone()
        if row is not None:
            return row["commitment"] == commitment           # a retry of the same hold: acknowledged, not stored twice
        c.execute("INSERT INTO custody_holds(peer_id, token, game_ref, reveal_ts, commitment, payload_enc, status, received_ts, attempts) "
                  "VALUES (?,?,?,?,?,?, 'holding', ?, 0)",
                  (peer["id"], token, game_ref, reveal_ts, commitment, crypto.encrypt(payload), time.time()))
        return True
    if not store.write(_w):
        return 409, {"type": "error", "body": {"reason": "that token is already held with a different commitment"}}
    print(f"custody: holding a sealed result for peer {peer['id']} (ref {game_ref}), due {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(reveal_ts))}", flush=True)
    return 200, {"type": "custody_hold_ack", "body": {"token": token, "commitment": commitment}}


def due(now: float | None = None) -> list[dict]:
    now = now if now is not None else time.time()
    return [dict(r) for r in store.read(lambda c: c.execute(
        "SELECT id, peer_id, token, reveal_ts FROM custody_holds WHERE status='holding' AND reveal_ts<=? ORDER BY reveal_ts", (now,)).fetchall())]


def release_one(hold_id: int) -> str:
    """Post the result back to its owner. Returns 'released' | 'waiting' (any failure: it is retried on the next tick, indefinitely)."""
    import peers
    row = store.read(lambda c: c.execute("SELECT * FROM custody_holds WHERE id=?", (hold_id,)).fetchone())
    if row is None or row["status"] != "holding" or not row["payload_enc"]:
        return "released" if row is not None and row["status"] == "released" else "waiting"
    peer = peers.get_peer(row["peer_id"])
    if peer is None or not peer["enabled"]:
        return _fail(hold_id, "the peer connection is missing or disabled")
    envelope = {"type": "custody_release", "paci_version": "1.0",
                "body": {"token": row["token"], "game_ref": row["game_ref"], "payload": crypto.decrypt(row["payload_enc"])}}
    try:
        resp = peers._post(peer, envelope)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        return _fail(hold_id, f"{type(exc).__name__}: {exc}")
    if resp.get("type") == "custody_release_ack":
        store.write(lambda c: c.execute("UPDATE custody_holds SET status='released', released_ts=?, payload_enc=NULL, last_error=NULL WHERE id=?",
                                        (time.time(), hold_id)))
        print(f"custody: released the sealed result (ref {row['game_ref']}) to peer {row['peer_id']}", flush=True)
        return "released"
    return _fail(hold_id, json.dumps(resp)[:200])


def _fail(hold_id: int, why: str) -> str:
    store.write(lambda c: c.execute("UPDATE custody_holds SET attempts=attempts+1, last_error=? WHERE id=?", (why[:200], hold_id)))
    return "waiting"


def tick() -> None:
    """Every scheduler tick (peers.tick): release whatever is due; retry whatever has not been acknowledged."""
    for h in due():
        try:
            release_one(h["id"])
        except Exception as exc:  # noqa: BLE001 -- one hold's failure must never stop the others
            _fail(h["id"], f"{type(exc).__name__}: {exc}")
