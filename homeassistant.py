# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Home Assistant integration -- Nori's way to read and control the smart
home. Native, not MCP (2026-09-15, design question the operator raised directly:
"he called it an MCP... check whether that's the right shape"). Same
reasoning that put web_search/web_fetch here instead of behind
mcp_servers.py: HA has an ordinary REST API, and every real requirement
here -- entity-level discovery kept structurally separate from what she
can actually see, per-entity peer reachability, one chokepoint for
logging and screening -- needs bespoke code regardless of transport.
mcp_servers.py's own design is for a THIRD-PARTY server whose tools are
discovered generically and locked to risk_tier='D' until a human reviews
each one; that model has no way to express "expose this ENTITY, not that
tool" without bespoke code sitting on top of it anyway -- at which point
the MCP layer would be pure overhead, not a shortcut. Going native means
this module owns the whole path end to end, same discipline webtools.py
already established: one chokepoint (_request()), screening on every
piece of live content that reaches a model (_screen_state()), ownership
scoping (workspace-wide -- the physical house is shared by the household,
not any one member), and a full audit log (ha_tool_log).

Discovery vs exposure, kept structurally separate (the operator's own explicit
requirement) as two different code paths, not one function with a filter
flag a caller could forget to pass:
  - discover_entities() -- admin-only, hits HA's real /api/states, upserts
    the FULL result into ha_entities. Never returns anything to a model;
    it's the admin discovery page's own data source, nothing else.
  - list_exposed_entities() and every tool impl below -- the ONLY way any
    model-facing code learns an entity exists at all. Every one of them
    reads `WHERE enabled=1` (or enabled_for_peers=1); there is no function
    in this module that hands the model-facing side the full discovered
    set, not even by accident -- structural, not a filter applied late
    that something could bypass.

Exposure -- two checkboxes per entity, the operator's own direct answer after I'd
floated a device-class severity scheme instead (2026-09-15, "use this
instead of any severity-based scheme... he decides which of his locks, if
any, [a sibling application] can touch -- rather than us inventing rules
about which device classes are dangerous"):
  - `enabled` -- Nori can see and use this entity, full stop. No separate
    read/write split for her own access; if it's on, she can read its
    state and control it.
  - `enabled_for_peers` -- a connected peer can ALSO reach this entity,
    subject to the same trust ladder (prompt/full) every other peer-
    requestable action already uses. Peer-enabled implies Nori-enabled --
    enforced in set_exposure() below (not just relied on in the admin
    page's own UI, which also enforces it) -- a peer reaching something
    Nori herself can't see would be incoherent.
  - No device-class distinction anywhere in this module any more: a lock
    and a light go through the exact same gate, the exact same tool
    (ha_control), the exact same peer trust ladder. The admin's own
    per-entity choice IS the policy; nothing here second-guesses it by
    domain.

Which flag a call is checked against is decided by _peer_act, a marker
peers.py sets on the session ONLY for the two places a peer's own request
actually executes after clearing the trust gate ( _make_act_impl's full-
trust branch, resolve_pending_action's approved branch) -- see peers.py's
own comment on that marker. Nori calling this tool on her own initiative,
even mid a conversation with a peer, is NOT peer_act -- session["_peer_act"]
is unset, and enabled (not enabled_for_peers) is what gates her.

Entity state is untrusted-ish content (the operator's own framing) -- it comes from
devices, not from anyone in the household. _screen_state() is the
chokepoint for it, one real ingest.summarize_untrusted() pass per call,
same mechanism every other untrusted-content path in this app already
uses. Deliberately NOT applied to list_exposed_entities()'s own roster
(entity_id/domain/friendly_name) -- those names were already reviewed by
an admin at the moment they checked the box to expose them, the same
human-review gate mcp_servers.py leans on for a discovered tool's own
description. What's never reviewed by a human, ongoing, is the live STATE
a device reports on every single call -- that's what actually gets
screened, every time, no exception for how often it's asked.

Failure legibility (2026-09-15, operator's own explicit ask, tied directly
to the confabulation pattern -- she should say what happened rather than
invent a reason): every real failure mode
below produces a distinct, honest string in the tool result, not a silent
gap she could paper over.
  - HA unreachable (network/timeout) -- _request()'s own connection-
    failed message, unchanged from urllib's real error text, same
    "no wording tell" precedent webtools.py's simulated-write path
    established, except this one is never simulated -- it's a real
    failure reported honestly.
  - Wrong/expired API key -- HA's own 401/403 gets a specific,
    unambiguous reason naming the credential, not a bare HTTP code.
  - An entity was exposed, then removed/renamed/unpaired in HA itself --
    get_state() diffs the exposed target set against what HA actually
    returned and reports a MISSING entity explicitly, per entity_id,
    rather than silently omitting it from the result. control() treats
    an empty response list from HA's own service-call endpoint (HA's own
    signal that zero entities matched) as a real failure, not a quiet
    success -- reporting "ok" when nothing actually happened is exactly
    the shape this guards against.
"""
from __future__ import annotations

import datetime
import json
import math
import os
import time
import urllib.error
import urllib.request

import store
import usertime

_TIMEOUT_S = 10
# 5MB, not 500KB (2026-09-15, raised after a real test against the operator's
# own instance: a real house is bigger than a demo -- /api/states came back
# at 642KB for 1378 entities, well past the original cap borrowed from
# webtools.py's own fetch limit. That limit exists there to bound an
# ARBITRARY, possibly-adversarial URL a model can ask this app to fetch;
# this endpoint is fixed and admin-configured, not attacker-reachable, so
# there's much less reason to hold it to the same number. Still capped,
# not unbounded -- a sane belt-and-suspenders ceiling against a genuinely
# pathological response, not a limit expected to bind in practice.
_MAX_BODY_BYTES = 5_000_000

# One domain->action->service map, not severity-split -- see module
# docstring. Every entity's own enabled/enabled_for_peers flag, plus
# peer trust level, is the whole policy; this map only says which real
# HA service a semantic action name maps to, and refuses an action name
# or domain it doesn't recognize.
_SERVICES = {
    "light": {"turn_on": "turn_on", "turn_off": "turn_off"},
    "switch": {"turn_on": "turn_on", "turn_off": "turn_off"},
    "fan": {"turn_on": "turn_on", "turn_off": "turn_off"},
    "climate": {"set_temperature": "set_temperature", "set_hvac_mode": "set_hvac_mode"},
    "lock": {"lock": "lock", "unlock": "unlock"},
    "cover": {"open": "open_cover", "close": "close_cover", "stop": "stop_cover"},
    # Widened 2026-09-15 (the operator's own ask, starting from one real
    # entity -- input_boolean.cage_lock, which despite the name/icon is an
    # input_boolean, not HA's lock domain -- turn_on/turn_off, no lock/
    # unlock vocabulary at all). Checked every domain here against the
    # operator's real 1378-entity house AND HA's own live service registry
    # (GET /api/services -- read-only, no entity touched to verify this)
    # before adding it; every action name below is a real HA service for
    # that domain, not a guess. Deliberately NOT every service that
    # domain has -- only the ones with one clear, unambiguous meaning;
    # left out deliberately, as needing more thought before exposing:
    # automation trigger, media_player's whole surface, alarm_control_panel.
    "input_boolean": {"turn_on": "turn_on", "turn_off": "turn_off"},
    "number": {"set_value": "set_value"},
    "input_number": {"set_value": "set_value"},
    "select": {"select_option": "select_option"},
    "input_select": {"select_option": "select_option"},
    "button": {"press": "press"},
    "scene": {"activate": "turn_on"},
    "script": {"run": "turn_on", "stop": "turn_off"},
    # automation: turn_on/turn_off only (enable/disable) -- NOT `trigger`,
    # a real, separate service that fires the automation's own actions
    # immediately, which can do anything the automation is defined to do.
    # Same class of "needs more thought" as media_player/alarm_control_
    # panel, not guessed at here.
    "automation": {"turn_on": "turn_on", "turn_off": "turn_off"},
    "vacuum": {"start": "start", "stop": "stop", "pause": "pause",
              "return_to_base": "return_to_base", "locate": "locate"},
    "valve": {"open": "open_valve", "close": "close_valve", "stop": "stop_valve"},
    # humidifier/siren/water_heater: real HA domains with this exact
    # vocabulary (confirmed against HA's own service registry), but zero
    # entities of any of these three exist in the operator's real house
    # right now -- unlike everything else above, there was nothing to
    # verify even a read-only mapping against. Included because he named
    # them; flagged here as unverified rather than silently claimed
    # equal-confidence.
    "humidifier": {"turn_on": "turn_on", "turn_off": "turn_off"},  # UNVERIFIED -- no real entity
    "siren": {"turn_on": "turn_on", "turn_off": "turn_off"},  # UNVERIFIED -- no real entity
    "water_heater": {"turn_on": "turn_on", "turn_off": "turn_off",
                     "set_temperature": "set_temperature"},  # UNVERIFIED -- no real entity
}

# Light brightness/color (2026-09-15, the operator's own ask). Before this, `value`
# was passed through to HA wholesale with no domain-specific validation at
# all -- a light's turn_on COULD technically already carry a brightness/
# color key (nothing blocked it), but nothing told the model these keys
# existed, and nothing checked whether THIS light could actually do
# anything with them; asking a plain on/off light for rgb_color wouldn't
# error, HA would just ignore the key it doesn't understand for that
# device -- exactly the "accepts anything and hopes" the operator asked
# NOT to ship. Each key here maps to the supported_color_modes value(s) that
# make it meaningful; _validate_light_value() below checks the entity's
# OWN reported modes (from the same /api/states/<entity_id> call
# control() already makes for the existence check) before ever sending
# the request, and refuses outright -- never silently drops the key -- on
# a mismatch.
_LIGHT_PARAM_MODES = {
    "color_temp_kelvin": {"color_temp"},
    "color_temp": {"color_temp"},
    "rgb_color": {"rgb", "rgbw", "rgbww"},
    "hs_color": {"hs"},
    "xy_color": {"xy"},
}


def _validate_light_value(value: dict, supported_modes: list) -> str | None:
    """Returns an error string on a real mismatch, None if value is fine
    for this entity's own reported capabilities. brightness/brightness_pct
    are special-cased -- HA's own rule is that EVERY color mode except a
    bare ['onoff'] implies dimming support, not just an explicit
    'brightness' entry (a light that reports ['color_temp','rgb'], say,
    still dims), so the check is "not onoff-only" rather than "brightness
    literally listed"."""
    modes = set(supported_modes or [])
    if ("brightness" in value or "brightness_pct" in value) and modes == {"onoff"}:
        return "this light doesn't support dimming (supported_color_modes: onoff only)"
    for key, needs in _LIGHT_PARAM_MODES.items():
        if key in value and not (modes & needs):
            return (f"this light doesn't support {key!r} -- its supported_color_modes are "
                    f"{sorted(modes) or 'none reported'}, not {sorted(needs)}")
    return None


def _base_url() -> str:
    return os.environ.get("HOME_ASSISTANT_URL", "").strip().rstrip("/")


def _api_key() -> str:
    return os.environ.get("HOME_ASSISTANT_API_KEY", "").strip()


def configured() -> bool:
    return bool(_base_url() and _api_key())


def _request(path: str, *, method: str = "GET", json_body: dict | None = None) -> tuple[bool, int | None, object]:
    """The one chokepoint for every real HTTP call this module makes to
    Home Assistant -- nothing outside this function ever opens a socket
    to it. Not SSRF-hardened the way webtools._fetch_checked is: that
    module calls attacker-reachable URLs a model supplies; this one only
    ever calls ONE fixed, admin-configured base URL from .env, never a
    URL any caller can influence. Returns (ok, http_status, data-or-
    error-message) -- the error message is deliberately specific per
    failure kind (see module docstring's "failure legibility" section),
    not a generic "request failed"."""
    url = _base_url() + path
    headers = {"Authorization": f"Bearer {_api_key()}", "Content-Type": "application/json"}
    data = json.dumps(json_body).encode("utf-8") if json_body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
            raw = resp.read(_MAX_BODY_BYTES + 1)
            status = resp.status
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return False, exc.code, ("Home Assistant rejected the request as unauthorized -- "
                                     "HOME_ASSISTANT_API_KEY is likely wrong or expired; check .env, "
                                     "this isn't something a retry will fix")
        detail = exc.read().decode("utf-8", "replace")[:300]
        return False, exc.code, f"HTTP {exc.code}: {detail}"
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return False, None, f"couldn't reach Home Assistant at {_base_url()}: {exc}"
    if len(raw) > _MAX_BODY_BYTES:
        return False, status, "response too large"
    try:
        parsed = json.loads(raw.decode("utf-8", "replace")) if raw else None
    except json.JSONDecodeError:
        parsed = raw.decode("utf-8", "replace")
    return True, status, parsed


# ── audit log ────────────────────────────────────────────────────────────
def _log(kind: str, *, entity_id: str | None = None, domain: str | None = None,
         service: str | None = None, ok: bool, reason: str | None = None,
         agent: str | None = None, status: int | None = None, payload: str | None = None) -> None:
    """Every call, read or write, allowed or failed -- same "log every
    request, complete rather than sampled" discipline web_tool_log already
    established for web_search/web_fetch (see that module's own _log())."""
    store.write(lambda c: c.execute(
        "INSERT INTO ha_tool_log(ts, kind, entity_id, domain, service, ok, reason, agent, status, payload) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (time.time(), kind, entity_id, domain, service, 1 if ok else 0,
         (reason or None) and str(reason)[:500], (agent or None) and str(agent)[:120],
         status, (payload or None) and str(payload)[:2000])))


def recent_log(limit: int = 100) -> list[dict]:
    limit = max(1, min(int(limit or 100), 500))
    return [dict(r) for r in store.read(lambda c: c.execute(
        "SELECT * FROM ha_tool_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall())]


# ── discovery (admin-only; enforced by the caller, server.py's own role
# check -- this module doesn't know what a session is, same convention
# accounts.py/webtools.py already use) ──────────────────────────────────
def discover_entities() -> dict:
    """Hits the real HA API and upserts every entity it reports. INSERT
    for one never seen before -- enabled/enabled_for_peers default 0, so
    a newly-discovered entity is invisible to her until an admin
    explicitly checks it. UPDATE for one already known touches ONLY
    domain/friendly_name/last_seen_ts/raw_json -- never enabled/
    enabled_for_peers, on purpose: rediscovering the house never silently
    re-exposes or un-exposes anything an admin already decided.

    ONE store.write() call for the whole batch, via executemany -- not
    one per entity (2026-09-15, found live: the operator's real house is 1378
    entities, and the original per-row loop called store.write(),
    meaning store.connect(), meaning a fresh sqlite3 connection AND a
    fresh commit, 1378 separate times. Timed for real against his actual
    data: the HTTP fetch itself is 1.3s; the OLD per-row write loop alone
    was 11+ of the 12.4s the whole call took. Nabu Casa was never the
    bottleneck -- confirmed by timing the fetch and the DB writes
    separately, not by reasoning about which one seemed more likely.
    executemany puts every row in the one transaction store.write()
    already wraps in _WRITE_LOCK, cutting 1378 connect+commit cycles to
    one."""
    if not configured():
        return {"ok": False, "reason": "Home Assistant isn't configured -- HOME_ASSISTANT_URL/"
                                       "HOME_ASSISTANT_API_KEY aren't set in .env"}
    ok, status, data = _request("/api/states")
    if not ok:
        return {"ok": False, "reason": str(data)}
    if not isinstance(data, list):
        return {"ok": False, "reason": "unexpected response shape from Home Assistant"}
    now = time.time()
    rows = []
    for row in data:
        entity_id = row.get("entity_id")
        if not entity_id or "." not in entity_id:
            continue
        domain = entity_id.split(".", 1)[0]
        friendly_name = (row.get("attributes") or {}).get("friendly_name") or entity_id
        raw_json = json.dumps(row, ensure_ascii=False)[:4000]
        rows.append((entity_id, domain, friendly_name, now, raw_json))
    store.write(lambda c: c.executemany(
        "INSERT INTO ha_entities(entity_id, domain, friendly_name, last_seen_ts, raw_json) "
        "VALUES (?,?,?,?,?) "
        "ON CONFLICT(entity_id) DO UPDATE SET domain=excluded.domain, "
        "friendly_name=excluded.friendly_name, last_seen_ts=excluded.last_seen_ts, "
        "raw_json=excluded.raw_json",
        rows))
    return {"ok": True, "count": len(rows)}


def all_entities() -> list[dict]:
    """The FULL discovered set -- the admin discovery page's own listing,
    grouped/rendered by server.py. Never called by anything model-facing;
    see this module's own docstring on why that split is structural."""
    return [dict(r) for r in store.read(lambda c: c.execute(
        "SELECT * FROM ha_entities ORDER BY domain, friendly_name").fetchall())]


def set_exposure(entity_id: str, *, enabled: bool, enabled_for_peers: bool) -> None:
    """Peer-enabled implies Nori-enabled -- enforced HERE, not just relied
    on in the admin page's own UI (which also enforces it, but a UI
    control is never the only guarantee something structural needs).
    A peer reaching something she can't see herself would be incoherent."""
    if enabled_for_peers:
        enabled = True
    store.write(lambda c: c.execute(
        "UPDATE ha_entities SET enabled=?, enabled_for_peers=? WHERE entity_id=?",
        (1 if enabled else 0, 1 if enabled_for_peers else 0, entity_id)))


# ── the structural gate -- every model-facing function below calls this
# FIRST, before making any real call to Home Assistant, and refuses
# outright on a miss ─────────────────────────────────────────────────────
def _exposed_row(entity_id: str, *, for_peer: bool = False) -> dict | None:
    row = store.read(lambda c: c.execute(
        "SELECT * FROM ha_entities WHERE entity_id=?", (entity_id,)).fetchone())
    if row is None:
        return None
    flag = "enabled_for_peers" if for_peer else "enabled"
    if not row[flag]:
        return None
    return dict(row)


def list_exposed_entities(*, for_peer: bool = False) -> list[dict]:
    """The ONLY roster a model (or a peer, through her) ever sees. Names/
    domains here were already reviewed by an admin at the moment they
    were exposed -- not re-screened per read, unlike live state below;
    see module docstring."""
    flag = "enabled_for_peers" if for_peer else "enabled"
    rows = store.read(lambda c: c.execute(
        f"SELECT entity_id, domain, friendly_name, enabled_for_peers FROM ha_entities "
        f"WHERE {flag}=1 ORDER BY domain, friendly_name").fetchall())
    return [dict(r) for r in rows]


# ── content screening chokepoint for live state (the operator's own framing:
# "entity state is untrusted-ish content... treat it consistently with
# everything else") ──────────────────────────────────────────────────────
_SCREEN_MAX_CHARS = 200_000


def _screen_state(text: str) -> dict:
    import ingest  # local: same load-order reasoning every other lazy import in this app uses
    return ingest.summarize_untrusted(text[:_SCREEN_MAX_CHARS], kind="home assistant state", preserve_content=True)


def get_state(entity_ids: list[str] | None, *, agent: str | None = None, for_peer: bool = False) -> dict:
    """entity_ids=None means every currently exposed entity. Anything
    requested that isn't exposed (for_peer decides which flag) is
    silently dropped from the target set before the single /api/states
    call below, not merely withheld from the result -- its real state is
    never fetched on anyone's behalf at all. One HA call regardless of
    how many entities are asked for (HA's own /api/states already
    returns everything; filtering locally beats N separate per-entity
    calls), so cost/logging/screening are all proportional to tool
    CALLS, not entity COUNT.

    A target that WAS exposed but isn't in HA's own response any more is
    reported explicitly as missing (see module docstring's failure-
    legibility section) -- never silently absent, which is exactly the
    gap a confabulated answer would fill in."""
    if not configured():
        return {"ok": False, "reason": "Home Assistant isn't configured yet"}
    exposed = {r["entity_id"] for r in list_exposed_entities(for_peer=for_peer)}
    if not exposed:
        who = "you" if for_peer else "her"
        return {"ok": False, "reason": f"no smart-home entities are exposed to {who} yet -- ask the "
                                       f"operator to expose some from the Home Assistant settings page"}
    targets = exposed if not entity_ids else {e for e in entity_ids if e in exposed}
    if entity_ids and not targets:
        return {"ok": False, "reason": "none of those entities are exposed"}
    ok, status, data = _request("/api/states")
    _log("get_state", ok=ok, reason=None if ok else str(data), agent=agent, status=status,
        payload=",".join(sorted(targets))[:2000])
    if not ok:
        return {"ok": False, "reason": str(data)}
    if not isinstance(data, list):
        return {"ok": False, "reason": "unexpected response shape from Home Assistant"}
    found = {row.get("entity_id") for row in data}
    subset = [row for row in data if row.get("entity_id") in targets]
    missing = sorted(targets - found)
    for entity_id in missing:
        subset.append({"entity_id": entity_id,
                       "error": "not found in Home Assistant -- it may have been removed, "
                                "renamed, or unpaired since it was exposed"})
    screened = _screen_state(json.dumps(subset, ensure_ascii=False))
    result = {"ok": True, **screened}
    if missing:
        result["missing_entities"] = missing
    return result


_HISTORY_MAX_HOURS = 168  # 7 days -- matches HA's own common default recorder retention; not detected per-instance, documented here instead
_HISTORY_MIN_HOURS = 1
_HISTORY_MAX_TRANSITIONS = 200  # belt-and-braces against a rapidly-flapping entity; irrelevant for any real query so far
# A stationary phone's own GPS accuracy alone commonly wanders 10-100m
# (confirmed against real data: gps_accuracy on the operator's own tracker ranged
# 13-100m at rest) -- 400m is comfortably above that jitter floor, so
# sitting still (even inside one large "not_home" zone) doesn't emit
# noise entries, while genuine movement (drove somewhere) does.
_MOVE_THRESHOLD_M = 400


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in meters -- no library, this is the whole
    formula. Precision to a few meters is more than enough for a 400m
    threshold; not trying to be survey-grade."""
    r = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def _local_since(last_changed: str | None, user_id: int) -> str | None:
    """HA's own `last_changed` comes back as a real UTC ISO8601 string
    (2026-09-18, real bug found: this used to ride straight through
    unconverted -- the REQUEST's own start_iso below is correctly built
    in UTC, HA's REST API requires that, but the RESPONSE's timestamps
    were never converted back to his zone before reaching the model,
    inconsistent with everywhere else in this app that shows him a
    time). Falls back to the raw string on anything unparseable rather
    than silently dropping the field."""
    if not last_changed:
        return last_changed
    try:
        dt = datetime.datetime.fromisoformat(last_changed.replace("Z", "+00:00"))
    except ValueError:
        return last_changed
    return usertime.fmt(user_id, dt.timestamp(), "%Y-%m-%d %H:%M %Z")


def get_history(entity_id: str, hours: int = 24, *, agent: str | None = None,
                for_peer: bool = False, user_id: int) -> dict:
    """Same exposed-entity gate as get_state/control -- history isn't a
    separate capability that bypasses enabled/enabled_for_peers, it's the
    same read permission over a longer window. Domain-agnostic, same as
    get_state -- HA's own /api/history/period/... endpoint works for any
    entity, not just device_tracker; nothing here special-cases location.

    Condensed to distinct STATE TRANSITIONS, not every raw poll (2026-09-
    15, the operator's own instruction, confirmed necessary by checking his real
    data first rather than assuming: device_tracker.pixel_8_pro over 24h
    came back as 109 individual poll entries -- HA records a new "state"
    on every update it receives, including ones where nothing actually
    changed (a phone re-reporting the same GPS fix) -- but only 3 real
    state transitions (home -> not_home -> home).

    Each kept entry carries latitude/longitude/gps_accuracy alongside
    state and timestamp (2026-09-15, the operator's own follow-up ask, after an
    earlier cut of mine dropped them -- keep the location, keep the
    collapsing). A SECOND, independent reason to emit an entry alongside
    "the state changed": moving more than _MOVE_THRESHOLD_M meters from
    the last entry's own position, even while the state string stays the
    same -- otherwise a multi-hour "not_home" stretch spent driving
    across town would still collapse to one entry with only the first
    GPS fix, discarding the actual trail while it's happening (the
    operator's own concern, not a hypothetical: today's real "not_home" excursion was
    short, ~7 minutes, so it didn't happen to show this, but a longer one
    would). Distance is haversine on lat/long -- no library, a few trig
    calls, negligible cost even over 109 raw entries. 400m is deliberately
    above ordinary GPS jitter (a stationary phone's own accuracy easily
    wanders 10-100m) so staying in one place, even a big one like a
    single "not_home" zone, doesn't emit noise entries -- and well below
    "actually went somewhere else." Entries missing lat/long entirely
    (state has no meaningful position, or the attribute wasn't reported)
    never trigger this movement check.

    entity's own attributes (lat/long/accuracy) ride straight through,
    not fabricated or re-derived -- if HA reports gps_accuracy: 100.0, a
    100m-uncertain fix is exactly what she should say, not something
    this rounds away."""
    row = _exposed_row(entity_id, for_peer=for_peer)
    if row is None:
        who = "peers" if for_peer else "her"
        return {"ok": False, "reason": f"{entity_id!r} isn't exposed to {who} -- ask the operator "
                                       f"to turn that on from the Home Assistant settings page"}
    # hours=0 is falsy but a real, if useless, request -- `hours or 24`
    # would have silently treated it as "not given" and used the default
    # instead of clamping it up to the real minimum. Found by testing the
    # boundary, not assumed correct from the one-liner reading right.
    hours = 24 if hours is None else int(hours)
    hours = max(_HISTORY_MIN_HOURS, min(hours, _HISTORY_MAX_HOURS))
    # Deliberately UTC, not usertime -- HA's own REST API requires it for
    # the request. The RESPONSE's timestamps are a different matter; see
    # _local_since() below for where those get converted to his zone.
    start_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - hours * 3600)) + "Z"
    ok, status, data = _request(f"/api/history/period/{start_iso}?filter_entity_id={entity_id}")
    _log("get_history", entity_id=entity_id, ok=ok, reason=None if ok else str(data),
        agent=agent, status=status, payload=f"{hours}h")
    if not ok:
        return {"ok": False, "reason": str(data)}
    if not isinstance(data, list) or not data:
        return {"ok": True, "entity_id": entity_id, "hours": hours, "transitions": [],
               "note": "no history recorded for this entity in that window"}
    series = data[0]
    transitions = []
    prev_state = None
    last_lat = last_lon = None
    for entry in series:
        state = entry.get("state")
        attrs = entry.get("attributes") or {}
        lat, lon = attrs.get("latitude"), attrs.get("longitude")
        moved = False
        if lat is not None and lon is not None and last_lat is not None:
            moved = _haversine_m(last_lat, last_lon, lat, lon) >= _MOVE_THRESHOLD_M
        if state == prev_state and not moved:
            continue
        transitions.append({"state": state, "since": _local_since(entry.get("last_changed"), user_id),
                            "latitude": lat, "longitude": lon,
                            "accuracy_m": attrs.get("gps_accuracy")})
        prev_state = state
        if lat is not None and lon is not None:
            last_lat, last_lon = lat, lon
        if len(transitions) >= _HISTORY_MAX_TRANSITIONS:
            break
    screened = _screen_state(json.dumps(transitions, ensure_ascii=False))
    result = {"ok": True, "entity_id": entity_id, "hours": hours,
             "raw_sample_count": len(series), **screened}
    return result


def control(entity_id: str, action: str, value=None, *, agent: str | None = None,
           for_peer: bool = False) -> dict:
    """The 2026-09-15 bug, found live against the operator's real porch light and
    fixed the same way it was found -- by calling the real HA API, not by
    reasoning about what it "should" do: HA's own POST /api/services/...
    endpoint returns 200 with an empty JSON list `[]` for EVERY outcome
    of a plain service call -- a real, verified state change (confirmed
    by re-reading /api/states/light.porch_light before and after: off ->
    on, for real), an idempotent no-op (already in the target state), and
    a call against an entity_id that doesn't exist -- all three produced
    the identical `200 []`. There is no way to tell them apart from this
    response's own shape. My original code treated an empty list as
    proof nothing matched and reported failure -- which meant every
    SUCCESSFUL control call was being reported as a failure; this is
    exactly the "stand-in server let this ship broken" case, since my
    fake HA mock's own empty-list-means-no-match behavior was guessed,
    never verified against the real API, and happened to be wrong.

    The fix: trust the HTTP status code for whether the call itself
    succeeded (verified separately: a call with a well-formed body
    against a real, existing entity returns 200 regardless of outcome;
    a malformed call still raises a real HTTPError, caught by
    _request() same as any other failure). For the actual "does this
    entity still exist" question -- the operator's own original ask, still real
    -- ask HA directly and separately, the one endpoint that DOES give a
    verified, unambiguous answer: GET /api/states/<entity_id> returns a
    real 404 ("Entity not found") for one that's gone, confirmed live
    against an entity_id made up for exactly this test. One extra small
    per-entity GET, not the full /api/states dump -- cheap, and an honest
    answer beats a guessed one."""
    row = _exposed_row(entity_id, for_peer=for_peer)
    if row is None:
        who = "peers" if for_peer else "her"
        return {"ok": False, "reason": f"{entity_id!r} isn't exposed to {who} -- ask the operator "
                                       f"to turn that on from the Home Assistant settings page"}
    domain = row["domain"]
    domain_services = _SERVICES.get(domain)
    if domain_services is None:
        return {"ok": False, "reason": f"{domain!r} entities aren't controllable through this tool"}
    service = domain_services.get(action)
    if service is None:
        return {"ok": False, "reason": f"{action!r} isn't a valid action for a {domain} entity -- "
                                       f"one of: {', '.join(domain_services)}"}
    exists_ok, exists_status, exists_data = _request(f"/api/states/{entity_id}")
    if not exists_ok and exists_status == 404:
        _log("control", entity_id=entity_id, domain=domain, service=service, ok=False,
            reason="entity not found in Home Assistant", agent=agent, status=404)
        return {"ok": False, "reason": f"{entity_id!r} no longer exists in Home Assistant -- it may "
                                       f"have been removed, renamed, or unpaired since it was exposed"}
    if domain == "light" and isinstance(value, dict) and exists_ok:
        supported_modes = (exists_data or {}).get("attributes", {}).get("supported_color_modes")
        err = _validate_light_value(value, supported_modes)
        if err:
            _log("control", entity_id=entity_id, domain=domain, service=service, ok=False,
                reason=err, agent=agent, status=None, payload=json.dumps(value))
            return {"ok": False, "reason": err}
    body = {"entity_id": entity_id}
    if value is not None:
        # value's shape is action-specific (set_temperature's own
        # "temperature" key, say) -- passed straight through as HA's own
        # service data, since this app has no generic way to know every
        # domain's own parameter names. Light entities get real validation
        # above, against the entity's own supported_color_modes; other
        # domains still rely on the action allowlist plus HA's own
        # rejection of a malformed call.
        body.update(value if isinstance(value, dict) else {"value": value})
    ok, status, data = _request(f"/api/services/{domain}/{service}", method="POST", json_body=body)
    if ok and domain == "light" and isinstance(value, dict):
        ok, mismatch_reason = _verify_light_applied(entity_id, value)
        if not ok:
            data = mismatch_reason
    _log("control", entity_id=entity_id, domain=domain, service=service, ok=ok,
        reason=None if ok else str(data), agent=agent, status=status, payload=json.dumps(body))
    if not ok:
        return {"ok": False, "reason": str(data)}
    return {"ok": True, "entity_id": entity_id, "action": action, "note": f"{entity_id}: {action}"}


# Verified live (2026-09-15) against the operator's real lights, ALL Meross-branded
# (msl120d/mss110 -- entity names, not a guess): HA's own light.turn_on
# call returns 200 with an empty list REGARDLESS of whether brightness_pct
# actually took effect. On these specific lights it silently didn't -- a
# real brightness_pct=40 call left the reported brightness completely
# unchanged, confirmed with a raw call bypassing this module entirely, on
# both a light group and an individual underlying bulb. Reusing "trust the
# HTTP status" for the toggle fix earlier today was correct for on/off
# (verified separately: a real on/off DOES land); it is NOT sufficient for
# brightness/color, which this integration can silently no-op. So: for
# these specific parameters only, read the entity back after the call and
# check the value that matters actually moved -- the same discipline as
# the toggle fix, extended to the one case it doesn't cover for free.
# color_temp_kelvin/color_temp aren't checked here because they failed
# with a real, honest HTTP error (500/400) on every light tested --
# _request() already reports that correctly; this function only needs to
# catch the SILENT no-op case, not the loud one.
_LIGHT_VERIFY_TOLERANCE = {"brightness": 8, "brightness_pct": 8}


def _verify_light_applied(entity_id: str, value: dict) -> tuple[bool, str | None]:
    checks = []
    if "brightness_pct" in value:
        checks.append(("brightness", round(float(value["brightness_pct"]) / 100 * 255),
                      _LIGHT_VERIFY_TOLERANCE["brightness_pct"]))
    elif "brightness" in value:
        checks.append(("brightness", float(value["brightness"]), _LIGHT_VERIFY_TOLERANCE["brightness"]))
    if "rgb_color" in value:
        checks.append(("rgb_color", list(value["rgb_color"]), 0))
    if "hs_color" in value:
        checks.append(("hs_color", list(value["hs_color"]), 0))
    if "xy_color" in value:
        checks.append(("xy_color", list(value["xy_color"]), 0))
    if not checks:
        return True, None
    last_seen = None
    for attempt in range(3):
        time.sleep(1.2)
        ok, _status, data = _request(f"/api/states/{entity_id}")
        if not ok:
            continue
        attrs = (data or {}).get("attributes", {})
        last_seen = attrs
        matched = True
        for attr, target, tol in checks:
            actual = attrs.get(attr)
            if actual is None:
                matched = False
                break
            if tol:
                if abs(float(actual) - float(target)) > tol:
                    matched = False
                    break
            elif list(actual) != target:
                matched = False
                break
        if matched:
            return True, None
    checked_keys = {"brightness_pct", "brightness", "rgb_color", "hs_color", "xy_color"} & value.keys()
    wanted = ", ".join(f"{k}={value[k]!r}" for k in checked_keys)
    return False, (f"Home Assistant accepted the request but {entity_id!r} doesn't show the change "
                   f"({wanted}) after checking -- this integration is known to silently no-op some "
                   f"light parameters; last reported state: {last_seen}")


# ── tool implementations ─────────────────────────────────────────────────
def _agent_label(session: dict) -> str:
    import accounts  # local: same load-order reasoning as webtools.py's own identical helper
    user = accounts.get_user(session["user_id"])
    return user["display_name"] if user else f"user {session['user_id']}"


def _for_peer(session: dict) -> bool:
    """True only for the two places in peers.py where a peer's OWN
    request is actually executing after clearing the trust gate -- see
    that module's own comment on _peer_act. Nori calling this tool on her
    own initiative, even mid a conversation with a peer, is False here:
    _peer_context (set for the whole turn) is a different, broader signal
    than this one, deliberately not used for this check."""
    return bool(session.get("_peer_act"))


def _ha_list_entities_impl(session: dict) -> dict:
    if not configured():
        return {"ok": False, "reason": "Home Assistant isn't configured yet"}
    return {"ok": True, "entities": list_exposed_entities(for_peer=_for_peer(session))}


def _ha_get_state_impl(session: dict, entity_ids: list | None = None) -> dict:
    return get_state(entity_ids, agent=_agent_label(session), for_peer=_for_peer(session))


def _ha_control_impl(session: dict, entity_id: str, action: str, value=None) -> dict:
    return control(entity_id, action, value, agent=_agent_label(session), for_peer=_for_peer(session))


def _ha_get_history_impl(session: dict, entity_id: str, hours: int = 24) -> dict:
    return get_history(entity_id, hours, agent=_agent_label(session), for_peer=_for_peer(session),
                       user_id=session["user_id"])


def _register_tools() -> None:
    import tools  # local: same reasoning as household.py/webtools.py

    tools.register(tools.Tool(
        "ha_list_entities",
        {"type": "function", "function": {
            "name": "ha_list_entities",
            "description": ("List the smart-home entities the operator has actually exposed to "
                            "you -- lights, locks, covers, climate, sensors, whatever he's checked "
                            "off. Nothing else in the house exists as far as you're concerned; if "
                            "this comes back empty, say so plainly rather than guessing at what's "
                            "there."),
            "parameters": {"type": "object", "properties": {}}}},
        _ha_list_entities_impl, min_role="member", data_scope="workspace", risk_tier="A"))

    tools.register(tools.Tool(
        "ha_get_state",
        {"type": "function", "function": {
            "name": "ha_get_state",
            "description": ("Check the current state of one or more exposed smart-home entities -- "
                            "is a light on, is the door locked, what the thermostat's set to. Omit "
                            "entity_ids for the state of everything exposed at once. If an entity "
                            "comes back missing, say so plainly -- it may have been removed or "
                            "renamed in Home Assistant since it was exposed, don't guess at its "
                            "state."),
            "parameters": {"type": "object", "properties": {
                "entity_ids": {"type": "array", "items": {"type": "string"},
                              "description": "specific entity_ids from ha_list_entities -- omit for all exposed"}}}}},
        _ha_get_state_impl, min_role="member", data_scope="workspace", risk_tier="A"))

    tools.register(tools.Tool(
        "ha_control",
        {"type": "function", "function": {
            "name": "ha_control",
            "description": ("Control an exposed smart-home entity -- turn a light, switch, fan, "
                            "input_boolean, siren, or humidifier on or off; lock or unlock a door; "
                            "open/close/stop a cover or valve; adjust a thermostat or water heater; "
                            "set a number/input_number's value or a select/input_select's option; "
                            "press a button; activate a scene; run or stop a script; enable/disable "
                            "an automation (never trigger it -- ask instead, that fires its real "
                            "actions immediately); start/stop/pause/return-to-base/locate a vacuum. "
                            "Only works on an entity the operator has exposed. For a light, check "
                            "ha_get_state first if you're not sure what it can do -- its "
                            "supported_color_modes attribute says whether it dims or takes color at "
                            "all, and asking for something it doesn't support is refused outright, "
                            "not silently ignored. Activating a scene or running a script isn't "
                            "reversible the way flipping a light back is -- be sure that's actually "
                            "what was asked before calling it."),
            "parameters": {"type": "object", "properties": {
                "entity_id": {"type": "string"},
                "action": {"type": "string",
                          "description": ("e.g. turn_on, turn_off, lock, unlock, open, close, stop, "
                                          "set_temperature, set_hvac_mode, set_value, select_option, "
                                          "press, activate, run, start, pause, return_to_base, "
                                          "locate -- valid actions depend on the entity's own "
                                          "domain")},
                "value": {"description": ("extra data the action needs. climate's set_temperature: "
                                         "{\"temperature\": 70}. set_value (number/input_number): "
                                         "{\"value\": 42}. select_option (select/input_select): "
                                         "{\"option\": \"Item A\"} -- must be one of that entity's own "
                                         "listed options. A light's turn_on can carry brightness_pct "
                                         "(0-100), color_temp_kelvin, rgb_color ([r,g,b] 0-255 each), "
                                         "hs_color ([hue 0-360, sat 0-100]), or xy_color -- only the "
                                         "ones that entity's own supported_color_modes actually "
                                         "lists. Omit for an action that needs no extra data (press, "
                                         "activate, run, stop, lock, unlock, turn_on/off, and most "
                                         "others).")}},
                "required": ["entity_id", "action"]}}},
        _ha_control_impl, min_role="member", data_scope="workspace", risk_tier="B",
        consequential=True))

    tools.register(tools.Tool(
        "ha_get_history",
        {"type": "function", "function": {
            "name": "ha_get_history",
            "description": ("Look back at how an exposed entity's state changed over a recent "
                            "time window -- was a device tracker at home or away, when did a "
                            "light last go on. Returns a condensed sequence of distinct state "
                            "changes with when each one started, not every individual poll -- a "
                            "device that reports frequently (a phone's location, say) still "
                            "collapses to just the times it actually changed OR moved a real "
                            "distance. For a location tracker, each entry also carries latitude/"
                            "longitude/accuracy_m -- real GPS coordinates, not withheld or "
                            "rounded, so treat them as exactly that. Same exposure rule as "
                            "everything else: only works on an entity the operator has exposed."),
            "parameters": {"type": "object", "properties": {
                "entity_id": {"type": "string"},
                "hours": {"type": "integer",
                         "description": f"how far back to look, {_HISTORY_MIN_HOURS}-{_HISTORY_MAX_HOURS} "
                                        f"(default 24)"}},
                "required": ["entity_id"]}}},
        _ha_get_history_impl, min_role="member", data_scope="workspace", risk_tier="A"))


def register_peer_actions() -> None:
    """Deferred to server.py's own main(), same reasoning as memory.py's
    identical function (see that one's own docstring for the exact
    circular-import failure -- peers.py's own top-level `import chat`
    pulling in a module that calls back into peers.register_peer_
    requestable before that name is defined -- this avoids).

    All three HA tools on the STANDARD trust ladder -- the operator's own
    explicit ask ("exposed entities can be reached by peers at prompt or
    full trust"), and his own later correction dropped the idea of a stricter
    full_trust_only tier for any particular device class. 'prompt' holds
    a request for the operator's own approval, 'full' skips that gate,
    'none' refuses outright. The per-entity enabled_for_peers checkbox
    (homeassistant.py's own gate, checked via _for_peer()/_peer_act) is a
    SEPARATE, independent axis from this trust level -- both have to
    allow a given peer's given entity for anything to actually happen,
    neither substitutes for the other."""
    import peers
    peers.register_peer_requestable("ha_list_entities")
    peers.register_peer_requestable("ha_get_state")
    peers.register_peer_requestable("ha_control")
    peers.register_peer_requestable("ha_get_history")


_register_tools()
