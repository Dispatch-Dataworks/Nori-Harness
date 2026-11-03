"""Constants for the (unofficial) Skylight API.

Everything here was reverse-engineered from the Skylight web app by the
community. Skylight does not publish an API, so any of it may change.
"""

from __future__ import annotations

#: Host that serves both the OAuth pages and the JSON API (under ``/api``).
BASE_URL = "https://app.ourskylight.com"

#: The web app's own origin; also the OAuth redirect target.
WEB_APP_URL = "https://ourskylight.com"

#: OAuth2 client registered for the mobile/web apps.
OAUTH_CLIENT_ID = "skylight-mobile"
OAUTH_SCOPE = "everything"
OAUTH_REDIRECT_URI = f"{WEB_APP_URL}/welcome"

#: ``Skylight-Api-Version`` header. Some endpoints 422 without it.
DEFAULT_API_VERSION = "2026-05-01"
#: Newer version the web app sends when editing/deleting meal sittings.
MEAL_EDIT_API_VERSION = "2026-10-15"

#: What the web app identifies itself as on API calls.
API_USER_AGENT = "SkylightMobile (web)"
#: A browser-ish UA for the server-rendered login pages.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

DEFAULT_TIMEOUT = 30.0

#: Environment variables read by :func:`skylight_calendar.SkylightClient.from_env`.
ENV_EMAIL = "SKYLIGHT_EMAIL"
ENV_PASSWORD = "SKYLIGHT_PASSWORD"
ENV_ACCESS_TOKEN = "SKYLIGHT_ACCESS_TOKEN"
ENV_FRAME_ID = "SKYLIGHT_FRAME_ID"
ENV_TIMEZONE = "SKYLIGHT_TIMEZONE"
ENV_TOKEN_FILE = "SKYLIGHT_TOKEN_FILE"

#: Chores: Skylight spells a finished chore ``complete``; list items use ``completed``.
CHORE_STATUS_COMPLETE = "complete"
CHORE_STATUS_PENDING = "pending"
CHORE_DONE_STATUSES = frozenset({"complete", "completed"})

LIST_ITEM_STATUS_COMPLETED = "completed"
LIST_ITEM_STATUS_PENDING = "pending"

LIST_KIND_SHOPPING = "shopping"
LIST_KIND_TODO = "to_do"
