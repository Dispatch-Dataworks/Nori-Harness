"""Unofficial Python client for the Skylight family calendar.

    from skylight_calendar import SkylightClient

    client = SkylightClient.from_env()          # SKYLIGHT_EMAIL / SKYLIGHT_PASSWORD, or a saved login
    for event in client.list_events(days=7):
        print(event.starts_at, event.summary)

Skylight has no public API; this talks to the private one its apps use and
may break when Skylight changes it.
"""

from __future__ import annotations

from ._http import StdlibClient, TransportError
from ._ops import Operation
from .auth import BrowserLogin, async_login, async_refresh, login, refresh
from .client import AsyncSkylightClient, SkylightClient
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
from .tokens import Credentials, FileTokenStore, MemoryTokenStore, TokenStore, default_token_path

__version__ = "0.2.0"

__all__ = [
    "AsyncSkylightClient",
    "BrowserLogin",
    "CalendarEvent",
    "Chore",
    "ChoreCompletion",
    "ChoreOccurrence",
    "Credentials",
    "Device",
    "FileTokenStore",
    "Frame",
    "ListItem",
    "MealCategory",
    "MealPlan",
    "MealSitting",
    "MemoryTokenStore",
    "Operation",
    "Profile",
    "Recipe",
    "Reward",
    "RewardPoints",
    "SkylightAPIError",
    "SkylightAuthError",
    "SkylightClient",
    "SkylightConfigError",
    "SkylightError",
    "SkylightList",
    "SkylightNotFoundError",
    "SkylightRateLimitError",
    "SkylightValidationError",
    "SourceCalendar",
    "StdlibClient",
    "TaskBoxItem",
    "TokenStore",
    "TransportError",
    "User",
    "__version__",
    "async_login",
    "async_refresh",
    "default_token_path",
    "login",
    "refresh",
]
