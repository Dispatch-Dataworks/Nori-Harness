"""The Skylight clients.

:class:`SkylightClient` is synchronous; :class:`AsyncSkylightClient` is its
``asyncio`` twin (for Home Assistant and async agent runtimes). Both share
:class:`_Core`, which handles credentials, headers, error mapping and
turning JSON into models. The endpoint catalogue lives in :mod:`._ops`.

Most methods take ``frame_id=None``: the client's default frame is used,
which is the one you passed in, ``$SKYLIGHT_FRAME_ID``, or the first frame on
the account (looked up once).
"""

from __future__ import annotations

import json as _json
import logging
import os
from collections.abc import Iterable, Mapping
from datetime import date, datetime, timedelta
from typing import Any, TypeVar
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from . import _ops as ops
from ._http import TRANSPORT_ERRORS, default_client, require_httpx
from . import auth as _auth
from .const import (
    API_USER_AGENT,
    BASE_URL,
    DEFAULT_API_VERSION,
    DEFAULT_TIMEOUT,
    ENV_ACCESS_TOKEN,
    ENV_EMAIL,
    ENV_FRAME_ID,
    ENV_PASSWORD,
    ENV_TIMEZONE,
    LIST_KIND_SHOPPING,
    LIST_KIND_TODO,
)
from .errors import (
    SkylightAPIError,
    SkylightAuthError,
    SkylightConfigError,
    SkylightError,
    SkylightNotFoundError,
    SkylightRateLimitError,
    SkylightValidationError,
)
from .models import (
    CalendarEvent,
    Chore,
    ChoreCompletion,
    ChoreOccurrence,
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
)
from .tokens import Credentials, FileTokenStore, TokenStore

if TYPE_CHECKING:
    import httpx

_LOGGER = logging.getLogger(__name__)

T = TypeVar("T")
FrameId = str | int | None
DateLike = date | datetime | str


class _Core:
    """Everything that does not touch the network."""

    def __init__(
        self,
        *,
        email: str | None = None,
        password: str | None = None,
        access_token: str | None = None,
        credentials: Credentials | None = None,
        token_store: TokenStore | None = None,
        frame_id: str | int | None = None,
        timezone: str | None = None,
        base_url: str = BASE_URL,
        api_version: str = DEFAULT_API_VERSION,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._email = email
        self._password = password
        self._token_store = token_store
        self._base_url = base_url.rstrip("/")
        self._api_version = api_version
        self._timeout = timeout
        self._frame_id: str | None = str(frame_id) if frame_id is not None else None
        self._timezone = timezone
        self._static_token = False
        if credentials is not None:
            self._credentials: Credentials | None = credentials
        elif access_token:
            self._credentials = Credentials(access_token=access_token)
            self._static_token = True
        else:
            self._credentials = None
        if self._credentials is None and self._token_store is None and not (email and password):
            raise SkylightConfigError(
                "Give the client an access_token, credentials, a token_store with a saved login, "
                "or an email and password."
            )

    # -- public state -----------------------------------------------------

    @property
    def credentials(self) -> Credentials | None:
        """The session currently in use (``None`` until the first request)."""
        return self._credentials

    @property
    def frame_id(self) -> str | None:
        """The default frame, once known."""
        return self._frame_id

    @frame_id.setter
    def frame_id(self, value: str | int | None) -> None:
        self._frame_id = str(value) if value is not None else None

    @property
    def timezone(self) -> str | None:
        return self._timezone

    @timezone.setter
    def timezone(self, value: str | None) -> None:
        self._timezone = value

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _env_kwargs(environ: Mapping[str, str]) -> dict[str, Any]:
        return {
            "email": environ.get(ENV_EMAIL),
            "password": environ.get(ENV_PASSWORD),
            "access_token": environ.get(ENV_ACCESS_TOKEN),
            "frame_id": environ.get(ENV_FRAME_ID),
            "timezone": environ.get(ENV_TIMEZONE),
        }

    def _url(self, op: ops.Operation[Any]) -> str:
        return f"{self._base_url}/api{op.path}"

    def _headers(self, token: str, op: ops.Operation[Any]) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": API_USER_AGENT,
            "Skylight-Api-Version": op.api_version or self._api_version,
        }
        if op.has_body:
            headers["Content-Type"] = "application/json"
        return headers

    def _persist(self, credentials: Credentials) -> None:
        self._credentials = credentials
        if self._token_store is not None:
            try:
                self._token_store.save(credentials)
            except OSError as exc:  # pragma: no cover - depends on filesystem
                _LOGGER.warning("Could not save Skylight credentials: %s", exc)

    def _can_login(self) -> bool:
        return bool(self._email and self._password)

    def _raise_for_status(self, resp: httpx.Response, op: ops.Operation[Any]) -> None:
        status = resp.status_code
        if status < 400:
            return
        text = resp.text[:500]
        detail = f"{op.method} {op.path} -> HTTP {status}"
        if text.strip():
            detail += f": {text[:200].strip()}"
        kwargs: dict[str, Any] = {"status": status, "method": op.method, "path": op.path, "body": text}
        if status == 404:
            raise SkylightNotFoundError(detail, **kwargs)
        if status == 422:
            sent = _json.dumps(op.json) if op.has_body else "<no body>"
            raise SkylightValidationError(f"{detail} (sent {sent})", **kwargs)
        if status == 429:
            retry = resp.headers.get("Retry-After")
            try:
                retry_after: float | None = float(retry) if retry else None
            except ValueError:
                retry_after = None
            raise SkylightRateLimitError(detail, retry_after=retry_after, **kwargs)
        raise SkylightAPIError(detail, **kwargs)

    @staticmethod
    def _decode(resp: httpx.Response) -> Any:
        if resp.status_code == 204 or not resp.content.strip():
            return {}
        try:
            return resp.json()
        except ValueError as exc:
            raise SkylightError(f"Skylight returned a non-JSON response ({resp.status_code})") from exc

    def _fallback_timezone(self) -> str:
        return "UTC"

    def _today(self) -> date:
        tz = self._timezone
        if tz:
            try:
                return datetime.now(ZoneInfo(tz)).date()
            except Exception:  # pragma: no cover - bad tz name
                pass
        return date.today()

    @staticmethod
    def _completion_args(
        chore_id: Any, instance_date: date | str | None, instance_time: str | None
    ) -> tuple[str, date | str | None, str | None]:
        """Split an occurrence id into the series id plus instance date/time if needed."""
        chore_id = str(chore_id)
        occ = ChoreOccurrence.parse(chore_id)
        if occ is None:
            return chore_id, instance_date, instance_time
        return (
            occ.series_id,
            instance_date if instance_date is not None else occ.instance_date,
            instance_time if instance_time is not None else occ.instance_time,
        )


# ---------------------------------------------------------------------------
# Sync client
# ---------------------------------------------------------------------------


class SkylightClient(_Core):
    """Synchronous client.

    >>> client = SkylightClient(email="me@example.com", password="...", token_store=FileTokenStore())
    >>> for event in client.list_events(date.today(), date.today() + timedelta(days=7)):
    ...     print(event.summary, event.starts_at)

    Credentials are resolved in this order: ``credentials``/``access_token``
    given directly, a saved login in ``token_store``, then a fresh login with
    ``email``/``password`` (which is saved to ``token_store`` if there is one).
    Expired tokens are refreshed automatically and a 401 is retried once
    after refreshing or logging in again.
    """

    def __init__(self, *, http: httpx.Client | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._owns_http = http is None
        self._http = http or default_client(self._timeout)

    @classmethod
    def from_env(cls, *, token_store: TokenStore | None = None, **overrides: Any) -> "SkylightClient":
        """Build a client from ``SKYLIGHT_*`` environment variables.

        Uses a :class:`FileTokenStore` at the default path unless ``token_store``
        is given, so a CLI ``skylight login`` is enough for other programs.
        """
        kwargs = cls._env_kwargs(os.environ)
        kwargs.update(overrides)
        return cls(token_store=token_store if token_store is not None else FileTokenStore(), **kwargs)

    # -- lifecycle --------------------------------------------------------

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> "SkylightClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- auth -------------------------------------------------------------

    def _token(self) -> str:
        creds = self._credentials
        if creds is None and self._token_store is not None:
            creds = self._token_store.load()
            self._credentials = creds
        if creds is None:
            creds = self._login()
        elif creds.is_expired() and not self._static_token:
            creds = self._reauthenticate()
        return creds.access_token

    def _login(self) -> Credentials:
        if not self._can_login():
            raise SkylightAuthError("Not logged in to Skylight and no email/password to log in with")
        assert self._email and self._password
        fingerprint = self._credentials.device_fingerprint if self._credentials else None
        creds = _auth.login(
            self._email, self._password, base_url=self._base_url, http=self._http, device_fingerprint=fingerprint
        )
        self._persist(creds)
        return creds

    def _reauthenticate(self) -> Credentials:
        if self._static_token:
            raise SkylightAuthError("Skylight rejected the access token (401) and no refresh token or password is available")
        creds = self._credentials
        if creds and creds.refresh_token:
            try:
                fresh = _auth.refresh(creds, base_url=self._base_url, http=self._http)
            except SkylightAuthError as exc:
                _LOGGER.debug("Skylight token refresh failed (%s); trying a fresh login", exc)
            else:
                self._persist(fresh)
                return fresh
        if self._can_login():
            return self._login()
        if self._token_store is not None:
            self._token_store.clear()
        raise SkylightAuthError("Skylight session expired; log in again")

    def login(self) -> Credentials:
        """Force a fresh email/password login (and save it)."""
        return self._login()

    def ensure_authenticated(self) -> Credentials:
        """Make sure we hold usable credentials, logging in if necessary."""
        self._token()
        assert self._credentials is not None
        return self._credentials

    def logout(self) -> None:
        """Forget the session locally (and clear the token store)."""
        self._credentials = None
        if self._token_store is not None:
            self._token_store.clear()

    # -- transport --------------------------------------------------------

    def _send(self, op: ops.Operation[Any], *, retry: bool = True) -> httpx.Response:
        token = self._token()
        try:
            resp = self._http.request(
                op.method,
                self._url(op),
                params=op.params or None,
                json=op.json if op.has_body else None,
                headers=self._headers(token, op),
            )
        except TRANSPORT_ERRORS as exc:
            raise SkylightError(f"Network error talking to Skylight: {exc}") from exc
        if resp.status_code == 401 and retry:
            _LOGGER.debug("Skylight 401 on %s %s; re-authenticating", op.method, op.path)
            self._reauthenticate()
            return self._send(op, retry=False)
        if resp.status_code == 401:
            raise SkylightAuthError("Skylight rejected our credentials (401)")
        self._raise_for_status(resp, op)
        return resp

    def run(self, op: ops.Operation[T]) -> T:
        """Execute an :class:`~skylight_calendar._ops.Operation` and parse its result."""
        return op.parse(self._decode(self._send(op)))

    def request(
        self, method: str, path: str, *, params: Mapping[str, Any] | None = None, json: Any = None, api_version: str | None = None
    ) -> Any:
        """Raw escape hatch: ``client.request("GET", "/frames/123/nudges")`` returns decoded JSON."""
        return self.run(ops.Operation(method.upper(), path, params=dict(params or {}), json=json, api_version=api_version))

    # -- frame / timezone defaults ---------------------------------------

    def _frame(self, frame_id: FrameId) -> str:
        if frame_id is not None:
            return str(frame_id)
        if self._frame_id is None:
            frames = self.list_frames()
            if not frames:
                raise SkylightConfigError("This Skylight account has no frames")
            self._adopt_frame(frames[0])
        assert self._frame_id is not None
        return self._frame_id

    def _adopt_frame(self, frame: Frame) -> None:
        self._frame_id = frame.id
        if not self._timezone and frame.timezone:
            self._timezone = frame.timezone

    def _tz(self, frame_id: FrameId) -> str:
        if self._timezone:
            return self._timezone
        frame = self.get_frame(frame_id)
        if frame.timezone:
            self._timezone = frame.timezone
            return frame.timezone
        _LOGGER.warning("Skylight frame has no timezone; using UTC. Pass timezone= to the client.")
        self._timezone = self._fallback_timezone()
        return self._timezone

    # -- account / frames -------------------------------------------------

    def get_user(self) -> User:
        return self.run(ops.get_user())

    def list_frames(self) -> list[Frame]:
        return self.run(ops.list_frames())

    def get_frame(self, frame_id: FrameId = None) -> Frame:
        return self.run(ops.get_frame(self._frame(frame_id)))

    def list_devices(self, frame_id: FrameId = None) -> list[Device]:
        return self.run(ops.list_devices(self._frame(frame_id)))

    def update_device(self, device_id: Any, *, frame_id: FrameId = None, **attributes: Any) -> Device:
        """PATCH device settings, e.g. ``brightness=120`` or ``sleep_mode_on=True``."""
        return self.run(ops.update_device(self._frame(frame_id), device_id, attributes))

    # -- profiles ---------------------------------------------------------

    def list_profiles(self, frame_id: FrameId = None) -> list[Profile]:
        """Family members (Skylight "categories")."""
        return self.run(ops.list_profiles(self._frame(frame_id)))

    def get_profile(self, profile_id: Any, *, frame_id: FrameId = None) -> Profile:
        return self.run(ops.get_profile(self._frame(frame_id), profile_id))

    def find_profile(self, name: str, *, frame_id: FrameId = None) -> Profile:
        """Exact, case-insensitive match on the profile label."""
        return _find_profile(self.list_profiles(frame_id), name)

    def create_profile(self, label: str, *, color: str | None = None, frame_id: FrameId = None, **extra: Any) -> Profile:
        return self.run(ops.create_profile(self._frame(frame_id), label, color, extra))

    def update_profile(self, profile_id: Any, *, frame_id: FrameId = None, **attributes: Any) -> Profile:
        return self.run(ops.update_profile(self._frame(frame_id), profile_id, attributes))

    def delete_profile(self, profile_id: Any, *, reassign_to_id: Any = None, frame_id: FrameId = None) -> None:
        self.run(ops.delete_profile(self._frame(frame_id), profile_id, reassign_to_id))

    # -- calendar ---------------------------------------------------------

    def list_events(
        self,
        start: DateLike | None = None,
        end: DateLike | None = None,
        *,
        days: int = 7,
        timezone: str | None = None,
        frame_id: FrameId = None,
    ) -> list[CalendarEvent]:
        """Events from ``start`` (default today) to ``end`` (default ``start + days``), end inclusive."""
        fid = self._frame(frame_id)
        start, end = _window(start, end, days, self._today())
        return self.run(ops.list_events(fid, start, end, timezone or self._tz(fid)))

    def create_event(
        self,
        summary: str,
        starts_at: DateLike,
        ends_at: DateLike | None = None,
        *,
        all_day: bool = False,
        description: str | None = None,
        location: str | None = None,
        profile_ids: Iterable[Any] | None = None,
        calendar_account_id: Any = None,
        calendar_id: str | None = None,
        rrule: str | list[str] | None = None,
        countdown_enabled: bool = False,
        invited_emails: Iterable[str] | None = None,
        timezone: str | None = None,
        frame_id: FrameId = None,
    ) -> CalendarEvent:
        """Create an event on the frame's own calendar (or a synced one via ``calendar_account_id``).

        Naive datetimes are taken as wall-clock time in the frame's timezone.
        ``ends_at`` defaults to one hour after ``starts_at`` (or the same day for all-day).
        """
        fid = self._frame(frame_id)
        tz = timezone or self._tz(fid)
        starts_at, ends_at = _default_end(starts_at, ends_at, all_day)
        return self.run(
            ops.create_event(
                fid,
                summary=summary,
                starts_at=starts_at,
                ends_at=ends_at,
                timezone=tz,
                all_day=all_day,
                description=description,
                location=location,
                profile_ids=profile_ids,
                calendar_account_id=calendar_account_id,
                calendar_id=calendar_id,
                rrule=rrule,
                countdown_enabled=countdown_enabled,
                invited_emails=invited_emails,
            )
        )

    def update_event(self, event_id: Any, *, frame_id: FrameId = None, timezone: str | None = None, **changes: Any) -> CalendarEvent:
        """Change ``summary``, ``starts_at``, ``ends_at``, ``all_day``, ``description``, ``location``, ``profile_ids``, ``rrule``..."""
        fid = self._frame(frame_id)
        return self.run(ops.update_event(fid, event_id, changes, timezone or self._tz(fid)))

    def delete_event(self, event_id: Any, *, frame_id: FrameId = None) -> None:
        self.run(ops.delete_event(self._frame(frame_id), event_id))

    def list_source_calendars(self, frame_id: FrameId = None) -> list[SourceCalendar]:
        return self.run(ops.list_source_calendars(self._frame(frame_id)))

    # -- lists ------------------------------------------------------------

    def list_lists(self, frame_id: FrameId = None) -> list[SkylightList]:
        """All shopping and to-do lists, items included."""
        return self.run(ops.list_lists(self._frame(frame_id)))

    def get_list(self, list_id: Any, *, frame_id: FrameId = None) -> SkylightList:
        return self.run(ops.get_list(self._frame(frame_id), list_id))

    def find_list(self, name: str | None = None, *, kind: str | None = None, frame_id: FrameId = None) -> SkylightList:
        """By name (case-insensitive substring) and/or kind (``shopping``/``to_do``); the default grocery list wins ties."""
        return _find_list(self.list_lists(frame_id), name, kind)

    def grocery_list(self, frame_id: FrameId = None) -> SkylightList:
        return self.find_list(kind=LIST_KIND_SHOPPING, frame_id=frame_id)

    def todo_list(self, frame_id: FrameId = None) -> SkylightList:
        return self.find_list(kind=LIST_KIND_TODO, frame_id=frame_id)

    def create_list(self, label: str, *, kind: str = LIST_KIND_TODO, color: str | None = None, frame_id: FrameId = None) -> SkylightList:
        return self.run(ops.create_list(self._frame(frame_id), label, kind, color))

    def update_list(self, list_id: Any, *, frame_id: FrameId = None, **attributes: Any) -> SkylightList:
        return self.run(ops.update_list(self._frame(frame_id), list_id, attributes))

    def delete_list(self, list_id: Any, *, frame_id: FrameId = None) -> None:
        self.run(ops.delete_list(self._frame(frame_id), list_id))

    def add_list_item(self, list_id: Any, label: str, *, section: str | None = None, frame_id: FrameId = None) -> ListItem:
        return self.run(ops.add_list_item(self._frame(frame_id), list_id, label, section))

    def update_list_item(self, list_id: Any, item_id: Any, *, frame_id: FrameId = None, **attributes: Any) -> ListItem:
        return self.run(ops.update_list_item(self._frame(frame_id), list_id, item_id, attributes))

    def complete_list_item(self, list_id: Any, item_id: Any, *, completed: bool = True, frame_id: FrameId = None) -> ListItem:
        return self.run(ops.set_list_item_completed(self._frame(frame_id), list_id, item_id, completed))

    def delete_list_item(self, list_id: Any, item_id: Any, *, frame_id: FrameId = None) -> None:
        self.run(ops.delete_list_item(self._frame(frame_id), list_id, item_id))

    def delete_list_items(self, list_id: Any, item_ids: Iterable[Any], *, frame_id: FrameId = None) -> None:
        self.run(ops.delete_list_items(self._frame(frame_id), list_id, item_ids))

    # -- chores / tasks ---------------------------------------------------

    def list_chores(
        self,
        start: DateLike | None = None,
        end: DateLike | None = None,
        *,
        days: int = 0,
        include_late: bool = True,
        include_up_for_grabs: bool = True,
        linked_to_profile: bool = False,
        frame_id: FrameId = None,
    ) -> list[Chore]:
        """Chores due between ``start`` and ``end`` (both default to today)."""
        start, end = _window(start, end, days, self._today())
        return self.run(
            ops.list_chores(
                self._frame(frame_id),
                start,
                end,
                include_late=include_late,
                include_up_for_grabs=include_up_for_grabs,
                linked_to_profile=linked_to_profile,
            )
        )

    def create_chore(
        self,
        summary: str,
        *,
        profile_ids: Iterable[Any] = (),
        start: DateLike | None = None,
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
        frame_id: FrameId = None,
    ) -> list[Chore]:
        """Create a chore for each profile (Skylight makes one record per assignee).

        ``start`` defaults to today unless ``renewal_interval`` is set (an on-demand chore).
        Set ``up_for_grabs=True`` with no profiles for a shared, unassigned chore.
        """
        profile_ids = list(profile_ids)
        if not profile_ids and not up_for_grabs:
            raise SkylightConfigError("create_chore needs profile_ids, or up_for_grabs=True")
        if start is None and renewal_interval is None:
            start = self._today()
        return self.run(
            ops.create_chores(
                self._frame(frame_id),
                summary=summary,
                profile_ids=profile_ids,
                start=start,
                description=description,
                start_time=start_time,
                routine=routine,
                up_for_grabs=up_for_grabs,
                recurrence_set=recurrence_set,
                recurring_until=recurring_until,
                reward_points=reward_points,
                emoji_icon=emoji_icon,
                renewal_interval=renewal_interval,
                renewal_unit=renewal_unit,
                track_habit=track_habit,
            )
        )

    def update_chore(self, chore_id: Any, *, frame_id: FrameId = None, **attributes: Any) -> Chore:
        """Edit a chore's fields. Skylight returns it under a new id; use that from then on."""
        return self.run(ops.update_chore(self._frame(frame_id), chore_id, attributes))

    def complete_chore(
        self,
        chore_id: Any,
        *,
        completed_on: date | str | None = None,
        instance_date: date | str | None = None,
        instance_time: str | None = None,
        frame_id: FrameId = None,
    ) -> ChoreCompletion:
        """Tick a chore. Pass the id from :meth:`list_chores`; occurrence ids are split automatically.

        For a one-off chore that has a start date, ``instance_date`` defaults to
        today if not derivable from the id. ``completed_on`` defaults to today.
        """
        series, inst_date, inst_time = self._completion_args(chore_id, instance_date, instance_time)
        return self.run(
            ops.set_chore_status(
                self._frame(frame_id),
                series,
                complete=True,
                completed_on=completed_on or self._today(),
                instance_date=inst_date,
                instance_time=inst_time,
            )
        )

    def uncomplete_chore(
        self,
        chore_id: Any,
        *,
        instance_date: date | str | None = None,
        instance_time: str | None = None,
        frame_id: FrameId = None,
    ) -> ChoreCompletion:
        """Un-tick a chore (same endpoint as :meth:`complete_chore`, status ``pending``)."""
        series, inst_date, inst_time = self._completion_args(chore_id, instance_date, instance_time)
        return self.run(
            ops.set_chore_status(
                self._frame(frame_id), series, complete=False, completed_on=None, instance_date=inst_date, instance_time=inst_time
            )
        )

    def delete_chore(self, chore_id: Any, *, apply_to: str | None = None, frame_id: FrameId = None) -> None:
        """Delete a chore; for a recurring one pass ``apply_to="future"`` or ``"all"``."""
        self.run(ops.delete_chore(self._frame(frame_id), chore_id, apply_to))

    def list_task_box_items(self, frame_id: FrameId = None) -> list[TaskBoxItem]:
        return self.run(ops.list_task_box_items(self._frame(frame_id)))

    def create_task_box_item(
        self, summary: str, *, emoji_icon: str | None = None, routine: bool = False, reward_points: int | None = None, frame_id: FrameId = None
    ) -> TaskBoxItem:
        return self.run(
            ops.create_task_box_item(self._frame(frame_id), summary, emoji_icon=emoji_icon, routine=routine, reward_points=reward_points)
        )

    def delete_task_box_item(self, item_id: Any, *, frame_id: FrameId = None) -> None:
        self.run(ops.delete_task_box_item(self._frame(frame_id), item_id))

    # -- rewards ----------------------------------------------------------

    def list_rewards(
        self, *, redeemed_since: DateLike | None = None, redeemed_until: DateLike | None = None, frame_id: FrameId = None
    ) -> list[Reward]:
        """Rewards (one record per family member). The redeemed window also returns redeemed ones."""
        return self.run(ops.list_rewards(self._frame(frame_id), redeemed_since, redeemed_until))

    def get_reward_points(self, frame_id: FrameId = None) -> list[RewardPoints]:
        return self.run(ops.get_reward_points(self._frame(frame_id)))

    def create_reward(
        self,
        name: str,
        point_value: int,
        *,
        profile_ids: Iterable[Any],
        description: str | None = None,
        emoji_icon: str | None = None,
        respawn_on_redemption: bool = False,
        frame_id: FrameId = None,
    ) -> list[Reward]:
        return self.run(
            ops.create_rewards(
                self._frame(frame_id),
                name=name,
                point_value=point_value,
                profile_ids=profile_ids,
                description=description,
                emoji_icon=emoji_icon,
                respawn_on_redemption=respawn_on_redemption,
            )
        )

    def update_reward(self, reward_id: Any, *, frame_id: FrameId = None, **attributes: Any) -> Reward:
        return self.run(ops.update_reward(self._frame(frame_id), reward_id, attributes))

    def delete_reward(self, reward_id: Any, *, frame_id: FrameId = None) -> None:
        self.run(ops.delete_reward(self._frame(frame_id), reward_id))

    def redeem_reward(self, reward_id: Any, *, profile_id: Any = None, frame_id: FrameId = None) -> Reward:
        return self.run(ops.redeem_reward(self._frame(frame_id), reward_id, profile_id))

    def unredeem_reward(self, reward_id: Any, *, frame_id: FrameId = None) -> Reward:
        return self.run(ops.unredeem_reward(self._frame(frame_id), reward_id))

    # -- meals ------------------------------------------------------------

    def list_meal_categories(self, frame_id: FrameId = None) -> list[MealCategory]:
        return self.run(ops.list_meal_categories(self._frame(frame_id)))

    def list_recipes(self, frame_id: FrameId = None) -> list[Recipe]:
        return self.run(ops.list_recipes(self._frame(frame_id)))

    def get_recipe(self, recipe_id: Any, *, frame_id: FrameId = None) -> Recipe:
        return self.run(ops.get_recipe(self._frame(frame_id), recipe_id))

    def create_recipe(
        self, summary: str, *, description: str | None = None, meal_category_id: Any = None, frame_id: FrameId = None
    ) -> Recipe:
        return self.run(ops.create_recipe(self._frame(frame_id), summary, description=description, meal_category_id=meal_category_id))

    def update_recipe(self, recipe_id: Any, *, frame_id: FrameId = None, **attributes: Any) -> Recipe:
        return self.run(ops.update_recipe(self._frame(frame_id), recipe_id, attributes))

    def delete_recipe(self, recipe_id: Any, *, frame_id: FrameId = None) -> None:
        self.run(ops.delete_recipe(self._frame(frame_id), recipe_id))

    def add_recipe_to_grocery_list(self, recipe_id: Any, *, frame_id: FrameId = None) -> None:
        self.run(ops.add_recipe_to_grocery_list(self._frame(frame_id), recipe_id))

    def get_meal_plan(
        self, start: DateLike | None = None, end: DateLike | None = None, *, days: int = 7, frame_id: FrameId = None
    ) -> MealPlan:
        """Planned meals from ``start`` (default today) to ``end`` (default ``start + days``)."""
        start, end = _window(start, end, days, self._today())
        return self.run(ops.get_meal_plan(self._frame(frame_id), start, end))

    def create_meal_sitting(
        self,
        day: date | str,
        meal_category_id: Any,
        *,
        summary: str | None = None,
        recipe_id: Any = None,
        note: str | None = None,
        profile_ids: Iterable[Any] | None = None,
        repeat_weekly: bool = False,
        frame_id: FrameId = None,
    ) -> MealSitting:
        """Plan a meal in a slot on a date. Give ``summary`` (free text) and/or ``recipe_id``."""
        rrule = ops.weekly_rrule(date.fromisoformat(ops.iso_date(day))) if repeat_weekly else None
        return self.run(
            ops.create_meal_sitting(
                self._frame(frame_id),
                day=day,
                meal_category_id=meal_category_id,
                summary=summary,
                recipe_id=recipe_id,
                note=note,
                profile_ids=profile_ids,
                rrule=rrule,
            )
        )

    def update_meal_sitting(
        self,
        sitting: MealSitting,
        day: date | str | None = None,
        *,
        new_day: date | str | None = None,
        summary: str | None = None,
        note: str | None = None,
        meal_category_id: Any = None,
        recipe_id: Any = None,
        frame_id: FrameId = None,
    ) -> MealSitting:
        """Edit one occurrence (``day`` defaults to the sitting's first date)."""
        day = day if day is not None else sitting.date
        if day is None:
            raise SkylightConfigError("update_meal_sitting needs a day")
        return self.run(
            ops.update_meal_sitting(
                self._frame(frame_id),
                sitting,
                day,
                new_day=new_day,
                summary=summary,
                note=note,
                meal_category_id=meal_category_id,
                recipe_id=recipe_id,
            )
        )

    def delete_meal_sitting(self, sitting_id: Any, day: date | str, *, apply_to: str | None = None, frame_id: FrameId = None) -> None:
        """Remove a planned meal on ``day``; for a repeating one pass ``apply_to`` (``one``/``future``/``all``)."""
        self.run(ops.delete_meal_sitting(self._frame(frame_id), sitting_id, day, apply_to))


# ---------------------------------------------------------------------------
# Async client
# ---------------------------------------------------------------------------


class AsyncSkylightClient(_Core):
    """``asyncio`` twin of :class:`SkylightClient`; every method is awaitable.

    Pass ``http=`` to share an ``httpx.AsyncClient`` (e.g. Home Assistant's
    ``get_async_client``); the client is never closed when it is not ours.
    """

    def __init__(self, *, http: httpx.AsyncClient | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._owns_http = http is None
        self._http = http or require_httpx("AsyncSkylightClient").AsyncClient(timeout=self._timeout)

    @classmethod
    def from_env(cls, *, token_store: TokenStore | None = None, **overrides: Any) -> "AsyncSkylightClient":
        kwargs = cls._env_kwargs(os.environ)
        kwargs.update(overrides)
        return cls(token_store=token_store if token_store is not None else FileTokenStore(), **kwargs)

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def __aenter__(self) -> "AsyncSkylightClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # -- auth -------------------------------------------------------------

    async def _token(self) -> str:
        creds = self._credentials
        if creds is None and self._token_store is not None:
            creds = self._token_store.load()
            self._credentials = creds
        if creds is None:
            creds = await self._login()
        elif creds.is_expired() and not self._static_token:
            creds = await self._reauthenticate()
        return creds.access_token

    async def _login(self) -> Credentials:
        if not self._can_login():
            raise SkylightAuthError("Not logged in to Skylight and no email/password to log in with")
        assert self._email and self._password
        fingerprint = self._credentials.device_fingerprint if self._credentials else None
        creds = await _auth.async_login(
            self._email, self._password, base_url=self._base_url, http=self._http, device_fingerprint=fingerprint
        )
        self._persist(creds)
        return creds

    async def _reauthenticate(self) -> Credentials:
        if self._static_token:
            raise SkylightAuthError("Skylight rejected the access token (401) and no refresh token or password is available")
        creds = self._credentials
        if creds and creds.refresh_token:
            try:
                fresh = await _auth.async_refresh(creds, base_url=self._base_url, http=self._http)
            except SkylightAuthError as exc:
                _LOGGER.debug("Skylight token refresh failed (%s); trying a fresh login", exc)
            else:
                self._persist(fresh)
                return fresh
        if self._can_login():
            return await self._login()
        if self._token_store is not None:
            self._token_store.clear()
        raise SkylightAuthError("Skylight session expired; log in again")

    async def login(self) -> Credentials:
        return await self._login()

    async def ensure_authenticated(self) -> Credentials:
        await self._token()
        assert self._credentials is not None
        return self._credentials

    def logout(self) -> None:
        self._credentials = None
        if self._token_store is not None:
            self._token_store.clear()

    # -- transport --------------------------------------------------------

    async def _send(self, op: ops.Operation[Any], *, retry: bool = True) -> httpx.Response:
        token = await self._token()
        try:
            resp = await self._http.request(
                op.method,
                self._url(op),
                params=op.params or None,
                json=op.json if op.has_body else None,
                headers=self._headers(token, op),
            )
        except TRANSPORT_ERRORS as exc:
            raise SkylightError(f"Network error talking to Skylight: {exc}") from exc
        if resp.status_code == 401 and retry:
            _LOGGER.debug("Skylight 401 on %s %s; re-authenticating", op.method, op.path)
            await self._reauthenticate()
            return await self._send(op, retry=False)
        if resp.status_code == 401:
            raise SkylightAuthError("Skylight rejected our credentials (401)")
        self._raise_for_status(resp, op)
        return resp

    async def run(self, op: ops.Operation[T]) -> T:
        return op.parse(self._decode(await self._send(op)))

    async def request(
        self, method: str, path: str, *, params: Mapping[str, Any] | None = None, json: Any = None, api_version: str | None = None
    ) -> Any:
        return await self.run(ops.Operation(method.upper(), path, params=dict(params or {}), json=json, api_version=api_version))

    # -- frame / timezone defaults ---------------------------------------

    async def _frame(self, frame_id: FrameId) -> str:
        if frame_id is not None:
            return str(frame_id)
        if self._frame_id is None:
            frames = await self.list_frames()
            if not frames:
                raise SkylightConfigError("This Skylight account has no frames")
            self._adopt_frame(frames[0])
        assert self._frame_id is not None
        return self._frame_id

    def _adopt_frame(self, frame: Frame) -> None:
        self._frame_id = frame.id
        if not self._timezone and frame.timezone:
            self._timezone = frame.timezone

    async def _tz(self, frame_id: FrameId) -> str:
        if self._timezone:
            return self._timezone
        frame = await self.get_frame(frame_id)
        if frame.timezone:
            self._timezone = frame.timezone
            return frame.timezone
        _LOGGER.warning("Skylight frame has no timezone; using UTC. Pass timezone= to the client.")
        self._timezone = self._fallback_timezone()
        return self._timezone

    # -- account / frames -------------------------------------------------

    async def get_user(self) -> User:
        return await self.run(ops.get_user())

    async def list_frames(self) -> list[Frame]:
        return await self.run(ops.list_frames())

    async def get_frame(self, frame_id: FrameId = None) -> Frame:
        return await self.run(ops.get_frame(await self._frame(frame_id)))

    async def list_devices(self, frame_id: FrameId = None) -> list[Device]:
        return await self.run(ops.list_devices(await self._frame(frame_id)))

    async def update_device(self, device_id: Any, *, frame_id: FrameId = None, **attributes: Any) -> Device:
        return await self.run(ops.update_device(await self._frame(frame_id), device_id, attributes))

    # -- profiles ---------------------------------------------------------

    async def list_profiles(self, frame_id: FrameId = None) -> list[Profile]:
        return await self.run(ops.list_profiles(await self._frame(frame_id)))

    async def get_profile(self, profile_id: Any, *, frame_id: FrameId = None) -> Profile:
        return await self.run(ops.get_profile(await self._frame(frame_id), profile_id))

    async def find_profile(self, name: str, *, frame_id: FrameId = None) -> Profile:
        return _find_profile(await self.list_profiles(frame_id), name)

    async def create_profile(self, label: str, *, color: str | None = None, frame_id: FrameId = None, **extra: Any) -> Profile:
        return await self.run(ops.create_profile(await self._frame(frame_id), label, color, extra))

    async def update_profile(self, profile_id: Any, *, frame_id: FrameId = None, **attributes: Any) -> Profile:
        return await self.run(ops.update_profile(await self._frame(frame_id), profile_id, attributes))

    async def delete_profile(self, profile_id: Any, *, reassign_to_id: Any = None, frame_id: FrameId = None) -> None:
        await self.run(ops.delete_profile(await self._frame(frame_id), profile_id, reassign_to_id))

    # -- calendar ---------------------------------------------------------

    async def list_events(
        self,
        start: DateLike | None = None,
        end: DateLike | None = None,
        *,
        days: int = 7,
        timezone: str | None = None,
        frame_id: FrameId = None,
    ) -> list[CalendarEvent]:
        fid = await self._frame(frame_id)
        start, end = _window(start, end, days, self._today())
        return await self.run(ops.list_events(fid, start, end, timezone or await self._tz(fid)))

    async def create_event(
        self,
        summary: str,
        starts_at: DateLike,
        ends_at: DateLike | None = None,
        *,
        all_day: bool = False,
        description: str | None = None,
        location: str | None = None,
        profile_ids: Iterable[Any] | None = None,
        calendar_account_id: Any = None,
        calendar_id: str | None = None,
        rrule: str | list[str] | None = None,
        countdown_enabled: bool = False,
        invited_emails: Iterable[str] | None = None,
        timezone: str | None = None,
        frame_id: FrameId = None,
    ) -> CalendarEvent:
        fid = await self._frame(frame_id)
        tz = timezone or await self._tz(fid)
        starts_at, ends_at = _default_end(starts_at, ends_at, all_day)
        return await self.run(
            ops.create_event(
                fid,
                summary=summary,
                starts_at=starts_at,
                ends_at=ends_at,
                timezone=tz,
                all_day=all_day,
                description=description,
                location=location,
                profile_ids=profile_ids,
                calendar_account_id=calendar_account_id,
                calendar_id=calendar_id,
                rrule=rrule,
                countdown_enabled=countdown_enabled,
                invited_emails=invited_emails,
            )
        )

    async def update_event(self, event_id: Any, *, frame_id: FrameId = None, timezone: str | None = None, **changes: Any) -> CalendarEvent:
        fid = await self._frame(frame_id)
        return await self.run(ops.update_event(fid, event_id, changes, timezone or await self._tz(fid)))

    async def delete_event(self, event_id: Any, *, frame_id: FrameId = None) -> None:
        await self.run(ops.delete_event(await self._frame(frame_id), event_id))

    async def list_source_calendars(self, frame_id: FrameId = None) -> list[SourceCalendar]:
        return await self.run(ops.list_source_calendars(await self._frame(frame_id)))

    # -- lists ------------------------------------------------------------

    async def list_lists(self, frame_id: FrameId = None) -> list[SkylightList]:
        return await self.run(ops.list_lists(await self._frame(frame_id)))

    async def get_list(self, list_id: Any, *, frame_id: FrameId = None) -> SkylightList:
        return await self.run(ops.get_list(await self._frame(frame_id), list_id))

    async def find_list(self, name: str | None = None, *, kind: str | None = None, frame_id: FrameId = None) -> SkylightList:
        return _find_list(await self.list_lists(frame_id), name, kind)

    async def grocery_list(self, frame_id: FrameId = None) -> SkylightList:
        return await self.find_list(kind=LIST_KIND_SHOPPING, frame_id=frame_id)

    async def todo_list(self, frame_id: FrameId = None) -> SkylightList:
        return await self.find_list(kind=LIST_KIND_TODO, frame_id=frame_id)

    async def create_list(self, label: str, *, kind: str = LIST_KIND_TODO, color: str | None = None, frame_id: FrameId = None) -> SkylightList:
        return await self.run(ops.create_list(await self._frame(frame_id), label, kind, color))

    async def update_list(self, list_id: Any, *, frame_id: FrameId = None, **attributes: Any) -> SkylightList:
        return await self.run(ops.update_list(await self._frame(frame_id), list_id, attributes))

    async def delete_list(self, list_id: Any, *, frame_id: FrameId = None) -> None:
        await self.run(ops.delete_list(await self._frame(frame_id), list_id))

    async def add_list_item(self, list_id: Any, label: str, *, section: str | None = None, frame_id: FrameId = None) -> ListItem:
        return await self.run(ops.add_list_item(await self._frame(frame_id), list_id, label, section))

    async def update_list_item(self, list_id: Any, item_id: Any, *, frame_id: FrameId = None, **attributes: Any) -> ListItem:
        return await self.run(ops.update_list_item(await self._frame(frame_id), list_id, item_id, attributes))

    async def complete_list_item(self, list_id: Any, item_id: Any, *, completed: bool = True, frame_id: FrameId = None) -> ListItem:
        return await self.run(ops.set_list_item_completed(await self._frame(frame_id), list_id, item_id, completed))

    async def delete_list_item(self, list_id: Any, item_id: Any, *, frame_id: FrameId = None) -> None:
        await self.run(ops.delete_list_item(await self._frame(frame_id), list_id, item_id))

    async def delete_list_items(self, list_id: Any, item_ids: Iterable[Any], *, frame_id: FrameId = None) -> None:
        await self.run(ops.delete_list_items(await self._frame(frame_id), list_id, item_ids))

    # -- chores / tasks ---------------------------------------------------

    async def list_chores(
        self,
        start: DateLike | None = None,
        end: DateLike | None = None,
        *,
        days: int = 0,
        include_late: bool = True,
        include_up_for_grabs: bool = True,
        linked_to_profile: bool = False,
        frame_id: FrameId = None,
    ) -> list[Chore]:
        start, end = _window(start, end, days, self._today())
        return await self.run(
            ops.list_chores(
                await self._frame(frame_id),
                start,
                end,
                include_late=include_late,
                include_up_for_grabs=include_up_for_grabs,
                linked_to_profile=linked_to_profile,
            )
        )

    async def create_chore(
        self,
        summary: str,
        *,
        profile_ids: Iterable[Any] = (),
        start: DateLike | None = None,
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
        frame_id: FrameId = None,
    ) -> list[Chore]:
        profile_ids = list(profile_ids)
        if not profile_ids and not up_for_grabs:
            raise SkylightConfigError("create_chore needs profile_ids, or up_for_grabs=True")
        if start is None and renewal_interval is None:
            start = self._today()
        return await self.run(
            ops.create_chores(
                await self._frame(frame_id),
                summary=summary,
                profile_ids=profile_ids,
                start=start,
                description=description,
                start_time=start_time,
                routine=routine,
                up_for_grabs=up_for_grabs,
                recurrence_set=recurrence_set,
                recurring_until=recurring_until,
                reward_points=reward_points,
                emoji_icon=emoji_icon,
                renewal_interval=renewal_interval,
                renewal_unit=renewal_unit,
                track_habit=track_habit,
            )
        )

    async def update_chore(self, chore_id: Any, *, frame_id: FrameId = None, **attributes: Any) -> Chore:
        return await self.run(ops.update_chore(await self._frame(frame_id), chore_id, attributes))

    async def complete_chore(
        self,
        chore_id: Any,
        *,
        completed_on: date | str | None = None,
        instance_date: date | str | None = None,
        instance_time: str | None = None,
        frame_id: FrameId = None,
    ) -> ChoreCompletion:
        series, inst_date, inst_time = self._completion_args(chore_id, instance_date, instance_time)
        return await self.run(
            ops.set_chore_status(
                await self._frame(frame_id),
                series,
                complete=True,
                completed_on=completed_on or self._today(),
                instance_date=inst_date,
                instance_time=inst_time,
            )
        )

    async def uncomplete_chore(
        self,
        chore_id: Any,
        *,
        instance_date: date | str | None = None,
        instance_time: str | None = None,
        frame_id: FrameId = None,
    ) -> ChoreCompletion:
        series, inst_date, inst_time = self._completion_args(chore_id, instance_date, instance_time)
        return await self.run(
            ops.set_chore_status(
                await self._frame(frame_id), series, complete=False, completed_on=None, instance_date=inst_date, instance_time=inst_time
            )
        )

    async def delete_chore(self, chore_id: Any, *, apply_to: str | None = None, frame_id: FrameId = None) -> None:
        await self.run(ops.delete_chore(await self._frame(frame_id), chore_id, apply_to))

    async def list_task_box_items(self, frame_id: FrameId = None) -> list[TaskBoxItem]:
        return await self.run(ops.list_task_box_items(await self._frame(frame_id)))

    async def create_task_box_item(
        self, summary: str, *, emoji_icon: str | None = None, routine: bool = False, reward_points: int | None = None, frame_id: FrameId = None
    ) -> TaskBoxItem:
        return await self.run(
            ops.create_task_box_item(await self._frame(frame_id), summary, emoji_icon=emoji_icon, routine=routine, reward_points=reward_points)
        )

    async def delete_task_box_item(self, item_id: Any, *, frame_id: FrameId = None) -> None:
        await self.run(ops.delete_task_box_item(await self._frame(frame_id), item_id))

    # -- rewards ----------------------------------------------------------

    async def list_rewards(
        self, *, redeemed_since: DateLike | None = None, redeemed_until: DateLike | None = None, frame_id: FrameId = None
    ) -> list[Reward]:
        return await self.run(ops.list_rewards(await self._frame(frame_id), redeemed_since, redeemed_until))

    async def get_reward_points(self, frame_id: FrameId = None) -> list[RewardPoints]:
        return await self.run(ops.get_reward_points(await self._frame(frame_id)))

    async def create_reward(
        self,
        name: str,
        point_value: int,
        *,
        profile_ids: Iterable[Any],
        description: str | None = None,
        emoji_icon: str | None = None,
        respawn_on_redemption: bool = False,
        frame_id: FrameId = None,
    ) -> list[Reward]:
        return await self.run(
            ops.create_rewards(
                await self._frame(frame_id),
                name=name,
                point_value=point_value,
                profile_ids=profile_ids,
                description=description,
                emoji_icon=emoji_icon,
                respawn_on_redemption=respawn_on_redemption,
            )
        )

    async def update_reward(self, reward_id: Any, *, frame_id: FrameId = None, **attributes: Any) -> Reward:
        return await self.run(ops.update_reward(await self._frame(frame_id), reward_id, attributes))

    async def delete_reward(self, reward_id: Any, *, frame_id: FrameId = None) -> None:
        await self.run(ops.delete_reward(await self._frame(frame_id), reward_id))

    async def redeem_reward(self, reward_id: Any, *, profile_id: Any = None, frame_id: FrameId = None) -> Reward:
        return await self.run(ops.redeem_reward(await self._frame(frame_id), reward_id, profile_id))

    async def unredeem_reward(self, reward_id: Any, *, frame_id: FrameId = None) -> Reward:
        return await self.run(ops.unredeem_reward(await self._frame(frame_id), reward_id))

    # -- meals ------------------------------------------------------------

    async def list_meal_categories(self, frame_id: FrameId = None) -> list[MealCategory]:
        return await self.run(ops.list_meal_categories(await self._frame(frame_id)))

    async def list_recipes(self, frame_id: FrameId = None) -> list[Recipe]:
        return await self.run(ops.list_recipes(await self._frame(frame_id)))

    async def get_recipe(self, recipe_id: Any, *, frame_id: FrameId = None) -> Recipe:
        return await self.run(ops.get_recipe(await self._frame(frame_id), recipe_id))

    async def create_recipe(
        self, summary: str, *, description: str | None = None, meal_category_id: Any = None, frame_id: FrameId = None
    ) -> Recipe:
        return await self.run(
            ops.create_recipe(await self._frame(frame_id), summary, description=description, meal_category_id=meal_category_id)
        )

    async def update_recipe(self, recipe_id: Any, *, frame_id: FrameId = None, **attributes: Any) -> Recipe:
        return await self.run(ops.update_recipe(await self._frame(frame_id), recipe_id, attributes))

    async def delete_recipe(self, recipe_id: Any, *, frame_id: FrameId = None) -> None:
        await self.run(ops.delete_recipe(await self._frame(frame_id), recipe_id))

    async def add_recipe_to_grocery_list(self, recipe_id: Any, *, frame_id: FrameId = None) -> None:
        await self.run(ops.add_recipe_to_grocery_list(await self._frame(frame_id), recipe_id))

    async def get_meal_plan(
        self, start: DateLike | None = None, end: DateLike | None = None, *, days: int = 7, frame_id: FrameId = None
    ) -> MealPlan:
        start, end = _window(start, end, days, self._today())
        return await self.run(ops.get_meal_plan(await self._frame(frame_id), start, end))

    async def create_meal_sitting(
        self,
        day: date | str,
        meal_category_id: Any,
        *,
        summary: str | None = None,
        recipe_id: Any = None,
        note: str | None = None,
        profile_ids: Iterable[Any] | None = None,
        repeat_weekly: bool = False,
        frame_id: FrameId = None,
    ) -> MealSitting:
        rrule = ops.weekly_rrule(date.fromisoformat(ops.iso_date(day))) if repeat_weekly else None
        return await self.run(
            ops.create_meal_sitting(
                await self._frame(frame_id),
                day=day,
                meal_category_id=meal_category_id,
                summary=summary,
                recipe_id=recipe_id,
                note=note,
                profile_ids=profile_ids,
                rrule=rrule,
            )
        )

    async def update_meal_sitting(
        self,
        sitting: MealSitting,
        day: date | str | None = None,
        *,
        new_day: date | str | None = None,
        summary: str | None = None,
        note: str | None = None,
        meal_category_id: Any = None,
        recipe_id: Any = None,
        frame_id: FrameId = None,
    ) -> MealSitting:
        day = day if day is not None else sitting.date
        if day is None:
            raise SkylightConfigError("update_meal_sitting needs a day")
        return await self.run(
            ops.update_meal_sitting(
                await self._frame(frame_id),
                sitting,
                day,
                new_day=new_day,
                summary=summary,
                note=note,
                meal_category_id=meal_category_id,
                recipe_id=recipe_id,
            )
        )

    async def delete_meal_sitting(self, sitting_id: Any, day: date | str, *, apply_to: str | None = None, frame_id: FrameId = None) -> None:
        await self.run(ops.delete_meal_sitting(await self._frame(frame_id), sitting_id, day, apply_to))


# ---------------------------------------------------------------------------
# Shared pure helpers
# ---------------------------------------------------------------------------


def _window(start: DateLike | None, end: DateLike | None, days: int, today: date) -> tuple[DateLike, DateLike]:
    if start is None:
        start = today
    if end is None:
        base = start if isinstance(start, date) else date.fromisoformat(ops.iso_date(start))
        if isinstance(base, datetime):
            base = base.date()
        end = base + timedelta(days=days)
    return start, end


def _default_end(starts_at: DateLike, ends_at: DateLike | None, all_day: bool) -> tuple[DateLike, DateLike]:
    if ends_at is not None:
        return starts_at, ends_at
    if isinstance(starts_at, datetime):
        return starts_at, starts_at + timedelta(hours=1)
    if isinstance(starts_at, date):
        return starts_at, (starts_at + timedelta(days=1) if all_day else starts_at)
    raise SkylightConfigError("ends_at is required when starts_at is a string")


def _find_profile(profiles: list[Profile], name: str) -> Profile:
    wanted = name.strip().lower()
    hits = [p for p in profiles if (p.label or "").strip().lower() == wanted]
    if len(hits) == 1:
        return hits[0]
    known = ", ".join(p.label or p.id for p in profiles)
    if not hits:
        raise SkylightNotFoundError(f'No profile called "{name}". Profiles: {known}', status=404, method="GET", path="/categories")
    raise SkylightConfigError(f'"{name}" matches {len(hits)} profiles; rename one in Skylight')


def _find_list(lists: list[SkylightList], name: str | None, kind: str | None) -> SkylightList:
    candidates = lists
    if kind:
        candidates = [lst for lst in candidates if lst.kind == kind]
    if name:
        wanted = name.strip().lower()
        exact = [lst for lst in candidates if (lst.label or "").strip().lower() == wanted]
        candidates = exact or [lst for lst in candidates if wanted in (lst.label or "").lower()]
    if not candidates:
        known = ", ".join(f"{lst.label} ({lst.kind})" for lst in lists)
        raise SkylightNotFoundError(f"No matching list. Lists: {known}", status=404, method="GET", path="/lists")
    default = [lst for lst in candidates if lst.default_grocery_list]
    return (default or candidates)[0]
