# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Personal data-point tracking (2026-09-16) -- water, weight, blood
pressure, medicine, sleep, mood, and whatever comes after (the
operator's own examples): she can define new tracker TYPES herself, not
pick from a fixed list, then log dated entries against them.

**The real design decision, settled before writing a line of this**
(the operator's own framing: "a type carrying a declared value-kind is
the obvious answer but it's a real design decision"): free-text-only
values would make history unqueryable; strictly-numeric-only values
would leave medicine ("did I take it") and mood ("a word, or a scale")
with nowhere honest to go. So every type declares a `value_kind` up
front, one of VALUE_KINDS below, and every entry's actual value columns
(value_1/value_2/value_text on `tracker_entries`) are validated against
whatever that type promised, at log time -- not floated on trust. See
store.py's own schema comment for the exact shape each kind gets and why.

**Disable is reversible; delete only works when empty** (2026-09-16,
revised after his own bug report: disabling a type used to be a one-way
trap with no edit and no delete, so a type created with the wrong unit
was stuck forever, and its old name blocked a fresh replacement too).
A disabled type's own existing entries are untouched and stay fully
queryable through history_for_type(); enabled=0 only gates NEW logging,
and enable_type() reverses it. delete_type() only succeeds when a type
has zero entries -- once it's ever been logged against, disable is the
only way to retire it, same reasoning tasks.py/notes.py apply to their
own objects with real history. update_type() lets unit/value_kind/name
be fixed in place (unit/value_kind lock the moment a first entry
exists; name never does). A disabled type's name no longer blocks a
new type of the same name either -- see add_type()'s own comment.

**History returns both raw and summarized** -- the operator's own
question, answered directly: raw entries alone are fine for a handful of
weight readings, useless for judging a week of water logs at a glance;
summary-only throws away the actual numbers. history_for_type() always
returns both -- entries (bounded to a real time window, default the last
7 days, same "explicit override, sane default" shape ha_get_history
already established) and a summary shaped by the type's own value_kind
(count/avg/min/max always where numeric; sum ONLY for 'number' -- summing
blood pressure or a mood scale is meaningless, summing total ounces of
water isn't; a taken-rate fraction for 'boolean'; no numeric summary at
all for 'text', just a count).

**No board card, deliberately -- but a real page exists.** A tracker is
a time series, not a static card someone glances at and acts on once --
the board's own shape (WISHLIST.md's card surface) doesn't obviously
fit a stream of water-log entries the way a task or a note does, so
there's still no board card for one, only a single button pointing
elsewhere (his own instruction: "reachable from the board but don't
clutter it as items"). His own UI for viewing/updating types and
entries lives at `/trackers` (2026-09-17, moved out of Settings onto
its own first-class page, alongside /inventory and /meals) -- see
server.py's own trackers_page(). This module still owns her tool
surface only; the page calls trackers.py's functions directly with
actor="user", never through the tool wrappers, same convention every
other settings-page-turned-first-class-page POST handler in this app
uses.

**Peers: wired in** (2026-09-16, his own follow-up answer, once notes/
tasks/reminders/trackers all existed) -- see register_peer_actions()
below, standard trust ladder, same as schedules.py/homeassistant.py/
memory.py.

**Not connected to anything else** -- the operator's own note: this
lands the "health metrics" idea that's been sitting in his Nodrya note,
and touches things he's mentioned before (Nori pushing water intake,
a peer's own motivation role), but none of that is built here. This module
is the data model and her own tools alone.
"""
from __future__ import annotations

import json
import time

import store
import tasks  # for tasks.parse_when() -- see that function's own docstring: reused, not duplicated

VALUE_KINDS = ("number", "pair", "boolean", "scale", "text")

_UNSET = object()

# Aggregation window default (2026-09-16) -- same "explicit override,
# sane default" shape homeassistant.py's own ha_get_history already
# established for the identical problem (a time-series read needs SOME
# bound or it dumps an unbounded history into her prompt).
DEFAULT_HISTORY_DAYS = 7
MAX_HISTORY_DAYS = 365
MAX_HISTORY_ENTRIES = 500


def _build_value_meta(value_kind: str, *, component_1_label: str | None = None,
                      component_2_label: str | None = None, scale_min: int | None = None,
                      scale_max: int | None = None, scale_min_label: str | None = None,
                      scale_max_label: str | None = None, true_label: str | None = None,
                      false_label: str | None = None) -> tuple[str | None, dict | None]:
    """Returns (error, value_meta). Only the fields the declared kind
    actually needs are required -- a scale_min passed alongside
    value_kind='number' is simply ignored, not an error, since the tool
    schema offers every kind's params together (a JSON schema can't
    easily express "these three are required only if that enum value is
    X")."""
    if value_kind == "pair":
        if not (component_1_label or "").strip() or not (component_2_label or "").strip():
            return "value_kind='pair' needs component_1_label and component_2_label (e.g. 'systolic'/'diastolic')", None
        return None, {"labels": [component_1_label.strip(), component_2_label.strip()]}
    if value_kind == "scale":
        if scale_min is None or scale_max is None:
            return "value_kind='scale' needs scale_min and scale_max", None
        if scale_min >= scale_max:
            return "scale_min must be less than scale_max", None
        return None, {"min": scale_min, "max": scale_max,
                      "min_label": (scale_min_label or "").strip() or None,
                      "max_label": (scale_max_label or "").strip() or None}
    if value_kind == "boolean":
        return None, {"true_label": (true_label or "").strip() or "taken",
                      "false_label": (false_label or "").strip() or "not taken"}
    if value_kind in ("number", "text"):
        return None, {}
    return f"value_kind must be one of: {', '.join(VALUE_KINDS)}", None


def add_type(session: dict, *, name: str, value_kind: str, unit: str | None = None,
            component_1_label: str | None = None, component_2_label: str | None = None,
            scale_min: int | None = None, scale_max: int | None = None,
            scale_min_label: str | None = None, scale_max_label: str | None = None,
            true_label: str | None = None, false_label: str | None = None,
            created_by_type: str = "user", created_by_peer_name: str | None = None) -> dict:
    name = (name or "").strip()
    if not name:
        return {"error": "name can't be empty"}
    err, meta = _build_value_meta(value_kind, component_1_label=component_1_label,
                                  component_2_label=component_2_label, scale_min=scale_min,
                                  scale_max=scale_max, scale_min_label=scale_min_label,
                                  scale_max_label=scale_max_label, true_label=true_label,
                                  false_label=false_label)
    if err:
        return {"error": err}
    # Only an ENABLED type reserves its name (2026-09-16, his own bug
    # report: a disabled "coffee" blocked a fresh "coffee" forever, with
    # no way out -- checked catalog.py's own add()/rename() and found
    # the identical gap there, flagged separately rather than fixed here
    # since he only reported this for trackers). A disabled type is
    # inert by design (disable_type's own docstring: "existing entries
    # are untouched... it only gates log_entry() going forward") -- it
    # shouldn't also gate NEW types from using its old name.
    existing = store.read(lambda c: c.execute(
        "SELECT 1 FROM tracker_types WHERE user_id=? AND name=? AND enabled=1",
        (session["user_id"], name)).fetchone())
    if existing:
        return {"error": f"an active tracker type named {name!r} already exists -- rename or disable "
                         f"it first, or edit it directly (tracker_type_update) instead of adding a "
                         f"duplicate"}
    disabled_dup = store.read(lambda c: c.execute(
        "SELECT 1 FROM tracker_types WHERE user_id=? AND name=? AND enabled=0",
        (session["user_id"], name)).fetchone())
    now = time.time()
    def _w(c):
        return c.execute(
            "INSERT INTO tracker_types(user_id, name, value_kind, unit, value_meta, enabled, "
            "created_by_type, created_by_peer_name, created_ts) VALUES (?,?,?,?,?,1,?,?,?)",
            (session["user_id"], name, value_kind, (unit or "").strip() or None, json.dumps(meta),
             created_by_type, created_by_peer_name, now)).lastrowid
    tid = store.write(_w)
    _log_event("type", tid, created_by_type, created_by_peer_name, "created")
    result = {"ok": True, "type_id": tid}
    if disabled_dup:
        result["note"] = (f"a disabled tracker type also named {name!r} already exists -- this is a "
                          f"separate, new type; call tracker_type_enable on the old one instead if you "
                          f"actually meant to keep logging under it")
    return result


def _get_type_owned(session: dict, type_id: int) -> dict | None:
    row = store.read(lambda c: c.execute("SELECT * FROM tracker_types WHERE id=?", (type_id,)).fetchone())
    if row is None or row["user_id"] != session["user_id"]:
        return None
    d = dict(row)
    try:
        d["value_meta"] = json.loads(d["value_meta"]) if d.get("value_meta") else {}
    except (ValueError, TypeError):
        d["value_meta"] = {}
    return d


def _set_type_enabled(session: dict, type_id: int, enabled: bool, *, actor: str = "user",
                      actor_peer_name: str | None = None) -> dict:
    row = _get_type_owned(session, type_id)
    if row is None:
        return {"error": "no such tracker type"}
    new_val = 1 if enabled else 0
    if row["enabled"] == new_val:
        return {"ok": True}  # no-op -- nothing to log
    if enabled:
        dup = store.read(lambda c: c.execute(
            "SELECT 1 FROM tracker_types WHERE user_id=? AND name=? AND enabled=1 AND id!=?",
            (session["user_id"], row["name"], type_id)).fetchone())
        if dup:
            return {"error": f"can't re-enable -- an active tracker type named {row['name']!r} "
                             f"already exists. Rename one of them first."}
    store.write(lambda c: c.execute("UPDATE tracker_types SET enabled=? WHERE id=?", (new_val, type_id)))
    _log_event("type", type_id, actor, actor_peer_name, "enabled" if enabled else "disabled")
    return {"ok": True}


def entry_count_for_type(session: dict, type_id: int) -> int:
    """The real total, not the windowed count history_for_type()'s own
    listing shows -- what update_type()/delete_type() actually gate on,
    exposed here so the /trackers page can show the same lock state
    without duplicating that query or guessing from a windowed number."""
    row = _get_type_owned(session, type_id)
    if row is None:
        return 0
    return store.read(lambda c: c.execute(
        "SELECT COUNT(*) AS n FROM tracker_entries WHERE type_id=?", (type_id,)).fetchone())["n"]


def disable_type(session: dict, type_id: int, *, actor: str = "user", actor_peer_name: str | None = None) -> dict:
    """Existing entries are completely untouched and stay queryable
    through history_for_type() regardless of this flag; it only gates
    log_entry() going forward. Reversible -- see enable_type() below
    (2026-09-16, his own bug report: there was no way back from this at
    all, which is exactly what trapped his mistaken 'coffee' type)."""
    return _set_type_enabled(session, type_id, False, actor=actor, actor_peer_name=actor_peer_name)


def enable_type(session: dict, type_id: int, *, actor: str = "user", actor_peer_name: str | None = None) -> dict:
    """The other half of disable_type() -- didn't exist before
    (2026-09-16, his own bug report). Refuses if an already-active type
    has the same name (can't have two active types sharing one name),
    with a clear reason rather than a bare conflict."""
    return _set_type_enabled(session, type_id, True, actor=actor, actor_peer_name=actor_peer_name)


def delete_type(session: dict, type_id: int, *, actor: str = "user", actor_peer_name: str | None = None) -> dict:
    """Real delete, but ONLY when nothing has ever been logged against
    this type (2026-09-16, his own bug report: "allow delete, at least
    for a type with no entries"). A type with real entries stays
    disable-only -- deleting it would orphan those tracker_entries rows
    (no FK enforces a cascade here, same as every event/entry table in
    this app not being FK'd to its own parent, but silently orphaning
    real logged data was never the point of that choice). tracker_events
    for this type survive the type being gone anyway, same as every
    other delete-with-history in this app."""
    row = _get_type_owned(session, type_id)
    if row is None:
        return {"error": "no such tracker type"}
    entry_count = store.read(lambda c: c.execute(
        "SELECT COUNT(*) AS n FROM tracker_entries WHERE type_id=?", (type_id,)).fetchone())["n"]
    if entry_count:
        return {"error": f"{row['name']!r} has {entry_count} logged "
                         f"{'entry' if entry_count == 1 else 'entries'} -- delete only works on a type "
                         f"with nothing logged yet. Disable it instead (tracker_type_disable)."}
    _log_event("type", type_id, actor, actor_peer_name, "deleted", note=f"deleted {row['name']!r} (no entries)")
    store.write(lambda c: c.execute("DELETE FROM tracker_types WHERE id=?", (type_id,)))
    return {"ok": True}


def update_type(session: dict, type_id: int, *, name=_UNSET, value_kind=_UNSET, unit=_UNSET,
                component_1_label=_UNSET, component_2_label=_UNSET, scale_min=_UNSET, scale_max=_UNSET,
                scale_min_label=_UNSET, scale_max_label=_UNSET, true_label=_UNSET, false_label=_UNSET,
                actor: str = "user", actor_peer_name: str | None = None) -> dict:
    """Editing a type in place -- the real fix for his bug report ("she
    disabled it, but can't edit it and can't delete it... a naming
    conflict traps the mistake permanently"). His own read was right:
    for a type with nothing logged yet, unit/value_kind/labels are all
    freely editable, same as at creation -- there's nothing to protect.

    Once a first entry exists, unit/value_kind/value_meta LOCK. Renaming
    still works regardless (a name is just the type's own label, never
    read by _validate_value -- it can't misrepresent an entry's value).
    Refuses the structural change outright rather than converting or
    annotating (the other two options he named): there's no unit-
    conversion table in this app, and there's no honest way to
    "annotate" a bare number without a human deciding what it actually
    meant -- refusing is the only choice that can never quietly corrupt
    a real logged value. The message says exactly what to do instead:
    add a new type for the new unit/kind."""
    row = _get_type_owned(session, type_id)
    if row is None:
        return {"error": "no such tracker type"}
    structural = (value_kind, unit, component_1_label, component_2_label, scale_min, scale_max,
                 scale_min_label, scale_max_label, true_label, false_label)
    changing_structure = any(f is not _UNSET for f in structural)
    entry_count = 0
    if changing_structure:
        entry_count = store.read(lambda c: c.execute(
            "SELECT COUNT(*) AS n FROM tracker_entries WHERE type_id=?", (type_id,)).fetchone())["n"]
        if entry_count:
            return {"error": f"{row['name']!r} already has {entry_count} logged "
                             f"{'entry' if entry_count == 1 else 'entries'} recorded under its current "
                             f"unit/kind -- changing unit or value_kind now would misrepresent them, so "
                             f"this is refused. Add a new tracker type for the new unit/kind instead (you "
                             f"can still rename THIS one, or disable it once its replacement exists). "
                             f"Unit/value_kind can only be edited freely on a type with no entries yet."}
    changes = {}
    new_name = row["name"]
    if name is not _UNSET:
        new_name = (name or "").strip()
        if not new_name:
            return {"error": "name can't be empty"}
        if new_name != row["name"]:
            dup = store.read(lambda c: c.execute(
                "SELECT 1 FROM tracker_types WHERE user_id=? AND name=? AND enabled=1 AND id!=?",
                (session["user_id"], new_name, type_id)).fetchone())
            if dup:
                return {"error": f"an active tracker type named {new_name!r} already exists"}
    new_kind, new_unit, new_meta = row["value_kind"], row["unit"], row["value_meta"]
    if changing_structure:
        new_kind = value_kind if value_kind is not _UNSET else row["value_kind"]
        new_unit = unit if unit is not _UNSET else row["unit"]
        old_meta = row["value_meta"]
        labels = old_meta.get("labels") or [None, None]
        err, new_meta = _build_value_meta(
            new_kind,
            component_1_label=component_1_label if component_1_label is not _UNSET else labels[0],
            component_2_label=component_2_label if component_2_label is not _UNSET else labels[1],
            scale_min=scale_min if scale_min is not _UNSET else old_meta.get("min"),
            scale_max=scale_max if scale_max is not _UNSET else old_meta.get("max"),
            scale_min_label=scale_min_label if scale_min_label is not _UNSET else old_meta.get("min_label"),
            scale_max_label=scale_max_label if scale_max_label is not _UNSET else old_meta.get("max_label"),
            true_label=true_label if true_label is not _UNSET else old_meta.get("true_label"),
            false_label=false_label if false_label is not _UNSET else old_meta.get("false_label"))
        if err:
            return {"error": err}
    store.write(lambda c: c.execute(
        "UPDATE tracker_types SET name=?, value_kind=?, unit=?, value_meta=? WHERE id=?",
        (new_name, new_kind, (new_unit or "").strip() or None, json.dumps(new_meta), type_id)))
    if new_name != row["name"]:
        changes["name"] = {"old": row["name"], "new": new_name}
    if new_kind != row["value_kind"]:
        changes["value_kind"] = {"old": row["value_kind"], "new": new_kind}
    if new_unit != row["unit"]:
        changes["unit"] = {"old": row["unit"], "new": new_unit}
    if changes:
        _log_event("type", type_id, actor, actor_peer_name, "updated", changes=changes)
    return {"ok": True}


def list_types(session: dict, *, enabled_only: bool = False) -> dict:
    if enabled_only:
        rows = store.read(lambda c: c.execute(
            "SELECT * FROM tracker_types WHERE user_id=? AND enabled=1 ORDER BY name",
            (session["user_id"],)).fetchall())
    else:
        rows = store.read(lambda c: c.execute(
            "SELECT * FROM tracker_types WHERE user_id=? ORDER BY name", (session["user_id"],)).fetchall())
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["value_meta"] = json.loads(d["value_meta"]) if d.get("value_meta") else {}
        except (ValueError, TypeError):
            d["value_meta"] = {}
        out.append(d)
    return {"types": out}


def _validate_value(type_row: dict, *, value_1, value_2, value_text) -> str | None:
    """The actual enforcement that makes a declared value_kind mean
    something -- an entry logged against a type must match the shape
    that type promised, checked here, not left to whoever's calling to
    get right."""
    kind = type_row["value_kind"]
    meta = type_row["value_meta"]
    if kind == "number":
        if value_1 is None:
            return "this type needs a numeric value_1"
        if value_2 is not None or value_text is not None:
            return "value_kind='number' takes only value_1"
    elif kind == "pair":
        if value_1 is None or value_2 is None:
            return f"this type needs both values ({meta.get('labels', ['value_1', 'value_2'])[0]}, " \
                  f"{meta.get('labels', ['value_1', 'value_2'])[1]})"
        if value_text is not None:
            return "value_kind='pair' doesn't take value_text"
    elif kind == "boolean":
        if value_1 is None:
            return "this type needs value_1 as 0/1 (or true/false)"
        if value_1 not in (0, 1, 0.0, 1.0):
            return "value_kind='boolean' -- value_1 must be 0 or 1"
        if value_2 is not None or value_text is not None:
            return "value_kind='boolean' takes only value_1"
    elif kind == "scale":
        if value_1 is None:
            return "this type needs an integer value_1"
        lo, hi = meta.get("min"), meta.get("max")
        if lo is not None and hi is not None and not (lo <= value_1 <= hi):
            return f"value_1 must be between {lo} and {hi} for this type"
        if value_2 is not None or value_text is not None:
            return "value_kind='scale' takes only value_1"
    elif kind == "text":
        if not (value_text or "").strip():
            return "this type needs a non-empty value_text"
        if value_1 is not None or value_2 is not None:
            return "value_kind='text' takes only value_text"
    return None


def log_entry(session: dict, *, type_id: int, value_1: float | None = None, value_2: float | None = None,
             value_text: str | None = None, note: str | None = None, ts=None,
             created_by_type: str = "user", created_by_peer_name: str | None = None) -> dict:
    type_row = _get_type_owned(session, type_id)
    if type_row is None:
        return {"error": "no such tracker type"}
    if not type_row["enabled"]:
        return {"error": f"tracker type {type_row['name']!r} is disabled -- re-enable it before logging"}
    err = _validate_value(type_row, value_1=value_1, value_2=value_2, value_text=value_text)
    if err:
        return {"error": err}
    when = tasks.parse_when(ts, session["user_id"]) if ts else time.time()
    if ts and when is None:
        return {"error": f"couldn't understand ts {ts!r}"}
    now = time.time()
    def _w(c):
        return c.execute(
            "INSERT INTO tracker_entries(type_id, user_id, value_1, value_2, value_text, note, ts, "
            "created_ts, created_by_type, created_by_peer_name) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (type_id, session["user_id"], value_1, value_2, value_text.strip() if value_text else None,
             note.strip() if note else None, when, now, created_by_type, created_by_peer_name)).lastrowid
    eid = store.write(_w)
    _log_event("entry", eid, created_by_type, created_by_peer_name, "created")
    return {"ok": True, "entry_id": eid}


def _get_entry_owned(session: dict, entry_id: int) -> dict | None:
    row = store.read(lambda c: c.execute("SELECT * FROM tracker_entries WHERE id=?", (entry_id,)).fetchone())
    if row is None or row["user_id"] != session["user_id"]:
        return None
    return dict(row)


def update_entry(session: dict, entry_id: int, *, value_1=_UNSET, value_2=_UNSET, value_text=_UNSET,
                 note=_UNSET, ts=_UNSET, actor: str = "user", actor_peer_name: str | None = None) -> dict:
    row = _get_entry_owned(session, entry_id)
    if row is None:
        return {"error": "no such entry"}
    type_row = _get_type_owned(session, row["type_id"])
    if type_row is None:
        return {"error": "the tracker type this entry belongs to no longer exists"}
    merged = dict(row)
    for key, val in (("value_1", value_1), ("value_2", value_2), ("value_text", value_text), ("note", note)):
        if val is not _UNSET:
            merged[key] = val
    if ts is not _UNSET:
        when = tasks.parse_when(ts, session["user_id"]) if ts else None
        if ts and when is None:
            return {"error": f"couldn't understand ts {ts!r}"}
        merged["ts"] = when if when is not None else row["ts"]
    err = _validate_value(type_row, value_1=merged["value_1"], value_2=merged["value_2"],
                          value_text=merged["value_text"])
    if err:
        return {"error": err}
    final = {**merged, "value_text": (merged["value_text"].strip() if merged.get("value_text") else None),
            "note": (merged["note"].strip() if merged.get("note") else None)}
    store.write(lambda c: c.execute(
        "UPDATE tracker_entries SET value_1=?, value_2=?, value_text=?, note=?, ts=? WHERE id=?",
        (final["value_1"], final["value_2"], final["value_text"], final["note"], final["ts"], entry_id)))
    changes = {}
    for f in ("value_1", "value_2", "value_text", "note", "ts"):
        if row.get(f) != final.get(f):
            changes[f] = {"old": row.get(f), "new": final.get(f)}
    if changes:
        _log_event("entry", entry_id, actor, actor_peer_name, "updated", changes=changes)
    return {"ok": True}


def _summarize(type_row: dict, entries: list[dict]) -> dict:
    """Shaped by value_kind -- sum only ever appears for 'number' (the
    one kind where adding up the values means something real, e.g.
    total ounces of water this week); 'pair'/'scale' get avg/min/max per
    component but never a sum; 'boolean' gets a taken-rate fraction;
    'text' gets a plain count, no numeric summary at all."""
    kind = type_row["value_kind"]
    n = len(entries)
    if n == 0:
        return {"count": 0}
    if kind == "number":
        vals = [e["value_1"] for e in entries]
        return {"count": n, "sum": sum(vals), "avg": sum(vals) / n, "min": min(vals), "max": max(vals)}
    if kind == "pair":
        v1 = [e["value_1"] for e in entries]
        v2 = [e["value_2"] for e in entries]
        labels = type_row["value_meta"].get("labels", ["value_1", "value_2"])
        return {"count": n,
               labels[0]: {"avg": sum(v1) / n, "min": min(v1), "max": max(v1)},
               labels[1]: {"avg": sum(v2) / n, "min": min(v2), "max": max(v2)}}
    if kind == "scale":
        vals = [e["value_1"] for e in entries]
        return {"count": n, "avg": sum(vals) / n, "min": min(vals), "max": max(vals)}
    if kind == "boolean":
        taken = sum(1 for e in entries if e["value_1"])
        return {"count": n, "taken": taken, "rate": taken / n}
    return {"count": n}  # 'text' -- nothing numeric to summarize


def history_for_type(session: dict, type_id: int, *, since=None, until=None) -> dict:
    """Always returns both raw entries and a summary -- the operator's
    own question, answered directly rather than picking one (see the
    module docstring's own reasoning). `since` goes through
    tasks.parse_since() (2026-09-18, real bug fixed: "today" used to go
    through parse_when, which resolves it to 23:59 TODAY -- after "now"
    for all but the last minute of the day, so since="today" produced
    an inverted, always-empty range; reproduced against his real water-
    tracker data before this fix existed). `until` still uses
    parse_when -- "until today" meaning end-of-day is the right
    ceiling reading for an upper bound, unlike since. `since` defaults
    to DEFAULT_HISTORY_DAYS ago, same "explicit override, sane default"
    shape ha_get_history already uses, so a bare call never dumps an
    unbounded history into her prompt."""
    type_row = _get_type_owned(session, type_id)
    if type_row is None:
        return {"error": "no such tracker type"}
    now = time.time()
    uid = session["user_id"]
    since_ts = tasks.parse_since(since, uid) if since else now - DEFAULT_HISTORY_DAYS * 86400
    if since and since_ts is None:
        return {"error": f"couldn't understand since {since!r}"}
    until_ts = tasks.parse_when(until, uid) if until else now
    if until and until_ts is None:
        return {"error": f"couldn't understand until {until!r}"}
    if now - since_ts > MAX_HISTORY_DAYS * 86400:
        since_ts = now - MAX_HISTORY_DAYS * 86400
    rows = store.read(lambda c: c.execute(
        "SELECT * FROM tracker_entries WHERE type_id=? AND ts>=? AND ts<=? ORDER BY ts",
        (type_id, since_ts, until_ts)).fetchall())
    entries = [dict(r) for r in rows]
    truncated = len(entries) > MAX_HISTORY_ENTRIES
    if truncated:
        entries = entries[-MAX_HISTORY_ENTRIES:]
    return {"type": type_row, "since_ts": since_ts, "until_ts": until_ts,
           "entries": entries, "summary": _summarize(type_row, entries),
           "truncated": truncated}


def _log_event(subject: str, subject_id: int, actor: str, actor_peer_name: str | None, action: str,
              changes: dict | None = None, note: str | None = None) -> None:
    store.write(lambda c: c.execute(
        "INSERT INTO tracker_events(subject, subject_id, ts, actor, actor_peer_name, action, changes, note) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (subject, subject_id, time.time(), actor, actor_peer_name, action,
         json.dumps(changes) if changes else None, note)))


def history_for(subject: str, subject_id: int, limit: int = 50) -> list[dict]:
    rows = store.read(lambda c: c.execute(
        "SELECT * FROM tracker_events WHERE subject=? AND subject_id=? ORDER BY ts DESC LIMIT ?",
        (subject, subject_id, limit)).fetchall())
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["changes"] = json.loads(d["changes"]) if d.get("changes") else {}
        except (ValueError, TypeError):
            d["changes"] = {}
        out.append(d)
    return out


# ── tool registration ────────────────────────────────────────────────────
def _tracker_type_add_impl(session: dict, **kw) -> dict:
    import peers
    actor, actor_peer_name = peers.actor_for(session)
    return add_type(session, created_by_type=actor, created_by_peer_name=actor_peer_name, **kw)


def _tracker_type_disable_impl(session: dict, *, type_id: int) -> dict:
    import peers
    actor, actor_peer_name = peers.actor_for(session)
    return disable_type(session, type_id, actor=actor, actor_peer_name=actor_peer_name)


def _tracker_type_enable_impl(session: dict, *, type_id: int) -> dict:
    import peers
    actor, actor_peer_name = peers.actor_for(session)
    return enable_type(session, type_id, actor=actor, actor_peer_name=actor_peer_name)


def _tracker_type_delete_impl(session: dict, *, type_id: int) -> dict:
    import peers
    actor, actor_peer_name = peers.actor_for(session)
    return delete_type(session, type_id, actor=actor, actor_peer_name=actor_peer_name)


def _tracker_type_update_impl(session: dict, *, type_id: int, **kw) -> dict:
    import peers
    actor, actor_peer_name = peers.actor_for(session)
    return update_type(session, type_id, actor=actor, actor_peer_name=actor_peer_name, **kw)


def _tracker_type_list_impl(session: dict, *, enabled_only: bool = False) -> dict:
    return list_types(session, enabled_only=enabled_only)


def _tracker_log_impl(session: dict, **kw) -> dict:
    import peers
    actor, actor_peer_name = peers.actor_for(session)
    return log_entry(session, created_by_type=actor, created_by_peer_name=actor_peer_name, **kw)


def _tracker_entry_update_impl(session: dict, *, entry_id: int, **kw) -> dict:
    import peers
    actor, actor_peer_name = peers.actor_for(session)
    return update_entry(session, entry_id, actor=actor, actor_peer_name=actor_peer_name, **kw)


def _tracker_history_impl(session: dict, *, type_id: int, since: str | None = None,
                          until: str | None = None) -> dict:
    return history_for_type(session, type_id, since=since, until=until)


def _register_tools() -> None:
    import tools  # local: same reasoning as household.py/memory.py/schedules.py/tasks.py/notes.py

    value_kind_props = {
        "value_kind": {"type": "string", "enum": list(VALUE_KINDS),
                       "description": "'number' (one value + a unit -- water in oz, weight in lbs). "
                                     "'pair' (two labeled values sharing a unit -- blood pressure). "
                                     "'boolean' (took it or didn't -- medicine). 'scale' (an integer "
                                     "within a min/max -- mood as 1-5). 'text' (anything that doesn't "
                                     "reduce to a number -- mood as a word)."},
        "unit": {"type": "string", "description": "e.g. 'oz', 'lbs', 'mmHg'. Leave out for boolean/scale/text."},
        "component_1_label": {"type": "string", "description": "For value_kind='pair' only, e.g. 'systolic'."},
        "component_2_label": {"type": "string", "description": "For value_kind='pair' only, e.g. 'diastolic'."},
        "scale_min": {"type": "integer", "description": "For value_kind='scale' only."},
        "scale_max": {"type": "integer", "description": "For value_kind='scale' only."},
        "scale_min_label": {"type": "string", "description": "Optional, for value_kind='scale'."},
        "scale_max_label": {"type": "string", "description": "Optional, for value_kind='scale'."},
        "true_label": {"type": "string", "description": "Optional, for value_kind='boolean' (default 'taken')."},
        "false_label": {"type": "string", "description": "Optional, for value_kind='boolean' (default 'not taken')."},
    }

    tools.register(tools.Tool(
        "tracker_type_add",
        {"type": "function", "function": {
            "name": "tracker_type_add",
            "description": "Define a new kind of thing to track -- water, weight, blood pressure, "
                           "medicine, sleep, mood, or anything else he wants logged over time. Not a "
                           "fixed list -- you can create new types as they come up.",
            "parameters": {"type": "object", "properties": {"name": {"type": "string"}, **value_kind_props},
                           "required": ["name", "value_kind"]}}},
        _tracker_type_add_impl, min_role="member", data_scope="self", risk_tier="B"))

    tools.register(tools.Tool(
        "tracker_type_disable",
        {"type": "function", "function": {
            "name": "tracker_type_disable",
            "description": "Disable a tracker type -- never deletes it. Its existing logged entries "
                           "stay exactly as they are and remain readable via tracker_history; this "
                           "only stops new entries being logged against it.",
            "parameters": {"type": "object", "properties": {"type_id": {"type": "integer"}},
                           "required": ["type_id"]}}},
        _tracker_type_disable_impl, min_role="member", data_scope="self", risk_tier="B"))

    tools.register(tools.Tool(
        "tracker_type_enable",
        {"type": "function", "function": {
            "name": "tracker_type_enable",
            "description": "Re-enable a disabled tracker type so it can be logged against again. "
                           "Refuses if an active type already has this same name.",
            "parameters": {"type": "object", "properties": {"type_id": {"type": "integer"}},
                           "required": ["type_id"]}}},
        _tracker_type_enable_impl, min_role="member", data_scope="self", risk_tier="B"))

    tools.register(tools.Tool(
        "tracker_type_delete",
        {"type": "function", "function": {
            "name": "tracker_type_delete",
            "description": "Delete a tracker type permanently -- only works if nothing has been "
                           "logged against it yet. If it already has entries, disable it instead "
                           "(tracker_type_disable); its entries stay exactly as they are.",
            "parameters": {"type": "object", "properties": {"type_id": {"type": "integer"}},
                           "required": ["type_id"]}}},
        _tracker_type_delete_impl, min_role="member", data_scope="self", risk_tier="B"))

    tools.register(tools.Tool(
        "tracker_type_update",
        {"type": "function", "function": {
            "name": "tracker_type_update",
            "description": "Fix a tracker type created wrong -- rename it, or change its unit/value_kind "
                           "if nothing has been logged against it yet (created it assuming cups, he "
                           "actually wants oz: this is the tool for that, not adding a duplicate). Only "
                           "pass the fields you're actually changing. Once entries exist, unit/value_kind "
                           "lock -- changing them would misrepresent numbers already recorded under the "
                           "old meaning, so that's refused with a clear reason; renaming still works "
                           "regardless of how many entries exist.",
            "parameters": {"type": "object", "properties": {
                "type_id": {"type": "integer"}, "name": {"type": "string"}, **value_kind_props},
                "required": ["type_id"]}}},
        _tracker_type_update_impl, min_role="member", data_scope="self", risk_tier="B"))

    tools.register(tools.Tool(
        "tracker_type_list",
        {"type": "function", "function": {
            "name": "tracker_type_list",
            "description": "List his tracker types -- so you know what already exists (and each "
                           "one's real id and declared value shape) before creating a duplicate or "
                           "logging against one.",
            "parameters": {"type": "object", "properties": {
                "enabled_only": {"type": "boolean", "description": "Default false -- includes disabled types too."}}}}},
        _tracker_type_list_impl, min_role="member", data_scope="self", risk_tier="A"))

    tools.register(tools.Tool(
        "tracker_log",
        {"type": "function", "function": {
            "name": "tracker_log",
            "description": "Log one data point against an existing tracker type. The value fields "
                           "you pass must match that type's own declared value_kind (check with "
                           "tracker_type_list if unsure) -- e.g. a 'pair' type needs both value_1 and "
                           "value_2, a 'text' type needs value_text, a 'boolean' type needs value_1 as "
                           "0 or 1.",
            "parameters": {"type": "object", "properties": {
                "type_id": {"type": "integer"},
                "value_1": {"type": "number"}, "value_2": {"type": "number"},
                "value_text": {"type": "string"},
                "note": {"type": "string", "description": "Optional free-text context."},
                "ts": {"type": "string", "description": "Optional -- when this actually happened, if "
                                                         "not right now (e.g. 'yesterday', '3 hours "
                                                         "ago', an ISO date/datetime). Omit to use "
                                                         "the current time."}},
                "required": ["type_id"]}}},
        _tracker_log_impl, min_role="member", data_scope="self", risk_tier="B"))

    tools.register(tools.Tool(
        "tracker_entry_update",
        {"type": "function", "function": {
            "name": "tracker_entry_update",
            "description": "Correct one of his existing logged entries -- only pass the fields "
                           "you're actually changing.",
            "parameters": {"type": "object", "properties": {
                "entry_id": {"type": "integer"},
                "value_1": {"type": "number"}, "value_2": {"type": "number"},
                "value_text": {"type": "string"}, "note": {"type": "string"},
                "ts": {"type": "string"}},
                "required": ["entry_id"]}}},
        _tracker_entry_update_impl, min_role="member", data_scope="self", risk_tier="B"))

    tools.register(tools.Tool(
        "tracker_history",
        {"type": "function", "function": {
            "name": "tracker_history",
            "description": "Pull history for one tracker type -- returns both the raw entries in "
                           "the window and a summary shaped by that type's own value_kind (average/ "
                           "min/max for numeric types, a total for 'number' specifically, a taken-rate "
                           "for 'boolean'). Defaults to the last 7 days if you don't say otherwise.",
            "parameters": {"type": "object", "properties": {
                "type_id": {"type": "integer"},
                "since": {"type": "string", "description": "Optional. 'today' or 'this week' (start "
                                                            "of that period, his local time), "
                                                            "'yesterday', an ISO date, or omit for "
                                                            "7 days ago."},
                "until": {"type": "string", "description": "Optional. Omit for now."}},
                "required": ["type_id"]}}},
        _tracker_history_impl, min_role="member", data_scope="self", risk_tier="A"))


_register_tools()


def register_peer_actions() -> None:
    """Deferred to server.py's own main(), same reasoning as memory.py's/
    homeassistant.py's/schedules.py's identical functions. All nine
    tools on the STANDARD trust ladder, no full_trust_only tier -- his
    own instruction (2026-09-16): "peers get access to everything just
    built... gated on trust level, same pattern as the settings tool
    and MCP toggles." tracker_type_delete needs no destructive-op
    softening the way note_delete has one -- delete_type() already
    refuses outright the moment a type has any entries at all (see its
    own docstring), at any trust level, for anyone; there's no real
    content to lose from deleting an empty, never-logged type
    definition, and its own tracker_events row survives the delete
    anyway. update_type() is similarly self-guarding (refuses to touch
    unit/value_kind once entries exist), so it needs no special
    handling here either."""
    import peers
    peers.register_peer_requestable("tracker_type_add")
    peers.register_peer_requestable("tracker_type_disable")
    peers.register_peer_requestable("tracker_type_enable")
    peers.register_peer_requestable("tracker_type_delete")
    peers.register_peer_requestable("tracker_type_update")
    peers.register_peer_requestable("tracker_type_list")
    peers.register_peer_requestable("tracker_log")
    peers.register_peer_requestable("tracker_entry_update")
    peers.register_peer_requestable("tracker_history")
