# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Per-turn timing instrumentation -- toggleable (config.debug_timing_enabled,
workspace-scoped), OFF by default. Built 2026-09-13 because turns felt slow
and nobody could say where.

One config.get() check per TURN, not per stage -- timing.start() is the
only place that reads the setting; everything downstream just uses the
Turn/Stage object it returns, which never re-checks anything. When the
setting is off, start() returns NULL_TURN, a singleton whose .stage()
returns a shared no-op context manager (_NULL_STAGE) -- no allocation, no
time.perf_counter() call, nothing measurable, so an ordinary turn with
this off costs one cheap SELECT and nothing else.

One JSON line per Turn (not one line per stage) via print(...) -- reuses
the same stdout-to-.nori.log redirection every other debug line in this
app already goes through, tagged "TIMING " so it's grep-able on its own.
Each line carries turn_id/kind/model/total_ms plus every stage recorded,
each stage itself real elapsed ms plus whatever extra tags its caller
attached (round, tool name, ...) -- enough structure to aggregate
programmatically (turn id, kind, model, round), not just eyeball one line
at a time, per the operator's own explicit ask.
"""
from __future__ import annotations

import json
import time
import uuid

import config


class _NullStage:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


_NULL_STAGE = _NullStage()


class _NullTurn:
    turn_id = None

    def stage(self, name, **extra):
        return _NULL_STAGE

    def finish(self):
        pass


NULL_TURN = _NullTurn()


class _Stage:
    __slots__ = ("turn", "name", "extra", "t0")

    def __init__(self, turn, name, extra):
        self.turn, self.name, self.extra = turn, name, extra

    def __enter__(self):
        self.t0 = time.perf_counter()
        return self

    def __exit__(self, *exc):
        ms = (time.perf_counter() - self.t0) * 1000
        entry = {"name": self.name, "ms": round(ms, 2)}
        entry.update(self.extra)
        self.turn.stages.append(entry)
        return False


class Turn:
    def __init__(self, kind: str, *, model: str | None = None, turn_id: str | None = None):
        self.turn_id = turn_id or uuid.uuid4().hex[:8]
        self.kind = kind
        self.model = model
        self.start = time.perf_counter()
        self.stages: list[dict] = []

    def stage(self, name: str, **extra):
        return _Stage(self, name, extra)

    def finish(self) -> None:
        total_ms = (time.perf_counter() - self.start) * 1000
        line = {"turn_id": self.turn_id, "kind": self.kind, "model": self.model,
                "total_ms": round(total_ms, 1), "stages": self.stages}
        print("TIMING " + json.dumps(line, default=str), flush=True)


def start(workspace_id: int, kind: str, *, model: str | None = None, turn_id: str | None = None) -> Turn:
    """The one place the setting is checked. Returns NULL_TURN (free) when
    off, a real Turn (one cheap SELECT, then real perf_counter() calls
    only from here on) when on."""
    if not config.get("workspace", workspace_id, "debug_timing_enabled"):
        return NULL_TURN
    return Turn(kind, model=model, turn_id=turn_id)


def start_anywhere(kind: str, *, model: str | None = None, turn_id: str | None = None) -> Turn:
    """Same as start(), for a call site with no clean workspace_id at hand
    yet -- chiefly the very top of HTTP request routing, before any
    session/cookie has been resolved, so there's no per-workspace setting
    to check yet. Uses enabled_anywhere() instead."""
    if not enabled_anywhere():
        return NULL_TURN
    return Turn(kind, model=model, turn_id=turn_id)


def enabled_anywhere() -> bool:
    """Workspace-independent check, for a call site with no clean
    workspace_id at hand (ingest.py's screening callers -- peers.py's
    handle_inbound chief among them, a peer connection's own scope can be
    per-user or per-workspace and there's no live 'session' the way an
    ordinary tool call has one; tools.dispatch() itself, when called with
    no timing_turn; an HTTP request before any session is known). Gated
    by whether ANY workspace has the toggle on, not a specific one --
    avoids threading workspace_id through places that don't naturally
    have it just for this."""
    import store
    row = store.read(lambda c: c.execute(
        "SELECT 1 FROM settings WHERE key='debug_timing_enabled' AND v='true' LIMIT 1").fetchone())
    return row is not None


def log_event(kind: str, ms: float, **extra) -> None:
    entry = {"kind": kind, "ms": round(ms, 2)}
    entry.update(extra)
    print("TIMING " + json.dumps(entry, default=str), flush=True)


def log_screening(kind: str, ms: float) -> None:
    log_event("screening", ms, screen_kind=kind)


class _StandaloneStage:
    """A stage timer with no parent Turn -- for a call site with no live
    Turn object at hand (tools.dispatch() when nobody passed one in,
    request-level timing before a page handler runs) that still wants to
    be timed on its own, logged as its own independent line. Checks
    enabled_anywhere() once at __enter__, not before -- a caller building
    this never pays for the check unless it's actually entered."""
    __slots__ = ("kind", "extra", "active", "t0")

    def __init__(self, kind: str, extra: dict):
        self.kind, self.extra = kind, extra
        self.active = False

    def __enter__(self):
        self.active = enabled_anywhere()
        if self.active:
            self.t0 = time.perf_counter()
        return self

    def __exit__(self, *exc):
        if self.active:
            log_event(self.kind, (time.perf_counter() - self.t0) * 1000, **self.extra)
        return False


def standalone(kind: str, **extra):
    return _StandaloneStage(kind, extra)
