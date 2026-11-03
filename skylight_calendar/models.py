"""Typed views over Skylight's JSON:API resources.

Every model keeps the untouched resource under ``raw`` so nothing the API
sends is lost, and exposes the attributes we understand as plain fields.
``to_dict()`` gives a JSON-friendly dict (handy for agent tools and logging).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, fields
from datetime import date, datetime
from typing import Any, ClassVar, TypeVar

from .const import CHORE_DONE_STATUSES, LIST_ITEM_STATUS_COMPLETED

# ---------------------------------------------------------------------------
# JSON:API helpers
# ---------------------------------------------------------------------------


def _attrs(resource: dict[str, Any]) -> dict[str, Any]:
    return dict(resource.get("attributes") or {})


def _rel_id(resource: dict[str, Any], name: str) -> str | None:
    """Id of a to-one relationship, or ``None``."""
    rel = (resource.get("relationships") or {}).get(name) or {}
    data = rel.get("data")
    if isinstance(data, dict) and data.get("id") is not None:
        return str(data["id"])
    return None


def _rel_ids(resource: dict[str, Any], name: str) -> list[str]:
    """Ids of a to-many relationship."""
    rel = (resource.get("relationships") or {}).get(name) or {}
    data = rel.get("data")
    if isinstance(data, list):
        return [str(d["id"]) for d in data if isinstance(d, dict) and d.get("id") is not None]
    return []


def parse_datetime(value: Any) -> datetime | None:
    """ISO-8601 string (with ``Z``/offset) or ``YYYY-MM-DD`` to :class:`datetime`.

    A date-only value becomes a naive midnight datetime. Anything unparsable
    returns ``None`` rather than raising, since the API shape is unofficial.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def parse_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _jsonable(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return value


@dataclass
class Resource:
    """Base for everything parsed from a JSON:API resource object."""

    id: str
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    #: JSON:API ``type`` values this model is built from (informational).
    jsonapi_types: ClassVar[tuple[str, ...]] = ()

    @property
    def attributes(self) -> dict[str, Any]:
        """The raw ``attributes`` object, for fields this package doesn't model."""
        return self.raw.get("attributes") or {}

    def to_dict(self, *, include_raw: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for f in fields(self):
            if f.name == "raw" and not include_raw:
                continue
            out[f.name] = _jsonable(getattr(self, f.name))
        return out

    @classmethod
    def from_resource(cls, resource: dict[str, Any]) -> Any:  # pragma: no cover - overridden
        raise NotImplementedError


R = TypeVar("R", bound=Resource)


def _data_list(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        if "data" in payload:
            data = payload.get("data")
        elif "id" in payload or "attributes" in payload:
            data = payload  # a bare resource object, no envelope
        else:
            data = None
    else:
        data = payload
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    return []


def parse_many(payload: Any, model: type[R]) -> list[R]:
    """``{"data": [...]}`` (or a bare list) to models."""
    return [model.from_resource(d) for d in _data_list(payload)]


def parse_one(payload: Any, model: type[R]) -> R:
    """``{"data": {...}}`` (or ``{"data": [one]}``) to a model."""
    items = _data_list(payload)
    if not items:
        raise ValueError(f"expected one {model.__name__} resource, got none")
    return model.from_resource(items[0])


def included(payload: Any, jsonapi_type: str) -> list[dict[str, Any]]:
    """Resources of one type from a payload's ``included`` array."""
    if not isinstance(payload, dict):
        return []
    return [
        r
        for r in payload.get("included") or []
        if isinstance(r, dict) and r.get("type") == jsonapi_type
    ]


# ---------------------------------------------------------------------------
# Account
# ---------------------------------------------------------------------------


@dataclass
class User(Resource):
    email: str | None = None
    first_name: str | None = None
    last_name: str | None = None

    jsonapi_types = ("user",)

    @property
    def name(self) -> str | None:
        parts = [p for p in (self.first_name, self.last_name) if p]
        return " ".join(parts) or self.attributes.get("name")

    @classmethod
    def from_resource(cls, resource: dict[str, Any]) -> "User":
        a = _attrs(resource)
        return cls(
            id=str(resource.get("id", "")),
            email=a.get("email"),
            first_name=a.get("first_name"),
            last_name=a.get("last_name"),
            raw=resource,
        )


@dataclass
class Frame(Resource):
    """A household. Nearly every endpoint is scoped to one of these."""

    name: str | None = None
    household_name: str | None = None
    timezone: str | None = None
    frame_type: str | None = None

    jsonapi_types = ("frame",)

    @property
    def display_name(self) -> str:
        return self.household_name or self.name or f"Frame {self.id}"

    @classmethod
    def from_resource(cls, resource: dict[str, Any]) -> "Frame":
        a = _attrs(resource)
        return cls(
            id=str(resource.get("id", "")),
            name=a.get("name"),
            household_name=a.get("household_name"),
            timezone=a.get("timezone") or a.get("time_zone"),
            frame_type=a.get("frame_type") or a.get("kind"),
            raw=resource,
        )


@dataclass
class Device(Resource):
    """A physical Skylight screen attached to a frame (brightness etc. live here)."""

    name: str | None = None

    jsonapi_types = ("device",)

    @classmethod
    def from_resource(cls, resource: dict[str, Any]) -> "Device":
        a = _attrs(resource)
        return cls(id=str(resource.get("id", "")), name=a.get("name") or a.get("label"), raw=resource)


@dataclass
class Profile(Resource):
    """A family member. Skylight calls these *categories* on the wire."""

    label: str | None = None
    color: str | None = None
    linked_to_profile: bool = False
    selected_for_chore_chart: bool = False
    profile_pic_url: str | None = None
    avatar_id: str | None = None
    source_calendar_ids: list[str] = field(default_factory=list)

    jsonapi_types = ("category", "category_detail")

    @property
    def name(self) -> str | None:
        return self.label

    @classmethod
    def from_resource(cls, resource: dict[str, Any]) -> "Profile":
        a = _attrs(resource)
        return cls(
            id=str(resource.get("id", "")),
            label=a.get("label"),
            color=a.get("color"),
            linked_to_profile=bool(a.get("linked_to_profile")),
            selected_for_chore_chart=bool(a.get("selected_for_chore_chart")),
            profile_pic_url=a.get("profile_pic_url"),
            avatar_id=_rel_id(resource, "avatar"),
            source_calendar_ids=_rel_ids(resource, "source_calendars"),
            raw=resource,
        )


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------


@dataclass
class CalendarEvent(Resource):
    summary: str | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    all_day: bool = False
    description: str | None = None
    location: str | None = None
    timezone: str | None = None
    recurring: bool = False
    rrule: list[str] = field(default_factory=list)
    status: str | None = None
    source: str | None = None
    kind: str | None = None
    editable: bool = True
    countdown_enabled: bool = False
    invited_emails: list[str] = field(default_factory=list)
    owner_email: str | None = None
    calendar_id: str | None = None
    calendar_account_id: str | None = None
    master_event_id: str | None = None
    uid: str | None = None
    profile_ids: list[str] = field(default_factory=list)

    jsonapi_types = ("calendar_event",)

    @property
    def series_id(self) -> str:
        """Recurring occurrences are listed as ``<series id>-<epoch>``; this is the series part."""
        return self.id.split("-", 1)[0]

    @classmethod
    def from_resource(cls, resource: dict[str, Any]) -> "CalendarEvent":
        a = _attrs(resource)
        rrule = a.get("rrule")
        if isinstance(rrule, str):
            rrule = [rrule]
        profile_ids = _rel_ids(resource, "categories")
        single = _rel_id(resource, "category")
        if single and single not in profile_ids:
            profile_ids.append(single)
        return cls(
            id=str(resource.get("id", "")),
            summary=a.get("summary"),
            starts_at=parse_datetime(a.get("starts_at")),
            ends_at=parse_datetime(a.get("ends_at")),
            all_day=bool(a.get("all_day")),
            description=a.get("description"),
            location=a.get("location"),
            timezone=a.get("timezone"),
            recurring=bool(a.get("recurring")),
            rrule=list(rrule or []),
            status=a.get("status"),
            source=a.get("source"),
            kind=a.get("kind"),
            editable=bool(a.get("editable", True)),
            countdown_enabled=bool(a.get("countdown_enabled")),
            invited_emails=list(a.get("invited_emails") or []),
            owner_email=a.get("owner_email"),
            calendar_id=a.get("calendar_id"),
            calendar_account_id=_rel_id(resource, "calendar_account"),
            master_event_id=str(a["master_event_id"]) if a.get("master_event_id") is not None else None,
            uid=a.get("uid"),
            profile_ids=profile_ids,
            raw=resource,
        )


@dataclass
class SourceCalendar(Resource):
    """A calendar synced into Skylight (Google, iCloud, Outlook, webcal...)."""

    name: str | None = None
    color: str | None = None
    enabled: bool = True
    account_id: str | None = None
    profile_ids: list[str] = field(default_factory=list)

    jsonapi_types = ("source_calendar",)

    @classmethod
    def from_resource(cls, resource: dict[str, Any]) -> "SourceCalendar":
        a = _attrs(resource)
        enabled = a.get("enabled")
        if enabled is None:
            enabled = a.get("selected")
        return cls(
            id=str(resource.get("id", "")),
            name=a.get("name") or a.get("label") or a.get("summary"),
            color=a.get("color"),
            enabled=True if enabled is None else bool(enabled),
            account_id=_rel_id(resource, "calendar_account"),
            profile_ids=_rel_ids(resource, "categories"),
            raw=resource,
        )


# ---------------------------------------------------------------------------
# Lists
# ---------------------------------------------------------------------------


@dataclass
class ListItem(Resource):
    label: str | None = None
    status: str = "pending"
    section: str | None = None
    position: int | None = None
    created_at: datetime | None = None
    list_id: str | None = None

    jsonapi_types = ("list_item",)

    @property
    def completed(self) -> bool:
        return self.status == LIST_ITEM_STATUS_COMPLETED

    @classmethod
    def from_resource(cls, resource: dict[str, Any]) -> "ListItem":
        a = _attrs(resource)
        return cls(
            id=str(resource.get("id", "")),
            label=a.get("label"),
            status=a.get("status") or "pending",
            section=a.get("section"),
            position=a.get("position"),
            created_at=parse_datetime(a.get("created_at")),
            list_id=_rel_id(resource, "list"),
            raw=resource,
        )


@dataclass
class SkylightList(Resource):
    """A shopping or to-do list. ``items`` is filled when the API included them."""

    label: str | None = None
    kind: str | None = None
    color: str | None = None
    default_grocery_list: bool = False
    hide_on_device: bool = False
    item_ids: list[str] = field(default_factory=list)
    items: list[ListItem] = field(default_factory=list)

    jsonapi_types = ("list",)

    @property
    def is_shopping(self) -> bool:
        return self.kind == "shopping"

    @property
    def pending_items(self) -> list[ListItem]:
        return [i for i in self.items if not i.completed]

    @classmethod
    def from_resource(cls, resource: dict[str, Any]) -> "SkylightList":
        a = _attrs(resource)
        return cls(
            id=str(resource.get("id", "")),
            label=a.get("label"),
            kind=a.get("kind"),
            color=a.get("color"),
            default_grocery_list=bool(a.get("default_grocery_list")),
            hide_on_device=bool(a.get("hide_on_device")),
            item_ids=_rel_ids(resource, "list_items"),
            raw=resource,
        )

    def attach_items(self, items: list[ListItem]) -> "SkylightList":
        """Attach the items that belong to this list, in position order."""
        wanted = set(self.item_ids)
        mine = [i for i in items if i.id in wanted or i.list_id == self.id]
        mine.sort(key=lambda i: (i.position if i.position is not None else 1_000_000, i.id))
        self.items = mine
        return self


def parse_lists(payload: Any) -> list[SkylightList]:
    """Lists plus their ``included`` items, as the lists endpoints return them."""
    items = [ListItem.from_resource(r) for r in included(payload, "list_item")]
    return [lst.attach_items(items) for lst in parse_many(payload, SkylightList)]


# ---------------------------------------------------------------------------
# Chores / tasks
# ---------------------------------------------------------------------------

_OCCURRENCE_ID = re.compile(r"^(\d+)-(\d{4}-\d{2}-\d{2})(?:-(\d{2})(\d{2}))?$")


@dataclass(frozen=True)
class ChoreOccurrence:
    """The pieces of a recurring-chore occurrence id: ``<series>-<YYYY-MM-DD>[-<HHMM>]``."""

    series_id: str
    instance_date: str
    instance_time: str | None = None

    @classmethod
    def parse(cls, chore_id: str) -> "ChoreOccurrence | None":
        m = _OCCURRENCE_ID.match(str(chore_id))
        if not m:
            return None
        series, day, hh, mm = m.groups()
        return cls(series, day, f"{hh}:{mm}" if hh else None)


@dataclass
class Chore(Resource):
    """A task on the chore chart. Recurring ones are listed once per occurrence."""

    summary: str | None = None
    status: str = "pending"
    start: date | None = None
    start_time: str | None = None
    completed_on: date | None = None
    description: str | None = None
    recurring: bool = False
    recurrence_set: list[str] = field(default_factory=list)
    recurring_until: date | None = None
    reward_points: int | None = None
    emoji_icon: str | None = None
    routine: bool = False
    up_for_grabs: bool = False
    position: int | None = None
    group: str | None = None
    profile_id: str | None = None

    jsonapi_types = ("chore",)

    @property
    def done(self) -> bool:
        return self.status in CHORE_DONE_STATUSES

    @property
    def occurrence(self) -> ChoreOccurrence | None:
        return ChoreOccurrence.parse(self.id)

    @property
    def series_id(self) -> str:
        occ = self.occurrence
        return occ.series_id if occ else self.id

    @classmethod
    def from_resource(cls, resource: dict[str, Any]) -> "Chore":
        a = _attrs(resource)
        rs = a.get("recurrence_set")
        if isinstance(rs, str):
            rs = [rs]
        return cls(
            id=str(resource.get("id", "")),
            summary=a.get("summary"),
            status=a.get("status") or "pending",
            start=parse_date(a.get("start")),
            start_time=a.get("start_time"),
            completed_on=parse_date(a.get("completed_on")),
            description=a.get("description"),
            recurring=bool(a.get("recurring")),
            recurrence_set=list(rs or []),
            recurring_until=parse_date(a.get("recurring_until")),
            reward_points=a.get("reward_points"),
            emoji_icon=a.get("emoji_icon"),
            routine=bool(a.get("routine")),
            up_for_grabs=bool(a.get("up_for_grabs")),
            position=a.get("position"),
            group=str(a["group"]) if a.get("group") is not None else None,
            profile_id=_rel_id(resource, "category"),
            raw=resource,
        )


@dataclass
class ChoreCompletion:
    """What the completions endpoint reports after ticking a chore."""

    chore: Chore
    point_balance: int | None = None
    lifetime_points: int | None = None
    current_streak: int | None = None
    longest_streak: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "chore": self.chore.to_dict(),
            "point_balance": self.point_balance,
            "lifetime_points": self.lifetime_points,
            "current_streak": self.current_streak,
            "longest_streak": self.longest_streak,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "ChoreCompletion":
        chore = parse_one(payload, Chore)
        meta = payload.get("meta") or {}
        points = meta.get("reward_points") or {}
        if not isinstance(points, dict):
            points = {}
        tracker = next((t for t in included(payload, "habit_tracker")), None)
        ta = _attrs(tracker) if tracker else {}
        return cls(
            chore=chore,
            point_balance=points.get("current_point_balance"),
            lifetime_points=points.get("lifetime_points_earned"),
            current_streak=ta.get("current_streak"),
            longest_streak=ta.get("longest_streak"),
        )


@dataclass
class TaskBoxItem(Resource):
    """A reusable, unscheduled task template."""

    summary: str | None = None
    emoji_icon: str | None = None
    routine: bool = False
    reward_points: int | None = None

    jsonapi_types = ("task_box_item",)

    @classmethod
    def from_resource(cls, resource: dict[str, Any]) -> "TaskBoxItem":
        a = _attrs(resource)
        return cls(
            id=str(resource.get("id", "")),
            summary=a.get("summary"),
            emoji_icon=a.get("emoji_icon"),
            routine=bool(a.get("routine")),
            reward_points=a.get("reward_points"),
            raw=resource,
        )


# ---------------------------------------------------------------------------
# Rewards
# ---------------------------------------------------------------------------


@dataclass
class Reward(Resource):
    """A reward one family member can redeem. Skylight stores one record per member."""

    name: str | None = None
    description: str | None = None
    emoji_icon: str | None = None
    point_value: int | None = None
    redeemed_at: datetime | None = None
    respawn_on_redemption: bool = False
    profile_id: str | None = None

    jsonapi_types = ("reward",)

    @property
    def redeemed(self) -> bool:
        return self.redeemed_at is not None

    @classmethod
    def from_resource(cls, resource: dict[str, Any]) -> "Reward":
        a = _attrs(resource)
        return cls(
            id=str(resource.get("id", "")),
            name=a.get("name"),
            description=a.get("description"),
            emoji_icon=a.get("emoji_icon"),
            point_value=a.get("point_value"),
            redeemed_at=parse_datetime(a.get("redeemed_at")),
            respawn_on_redemption=bool(a.get("respawn_on_redemption")),
            profile_id=_rel_id(resource, "category"),
            raw=resource,
        )


@dataclass
class RewardPoints:
    """Point balance for one family member (not a JSON:API resource on the wire)."""

    profile_id: str
    current_point_balance: int = 0
    lifetime_points_earned: int = 0
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def to_dict(self, *, include_raw: bool = False) -> dict[str, Any]:
        out = {
            "profile_id": self.profile_id,
            "current_point_balance": self.current_point_balance,
            "lifetime_points_earned": self.lifetime_points_earned,
        }
        if include_raw:
            out["raw"] = self.raw
        return out

    @classmethod
    def from_payload(cls, payload: Any) -> list["RewardPoints"]:
        rows = payload
        if isinstance(payload, dict):
            rows = payload.get("data", payload.get("reward_points", []))
        out: list[RewardPoints] = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            a = row.get("attributes") if "attributes" in row else row
            a = a or {}
            pid = a.get("category_id") or _rel_id(row, "category") or row.get("id")
            if pid is None:
                continue
            out.append(
                cls(
                    profile_id=str(pid),
                    current_point_balance=int(a.get("current_point_balance") or 0),
                    lifetime_points_earned=int(a.get("lifetime_points_earned") or 0),
                    raw=row,
                )
            )
        return out


# ---------------------------------------------------------------------------
# Meals
# ---------------------------------------------------------------------------


@dataclass
class MealCategory(Resource):
    """A slot in the day: Breakfast, Lunch, Dinner, Snack."""

    label: str | None = None
    color: str | None = None
    position: int | None = None
    enabled: bool = True

    jsonapi_types = ("meal_category",)

    @classmethod
    def from_resource(cls, resource: dict[str, Any]) -> "MealCategory":
        a = _attrs(resource)
        return cls(
            id=str(resource.get("id", "")),
            label=a.get("label") or a.get("name"),
            color=a.get("color"),
            position=a.get("position"),
            enabled=bool(a.get("enabled", True)),
            raw=resource,
        )


@dataclass
class Recipe(Resource):
    summary: str | None = None
    description: str | None = None
    meal_category_id: str | None = None

    jsonapi_types = ("meal_recipe",)

    @classmethod
    def from_resource(cls, resource: dict[str, Any]) -> "Recipe":
        a = _attrs(resource)
        mc = _rel_id(resource, "meal_category")
        if mc is None and a.get("meal_category_id") is not None:
            mc = str(a["meal_category_id"])
        return cls(
            id=str(resource.get("id", "")),
            summary=a.get("summary"),
            description=a.get("description"),
            meal_category_id=mc,
            raw=resource,
        )


@dataclass
class MealSitting(Resource):
    """A planned meal: a slot on one or more dates, optionally tied to a recipe."""

    summary: str | None = None
    description: str | None = None
    note: str | None = None
    dates: list[date] = field(default_factory=list)
    recurring: bool = False
    rrule: str | None = None
    meal_category_id: str | None = None
    recipe_id: str | None = None
    profile_ids: list[str] = field(default_factory=list)

    jsonapi_types = ("meal_sitting",)

    @property
    def date(self) -> date | None:
        return self.dates[0] if self.dates else None

    @property
    def kind(self) -> str:
        """``series`` (has an rrule), ``detached`` (edited occurrence) or ``one-off``."""
        if self.rrule:
            return "series"
        if self.recurring:
            return "detached"
        return "one-off"

    @classmethod
    def from_resource(cls, resource: dict[str, Any]) -> "MealSitting":
        a = _attrs(resource)
        raw_dates = a.get("instances")
        if not isinstance(raw_dates, list):
            raw_dates = [a.get("date")] if a.get("date") else []
        dates = [d for d in (parse_date(x) for x in raw_dates) if d is not None]
        rrule = a.get("rrule")
        if isinstance(rrule, list):
            rrule = rrule[0] if rrule else None
        return cls(
            id=str(resource.get("id", "")),
            summary=a.get("summary"),
            description=a.get("description"),
            note=a.get("note"),
            dates=dates,
            recurring=bool(a.get("recurring")),
            rrule=rrule,
            meal_category_id=_rel_id(resource, "meal_category"),
            recipe_id=_rel_id(resource, "meal_recipe"),
            profile_ids=_rel_ids(resource, "profiles"),
            raw=resource,
        )


@dataclass
class MealPlan:
    """Sittings for a date range plus the categories and recipes they refer to."""

    sittings: list[MealSitting] = field(default_factory=list)
    categories: dict[str, MealCategory] = field(default_factory=dict)
    recipes: dict[str, Recipe] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sittings": [s.to_dict() for s in self.sittings],
            "categories": {k: v.to_dict() for k, v in self.categories.items()},
            "recipes": {k: v.to_dict() for k, v in self.recipes.items()},
        }

    def category_label(self, sitting: MealSitting) -> str | None:
        cat = self.categories.get(sitting.meal_category_id or "")
        return cat.label if cat else None

    def recipe_for(self, sitting: MealSitting) -> Recipe | None:
        return self.recipes.get(sitting.recipe_id or "")

    def on(self, day: date) -> list[MealSitting]:
        out = [s for s in self.sittings if day in s.dates]
        out.sort(key=lambda s: (self.categories.get(s.meal_category_id or "", MealCategory(id="")).position or 0))
        return out

    @classmethod
    def from_payload(cls, payload: Any) -> "MealPlan":
        cats = [MealCategory.from_resource(r) for r in included(payload, "meal_category")]
        recipes = [Recipe.from_resource(r) for r in included(payload, "meal_recipe")]
        return cls(
            sittings=parse_many(payload, MealSitting),
            categories={c.id: c for c in cats},
            recipes={r.id: r for r in recipes},
        )
