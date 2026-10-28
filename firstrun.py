# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""First-run setup (2026-10-28): the guided pass that comes before she says hello.

A brand-new instance used to open on an empty chat, and the only way to find out she needed a model was to wander into Settings. Now the first time the
admin arrives they land on /first-run: (1) connect a provider, (2) choose a primary model and fallbacks, (3) what to call her and her pronouns. Finishing
it hands over to the getting-to-know-you conversation (onboarding.py), and after that she is simply their assistant.

Nothing here is a second source of truth. Each step is "done" because the real data says so (a connected provider, a non-empty model chain), so the page
resumes wherever the admin left off and works equally for an instance that already has a model from OPENROUTER_API_KEY. The only stored facts are the
three workspace settings in config.py: `setup_complete`, `setup_identity`, `assistant_pronouns`.

An instance that already has conversation history (anything upgraded, including every instance that predates this) is never sent through it: the first
time `needed()` looks, it marks such a workspace complete.

See docs/onboarding.md."""
from __future__ import annotations

import config
import conversation
import providers

STEPS = ("connect", "models", "identity")
STEP_TITLES = {"connect": "Connect a provider", "models": "Choose models", "identity": "Her name"}
NAME_MAX = 40


def pronouns(workspace_id: int) -> str:
    value = config.get("workspace", workspace_id, "assistant_pronouns")
    return value if value in config.PRONOUNS else config.PRONOUNS[0]


def has_provider(workspace_id: int) -> bool:
    return any(p["enabled"] and p["status"] == "connected" for p in providers.list_all(workspace_id))


def has_model(workspace_id: int) -> bool:
    """Can she think yet? The same test the chat page uses."""
    try:
        import chat
        return bool(chat._resolve_model_chain(workspace_id))
    except Exception:  # noqa: BLE001
        return False


def needed(workspace_id: int) -> bool:
    """Should the admin be sent to /first-run? False once finished or skipped, and for any workspace that already has history (which is then marked
    complete so the answer never changes later)."""
    if config.get("workspace", workspace_id, "setup_complete"):
        return False
    if conversation.workspace_has_messages(workspace_id):
        config.set("workspace", workspace_id, "setup_complete", True)
        return False
    return True


def done(workspace_id: int) -> dict:
    """Which steps the data says are finished."""
    return {"connect": has_provider(workspace_id), "models": has_model(workspace_id),
            "identity": bool(config.get("workspace", workspace_id, "setup_identity"))}


def next_step(workspace_id: int) -> str:
    """The first unfinished step, or "identity" once everything is done (the page then offers the way in)."""
    d = done(workspace_id)
    return next((s for s in STEPS if not d[s]), STEPS[-1])


def save_identity(workspace_id: int, name: str, pronouns_choice: str) -> tuple[bool, str]:
    """Her name and pronouns, as the admin chose them. Validated here, not trusted from the form."""
    name = (name or "").strip()
    if not name or len(name) > NAME_MAX or any(ch in name for ch in "<>{}\\\r\n\t"):
        return False, f"a name is 1-{NAME_MAX} characters, with no line breaks or < > {{ }}"
    if pronouns_choice not in config.PRONOUNS:
        return False, "pick one of: " + ", ".join(config.PRONOUNS)
    config.set("workspace", workspace_id, "assistant_name", name)
    config.set("workspace", workspace_id, "assistant_pronouns", pronouns_choice)
    config.set("workspace", workspace_id, "setup_identity", True)
    return True, "saved"


def finish(workspace_id: int) -> tuple[bool, str]:
    """Close the wizard. Refused while she has nothing to think with: finishing then would drop the admin on a chat that can't answer."""
    if not has_model(workspace_id):
        return False, "choose a primary model first, she can't speak without one"
    config.set("workspace", workspace_id, "setup_complete", True)
    return True, "done"


def skip(workspace_id: int) -> None:
    """"Later": stop redirecting to the wizard. The chat explains what is missing, and Settings -> About can reopen it."""
    config.set("workspace", workspace_id, "setup_complete", True)
