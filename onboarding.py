# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Onboarding (2026-10-26): she introduces herself, asks, and sets herself up.

A fresh instance used to open on an empty chat. Now the first time someone opens the chat (once a model is connected) she speaks first: she says who
she is, asks what to call them and what they want to call her, confirms their timezone, gets to know them (saving what she learns with `remember`),
drafts her own persona for the admin to approve, asks about pings and notifications, and gives a tour of the settings and features. Every step can
be skipped, and she can be asked to stop at any time.

How it is built (nothing here is left to the model's good manners where the harness can hold the line):

  * A per-user state row (`onboarding`): pending -> active -> done/skipped. Anyone with existing history is marked done the first time it is looked
    at (an upgraded instance is never greeted); "Meet her again" in Settings -> About restarts it on request.
  * While it is active, every chat turn gets a guide as the LAST system message (the same slot a ping's reason uses, which adapters keep in place),
    listing what is done and what is left, computed from real data, so she doesn't repeat finished steps.
  * Her tools exist ONLY while onboarding is active for that user (an `enabled(session)` gate), and the shared ones are admin-only:
      onboarding_set_name (admin), onboarding_set_timezone, onboarding_propose_persona (admin), onboarding_tour, onboarding_finish.
  * THE PERSONA IS NOT TOOL-WRITABLE. It is the prompt that shapes every reply for the whole household, so (as docs/persona.md has always said) no
    model-callable tool can change it, and this module deliberately imports none of the persona code (a test enforces that). What she can do is
    PROPOSE a draft: the text is stored inert and shown to the admin in the chat as a card with Apply and Discard buttons. Applying is an
    authenticated admin POST (`/onboarding/persona`, server.py) that goes through persona_admin like any Settings -> Persona save, so the previous
    persona is snapshotted and restorable. She can't apply it herself, and a prompt injection can't either.
  * An invited member gets the short version: no name or persona (those are shared), just their own preferences and a tour of what is theirs.
  * It can't nag forever: it ends by itself after three days or sixty of her replies, and an unrelated request always comes first.

See docs/onboarding.md."""
from __future__ import annotations

import json
import re
import time

import accounts
import config
import conversation
import store
import tools
import usertime

PENDING, ACTIVE, DONE, SKIPPED = "pending", "active", "done", "skipped"
MAX_ACTIVE_S = 3 * 24 * 3600
MAX_REPLIES = 60
NAME_MAX = 40
PERSONA_BODY_MAX = 12000
DEFAULT_NAME = "Nori"
DRAFT_KIND = "persona_draft"


# ── state ────────────────────────────────────────────────────────────────
def _blank_steps() -> dict:
    return {"name": False, "timezone": False, "persona": None, "tour": []}


def _load(user_id: int) -> dict | None:
    r = store.read(lambda c: c.execute("SELECT * FROM onboarding WHERE user_id=?", (user_id,)).fetchone())
    if r is None:
        return None
    d = dict(r)
    try:
        d["steps"] = {**_blank_steps(), **json.loads(d.get("steps") or "{}")}
    except (ValueError, TypeError):
        d["steps"] = _blank_steps()
    return d


def _save(user_id: int, **fields) -> None:
    cur = _load(user_id) or {"user_id": user_id, "status": PENDING, "steps": _blank_steps(), "started_ts": None, "finished_ts": None,
                             "draft_persona": None, "draft_card_id": None}
    cur.update(fields)
    store.write(lambda c: c.execute(
        "INSERT INTO onboarding(user_id, status, steps, started_ts, finished_ts, draft_persona, draft_card_id) VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET status=excluded.status, steps=excluded.steps, started_ts=excluded.started_ts, "
        "finished_ts=excluded.finished_ts, draft_persona=excluded.draft_persona, draft_card_id=excluded.draft_card_id",
        (user_id, cur["status"], json.dumps(cur["steps"]), cur.get("started_ts"), cur.get("finished_ts"),
         cur.get("draft_persona"), cur.get("draft_card_id"))))


def state(user_id: int) -> dict:
    """The user's onboarding row. First time seen: anyone who already has a conversation is marked done (an upgrade must not greet a long-time user);
    someone with none starts pending."""
    row = _load(user_id)
    if row is not None:
        return row
    status = DONE if conversation.count(user_id) > 0 else PENDING
    _save(user_id, status=status, finished_ts=time.time() if status == DONE else None)
    return _load(user_id)


def flavor(user: dict) -> str:
    return "admin" if user.get("role") == "admin" else "member"


def status(user_id: int) -> str:
    return state(user_id)["status"]


def is_active(user_id: int) -> bool:
    """True while onboarding should steer this user's chat turns. Expires by itself (see MAX_*)."""
    row = state(user_id)
    if row["status"] != ACTIVE:
        return False
    started = row.get("started_ts") or time.time()
    if time.time() - started > MAX_ACTIVE_S or conversation.count_assistant_chat_since(user_id, started) > MAX_REPLIES:
        _save(user_id, status=DONE, finished_ts=time.time())
        return False
    return True


def has_model(workspace_id: int) -> bool:
    """Can she speak at all yet? A fresh instance has no model until the admin connects a provider."""
    try:
        import chat
        return bool(chat._resolve_model_chain(workspace_id))
    except Exception:  # noqa: BLE001
        return False


def should_begin(user_id: int, workspace_id: int) -> bool:
    """The chat page starts the introduction by itself only for a pending user with a model to think with."""
    return status(user_id) == PENDING and has_model(workspace_id)


def start(user_id: int) -> None:
    """Begin (or restart) onboarding for this user: fresh steps, active now. A draft persona from an earlier run is dropped."""
    _save(user_id, status=ACTIVE, steps=_blank_steps(), started_ts=time.time(), finished_ts=None, draft_persona=None, draft_card_id=None)


def reset_workspace(workspace_id: int) -> None:
    """Forget every user's progress in this workspace (start over, firstrun.reset): each is pending again, as if never seen."""
    store.write(lambda c: c.execute(
        "DELETE FROM onboarding WHERE user_id IN (SELECT id FROM users WHERE workspace_id=?)", (workspace_id,)))


def request_restart(user_id: int) -> None:
    """"Meet her again": back to pending; the chat page begins it on the next visit."""
    _save(user_id, status=PENDING, steps=_blank_steps(), started_ts=None, finished_ts=None, draft_persona=None, draft_card_id=None)


def finish(user_id: int, *, skipped: bool = False) -> None:
    _save(user_id, status=SKIPPED if skipped else DONE, finished_ts=time.time())


def _mark(user_id: int, **steps) -> None:
    row = state(user_id)
    row["steps"].update(steps)
    _save(user_id, steps=row["steps"])


def memories_saved(user_id: int) -> int:
    started = (state(user_id).get("started_ts")) or 0
    return store.read(lambda c: c.execute(
        "SELECT COUNT(*) FROM memory_events WHERE user_id=? AND action='add' AND ts>=?", (user_id, started)).fetchone())[0]


# ── the persona draft (inert until the admin applies it on its card) ─────
def get_draft(user_id: int) -> str | None:
    """The persona body waiting for the admin's decision, or None."""
    return state(user_id).get("draft_persona") or None


def draft_card_id(user_id: int) -> int | None:
    row = state(user_id)
    return row.get("draft_card_id") if row.get("draft_persona") else None


def settle_draft(user_id: int, outcome: str) -> None:
    """The admin pressed Apply ("applied", after persona_admin saved it) or Discard ("discarded"). Clears the draft and records the step."""
    _save(user_id, draft_persona=None, draft_card_id=None)
    _mark(user_id, persona=outcome)


# ── the guide she is given while it runs ──────────────────────────────────
NAME_TOKEN = "\u27e6n\u27e7"   # the assistant's name, in copy written without pronouns (server.py fills the same token in pages)


def _identity_settled(user: dict) -> bool:
    """Did the admin already choose her name and pronouns in the first-run wizard (firstrun.py)? Then the chat doesn't ask."""
    return bool(config.get("workspace", user["workspace_id"], "setup_identity"))


def _checklist(user: dict, persona_customised: bool) -> str:
    uid = user["id"]
    row = state(uid)
    steps = row["steps"]
    admin = flavor(user) == "admin"
    ws = user["workspace_id"]
    lines = []
    if admin:
        name = config.get("workspace", ws, "assistant_name")
        if _identity_settled(user):
            lines.append(f"- ⟦n⟧'s name and pronouns: settled in first-run setup as {name!r}, {config.get('workspace', ws, 'assistant_pronouns')}. Do not ask again; "
                         "change them only if they ask (onboarding_set_name for the name)")
        else:
            lines.append(f"- ⟦n⟧'s name: {'settled as ' + repr(name) if steps['name'] else 'not settled yet (currently ' + repr(name) + ')'}")
    lines.append(f"- Their timezone: {'confirmed as ' + usertime.zone_name(uid) if steps['timezone'] else 'not confirmed (currently ' + usertime.zone_name(uid) + ')'}")
    n = memories_saved(uid)
    lines.append(f"- Getting to know them: {n} thing(s) saved so far (aim for roughly 5-8 real, durable preferences and facts, not trivia)")
    if admin:
        if steps["persona"] == "applied":
            p = "applied by them"
        elif steps["persona"] == "discarded":
            p = "they discarded your last draft: ask what they'd change before drafting again"
        elif row.get("draft_persona"):
            p = "a draft is on screen as a card with Apply and Discard buttons; they decide, you can't apply it"
        else:
            p = ("not started; a persona already exists that they've customised, so refine it rather than replace it" if persona_customised
                 else "not started (⟦n⟧ is still on the shipped default)")
        lines.append(f"- ⟦n⟧'s persona: {p}")
    lines.append(f"- Tour: {', '.join(steps['tour']) if steps['tour'] else 'not started'}")
    return "\n".join(lines).replace(NAME_TOKEN, config.get("workspace", ws, "assistant_name"))


_GUIDE_COMMON = """You are in your first-meeting chat with {who}. This is how you get set up together. The app is tracking it; the state is below, so do not redo anything already done.

Where things stand:
{checklist}

How to run it:
- One question at a time, in short messages. This is a conversation, not a form. Stay in your own voice.
- Open by introducing yourself briefly, and say this is a short getting-set-up chat they can skip or stop any time ("skip" or "later" is fine).
- Suggested order, adapting to what they say: {order}
- Save what you learn as you learn it with `remember` (identity, routine, people, household, meals, preference ...): durable facts and real preferences, stated plainly. Don't announce every save, and never save secrets.
- Only say you changed or saved something if the tool result said ok, and when you change a setting say exactly what you set (the values). If a tool refuses, say so and move on.
- Settings, precisely: `ping_window_start`/`ping_window_end` (hours 0-23) are when you may start a message on your own; `notify_quiet_start`/`notify_quiet_end` are when their devices stay silent. "Don't nag" is not "turn pings off": never switch `ping_enabled` off unless they ask for that. Reminders are separate and keep nagging until done.
- If they ask for something unrelated, do that first and offer to pick this up later. If they say skip, later or stop, say so briefly and call `onboarding_finish` with skipped=true.
- The tour: don't dump information. Call `onboarding_tour` for the list, ask which areas they care about, then call it again with a stop for the details and where to find it (Settings -> ...). Two or three sentences per stop; offer to switch something on or off for them with `update_settings` when it fits. Never describe an app feature or a settings page from memory: use only what the tool returns, and if there isn't a stop for it, say you're not sure.
{extra}
When the main parts are done, close it out: a short recap of what you set up, what they can still do, and call `onboarding_finish`."""

_ORDER_ADMIN = ("(1) {names}; "
                "(2) confirm their timezone -> `onboarding_set_timezone`; (3) get to know them and what they expect of you: who is in the household, "
                "routines, how they like to be spoken to, what they most want help with, what they expect you to do on your own and what to leave alone, "
                "what annoys them; (4) when it's OK for you to message them unprompted and quiet hours "
                "(update with `update_settings`); (5) offer to write your own persona from what you've learned (ask first; draft only if they say yes); "
                "(6) the tour; (7) finish, and from then on simply be their assistant.")
_NAMES_ASK = ("what to call them (their account name is {name}; confirm or ask) and what they'd like to call you -> `onboarding_set_name`")
_NAMES_SETTLED = ("what to call them (their account name is {name}; confirm or ask). Your own name and pronouns are already settled, so don't ask about them")
_ORDER_MEMBER = ("(1) what to call them (their account name is {name}); (2) confirm their timezone -> `onboarding_set_timezone`; (3) get to know them: "
                 "routines, how they like to be spoken to, what they most want help with; (4) when it's OK for you to message them and quiet hours "
                 "(`update_settings`); (5) a tour of what's theirs; (6) finish.")

_NAME_RULE_ASK = "ask what they'd like to call you (keeping the current one is a fine answer). Settle it with `onboarding_set_name`, even if unchanged."
_NAME_RULE_SETTLED = ("they already chose your name and pronouns in setup, so use them and don't ask. If they say they want to change your name now, "
                      "`onboarding_set_name` does it.")

_EXTRA_ADMIN = """
Your name and your persona are shared by the whole household. You are the admin's assistant here:
- Name: {name_rule}
- Persona: your current persona is already in your instructions, so build on it. Keep what works (brevity, not nagging, dropping the personality when it's urgent); change tone, formality, the relationship and your quirks to fit what you learned. Use {{ASSISTANT_NAME}} wherever your name appears. Write it in second person ("You are..."), under about 500 words, no headings you don't need. Submit it with `onboarding_propose_persona`. You cannot apply it: the app shows them the whole draft as a card with Apply and Discard buttons, and only they can press one. So don't paste the draft again; say in a sentence what you emphasised and ask them to apply it or tell you what to change (a new proposal replaces the old one). Their previous persona is kept and can be restored in Settings -> Persona."""
_EXTRA_MEMBER = """
You are talking with a household member, not the admin: your name and persona are set by the admin and shared, so don't offer to change them. Keep this short."""

_KICKOFF = """

Nobody has said anything yet. Begin now: introduce yourself in two to four short sentences, say what this chat is (and that they can skip it), and ask your first question."""


def guide(user: dict, *, first: bool = False, persona_customised: bool = False) -> str:
    admin = flavor(user) == "admin"
    who_name = user.get("display_name") or "unknown"
    names = (_NAMES_SETTLED if _identity_settled(user) else _NAMES_ASK).format(name=who_name)
    order = _ORDER_ADMIN.format(names=names) if admin else _ORDER_MEMBER.format(name=who_name)
    text = _GUIDE_COMMON.format(who=user.get("display_name") or "someone new", checklist=_checklist(user, persona_customised), order=order,
                                extra=_EXTRA_ADMIN.replace("{name_rule}", _NAME_RULE_SETTLED if _identity_settled(user) else _NAME_RULE_ASK) if admin
                                else _EXTRA_MEMBER)
    return text + (_KICKOFF if first else "")


def guide_message(user: dict, *, persona_customised: bool = False) -> dict | None:
    """The system message to append to a chat turn while onboarding is active for this user, else None. A pending user who is already talking (the
    page didn't get to begin it) is activated here. `persona_customised` comes from the caller (server.py), so this module needn't touch persona code."""
    uid = user["id"]
    if status(uid) == PENDING and has_model(user["workspace_id"]):
        start(uid)
    if not is_active(uid):
        return None
    return {"role": "system", "content": guide(user, persona_customised=persona_customised)}


def kickoff_message(user: dict, *, persona_customised: bool = False) -> dict:
    """The turn that opens the conversation: activates onboarding and asks her to speak first."""
    start(user["id"])
    return {"role": "system", "content": guide(user, first=True, persona_customised=persona_customised)}


# ── the tour ─────────────────────────────────────────────────────────────
def _cfg(scope: str, scope_id: int, key: str):
    try:
        return config.get(scope, scope_id, key)
    except Exception:  # noqa: BLE001
        return None


def _onoff(v) -> str | None:
    return None if v is None else ("on" if v else "off")


def _memory_count(user_id: int, workspace_id: int) -> int | None:
    """How many things she remembers about them, through whichever memory backend is configured (None if it can't be read)."""
    try:
        import memory
        return len(memory._backend_for(workspace_id).all_rows(user_id=user_id))
    except Exception:  # noqa: BLE001
        return None


def _stops(user: dict) -> list[dict]:
    """The tour stops with the assistant's name filled in where the copy refers to it (it is written with a token so it holds for any pronouns)."""
    name = config.get("workspace", user["workspace_id"], "assistant_name")
    return [{**s, "title": s["title"].replace(NAME_TOKEN, name), "what": s["what"].replace(NAME_TOKEN, name)} for s in _stops_raw(user)]


def _stops_raw(user: dict) -> list[dict]:
    """Every place worth showing, with where it lives and its live state. (key, title, where, what, state, admin_only)"""
    uid, ws = user["id"], user["workspace_id"]
    n_mem = _memory_count(uid, ws)
    ping = _cfg("user", uid, "ping_enabled")
    window = f"{_cfg('user', uid, 'ping_window_start')}:00-{_cfg('user', uid, 'ping_window_end')}:00"
    return [
        dict(key="chat", title="Chat and voice", where="the chat; Settings -> Chat & voice",
             what="Type or talk to ⟦n⟧. Messages show their time, and the (i) on ⟦n⟧'s messages shows what triggered them and which model answered.", state=None, admin=False),
        dict(key="memory", title="Memory", where="Settings -> Memory",
             what="What ⟦n⟧ remembers about them. They can pin, edit or remove anything, and ⟦n⟧ saves new things as ⟦n⟧ learns them.",
             state=(f"{n_mem} remembered" if n_mem is not None else None), admin=False),
        dict(key="pings", title="Pings", where="Settings -> Pings",
             what="When ⟦n⟧ may speak up on ⟦n⟧'s own (a reminder, a calendar event, no dinner planned). ⟦n⟧ can choose to stay quiet when there's nothing worth saying.",
             state=f"{_onoff(ping)}, {window}", admin=False),
        dict(key="notifications", title="Notifications and timezone", where="Settings -> Notifications",
             what="Browser notifications, quiet hours, and the timezone every time in the app follows.",
             state=_onoff(_cfg("user", uid, "notify_enabled")), admin=False),
        dict(key="board", title="The board: tasks, notes, reminders, trackers", where="the board button at the top of the chat",
             what="Things to do, things to remember, reminders ⟦n⟧ nags about until done, and numbers to track over time (weight, water, sleep).", state=None, admin=False),
        dict(key="household", title="Inventory and meals", where="Inventory and Meals in the chat's top bar",
             what="What's in the house and what's for dinner this week. ⟦n⟧ can nudge when nothing is planned.", state=None, admin=False),
        dict(key="files", title="Files", where="Files in the top bar",
             what="A shared working folder ⟦n⟧ can read from and write to; they can upload and download.", state=None, admin=False),
        dict(key="history", title="History", where="History in the top bar",
             what="Search the whole conversation, hide messages from ⟦n⟧'s context, or delete them for good.", state=None, admin=False),
        dict(key="accounts", title="Connected accounts", where="Settings -> Connected accounts",
             what="Connect Google or Microsoft so ⟦n⟧ can read email and calendar, with the content screened before ⟦n⟧ acts on it.", state=None, admin=False),
        dict(key="devices", title="Devices", where="Settings -> Devices",
             what="The Android app and Chrome extension: real notifications, share to ⟦n⟧, health data into trackers, places, and (only while they allow it) phone and browser actions.",
             state=None, admin=False),
        dict(key="computer_use", title="Computer use", where="Settings -> Devices -> Computer use",
             what="⟦n⟧ can use their browser in a time-boxed session on sites they allow, pausing for approval on anything risky.",
             state=_onoff(_cfg("workspace", ws, "bridge_enabled")), admin=True),
        dict(key="web", title="Web search and fetch", where="Settings -> Web search/fetch",
             what="Lets ⟦n⟧ look things up and read pages.", state=f"search {_onoff(_cfg('workspace', ws, 'web_search_enabled'))}, fetch {_onoff(_cfg('workspace', ws, 'web_fetch_enabled'))}", admin=True),
        dict(key="images", title="Images", where="Settings -> Images",
             what="Selfies and imagined pictures, with a daily spend cap.", state=_onoff(_cfg("workspace", ws, "image_gen_enabled")), admin=True),
        dict(key="models", title="Models", where="Settings -> Model config",
             what="Which providers and models ⟦n⟧ thinks with, a primary and fallbacks.", state=None, admin=True),
        dict(key="persona", title="⟦n⟧'s persona", where="Settings -> Persona",
             what="Who ⟦n⟧ is. It can be edited and restored any time, but only from here: ⟦n⟧ can propose a draft, never change it on its own.", state=None, admin=True),
        dict(key="agents", title="Sub-agents and the code sandbox", where="Settings -> Sub-agents, Settings -> Code sandbox",
             what="Background jobs ⟦n⟧ can hand off, and a no-network sandbox where ⟦n⟧ can run python.",
             state=f"sandbox {_onoff(_cfg('workspace', ws, 'sandbox_enabled'))}", admin=True),
        dict(key="peers", title="Peer connections", where="Settings -> Peer connections",
             what="Connect ⟦n⟧ to another agent as an ongoing peer.", state=None, admin=True),
        dict(key="backups", title="Backups", where="Settings -> Backups",
             what="Encrypted backups and how to restore.", state=None, admin=True),
        dict(key="updates", title="Updates", where="Settings -> Updates",
             what="A pill appears in the header when a newer Nori release exists, with the release notes and how to update.",
             state=_onoff(_cfg("workspace", ws, "update_check_enabled")), admin=True),
    ]


def _visible_stops(user: dict) -> list[dict]:
    admin = flavor(user) == "admin"
    return [s for s in _stops(user) if admin or not s["admin"]]


# ── tools ────────────────────────────────────────────────────────────────
def _user_of(session: dict) -> dict | None:
    return accounts.get_user(session["user_id"])


def _gate(session: dict) -> bool:
    return is_active(session["user_id"])


def _gate_admin(session: dict) -> bool:
    return session.get("role") == "admin" and is_active(session["user_id"])


_NAME_RE = re.compile(r"^[^\x00-\x1f<>{}\\]{1,%d}$" % NAME_MAX)


def _set_name(session: dict, name: str = "") -> dict:
    name = (name or "").strip()
    if not _NAME_RE.match(name):
        return {"error": f"a name is 1-{NAME_MAX} characters, no line breaks or <>{{}}"}
    config.set("workspace", session["workspace_id"], "assistant_name", name)
    _mark(session["user_id"], name=True)
    return {"ok": True, "name": name, "note": "this is the household's name for you everywhere (chat header, notifications); the admin can change it in Settings"}


def _set_timezone(session: dict, timezone: str = "") -> dict:
    tz = (timezone or "").strip()
    if not usertime.is_valid_zone(tz):
        return {"error": f"{tz!r} isn't a real IANA timezone name -- use a name like America/New_York or Europe/London"}
    config.set("user", session["user_id"], "timezone", tz)
    _mark(session["user_id"], timezone=True)
    return {"ok": True, "timezone": tz, "now_for_them": usertime.fmt(session["user_id"], time.time(), "%A %H:%M %Z")}


def _propose_persona(session: dict, text: str = "") -> dict:
    """Store a DRAFT and show it to the admin as a card in the chat. Nothing here changes the live persona, and nothing a model can call does."""
    body = (text or "").replace("\r\n", "\n").strip()
    if not body:
        return {"error": "the persona text is empty"}
    if len(body) > PERSONA_BODY_MAX:
        return {"error": f"too long ({len(body)} characters; keep it under {PERSONA_BODY_MAX})"}
    if "=== PROMPT" in body:
        return {"error": "send only the persona text, without any PROMPT STARTS/ENDS marker lines; the app adds them"}
    if "{{ASSISTANT_NAME}}" not in body:
        return {"error": "use {{ASSISTANT_NAME}} where your name goes, so renaming you later keeps working"}
    uid = session["user_id"]
    card = conversation.add_message(uid, "assistant", body, kind=DRAFT_KIND, meta={"persona_draft": True})
    _save(uid, draft_persona=body, draft_card_id=card)
    return {"ok": True, "next": ("It is now on their screen as a card with Apply and Discard buttons. You can't apply it. Don't repeat the draft: say in a sentence what "
                                 "you emphasised and ask them to apply it or tell you what to change.")}


def _tour(session: dict, stop: str = "") -> dict:
    user = _user_of(session)
    stops = _visible_stops(user)
    uid = session["user_id"]
    if not stop:
        seen = set(state(uid)["steps"]["tour"])
        return {"stops": [{"stop": s["key"], "title": s["title"], "where": s["where"], "what": s["what"], "shown": s["key"] in seen} for s in stops],
                "note": "don't recite this list; ask which areas they care about, then call this again with a stop for the details and live state"}
    match = next((s for s in stops if s["key"] == stop), None)
    if match is None:
        return {"error": f"no such stop for them; choose one of: {', '.join(s['key'] for s in stops)}"}
    seen = state(uid)["steps"]["tour"]
    if stop not in seen:
        _mark(uid, tour=seen + [stop])
    return {"stop": match["key"], "title": match["title"], "where": match["where"], "what": match["what"], "current_state": match["state"]}


def _finish(session: dict, skipped: bool = False) -> dict:
    finish(session["user_id"], skipped=bool(skipped))
    return {"ok": True, "note": "onboarding is closed; they can redo it any time from Settings -> About (Meet the assistant again)"}


def _register_tools() -> None:
    def tool(name, desc, props, required, impl, *, gate, role="member"):
        tools.register(tools.Tool(
            name, {"type": "function", "function": {"name": name, "description": desc,
                                                     "parameters": {"type": "object", "properties": props, "required": required}}},
            impl, min_role=role, data_scope="self", risk_tier="B", enabled=gate))

    tool("onboarding_set_name", "Settle what the household calls you (the admin's choice; keeping the current name is fine). Shared by everyone in the household.",
         {"name": {"type": "string", "description": f"1-{NAME_MAX} characters"}}, ["name"], _set_name, gate=_gate_admin, role="admin")
    tool("onboarding_set_timezone", "Set the user's timezone (every time in the app, and the time you are told, follows it).",
         {"timezone": {"type": "string", "description": "an IANA name such as America/New_York or Europe/London"}}, ["timezone"], _set_timezone, gate=_gate)
    tool("onboarding_propose_persona",
         "Propose YOUR persona text (using {{ASSISTANT_NAME}} for your name). This only shows the admin a draft card with Apply and Discard buttons; you cannot apply it, "
         "and it changes nothing until they do.",
         {"text": {"type": "string"}}, ["text"], _propose_persona, gate=_gate_admin, role="admin")
    tool("onboarding_tour", "Give the tour: with no stop, the list of places; with a stop, what it is, where to find it and its current state.",
         {"stop": {"type": "string", "description": "a stop key from the list; omit for the list"}}, [], _tour, gate=_gate)
    tool("onboarding_finish", "Close the getting-set-up chat: done after the recap, or skipped=true if they said skip, later or stop.",
         {"skipped": {"type": "boolean", "default": False}}, [], _finish, gate=_gate)


_register_tools()
