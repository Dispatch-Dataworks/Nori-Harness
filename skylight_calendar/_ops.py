"""Request descriptions for every supported endpoint.

An :class:`Operation` says what to send and how to turn the JSON that comes
back into models. It does no I/O, so the sync and async clients share it and
tests can check payloads without a network.

Where a request body was captured from the Skylight web app it is marked
"captured"; where it was reconstructed from other clients it is marked
"inferred" and may need correcting from a real capture if it 422s.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Generic, TypeVar
from zoneinfo import ZoneInfo

from .const import (
    CHORE_STATUS_COMPLETE,
    CHORE_STATUS_PENDING,
    LIST_ITEM_STATUS_COMPLETED,
    LIST_ITEM_STATUS_PENDING,
    MEAL_EDIT_API_VERSION,
)
from .models import (
    CalendarEvent,
    Chore,
    ChoreCompletion,
    Device,
    Frame,
    ListItem,
    MealCategory,
    MealPlan,
    MealSitting,
    Profile,
    Recipe,
    Reward,
    RewardPoints,
    SkylightList,
    SourceCalendar,
    TaskBoxItem,
    User,
    parse_lists,
    parse_many,
    parse_one,
)

T = TypeVar("T")

JsonParams = Mapping[str, Any]


@dataclass(frozen=True)
class Operation(Generic[T]):
    method: str
    #: Path under ``/api`` (e.g. ``/frames/123/chores``).
    path: str
    params: dict[str, Any] = field(default_factory=dict)
    json: Any = None
    #: Override ``Skylight-Api-Version`` for this call.
    api_version: str | None = None
    parse: Callable[[Any], T] = lambda payload: payload  # type: ignore[assignment]

    @property
    def has_body(self) -> bool:
        return self.json is not None


def _none(_: Any) -> None:
    return None


def _compact(**fields: Any) -> dict[str, Any]:
    """Drop ``None`` values (for endpoints that reject explicit nulls)."""
    return {k: v for k, v in fields.items() if v is not None}


def _ids(values: Iterable[Any] | None) -> list[str]:
    return [str(v) for v in values or []]


def _frame(frame_id: Any) -> str:
    return f"/frames/{frame_id}"


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------


def iso_date(value: date | datetime | str) -> str:
    """``YYYY-MM-DD`` from a date, datetime or string."""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10]


def iso_datetime(value: datetime | date | str, timezone: str | None) -> str:
    """ISO-8601 with an offset.

    Skylight reads a timestamp with no offset as UTC even when a ``timezone``
    is sent alongside, so naive datetimes are localised to ``timezone`` first.
    A bare date becomes local midnight. Strings pass through untouched.
    """
    if isinstance(value, str):
        return value
    if not isinstance(value, datetime):
        value = datetime(value.year, value.month, value.day)
    if value.tzinfo is None and timezone:
        value = value.replace(tzinfo=ZoneInfo(timezone))
    return value.isoformat(timespec="milliseconds")


# ---------------------------------------------------------------------------
# Account, frames, devices
# ---------------------------------------------------------------------------


def get_user() -> Operation[User]:
    return Operation("GET", "/user", parse=lambda p: parse_one(p, User))


def list_frames() -> Operation[list[Frame]]:
    return Operation("GET", "/frames", parse=lambda p: parse_many(p, Frame))


def get_frame(frame_id: Any) -> Operation[Frame]:
    return Operation("GET", _frame(frame_id), parse=lambda p: parse_one(p, Frame))


def list_devices(frame_id: Any) -> Operation[list[Device]]:
    return Operation("GET", f"{_frame(frame_id)}/devices", parse=lambda p: parse_many(p, Device))


def update_device(frame_id: Any, device_id: Any, attributes: Mapping[str, Any]) -> Operation[Device]:
    """Flat PATCH body (captured). Keys such as ``brightness``, ``sleep_mode_on``."""
    return Operation(
        "PATCH", f"{_frame(frame_id)}/devices/{device_id}", json=dict(attributes), parse=lambda p: parse_one(p, Device)
    )


# ---------------------------------------------------------------------------
# Profiles (categories on the wire)
# ---------------------------------------------------------------------------


def list_profiles(frame_id: Any) -> Operation[list[Profile]]:
    return Operation(
        "GET",
        f"{_frame(frame_id)}/categories",
        params={"include_profiles": "true"},
        parse=lambda p: parse_many(p, Profile),
    )


def get_profile(frame_id: Any, profile_id: Any) -> Operation[Profile]:
    return Operation("GET", f"{_frame(frame_id)}/categories/{profile_id}", parse=lambda p: parse_one(p, Profile))


def create_profile(frame_id: Any, label: str, color: str | None, extra: Mapping[str, Any] | None = None) -> Operation[Profile]:
    """Flat POST body (inferred)."""
    body = _compact(label=label, color=color, **dict(extra or {}))
    return Operation("POST", f"{_frame(frame_id)}/categories", json=body, parse=lambda p: parse_one(p, Profile))


def update_profile(frame_id: Any, profile_id: Any, attributes: Mapping[str, Any]) -> Operation[Profile]:
    """Flat PUT body (captured): ``label``, ``color``, ``avatar_id``, ``linked_to_profile``, ``selected_for_chore_chart``."""
    return Operation(
        "PUT", f"{_frame(frame_id)}/categories/{profile_id}", json=dict(attributes), parse=lambda p: parse_one(p, Profile)
    )


def delete_profile(frame_id: Any, profile_id: Any, reassign_to_id: Any = None) -> Operation[None]:
    return Operation(
        "DELETE",
        f"{_frame(frame_id)}/categories/{profile_id}",
        params=_compact(reassign_to_id=str(reassign_to_id) if reassign_to_id is not None else None),
        parse=_none,
    )


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------


def list_events(
    frame_id: Any,
    start: date | datetime | str,
    end: date | datetime | str,
    timezone: str,
    include: str | None = "categories,calendar_account",
) -> Operation[list[CalendarEvent]]:
    """Events between ``start`` and ``end`` (``end`` inclusive when given as a date).

    The API treats ``date_max`` as exclusive, so a plain date is pushed one day.
    """
    date_max = end
    if isinstance(end, date) and not isinstance(end, datetime):
        date_max = end + timedelta(days=1)
    return Operation(
        "GET",
        f"{_frame(frame_id)}/calendar_events",
        params=_compact(date_min=iso_date(start), date_max=iso_date(date_max), timezone=timezone, include=include),
        parse=lambda p: parse_many(p, CalendarEvent),
    )


def create_event(
    frame_id: Any,
    *,
    summary: str,
    starts_at: datetime | date | str,
    ends_at: datetime | date | str,
    timezone: str,
    all_day: bool = False,
    description: str | None = None,
    location: str | None = None,
    profile_ids: Iterable[Any] | None = None,
    calendar_account_id: Any = None,
    calendar_id: str | None = None,
    rrule: str | list[str] | None = None,
    countdown_enabled: bool = False,
    invited_emails: Iterable[str] | None = None,
    kind: str = "standard",
) -> Operation[CalendarEvent]:
    """Flat POST body (captured from the web app)."""
    if isinstance(rrule, str):
        rrule = [rrule]
    body: dict[str, Any] = {
        "summary": summary,
        "starts_at": iso_datetime(starts_at, timezone),
        "ends_at": iso_datetime(ends_at, timezone),
        "all_day": all_day,
        "timezone": timezone,
        "kind": kind,
        "description": description,
        "location": location or "",
        "category_ids": _ids(profile_ids),
        "countdown_enabled": countdown_enabled,
        "invited_emails": list(invited_emails or []),
        "rrule": list(rrule) if rrule else None,
        "lat": None,
        "lng": None,
        "event_notification_setting_attributes": None,
    }
    if calendar_account_id is not None:
        body["calendar_account_id"] = str(calendar_account_id)
    if calendar_id is not None:
        body["calendar_id"] = calendar_id
    return Operation("POST", f"{_frame(frame_id)}/calendar_events", json=body, parse=lambda p: parse_one(p, CalendarEvent))


_EVENT_DATETIME_KEYS = ("starts_at", "ends_at")


def update_event(frame_id: Any, event_id: Any, changes: Mapping[str, Any], timezone: str) -> Operation[CalendarEvent]:
    """Flat PUT body; only the given keys change. ``profile_ids`` maps to ``category_ids``."""
    body: dict[str, Any] = {}
    for key, value in changes.items():
        if key == "profile_ids":
            body["category_ids"] = _ids(value)
        elif key in _EVENT_DATETIME_KEYS and value is not None:
            body[key] = iso_datetime(value, changes.get("timezone") or timezone)
        elif key == "rrule" and isinstance(value, str):
            body[key] = [value]
        else:
            body[key] = value
    return Operation(
        "PUT", f"{_frame(frame_id)}/calendar_events/{event_id}", json=body, parse=lambda p: parse_one(p, CalendarEvent)
    )


def delete_event(frame_id: Any, event_id: Any) -> Operation[None]:
    return Operation("DELETE", f"{_frame(frame_id)}/calendar_events/{event_id}", parse=_none)


def list_source_calendars(frame_id: Any) -> Operation[list[SourceCalendar]]:
    return Operation("GET", f"{_frame(frame_id)}/source_calendars", parse=lambda p: parse_many(p, SourceCalendar))


# ---------------------------------------------------------------------------
# Lists
# ---------------------------------------------------------------------------


def _jsonapi(resource_type: str, attributes: Mapping[str, Any]) -> dict[str, Any]:
    return {"data": {"type": resource_type, "attributes": dict(attributes)}}


def list_lists(frame_id: Any) -> Operation[list[SkylightList]]:
    """All lists with their items (the endpoint includes ``list_item`` resources)."""
    return Operation("GET", f"{_frame(frame_id)}/lists", parse=parse_lists)


def get_list(frame_id: Any, list_id: Any) -> Operation[SkylightList]:
    return Operation(
        "GET", f"{_frame(frame_id)}/lists/{list_id}", params={"include": "list_items"}, parse=lambda p: parse_lists(p)[0]
    )


def create_list(frame_id: Any, label: str, kind: str, color: str | None = None) -> Operation[SkylightList]:
    """JSON:API envelope (inferred)."""
    return Operation(
        "POST",
        f"{_frame(frame_id)}/lists",
        json=_jsonapi("list", {"label": label, "kind": kind, "color": color}),
        parse=lambda p: parse_one(p, SkylightList),
    )


def update_list(frame_id: Any, list_id: Any, attributes: Mapping[str, Any]) -> Operation[SkylightList]:
    """JSON:API envelope (inferred): ``label``, ``kind``, ``color``."""
    return Operation(
        "PUT", f"{_frame(frame_id)}/lists/{list_id}", json=_jsonapi("list", attributes), parse=lambda p: parse_one(p, SkylightList)
    )


def delete_list(frame_id: Any, list_id: Any) -> Operation[None]:
    return Operation("DELETE", f"{_frame(frame_id)}/lists/{list_id}", parse=_none)


def add_list_item(frame_id: Any, list_id: Any, label: str, section: str | None = None) -> Operation[ListItem]:
    """Flat POST body (captured)."""
    return Operation(
        "POST",
        f"{_frame(frame_id)}/lists/{list_id}/list_items",
        json=_compact(label=label, section=section),
        parse=lambda p: parse_one(p, ListItem),
    )


def update_list_item(frame_id: Any, list_id: Any, item_id: Any, attributes: Mapping[str, Any]) -> Operation[ListItem]:
    """Flat PUT body (captured): ``label``, ``status``, ``section``, ``position``."""
    return Operation(
        "PUT",
        f"{_frame(frame_id)}/lists/{list_id}/list_items/{item_id}",
        json=dict(attributes),
        parse=lambda p: parse_one(p, ListItem),
    )


def set_list_item_completed(frame_id: Any, list_id: Any, item_id: Any, completed: bool) -> Operation[ListItem]:
    status = LIST_ITEM_STATUS_COMPLETED if completed else LIST_ITEM_STATUS_PENDING
    return update_list_item(frame_id, list_id, item_id, {"status": status})


def delete_list_item(frame_id: Any, list_id: Any, item_id: Any) -> Operation[None]:
    return Operation("DELETE", f"{_frame(frame_id)}/lists/{list_id}/list_items/{item_id}", parse=_none)


def delete_list_items(frame_id: Any, list_id: Any, item_ids: Iterable[Any]) -> Operation[None]:
    """Bulk delete (inferred body)."""
    return Operation(
        "DELETE",
        f"{_frame(frame_id)}/lists/{list_id}/list_items/bulk_destroy",
        json={"item_ids": _ids(item_ids)},
        parse=_none,
    )


# ---------------------------------------------------------------------------
# Chores
# ---------------------------------------------------------------------------


def list_chores(
    frame_id: Any,
    after: date | datetime | str,
    before: date | datetime | str,
    *,
    include_late: bool = True,
    include_up_for_grabs: bool = True,
    linked_to_profile: bool = False,
) -> Operation[list[Chore]]:
    params: dict[str, Any] = {
        "after": iso_date(after),
        "before": iso_date(before),
        "include_late": "true" if include_late else "false",
        "include_up_for_grabs": "true" if include_up_for_grabs else "false",
    }
    if linked_to_profile:
        params["filter"] = "linked_to_profile"
    return Operation("GET", f"{_frame(frame_id)}/chores", params=params, parse=lambda p: parse_many(p, Chore))


def create_chores(
    frame_id: Any,
    *,
    summary: str,
    profile_ids: Iterable[Any],
    start: date | datetime | str | None,
    description: str | None = None,
    start_time: str | None = None,
    routine: bool = False,
    up_for_grabs: bool = False,
    recurrence_set: str | list[str] | None = None,
    recurring_until: date | str | None = None,
    reward_points: int | None = None,
    emoji_icon: str | None = None,
    renewal_interval: int | None = None,
    renewal_unit: str | None = None,
    track_habit: bool | None = None,
) -> Operation[list[Chore]]:
    """``POST .../chores/create_multiple`` (captured): one chore per profile, flat body, nulls included."""
    if isinstance(recurrence_set, str):
        recurrence_set = [recurrence_set]
    body: dict[str, Any] = {
        "summary": summary,
        "description": description,
        "start": iso_date(start) if start is not None else None,
        "start_time": start_time,
        "category_ids": _ids(profile_ids),
        "routine": routine,
        "up_for_grabs": up_for_grabs,
        "recurrence_set": list(recurrence_set) if recurrence_set else None,
        "recurring_until": iso_date(recurring_until) if recurring_until is not None else None,
        "renewal_interval": renewal_interval,
        "renewal_unit": renewal_unit,
    }
    if reward_points is not None:
        body["reward_points"] = reward_points
    if emoji_icon is not None:
        body["emoji_icon"] = emoji_icon
    if track_habit is not None:
        body["track_habit"] = track_habit
    return Operation("POST", f"{_frame(frame_id)}/chores/create_multiple", json=body, parse=lambda p: parse_many(p, Chore))


def update_chore(frame_id: Any, chore_id: Any, attributes: Mapping[str, Any]) -> Operation[Chore]:
    """Flat PUT body (captured for ``summary`` and ``recurrence_set``). Skylight re-keys the chore."""
    body = dict(attributes)
    if isinstance(body.get("recurrence_set"), str):
        body["recurrence_set"] = [body["recurrence_set"]]
    if body.get("start") is not None:
        body["start"] = iso_date(body["start"])
    return Operation("PUT", f"{_frame(frame_id)}/chores/{chore_id}", json=body, parse=lambda p: parse_one(p, Chore))


def set_chore_status(
    frame_id: Any,
    series_id: Any,
    *,
    complete: bool,
    completed_on: date | str | None,
    instance_date: date | str | None,
    instance_time: str | None,
) -> Operation[ChoreCompletion]:
    """``PUT .../chores/{series}/completions`` (captured).

    ``instance_date`` is required for a chore that has a start date and must
    be omitted for an on-demand (renewal) chore.
    """
    body = _compact(
        status=CHORE_STATUS_COMPLETE if complete else CHORE_STATUS_PENDING,
        instance_date=iso_date(instance_date) if instance_date is not None else None,
        instance_time=instance_time,
        completed_on=iso_date(completed_on) if (complete and completed_on is not None) else None,
    )
    return Operation(
        "PUT", f"{_frame(frame_id)}/chores/{series_id}/completions", json=body, parse=ChoreCompletion.from_payload
    )


def delete_chore(frame_id: Any, chore_id: Any, apply_to: str | None = None) -> Operation[None]:
    """``apply_to`` is ``future`` or ``all`` for recurring chores (captured)."""
    return Operation("DELETE", f"{_frame(frame_id)}/chores/{chore_id}", params=_compact(apply_to=apply_to), parse=_none)


def list_task_box_items(frame_id: Any) -> Operation[list[TaskBoxItem]]:
    return Operation("GET", f"{_frame(frame_id)}/task_box/items", parse=lambda p: parse_many(p, TaskBoxItem))


def create_task_box_item(
    frame_id: Any, summary: str, *, emoji_icon: str | None = None, routine: bool = False, reward_points: int | None = None
) -> Operation[TaskBoxItem]:
    """JSON:API envelope (inferred)."""
    attrs = {"summary": summary, "emoji_icon": emoji_icon, "routine": routine, "reward_points": reward_points}
    return Operation(
        "POST", f"{_frame(frame_id)}/task_box/items", json=_jsonapi("task_box_item", attrs), parse=lambda p: parse_one(p, TaskBoxItem)
    )


def delete_task_box_item(frame_id: Any, item_id: Any) -> Operation[None]:
    return Operation("DELETE", f"{_frame(frame_id)}/task_box/items/{item_id}", parse=_none)


# ---------------------------------------------------------------------------
# Rewards
# ---------------------------------------------------------------------------


def list_rewards(
    frame_id: Any, redeemed_at_min: datetime | date | str | None = None, redeemed_at_max: datetime | date | str | None = None
) -> Operation[list[Reward]]:
    params = _compact(
        redeemed_at_min=iso_datetime(redeemed_at_min, None) if redeemed_at_min is not None else None,
        redeemed_at_max=iso_datetime(redeemed_at_max, None) if redeemed_at_max is not None else None,
    )
    return Operation("GET", f"{_frame(frame_id)}/rewards", params=params, parse=lambda p: parse_many(p, Reward))


def get_reward_points(frame_id: Any) -> Operation[list[RewardPoints]]:
    return Operation("GET", f"{_frame(frame_id)}/reward_points", parse=RewardPoints.from_payload)


def create_rewards(
    frame_id: Any,
    *,
    name: str,
    point_value: int,
    profile_ids: Iterable[Any],
    description: str | None = None,
    emoji_icon: str | None = None,
    respawn_on_redemption: bool = False,
) -> Operation[list[Reward]]:
    """Flat POST body (captured); answers 201 with one reward per profile."""
    body = {
        "name": name,
        "point_value": point_value,
        "description": description,
        "emoji_icon": emoji_icon,
        "respawn_on_redemption": respawn_on_redemption,
        "category_ids": _ids(profile_ids),
    }
    return Operation("POST", f"{_frame(frame_id)}/rewards", json=body, parse=lambda p: parse_many(p, Reward))


def update_reward(frame_id: Any, reward_id: Any, attributes: Mapping[str, Any]) -> Operation[Reward]:
    """Flat PATCH body (inferred). ``profile_ids`` maps to ``category_ids``."""
    body = dict(attributes)
    if "profile_ids" in body:
        body["category_ids"] = _ids(body.pop("profile_ids"))
    return Operation("PATCH", f"{_frame(frame_id)}/rewards/{reward_id}", json=body, parse=lambda p: parse_one(p, Reward))


def delete_reward(frame_id: Any, reward_id: Any) -> Operation[None]:
    return Operation("DELETE", f"{_frame(frame_id)}/rewards/{reward_id}", parse=_none)


def redeem_reward(frame_id: Any, reward_id: Any, profile_id: Any = None) -> Operation[Reward]:
    body = _compact(category_id=str(profile_id) if profile_id is not None else None)
    return Operation("POST", f"{_frame(frame_id)}/rewards/{reward_id}/redeem", json=body, parse=lambda p: parse_one(p, Reward))


def unredeem_reward(frame_id: Any, reward_id: Any) -> Operation[Reward]:
    return Operation("POST", f"{_frame(frame_id)}/rewards/{reward_id}/unredeem", json={}, parse=lambda p: parse_one(p, Reward))


# ---------------------------------------------------------------------------
# Meals
# ---------------------------------------------------------------------------

_SITTING_INCLUDE = "meal_category,meal_recipe"


def list_meal_categories(frame_id: Any) -> Operation[list[MealCategory]]:
    return Operation("GET", f"{_frame(frame_id)}/meals/categories", parse=lambda p: parse_many(p, MealCategory))


def list_recipes(frame_id: Any) -> Operation[list[Recipe]]:
    return Operation(
        "GET", f"{_frame(frame_id)}/meals/recipes", params={"include": "meal_category"}, parse=lambda p: parse_many(p, Recipe)
    )


def get_recipe(frame_id: Any, recipe_id: Any) -> Operation[Recipe]:
    return Operation(
        "GET",
        f"{_frame(frame_id)}/meals/recipes/{recipe_id}",
        params={"include": "meal_category"},
        parse=lambda p: parse_one(p, Recipe),
    )


def create_recipe(
    frame_id: Any, summary: str, *, description: str | None = None, meal_category_id: Any = None
) -> Operation[Recipe]:
    """Flat POST body (captured)."""
    body: dict[str, Any] = {"summary": summary, "description": description}
    if meal_category_id is not None:
        body["meal_category_id"] = str(meal_category_id)
    return Operation(
        "POST", f"{_frame(frame_id)}/meals/recipes", params={"include": "meal_category"}, json=body, parse=lambda p: parse_one(p, Recipe)
    )


def update_recipe(frame_id: Any, recipe_id: Any, attributes: Mapping[str, Any]) -> Operation[Recipe]:
    """Flat PATCH body (captured): ``summary``, ``description``, ``meal_category_id``."""
    return Operation(
        "PATCH",
        f"{_frame(frame_id)}/meals/recipes/{recipe_id}",
        params={"include": "meal_category"},
        json=dict(attributes),
        parse=lambda p: parse_one(p, Recipe),
    )


def delete_recipe(frame_id: Any, recipe_id: Any) -> Operation[None]:
    return Operation("DELETE", f"{_frame(frame_id)}/meals/recipes/{recipe_id}", parse=_none)


def add_recipe_to_grocery_list(frame_id: Any, recipe_id: Any) -> Operation[None]:
    return Operation("POST", f"{_frame(frame_id)}/meals/recipes/{recipe_id}/add_to_grocery_list", json={}, parse=_none)


def get_meal_plan(frame_id: Any, start: date | datetime | str, end: date | datetime | str) -> Operation[MealPlan]:
    """Sittings whose dates fall in ``[start, end]`` (``end`` inclusive when a date)."""
    date_max = end
    if isinstance(end, date) and not isinstance(end, datetime):
        date_max = end + timedelta(days=1)
    return Operation(
        "GET",
        f"{_frame(frame_id)}/meals/sittings",
        params={"date_min": iso_date(start), "date_max": iso_date(date_max), "include": _SITTING_INCLUDE},
        parse=MealPlan.from_payload,
    )


_WEEKDAY_CODES = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")


def weekly_rrule(day: date) -> str:
    """The rule the web app sends for "repeats weekly on this weekday"."""
    return f"FREQ=WEEKLY;INTERVAL=1;WKST=SU;BYDAY={_WEEKDAY_CODES[day.weekday()]}"


def create_meal_sitting(
    frame_id: Any,
    *,
    day: date | str,
    meal_category_id: Any,
    summary: str | None = None,
    recipe_id: Any = None,
    note: str | None = None,
    profile_ids: Iterable[Any] | None = None,
    rrule: str | None = None,
) -> Operation[MealSitting]:
    """Flat POST body (captured). With ``rrule`` the web app's full form is sent instead."""
    if rrule:
        body: dict[str, Any] = {
            "date": iso_date(day),
            "summary": summary or "",
            "description": "",
            "note": note or "",
            "meal_category_id": str(meal_category_id),
            "meal_recipe_id": str(recipe_id) if recipe_id is not None else None,
            "profile_ids": _ids(profile_ids),
            "rrule": rrule,
            "add_to_grocery_list": False,
            "saveToRecipeBox": False,
        }
        version: str | None = MEAL_EDIT_API_VERSION
    else:
        body = {"date": iso_date(day), "meal_category_id": str(meal_category_id)}
        if summary:
            body["summary"] = summary
        if recipe_id is not None:
            body["meal_recipe_id"] = str(recipe_id)
        if note:
            body["note"] = note
        if profile_ids:
            body["profile_ids"] = _ids(profile_ids)
        version = None
    return Operation(
        "POST",
        f"{_frame(frame_id)}/meals/sittings",
        json=body,
        api_version=version,
        parse=lambda p: parse_one(p, MealSitting),
    )


def _instance_params(day: date | str) -> dict[str, Any]:
    d = date.fromisoformat(iso_date(day))
    return {"date_min": d.isoformat(), "date_max": (d + timedelta(days=1)).isoformat(), "include": f"{_SITTING_INCLUDE},profiles"}


def update_meal_sitting(
    frame_id: Any,
    sitting: MealSitting,
    day: date | str,
    *,
    new_day: date | str | None = None,
    summary: str | None = None,
    note: str | None = None,
    meal_category_id: Any = None,
    recipe_id: Any = None,
) -> Operation[MealSitting]:
    """``PATCH .../meals/sittings/{id}/instances/{date}`` with the whole form (captured for one-off sittings)."""
    payload = {
        "date": iso_date(new_day) if new_day is not None else iso_date(day),
        "note": note if note is not None else (sitting.note or ""),
        "rrule": sitting.rrule,
        "summary": summary if summary is not None else (sitting.summary or ""),
        "description": sitting.description or "",
        "meal_category_id": str(meal_category_id) if meal_category_id is not None else sitting.meal_category_id,
        "meal_recipe_id": str(recipe_id) if recipe_id is not None else sitting.recipe_id,
        "profile_ids": list(sitting.profile_ids),
        "saveToRecipeBox": False,
    }
    return Operation(
        "PATCH",
        f"{_frame(frame_id)}/meals/sittings/{sitting.id}/instances/{iso_date(day)}",
        params=_instance_params(day),
        json=payload,
        api_version=MEAL_EDIT_API_VERSION,
        parse=lambda p: parse_one(p, MealSitting),
    )


def delete_meal_sitting(frame_id: Any, sitting_id: Any, day: date | str, apply_to: str | None = None) -> Operation[Any]:
    """``DELETE .../meals/sittings/{id}/instances/{date}`` (captured).

    For a repeating sitting pass ``apply_to``: ``one``, ``future`` or ``all``.
    """
    params = _instance_params(day)
    if apply_to:
        params["apply_to"] = apply_to
    return Operation(
        "DELETE",
        f"{_frame(frame_id)}/meals/sittings/{sitting_id}/instances/{iso_date(day)}",
        params=params,
        api_version=MEAL_EDIT_API_VERSION,
    )
