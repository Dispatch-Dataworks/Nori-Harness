# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Pre-reply checklist: short, per-turn reminders injected as the LAST
message before the model actually replies -- never folded into the
standing system prompt.

Real, diagnosed distinction behind this module (2026-09-12): neither a
persona-level line nor a tool-usage-level mechanical instruction moved
set_emotion's call rate at all, on two separate real tests against
gpt-4.1-mini -- both live in "always present" background the model
apparently discounts. What demonstrably worked, the only thing that
ever did, was the operator's own direct, in-context reminder -- fresh content,
right before the reply. This module generalizes THAT mechanism (position,
not wording) instead of writing a third standing document.

One entry (emotional-state reconsideration, in emotion.py) ships with
this. A second later check (the operator's own example: new mail) registers the
same way scheduler.py's own _SIGNAL_PROVIDERS pattern already does, so
this module never needs rewriting to add one -- see register() below.
"""
from __future__ import annotations

from typing import Callable

# Each check gets session+user_id and returns either a short line to
# include this turn, or None if it has nothing to say right now -- same
# shape as scheduler.outstanding_reason(), so a check with nothing
# relevant this turn adds zero tokens, not an empty bullet.
_CHECKS: list[Callable[[dict, int], "str | None"]] = []


def register(fn: Callable[[dict, int], "str | None"]) -> None:
    _CHECKS.append(fn)


def build_block(session: dict, user_id: int) -> dict | None:
    """One system message combining every registered check's current
    line, or None if every check has nothing to add (an empty checklist
    would itself be noise, and a message with no real content is exactly
    the kind of standing-seeming filler this module exists to avoid).
    Appended by chat.run() as the LAST message before the model replies
    -- proximate by construction, never part of context.build_messages()'s
    own (persona-carrying) system message."""
    lines = [ln for fn in _CHECKS if (ln := fn(session, user_id))]
    if not lines:
        return None
    content = "Before you reply, quickly check:\n" + "\n".join(f"- {ln}" for ln in lines)
    return {"role": "system", "content": content}
