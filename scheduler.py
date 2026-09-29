# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""The proactive maintenance cycle -- a background daemon thread that
checks, per active user, whether there's something worth saying
unprompted and whether it's actually a good time to say it. Household-
tuned per-user cadence: quiet hours plus a minimum gap
since the last real activity, same shape a sibling application uses.

No real signal exists yet -- household inventory and meal planning
(built after this phase) register the first real entries in
_SIGNAL_PROVIDERS. Built and tested as generic infrastructure now, via a
synthetic test signal, the same way the reader/actor pipeline was proven
before any real connector existed.

Also runs memory reflection per user, per cycle (2026-09-12) -- checked
and applied BEFORE the ping-decision gates below, deliberately: whether
she's allowed to speak up unprompted right now is a different question
from whether her memory needs catching up, and the two shouldn't be
coupled (a sibling application's own reflection check is likewise independent of its
ping/mute gates in the same cycle).
"""
from __future__ import annotations

import os
import threading
import time
from typing import Callable

import accounts
import chat
import config
import conversation
import emotion
import memory
import peers
import reminders
import schedules
import timing
import turns
import usertime

INTERVAL_S = int(os.environ.get("NORI_SCHEDULER_INTERVAL_MIN", "15")) * 60

_SIGNAL_PROVIDERS: list[dict] = []
_started = False
_lock = threading.Lock()

_TIERS = ("urgent", "routine")
_DEFAULT_COOLDOWN_S = 6 * 3600


def _signal_config_key(key: str) -> str:
    return f"ping_signal_{key}_enabled"


def register_signal(fn: Callable[[int], "str | tuple[str, str] | None"], *, key: str, label: str,
                    tier: str = "routine", cooldown_seconds: float = _DEFAULT_COOLDOWN_S,
                    min_check_interval_seconds: "float | Callable[[int], float] | None" = None) -> None:
    """A domain module (household inventory, meal planning, overdue tasks,
    ...) calls this once at import time.

    fn takes a user_id and returns: None (nothing to say), a plain reason
    string (cooldown applies to the signal AS A WHOLE), or (reason,
    dedup_token) when a signal needs to dedupe per INSTANCE rather than
    per signal -- calendar's own registration passes the specific event's
    id as dedup_token, so a second, different event isn't blocked by the
    first one's cooldown.

    tier: "urgent" or "routine" -- when more than one signal is true at
    once, every urgent candidate is considered before any routine one
    (the operator, 2026-09-26: "getting this wrong means he enables six signals and
    only ever hears about one" -- this plus the rotation below is the fix).

    cooldown_seconds: once a signal actually fires for a user, it can't
    fire again (for that same dedup_token) until this elapses -- "the same
    nag can't repeat every ping until he acts."

    min_check_interval_seconds: for a signal whose own CHECK costs
    something (an API call, an LLM call) regardless of outcome -- the
    function itself is not even invoked more often than this, independent
    of the ping cycle's own 15-minute cadence. None (the default) means
    "cheap, check every cycle." May be a plain number or a callable(user_id)
    for a per-user cadence (the email signal's "N checks spread across his
    own waking hours" -- see email_calendar.py).

    This is also the ONLY place a per-signal toggle gets created (the operator,
    2026-09-26: "the settings tab is derived from what the ping actually
    pushes, never a hand-maintained list") -- registering here adds the
    config._SPEC entry for it at the same moment, so a new domain module
    always ships with a working toggle, never a nag with no way to turn
    it off. See server.py's "pings" tab and tests/test_nori_ping_signals.py's
    guard for the other half of that rule."""
    if tier not in _TIERS:
        raise ValueError(f"tier must be one of {_TIERS}, not {tier!r}")
    if any(s["key"] == key for s in _SIGNAL_PROVIDERS):
        raise ValueError(f"a ping signal with key {key!r} is already registered")
    config_key = _signal_config_key(key)
    config._SPEC[config_key] = (True, bool, "user", False, f"ping: {label}")
    _SIGNAL_PROVIDERS.append({"fn": fn, "key": key, "label": label, "config_key": config_key,
                              "tier": tier, "cooldown_seconds": cooldown_seconds,
                              "min_check_interval_seconds": min_check_interval_seconds})


def signal_registry() -> list[dict]:
    """Every registered ping signal -- {"key", "label", "config_key", "tier"}
    -- for the settings page and its guard test to derive from. Never
    hand-duplicated elsewhere."""
    return [{"key": s["key"], "label": s["label"], "config_key": s["config_key"], "tier": s["tier"]}
           for s in _SIGNAL_PROVIDERS]


def _signal_state(user_id: int, key: str, dedup_token: str = "") -> dict | None:
    import store
    return store.read(lambda c: c.execute(
        "SELECT * FROM ping_signal_state WHERE user_id=? AND signal_key=? AND dedup_token=?",
        (user_id, key, dedup_token)).fetchone())


def _mark_signal_checked(user_id: int, key: str) -> None:
    import store
    now = time.time()
    store.write(lambda c: c.execute(
        "INSERT INTO ping_signal_state(user_id,signal_key,dedup_token,last_checked_ts) VALUES (?,?,'',?) "
        "ON CONFLICT(user_id,signal_key,dedup_token) DO UPDATE SET last_checked_ts=excluded.last_checked_ts",
        (user_id, key, now)))


def mark_signal_fired(user_id: int, key: str, dedup_token: str = "") -> None:
    import store
    now = time.time()
    store.write(lambda c: c.execute(
        "INSERT INTO ping_signal_state(user_id,signal_key,dedup_token,last_fired_ts) VALUES (?,?,?,?) "
        "ON CONFLICT(user_id,signal_key,dedup_token) DO UPDATE SET last_fired_ts=excluded.last_fired_ts",
        (user_id, key, dedup_token, now)))


def _signal_last_fired_any(user_id: int, key: str) -> float:
    """The most recent time THIS SIGNAL TYPE fired, across every
    dedup_token -- the rotation-fairness input: among several currently-
    true signals of the same tier, the one that's gone longest without
    actually being said wins, not whichever happened to register first."""
    import store
    row = store.read(lambda c: c.execute(
        "SELECT MAX(last_fired_ts) AS m FROM ping_signal_state WHERE user_id=? AND signal_key=?",
        (user_id, key)).fetchone())
    return row["m"] if row and row["m"] is not None else 0.0


def outstanding_reason(user_id: int) -> dict | None:
    """{"reason", "key", "dedup_token"} for the one signal that should
    fire this cycle, or None. Priority (urgent before routine), then
    rotation (least-recently-fired signal of that tier), then per-signal
    cooldown, then the check-interval throttle for an expensive signal --
    see register_signal()'s own docstring for what each of these is for."""
    candidates = []
    for s in _SIGNAL_PROVIDERS:
        if not config.get("user", user_id, s["config_key"]):
            continue  # disabled: the function is never even called -- genuinely absent, not suppressed
        min_interval = s["min_check_interval_seconds"]
        if min_interval is not None:
            interval = min_interval(user_id) if callable(min_interval) else min_interval
            state = _signal_state(user_id, s["key"])
            last_checked = state["last_checked_ts"] if state else None
            if last_checked is not None and (time.time() - last_checked) < interval:
                continue  # too soon to check again -- bounds real API/LLM cost regardless of outcome
            _mark_signal_checked(user_id, s["key"])
        try:
            result = s["fn"](user_id)
        except Exception:  # noqa: BLE001 -- one provider's bug must never take the cycle down
            continue
        if not result:
            continue
        reason, dedup_token = result if isinstance(result, tuple) else (result, "")
        state = _signal_state(user_id, s["key"], dedup_token)
        last_fired = state["last_fired_ts"] if state else None
        if last_fired is not None and (time.time() - last_fired) < s["cooldown_seconds"]:
            continue  # said this recently -- cooling down, not eligible to win this cycle
        candidates.append({"key": s["key"], "reason": reason, "dedup_token": dedup_token, "tier": s["tier"],
                           "last_fired_any": _signal_last_fired_any(user_id, s["key"])})
    if not candidates:
        return None
    candidates.sort(key=lambda c: (0 if c["tier"] == "urgent" else 1, c["last_fired_any"]))
    winner = candidates[0]
    return {"reason": winner["reason"], "key": winner["key"], "dedup_token": winner["dedup_token"]}


def _within_window(user_id: int) -> bool:
    start = config.get("user", user_id, "ping_window_start")
    end = config.get("user", user_id, "ping_window_end")
    # usertime.local_dt (2026-09-18, real bug: this used to be
    # time.localtime() with no argument -- the SERVER's own OS hour, not
    # his configured zone's) -- correct for him only by coincidence
    # before this fix.
    hour = usertime.local_dt(user_id).hour
    if start <= end:
        return start <= hour < end
    return hour >= start or hour < end  # a window that wraps past midnight


def _recently_active(user_id: int) -> bool:
    last_ts = conversation.last_message_ts(user_id)
    if last_ts is None:
        return False
    gap_min = config.get("user", user_id, "ping_min_gap_min")
    return (time.time() - last_ts) / 60 < gap_min


def _send_proactive(user: dict, reason: str) -> str:
    """Returns an outcome tag for this cycle: 'sent', 'quiet', 'error: ...',
    or 'skipped_turn_in_progress' if blocked by this user's own in-flight
    turn. Goes through turns.run() (2026-09-12) -- previously called
    chat.run() directly, which let a proactive tick run fully concurrently
    with a live user turn for the SAME account (a real collision, found
    diagnosing the Nodrya-note read-back racing a live conversation). A
    blocked tick is never silently retried here: the next scheduled cycle
    (run_cycle_once, on its own INTERVAL_S cadence) re-evaluates this user
    from scratch, including whether `reason` still holds -- the same
    natural retry the periodic design already provides, so this doesn't
    need its own queue."""
    extra = {"role": "system",
             "content": f"You're checking in on your own, unprompted -- nothing was asked. "
                        f"Reason: {reason}. Say something brief and in character about it now; "
                        f"don't announce that you're 'checking in', just say the thing."}
    session = {"role": user["role"], "user_id": user["id"], "workspace_id": user["workspace_id"]}

    def _first():
        turn = timing.start(user["workspace_id"], "proactive")
        try:
            res = chat.run(session, user["id"], user["display_name"], extra_message=extra,
                           max_rounds=config.get("user", user["id"], "tool_rounds_proactive"),
                           timing_turn=turn)
        except chat.ModelError as exc:
            turn.finish()
            return {"error": str(exc)}
        with turn.stage("persist_reply"):
            conversation.add_message(user["id"], "assistant", res["text"], kind="proactive",
                                     emotion=emotion.get_state(user["id"]),
                                     meta={"reason": reason, **conversation.cost_meta(res["usage"])})
        turn.finish()
        return {"ok": True}

    def _sweep(_orphan):
        # A real user message arrived while this ping held the lock --
        # answer it for real, same as a live turn would, not with the
        # proactive framing above (that's this ping's own, not the
        # orphan's) -- same reasoning a sibling application's turns.py documents for
        # its own sweep_run. peer_pending="user" (2026-09-19, operator's
        # own reversal of the 2026-09-13 protection for THIS call site
        # specifically) -- this is answering him, and his own answer was
        # "inject unread peer content into every turn from me," which this
        # orphaned real message qualifies as same as a live turn would.
        # "user" specifically -- see peers.pending_delivery_messages()'s
        # own docstring on why this is tracked independently of "peer".
        turn = timing.start(user["workspace_id"], "chat")
        try:
            res = chat.run(session, user["id"], user["display_name"],
                           max_rounds=config.get("user", user["id"], "tool_rounds_chat"),
                           peer_pending="user", timing_turn=turn)
        except chat.ModelError:
            turn.finish()
            return
        with turn.stage("persist_reply"):
            conversation.add_message(user["id"], "assistant", res["text"],
                                     emotion=emotion.get_state(user["id"]),
                                     meta=conversation.cost_meta(res["usage"]))
        turn.finish()

    result = turns.run(user["id"], _first, _sweep)
    if result.get("queued"):
        return "skipped_turn_in_progress"
    if result.get("error"):
        return f"error: {result['error']}"
    return "sent"


# ── general-purpose scheduled tasks (2026-09-15) -- see schedules.py's own
# module docstring for the full design. Checked and fired independently of
# ping_enabled/_within_window/_recently_active below, by the operator's own
# explicit instruction: "she can act and speak at any hour; whether he's
# pinged is governed by the existing notification rules, same separation
# as the sub-agent completion trigger." Those three gates answer "is this a
# good moment for an unprompted HOUSEHOLD-SIGNAL ping" -- a question that
# doesn't apply to "an explicit instruction was due right now," the exact
# reasoning jobs.py's own _trigger_turn_for_job already documents for the
# job-completion trigger. Quiet hours are untouched here for the same
# reason they're untouched there: notify_quiet_start/end gates only the
# browser's own local push notification (server.py's client-side
# inQuiet()/notify()), never whether a turn runs or a message is persisted
# -- nothing to bypass, nothing to check, it was never in this function's
# way to begin with.
def _tool_available(session: dict, tool_name: str) -> bool:
    import tools
    return any(s["function"]["name"] == tool_name for s in tools.active_schemas(session))


def _fire_schedule(user: dict, sched: dict) -> None:
    """Fires one due schedule. required_tool is checked HERE, before any
    turn starts -- the operator's own explicit ask: 'you can check it's
    available and enabled before firing, and report a clear failure if it
    isn't, rather than her waking up, discovering she can't do the thing,
    and inventing a reason.' Checking first and skipping the turn entirely
    on a miss means she never has to reason her way through a failure that
    was already known before she was woken -- the exact shape of the
    confabulation pattern found repeatedly elsewhere in this project,
    closed here by construction rather than by prompting her not to do
    it."""
    session = {"role": user["role"], "user_id": user["id"], "workspace_id": user["workspace_id"]}
    speaks_to_user = sched["deliver_to"] in ("user", "both")
    tells_peer = sched["deliver_to"] in ("peer", "both") and sched["deliver_peer_id"]

    if sched["required_tool"] and not _tool_available(session, sched["required_tool"]):
        reason = (f"scheduled task \"{sched['name']}\" needs {sched['required_tool']}, which isn't "
                 f"available or enabled for this account right now -- skipped rather than guessing")
        schedules.record_run(sched["id"], sched["instruction"], status="skipped_tool_unavailable", error=reason)
        schedules.advance(sched["id"], sched["schedule_type"], sched["interval_min"], sched["time_hour"],
                          sched["time_minute"], status="skipped_tool_unavailable", error=reason,
                          user_id=sched["user_id"])
        if speaks_to_user:
            conversation.add_message(user["id"], "assistant", reason, kind="scheduled",
                                     meta={"reason": f"schedule #{sched['id']} ({sched['name']})",
                                          "schedule_id": sched["id"]})
        return

    peer = peers.get_peer(sched["deliver_peer_id"]) if tells_peer else None
    peer_note = (f" Use peer{peer['id']}_send to tell {peer['name']} the outcome -- that's the point "
                f"of this task, not optional." if peer is not None else "")
    quiet_note = ("" if speaks_to_user else
                 " This one reports elsewhere, not to him directly -- don't say anything to him about "
                 "it unless it's genuinely urgent (message_user is still there for that).")
    reason = f"schedule #{sched['id']} ({sched['name']})"
    prompt = (f"A scheduled task is due now: \"{sched['name']}\". This is why you're getting a turn "
             f"right now, regardless of the time or your usual check-in schedule.\n\n"
             f"Instruction: {sched['instruction']}{peer_note}{quiet_note}")
    extra = {"role": "system", "content": prompt}
    # _schedule_context (mirrors _peer_context/_job_context) is what widens
    # message_user's own owner_check to this trigger too (see peers.py) --
    # only actually needed when she isn't already speaking to him by
    # default, but harmless to set unconditionally, same as the other two
    # triggers do regardless of their own default.
    session["_schedule_context"] = sched["name"]

    def _first():
        turn = timing.start(user["workspace_id"], "schedule")
        try:
            res = chat.run(session, user["id"], user["display_name"], extra_message=extra,
                           max_rounds=config.get("user", user["id"], "tool_rounds_proactive"),
                           timing_turn=turn)
        except chat.ModelError as exc:
            turn.finish()
            schedules.record_run(sched["id"], sched["instruction"], status="error", error=str(exc))
            schedules.advance(sched["id"], sched["schedule_type"], sched["interval_min"], sched["time_hour"],
                              sched["time_minute"], status="error", error=str(exc), user_id=sched["user_id"])
            return {"error": str(exc)}
        if speaks_to_user:
            with turn.stage("persist_reply"):
                conversation.add_message(user["id"], "assistant", res["text"], kind="scheduled",
                                         emotion=emotion.get_state(user["id"]),
                                         meta={"reason": reason, "schedule_id": sched["id"],
                                              **conversation.cost_meta(res["usage"])})
        turn.finish()
        schedules.record_run(sched["id"], sched["instruction"], status="ok")
        schedules.advance(sched["id"], sched["schedule_type"], sched["interval_min"], sched["time_hour"],
                          sched["time_minute"], status="ok", error=None, user_id=sched["user_id"])
        return {"ok": True}

    def _sweep(_orphan):
        # A real user message arrived while this held the lock -- answer it
        # for real, never with the scheduled-task framing above (same
        # reasoning every other _sweep in this app documents).
        # peer_pending="user" (2026-09-19) -- see the household-ping
        # _sweep above for the reasoning; this is answering him too.
        turn = timing.start(user["workspace_id"], "chat")
        try:
            res = chat.run(session, user["id"], user["display_name"],
                           max_rounds=config.get("user", user["id"], "tool_rounds_chat"),
                           peer_pending="user", timing_turn=turn)
        except chat.ModelError:
            turn.finish()
            return
        with turn.stage("persist_reply"):
            conversation.add_message(user["id"], "assistant", res["text"],
                                     emotion=emotion.get_state(user["id"]),
                                     meta=conversation.cost_meta(res["usage"]))
        turn.finish()

    result = turns.run(user["id"], _first, _sweep)
    if result.get("queued"):
        # Never silently dropped -- the next cycle re-checks next_run_ts,
        # which is still in the past, so this same due schedule is picked
        # up again immediately rather than waiting a full extra interval.
        schedules.record_run(sched["id"], sched["instruction"], status="skipped_turn_in_progress")


def _check_due_schedules(user: dict) -> None:
    for sched in schedules.due_schedules(user["id"]):
        try:
            _fire_schedule(user, sched)
        except Exception as exc:  # noqa: BLE001 -- one schedule's bug must never take the cycle down,
                                  # or block every other due schedule for this same user this cycle
            print(f"schedule #{sched['id']} ({sched['name']}) for user {user['id']} raised: {exc}", flush=True)
            schedules.record_run(sched["id"], sched["instruction"], status="error", error=str(exc))
            schedules.advance(sched["id"], sched["schedule_type"], sched["interval_min"], sched["time_hour"],
                              sched["time_minute"], status="error", error=str(exc), user_id=sched["user_id"])


# ── reminders (2026-09-16) -- like a schedule's own due-check, but with
# a nag loop that has to persist state (attempt count, last-nag time)
# between repeated firings for the SAME due occurrence. Independent of
# ping_enabled/_within_window/_recently_active, same reasoning
# _check_due_schedules already documents -- an explicit due reminder
# isn't a household-signal ping, it fires and nags regardless of the
# hour; quiet hours still govern only whether he's actually PINGED
# (server.py's own client-side notify()), never whether this runs.
def _fire_reminder(user: dict, rem: dict) -> None:
    now = time.time()
    _, day_end = reminders.day_bounds(rem["user_id"], rem["next_due_ts"])
    if now > day_end:
        # Past the end of the due day, still pending -- missed, not
        # nagged further. Silent by design (2026-09-16, operator's own
        # scope left to this build): no turn fires for a missed
        # occurrence, just the log entry and the advance/close reminders.
        # mark_missed() already does -- inventing an "I'm sorry we missed
        # this" utterance was never asked for, and a real turn for pure
        # bookkeeping would be the opposite of what the nag budget exists
        # to bound.
        reminders.mark_missed(rem["id"], rem, fired_ts=now)
        return
    if rem["nag_count"] >= rem["nag_max_count"]:
        return  # nag budget spent for today -- the day-cutoff above resolves it, not another nag
    if rem["nag_count"] > 0 and rem["last_nag_ts"] and (now - rem["last_nag_ts"]) < rem["nag_interval_min"] * 60:
        return  # not time for the next nag yet

    session = {"role": user["role"], "user_id": user["id"], "workspace_id": user["workspace_id"]}
    attempt = rem["nag_count"] + 1
    urgency = ("" if attempt == 1 else
              f" This is contact {attempt} of {rem['nag_max_count']} about this -- a bit more insistent "
              f"than last time is fine, still in character, not a scolding.")
    detail = f"\n\nDetail: {rem['body']}" if rem.get("body") else ""
    reason = f"reminder #{rem['id']} ({rem['name']})"
    prompt = (f"A reminder is due now: \"{rem['name']}\". This is why you're getting a turn right now, "
             f"regardless of the time.{detail}\n\nTell him about it now, in your own voice.{urgency} "
             f"Once he's done it (or tells you he has), call reminder_close on reminder #{rem['id']} -- "
             f"don't just say it's done without actually closing it.")
    extra = {"role": "system", "content": prompt}
    # _schedule_context (mirrors _peer_context/_job_context, and
    # schedules' own reuse of the same marker) is what widens
    # message_user's owner_check to this trigger too -- harmless here
    # since she already speaks by default below, same reasoning
    # schedules._fire_schedule's own identical line documents.
    session["_schedule_context"] = rem["name"]

    def _first():
        turn = timing.start(user["workspace_id"], "reminder")
        try:
            res = chat.run(session, user["id"], user["display_name"], extra_message=extra,
                           max_rounds=config.get("user", user["id"], "tool_rounds_proactive"),
                           timing_turn=turn)
        except chat.ModelError as exc:
            turn.finish()
            return {"error": str(exc)}
        with turn.stage("persist_reply"):
            conversation.add_message(user["id"], "assistant", res["text"], kind="reminder",
                                     emotion=emotion.get_state(user["id"]),
                                     meta={"reason": reason, "reminder_id": rem["id"],
                                          **conversation.cost_meta(res["usage"])})
        turn.finish()
        reminders.mark_nagged(rem["id"], fired_ts=now)
        return {"ok": True}

    def _sweep(_orphan):
        # A real user message arrived while this held the lock -- answer
        # it for real, never with the reminder-nag framing above (same
        # reasoning every other _sweep in this app documents).
        # peer_pending="user" (2026-09-19) -- see the household-ping
        # _sweep further up this file for the reasoning; this is
        # answering him too.
        turn = timing.start(user["workspace_id"], "chat")
        try:
            res = chat.run(session, user["id"], user["display_name"],
                           max_rounds=config.get("user", user["id"], "tool_rounds_chat"),
                           peer_pending="user", timing_turn=turn)
        except chat.ModelError:
            turn.finish()
            return
        with turn.stage("persist_reply"):
            conversation.add_message(user["id"], "assistant", res["text"],
                                     emotion=emotion.get_state(user["id"]),
                                     meta=conversation.cost_meta(res["usage"]))
        turn.finish()

    result = turns.run(user["id"], _first, _sweep)
    # A queued/blocked attempt is never silently dropped -- next_due_ts
    # (and nag_count) are untouched either way, so the next cycle simply
    # tries the identical nag again, same "no separate retry queue
    # needed" reasoning schedules._fire_schedule's own comment documents.


def _check_due_reminders(user: dict) -> None:
    for rem in reminders.due_for_user(user["id"]):
        try:
            _fire_reminder(user, rem)
        except Exception as exc:  # noqa: BLE001 -- one reminder's bug must never take the cycle down,
                                  # or block every other due reminder for this same user this cycle
            print(f"reminder #{rem['id']} ({rem['name']}) for user {user['id']} raised: {exc}", flush=True)


def _maybe_reflect(user_id: int) -> None:
    try:
        if not memory.due_for_reflection(user_id):
            return
        result = memory.reflect(user_id)
    except Exception as exc:  # noqa: BLE001 -- one user's reflection bug must never take the cycle down
        print(f"reflection for user {user_id} raised: {exc}", flush=True)
        return
    if "error" in result:
        print(f"reflection for user {user_id} failed: {result['error']}", flush=True)
    elif result.get("skipped"):
        pass  # nothing to reflect on yet -- not worth a log line every cycle
    else:
        print(f"reflection for user {user_id}: {result}", flush=True)


def _maybe_compact(user_id: int, display_name: str) -> None:
    import compaction  # local: same load-order reasoning as context.py's own import of this
    try:
        result = compaction.compact_user(user_id, display_name)
    except Exception as exc:  # noqa: BLE001 -- one user's compaction bug must never take the cycle down
        print(f"compaction for user {user_id} raised: {exc}", flush=True)
        return
    if result.get("regenerated"):
        print(f"compaction for user {user_id}: {result}", flush=True)


def _maybe_run_backup() -> None:
    """Instance-wide, not per-user -- checked once per cycle (2026-09-18,
    see backup.py's own module docstring), reusing this same tick loop
    rather than a new timer. Gated on an hour match (uses the primary
    admin's own local time) plus "no successful backup in the last ~20h"
    (backup.last_run_ts) instead of tracking a separate calendar date --
    same hour-window idiom _within_window/_recently_active already use
    elsewhere in this file. Never lets a backup bug take the cycle down."""
    try:
        import backup
        admins = [u for u in accounts.all_active_users() if u["role"] == "admin"]
        if not admins:
            return
        admin = admins[0]
        wsid = admin["workspace_id"]
        if not config.get("workspace", wsid, "backup_enabled"):
            return
        if usertime.local_dt(admin["id"]).hour != config.get("workspace", wsid, "backup_hour"):
            return
        last = backup.last_run_ts(app="nori")
        if last and (time.time() - last) < 20 * 3600:
            return
        backup.run_daily(
            provider=config.get("workspace", wsid, "backup_remote_provider") or None,
            site_id=config.get("workspace", wsid, "backup_sharepoint_site_id") or None,
            retention_days=config.get("workspace", wsid, "backup_retention_days"))
    except Exception as exc:  # noqa: BLE001 -- a backup bug must never take the cycle down
        print(f"backup run_daily raised: {exc}", flush=True)


def _maybe_run_integration_health() -> None:
    """Instance-wide, not per-user -- same reuse-this-tick-loop reasoning
    as _maybe_run_backup just above (see integration_health.py's own
    module docstring for the interval gating and per-integration cost
    tradeoffs). Never lets a health-check bug take the cycle down."""
    try:
        import integration_health
        integration_health.tick()
    except Exception as exc:  # noqa: BLE001 -- a health-check bug must never take the cycle down
        print(f"integration_health.tick raised: {exc}", flush=True)


def run_cycle_once() -> int:
    """One pass over every active user. Returns how many proactive
    messages were actually sent -- exposed so real tests don't have to
    wait on the real interval."""
    _maybe_run_backup()
    _maybe_run_integration_health()
    sent = 0
    for user in accounts.all_active_users():
        _maybe_reflect(user["id"])
        _maybe_compact(user["id"], user["display_name"])
        # Independent of every gate below -- see _check_due_schedules'/
        # _fire_reminder's own module comments for why neither a
        # scheduled task's nor a reminder's own due-check goes through
        # ping_enabled/_within_window/_recently_active.
        _check_due_schedules(user)
        _check_due_reminders(user)

        if not config.get("user", user["id"], "ping_enabled"):
            continue
        if _recently_active(user["id"]):
            continue
        if not _within_window(user["id"]):
            continue
        # Best-effort early skip (racy by design, see turns.in_flight()) --
        # avoids computing outstanding_reason() just to get queued behind a
        # real conversational turn; turns.run() inside _send_proactive is
        # the actual correctness guarantee either way.
        if turns.in_flight(user["id"]):
            continue
        outcome = outstanding_reason(user["id"])
        if not outcome:
            continue
        # last_fired_ts only advances on a CONFIRMED send, never on a
        # merely-selected candidate -- a queued/blocked turn must not
        # cost this signal its next real chance to fire.
        if _send_proactive(user, outcome["reason"]) == "sent":
            mark_signal_fired(user["id"], outcome["key"], outcome["dedup_token"])
            sent += 1
    return sent


def start() -> None:
    """Idempotent -- safe to call more than once (e.g. accidentally from
    two import paths); only the first call actually starts the thread."""
    global _started
    with _lock:
        if _started:
            return
        _started = True
    def _loop():
        while True:
            try:
                run_cycle_once()
            except Exception:  # noqa: BLE001 -- the cycle must never kill the thread
                pass
            time.sleep(INTERVAL_S)
    threading.Thread(target=_loop, daemon=True).start()


# reminders is already imported above, at this module's own top -- its
# own register_scheduler_signal() couldn't call back into scheduler at
# ITS import time (this module would still be mid-import, before
# register_signal existed yet -- a real cycle, not hypothetical). Called
# here instead, now that this module has fully finished defining itself.
# household.py/meals.py/notes.py/tasks.py/email_calendar.py have no such
# cycle (scheduler.py never imports any of them) and register themselves
# normally, at their own import time.
reminders.register_scheduler_signal()
