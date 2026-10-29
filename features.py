# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Optional features (2026-10-29, operator's own ask): the things some households don't use at
all -- trackers, the shared inventory, meal planning, and whatever joins them later -- can each be
switched off, per person. Off means all three of:

  * its pages and their links disappear (the "⋯" menu, the chat's quick-action bar, the board's
    trackers button), and a direct visit is sent to Settings -> Features instead;
  * its proactive pings stop -- a ping signal registered with `feature=<key>`
    (scheduler.register_signal) is never even called while the feature is off, the same "genuinely
    absent, not suppressed" rule a switched-off signal already follows;
  * its tools leave the model's tool list and refuse if called anyway (tools._is_enabled), so
    Nori doesn't keep logging into something the person turned off. Nori can still switch a feature
    back on, with the existing `update_settings` tool: each feature's switch is an ordinary
    per-user setting, `feature_<key>_enabled`, listed in settings_tool.PEER_SETTINGS_KEYS.

Turning a feature off hides it and keeps the data: nothing here deletes a row, and turning it back
on brings everything back. On by default, so nothing changes until someone switches one off.

Adding a feature of this kind is one entry in FEATURES below (plus `feature="<key>"` on its ping
signal, if it has one). The switch, its place in Settings -> Features, the menu/page/ping/tool
behaviour above, and Nori's ability to flip it are all derived from that entry -- nothing else
names a feature by hand. tests/test_nori_features.py guards the other half (every `feature=` a
signal uses exists here; every switch is something update_settings can change).

Deliberately a separate, static table rather than something each module registers at import: the
settings tool builds its list of changeable settings once, when it registers, and every switch has
to be in it by then regardless of module import order. Imports config only, like config's other
low-level readers, so nothing here can be part of an import cycle.
"""
from __future__ import annotations

import config

FEATURES: tuple[dict, ...] = (
    {"key": "trackers", "label": "Trackers",
     "blurb": "Duration and value trackers (sleep, weight, water, ...), including what the phone's health data fills in.",
     "paths": ("/trackers",),
     "tool_prefixes": ("tracker_",), "tools": ()},
    {"key": "inventory", "label": "Inventory",
     "blurb": "The shared household inventory: what's low or out.",
     "paths": ("/inventory",),
     "tool_prefixes": (), "tools": ("update_household_item", "list_household_items", "remove_household_item")},
    {"key": "meals", "label": "Meal planning",
     "blurb": "The shared meal plan.",
     "paths": ("/meals",),
     "tool_prefixes": (), "tools": ("plan_meal", "list_meal_plan", "clear_meal_plan")},
)

_BY_KEY = {f["key"]: f for f in FEATURES}


def config_key(key: str) -> str:
    return f"feature_{key}_enabled"


for _f in FEATURES:
    # on by default; per user, because the menu and the pings are each person's own
    config._SPEC[config_key(_f["key"])] = (True, bool, "user", False, f"show {_f['label']} (its page, menu links, pings and tools)")


def config_keys() -> tuple[str, ...]:
    return tuple(config_key(f["key"]) for f in FEATURES)


def registry() -> list[dict]:
    return [dict(f, config_key=config_key(f["key"])) for f in FEATURES]


def label(key: str) -> str:
    f = _BY_KEY.get(key)
    return f["label"] if f else key


def enabled(user_id: int, key: str) -> bool:
    """Is this feature on for this person. An unknown key is on: a typo here must never hide something."""
    if key not in _BY_KEY:
        return True
    return bool(config.get("user", user_id, config_key(key)))


def feature_for_path(path: str) -> str | None:
    """The feature a page or form path belongs to, or None."""
    for f in FEATURES:
        for p in f["paths"]:
            if path == p or path.startswith(p + "/"):
                return f["key"]
    return None


def feature_for_tool(name: str) -> str | None:
    for f in FEATURES:
        if name in f["tools"] or any(name.startswith(p) for p in f["tool_prefixes"]):
            return f["key"]
    return None


def tool_allowed(name: str, user_id: int | None) -> bool:
    """False only for a tool of a feature this person has switched off (no person, nothing to have switched off)."""
    key = feature_for_tool(name)
    return key is None or user_id is None or enabled(user_id, key)
