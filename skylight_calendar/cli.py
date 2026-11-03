"""``skylight`` command line: log in once, then poke at the family calendar.

The saved login lives in the same token file :func:`SkylightClient.from_env`
reads, so ``skylight login`` is all another program on the machine needs.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from collections.abc import Callable, Sequence
from datetime import date, datetime, timedelta
from typing import Any

from . import __version__
from .client import SkylightClient
from .const import ENV_EMAIL, ENV_PASSWORD
from .errors import SkylightError
from .models import CalendarEvent, Chore, MealPlan, Profile, Reward, RewardPoints, SkylightList
from .tokens import FileTokenStore, default_token_path


def _parse_date(text: str) -> date:
    if text == "today":
        return date.today()
    if text == "tomorrow":
        return date.today() + timedelta(days=1)
    return date.fromisoformat(text)


def _emit(args: argparse.Namespace, data: Any, text: Callable[[], str]) -> None:
    if args.json:
        if hasattr(data, "to_dict"):
            data = data.to_dict()
        elif isinstance(data, list):
            data = [d.to_dict() if hasattr(d, "to_dict") else d for d in data]
        print(json.dumps(data, indent=2, default=str))
    else:
        print(text())


def _client(args: argparse.Namespace) -> SkylightClient:
    store = FileTokenStore(args.token_file) if args.token_file else FileTokenStore()
    overrides: dict[str, Any] = {}
    if args.frame:
        overrides["frame_id"] = args.frame
    if args.timezone:
        overrides["timezone"] = args.timezone
    return SkylightClient.from_env(token_store=store, **overrides)


# -- commands -----------------------------------------------------------------


def cmd_login(args: argparse.Namespace) -> int:
    email = args.email or os.environ.get(ENV_EMAIL) or input("Skylight email: ")
    password = os.environ.get(ENV_PASSWORD) or getpass.getpass("Skylight password: ")
    store = FileTokenStore(args.token_file) if args.token_file else FileTokenStore()
    with SkylightClient(email=email, password=password, token_store=store) as client:
        client.login()
        user = client.get_user()
        frames = client.list_frames()
    print(f"Logged in as {user.email or email}. Credentials saved to {store.path}")
    for frame in frames:
        print(f"  frame {frame.id}: {frame.display_name}" + (f" ({frame.timezone})" if frame.timezone else ""))
    if len(frames) > 1:
        print(f"Several frames: set SKYLIGHT_FRAME_ID or pass --frame to choose one.")
    return 0


def cmd_logout(args: argparse.Namespace) -> int:
    store = FileTokenStore(args.token_file) if args.token_file else FileTokenStore()
    store.clear()
    print(f"Removed {store.path}")
    return 0


def cmd_whoami(args: argparse.Namespace) -> int:
    with _client(args) as client:
        user = client.get_user()
        frames = client.list_frames()
    _emit(
        args,
        {"user": user.to_dict(), "frames": [f.to_dict() for f in frames]},
        lambda: "\n".join([f"{user.email or user.id}"] + [f"  frame {f.id}: {f.display_name}" for f in frames]),
    )
    return 0


def cmd_profiles(args: argparse.Namespace) -> int:
    with _client(args) as client:
        profiles = client.list_profiles()
    _emit(args, profiles, lambda: "\n".join(_fmt_profile(p) for p in profiles))
    return 0


def _fmt_profile(p: Profile) -> str:
    flags = []
    if p.linked_to_profile:
        flags.append("profile")
    if p.selected_for_chore_chart:
        flags.append("chores")
    return f"{p.id:>10}  {p.label or '?':<20} {p.color or '':<8} {' '.join(flags)}"


def cmd_events(args: argparse.Namespace) -> int:
    start = _parse_date(args.start) if args.start else date.today()
    end = _parse_date(args.end) if args.end else start + timedelta(days=args.days)
    with _client(args) as client:
        events = client.list_events(start, end)
        profiles = {p.id: p.label for p in client.list_profiles()} if not args.json else {}
    events.sort(key=lambda e: (e.starts_at.isoformat() if e.starts_at else ""))
    _emit(args, events, lambda: "\n".join(_fmt_event(e, profiles) for e in events) or "No events.")
    return 0


def _fmt_event(e: CalendarEvent, profiles: dict[str, str | None]) -> str:
    if e.starts_at is None:
        when = "?"
    elif e.all_day:
        when = e.starts_at.strftime("%a %Y-%m-%d  all day ")
    else:
        when = e.starts_at.strftime("%a %Y-%m-%d %H:%M")
        if e.ends_at:
            when += e.ends_at.strftime("-%H:%M")
    who = ", ".join(profiles.get(pid) or pid for pid in e.profile_ids)
    return f"{when}  {e.summary or ''}" + (f"  [{who}]" if who else "")


def cmd_lists(args: argparse.Namespace) -> int:
    with _client(args) as client:
        if args.add:
            lst = client.find_list(args.name) if args.name else client.grocery_list()
            for label in args.add:
                item = client.add_list_item(lst.id, label)
                print(f"Added '{item.label}' to {lst.label}")
            return 0
        lists = client.list_lists()
        if args.name:
            lists = [lst for lst in lists if args.name.lower() in (lst.label or "").lower()]
    _emit(args, lists, lambda: "\n\n".join(_fmt_list(lst, args.all) for lst in lists) or "No lists.")
    return 0


def _fmt_list(lst: SkylightList, show_all: bool) -> str:
    items = lst.items if show_all else lst.pending_items
    lines = [f"{lst.label} ({lst.kind}, id {lst.id})"]
    for item in items:
        mark = "x" if item.completed else " "
        section = f" [{item.section}]" if item.section else ""
        lines.append(f"  [{mark}] {item.label}{section}  (id {item.id})")
    return "\n".join(lines)


def cmd_chores(args: argparse.Namespace) -> int:
    start = _parse_date(args.start) if args.start else date.today()
    end = _parse_date(args.end) if args.end else start + timedelta(days=args.days)
    with _client(args) as client:
        if args.complete:
            for cid in args.complete:
                result = client.complete_chore(cid)
                print(f"Completed '{result.chore.summary}'" + (f" (balance {result.point_balance})" if result.point_balance is not None else ""))
            return 0
        chores = client.list_chores(start, end)
        profiles = {p.id: p.label for p in client.list_profiles()} if not args.json else {}
    _emit(args, chores, lambda: "\n".join(_fmt_chore(c, profiles) for c in chores) or "No chores.")
    return 0


def _fmt_chore(c: Chore, profiles: dict[str, str | None]) -> str:
    mark = "x" if c.done else " "
    who = profiles.get(c.profile_id or "") or ("up for grabs" if c.up_for_grabs else c.profile_id or "")
    pts = f" +{c.reward_points}" if c.reward_points else ""
    when = c.start.isoformat() if c.start else "on demand"
    return f"[{mark}] {when} {c.start_time or '     '}  {c.summary or ''}{pts}  [{who}]  (id {c.id})"


def cmd_rewards(args: argparse.Namespace) -> int:
    with _client(args) as client:
        rewards = client.list_rewards()
        points = client.get_reward_points()
        profiles = {p.id: p.label for p in client.list_profiles()}
    _emit(
        args,
        {"points": [p.to_dict() for p in points], "rewards": [r.to_dict() for r in rewards]},
        lambda: _fmt_rewards(rewards, points, profiles),
    )
    return 0


def _fmt_rewards(rewards: list[Reward], points: list[RewardPoints], profiles: dict[str, str | None]) -> str:
    lines = ["Points:"]
    for p in points:
        lines.append(f"  {profiles.get(p.profile_id) or p.profile_id:<20} {p.current_point_balance:>5} (lifetime {p.lifetime_points_earned})")
    lines.append("Rewards:")
    for r in rewards:
        who = profiles.get(r.profile_id or "") or r.profile_id or ""
        state = "redeemed" if r.redeemed else "available"
        lines.append(f"  {r.emoji_icon or ''} {r.name} ({r.point_value} pts) [{who}] {state}  (id {r.id})")
    return "\n".join(lines)


def cmd_meals(args: argparse.Namespace) -> int:
    start = _parse_date(args.start) if args.start else date.today()
    end = _parse_date(args.end) if args.end else start + timedelta(days=args.days)
    with _client(args) as client:
        plan = client.get_meal_plan(start, end)
    _emit(args, plan, lambda: _fmt_plan(plan, start, end))
    return 0


def _fmt_plan(plan: MealPlan, start: date, end: date) -> str:
    lines = []
    day = start
    while day <= end:
        sittings = plan.on(day)
        if sittings:
            lines.append(day.strftime("%a %Y-%m-%d"))
            for s in sittings:
                recipe = plan.recipe_for(s)
                name = s.summary or (recipe.summary if recipe else "") or "(unnamed)"
                lines.append(f"  {plan.category_label(s) or '?':<10} {name}")
        day += timedelta(days=1)
    return "\n".join(lines) or "No meals planned."


def cmd_raw(args: argparse.Namespace) -> int:
    body = json.loads(args.body) if args.body else None
    with _client(args) as client:
        if "{frame}" in args.path:
            args.path = args.path.replace("{frame}", client._frame(None))
        result = client.request(args.method, args.path, json=body)
    print(json.dumps(result, indent=2))
    return 0


# -- parser -------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="skylight", description="Talk to your Skylight family calendar.")
    parser.add_argument("--version", action="version", version=f"skylight-calendar {__version__}")
    parser.add_argument("--frame", help="frame id (default: $SKYLIGHT_FRAME_ID or the first frame)")
    parser.add_argument("--timezone", help="IANA timezone for calendar queries (default: the frame's)")
    parser.add_argument("--token-file", help=f"where the login is saved (default: {default_token_path()})")
    parser.add_argument("--json", action="store_true", help="print JSON instead of text")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("login", help="log in with email/password and save the session")
    p.add_argument("--email")
    p.set_defaults(func=cmd_login)

    sub.add_parser("logout", help="delete the saved session").set_defaults(func=cmd_logout)
    sub.add_parser("whoami", help="show the account and its frames").set_defaults(func=cmd_whoami)
    sub.add_parser("profiles", help="list family members").set_defaults(func=cmd_profiles)

    def window(p: argparse.ArgumentParser, days: int) -> None:
        p.add_argument("--start", help="YYYY-MM-DD, today or tomorrow (default today)")
        p.add_argument("--end", help="YYYY-MM-DD (default start + --days)")
        p.add_argument("--days", type=int, default=days)

    p = sub.add_parser("events", help="calendar events")
    window(p, 7)
    p.set_defaults(func=cmd_events)

    p = sub.add_parser("lists", help="shopping and to-do lists")
    p.add_argument("name", nargs="?", help="only this list (substring match)")
    p.add_argument("--add", nargs="+", metavar="ITEM", help="add items (to the named list, else the grocery list)")
    p.add_argument("--all", action="store_true", help="show completed items too")
    p.set_defaults(func=cmd_lists)

    p = sub.add_parser("chores", help="chores / tasks")
    window(p, 0)
    p.add_argument("--complete", nargs="+", metavar="CHORE_ID", help="mark these chores done")
    p.set_defaults(func=cmd_chores)

    sub.add_parser("rewards", help="reward points and rewards").set_defaults(func=cmd_rewards)

    p = sub.add_parser("meals", help="meal plan")
    window(p, 7)
    p.set_defaults(func=cmd_meals)

    p = sub.add_parser("raw", help="call any /api path, e.g. raw GET /frames/{frame}/nudges")
    p.add_argument("method")
    p.add_argument("path")
    p.add_argument("--body", help="JSON request body")
    p.set_defaults(func=cmd_raw)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except SkylightError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
