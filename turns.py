# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""The one enforcement point for "don't run two model turns for the same
user at once" -- retyped from a sibling application's turns.py (which fixed this same
problem, for real, the same day this was built) with one deliberate
change: a sibling application is single-user, so one global lock was correct there.
Nori is multi-user -- a single global lock here would serialize every
user in the household behind whichever one of them Nori happens to be
mid-reply to, which is real, needless contention, not just a style
difference. This module keyed everything by user_id instead: one lock
per user, so two different users' turns run fully in parallel and only
the SAME user's two tabs (or a tab plus a proactive ping) ever contend.

The problem this solves, proven in a sibling application first: two browser tabs
(same user) send a message ~0.5s apart. Without this, both requests reach
chat.run() and BOTH complete, producing two separate assistant replies to
what the user experiences as one conversation. A per-user threading.Lock
with a non-blocking acquire fixes the "two run concurrently" half; the
sweep below fixes the other half -- the second tab's message must not be
silently dropped just because it lost the race for the lock.

Mechanism: whoever calls run() first for a given user_id gets the lock and
actually runs the model (`first_run`). Anyone else for that SAME user_id
who calls run() while it's held gets {"queued": True} back immediately --
non-blocking, not a wait -- because their message was already written to
the DB by the caller *before* calling run(), so nothing is lost, only
delayed. The lock holder, once its own turn finishes, sweeps for any user
message that arrived after its own starting snapshot and answers that too
(`sweep_run`) before releasing -- so the queued tab's message gets a real
reply without a second concurrent chat.run() ever starting. Bounded to
_MAX_SWEEP_ROUNDS so a run of genuinely back-to-back messages (or a model
that keeps erroring) can't turn one request into an unbounded loop; a
sweep round beyond that limit picks its answer up on the NEXT natural
poll/send instead, not incorrectly.
"""
from __future__ import annotations

import threading
from typing import Callable

import conversation

_MAX_SWEEP_ROUNDS = 5

_locks_guard = threading.Lock()
_locks: dict[int, threading.Lock] = {}

# Generic release hooks (2026-09-19, the PACI specification v1.0) -- a domain module
# (peers.py) registers once, at import time, for "run this after ANY turn
# for this user_id finishes and the lock is released," without this module
# needing to know what that is -- same registration-list pattern
# scheduler.register_signal/precheck.register already use elsewhere in
# this codebase. Built specifically to drain a queued reply_requested
# compulsion the moment a busy account frees up, but deliberately generic:
# this module stays peer-agnostic.
_release_hooks: list[Callable[[int], None]] = []


def register_release_hook(fn: Callable[[int], None]) -> None:
    _release_hooks.append(fn)


def _lock_for(user_id: int) -> threading.Lock:
    with _locks_guard:
        lock = _locks.get(user_id)
        if lock is None:
            lock = threading.Lock()
            _locks[user_id] = lock
        return lock


def in_flight(user_id: int) -> bool:
    lock = _lock_for(user_id)
    got = lock.acquire(blocking=False)
    if got:
        lock.release()
    return not got


def run(user_id: int, first_run, sweep_run) -> dict:
    """first_run() answers the message that triggered this call; sweep_run
    (called with the swept-up row as a dict) answers anything that arrived
    for this SAME user while first_run() was in progress. Both must return
    the same {"ok", "messages", ...} shape callers pass straight back as
    the HTTP JSON response for THEIR OWN call -- a sweep's result isn't
    returned to anyone (nobody's request is waiting on it); it only needs
    to reach the DB, which both callbacks already do via conversation.
    add_message()."""
    lock = _lock_for(user_id)
    if not lock.acquire(blocking=False):
        return {"queued": True}
    try:
        last_seen_id = conversation.max_id(user_id)
        result = first_run()
        for _ in range(_MAX_SWEEP_ROUNDS):
            row = conversation.latest_user_message_after(user_id, last_seen_id)
            if row is None:
                break
            last_seen_id = conversation.max_id(user_id)
            sweep_run(row)
        return result
    finally:
        lock.release()
        # After release, not before -- a hook that itself wants this same
        # lock (peers.py's drain, via _run_prompted_turn's own turns.run()
        # call) must never deadlock against the turn that just finished.
        # One hook's bug must never take the turn that just completed down
        # with it.
        for hook in _release_hooks:
            try:
                hook(user_id)
            except Exception:  # noqa: BLE001
                pass
