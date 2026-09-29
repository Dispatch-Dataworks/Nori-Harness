# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Nori's persona — a thin wrapper around promptdoc.PromptDoc for the one
thing generic enough to matter: falling back to a blank template if there's
somehow no default at all (shouldn't happen once persona.default.md ships,
but costs nothing to guard).

Deliberately NOT building the "baseline" (operator's own saved-good-state,
distinct from both live and shipped default) that a sibling application has — that's a
real feature but not needed to reach a working chat, which is the Phase 3
bar. Add it later if it turns out to matter for Nori too; flagged here
rather than silently dropped.
"""
from __future__ import annotations

from pathlib import Path

import promptdoc

_doc = promptdoc.PromptDoc("persona")

_NAME_PLACEHOLDER = "{{ASSISTANT_NAME}}"


def assistant_name(workspace_id: int) -> str:
    """The operator's own name for this instance (2026-09-25) -- read fresh
    from config every call, never cached, never written into a file. The
    harness/product identity (the repo, the PACI specification, server_version, the
    Nori-PACI User-Agent) is a separate thing and never goes through this."""
    import config  # local: avoids a load-order assumption, same reasoning as emotion.py's local `import accounts`
    return config.get("workspace", workspace_id, "assistant_name")


def assistant_name_for_user(user_id: int | None) -> str:
    """Same as assistant_name(), but from a user_id -- the shape context.py's
    build_system() actually has on hand. None (no session context, e.g. a
    test or an admin preview/reset path -- see load_prompt()'s own docstring
    for why those paths don't resolve the placeholder at all) falls back to
    the shipped default name, matching what an unconfigured instance has
    always said."""
    if user_id is None:
        return "Nori"
    import accounts  # local: same reasoning as emotion.get_state()'s own lookup
    user = accounts.get_user(user_id)
    return assistant_name(user["workspace_id"]) if user is not None else "Nori"


def _resolve_name(text: str, user_id: int | None) -> str:
    return text.replace(_NAME_PLACEHOLDER, assistant_name_for_user(user_id)) if _NAME_PLACEHOLDER in text else text


def _template_path() -> Path:
    return _doc.path.parent / "persona.template.md"


# PERSONA_PATH/DEFAULT_PATH/TEMPLATE_PATH used to be plain module attributes, frozen the moment THIS module was
# first imported (2026-09-23, live-data incident: promptdoc.PromptDoc.path is now a property computed fresh from
# NORI_PROMPTS_DIR on every access, precisely so a test's override still works no matter when it runs -- but that
# fix is worthless if THIS module immediately re-freezes the same value into a plain attribute at import time,
# which is exactly what happened here and is why the live persona.md got overwritten by a test run). A module-level
# `__getattr__` (PEP 562) makes every access to persona.PERSONA_PATH/DEFAULT_PATH/TEMPLATE_PATH re-read the live
# value instead -- no caller changes (still `persona.PERSONA_PATH`, never `persona.PERSONA_PATH()`).
_MODULE_PATHS = {"PERSONA_PATH": lambda: _doc.path, "DEFAULT_PATH": lambda: _doc.default_path, "TEMPLATE_PATH": _template_path}


def __getattr__(name: str):
    if name in _MODULE_PATHS:
        return _MODULE_PATHS[name]()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def default_text() -> str:
    """The shipped default persona — what 'reset persona' restores to. Falls
    back to the blank template only if the default file is somehow missing."""
    for p in (_doc.default_path, _template_path()):
        if p.is_file():
            return p.read_text(encoding="utf-8")
    return ""


def is_default() -> bool:
    return load_file().strip() == default_text().strip()


def reset_to_default() -> tuple[bool, str]:
    d = default_text()
    return save(d) if d.strip() else (False, "no default file")


def has_baseline() -> bool:
    return _doc.has_baseline()


def baseline_text() -> str:
    return _doc.baseline_text()


def baseline_saved_ts() -> float | None:
    return _doc.baseline_saved_ts()


def save_baseline() -> tuple[bool, str]:
    return _doc.save_baseline()


def revert_to_baseline() -> tuple[bool, str]:
    return _doc.revert_to_baseline()


def restore_target_text() -> str:
    return _doc.restore_target_text()


def reset_to_preferred() -> tuple[bool, str]:
    return _doc.reset_to_preferred()


def load_prompt(user_id: int | None = None) -> str:
    """The text actually assembled into the system prompt. `user_id`
    resolves a literal {{ASSISTANT_NAME}} in the text (default or an
    operator's own custom persona, either one) to that workspace's
    configured name -- resolved HERE, at this read, never written back to
    the file (context.py's own build_system() is the one real caller that
    passes it). Omitted -- every admin preview/reset/diff path, and every
    existing test -- leaves the placeholder as literal text: those paths
    show or write the template itself, never the model-facing render, and
    substituting there would be exactly the find-and-replace-into-a-file
    this was built to avoid."""
    if _doc.load_prompt():
        return _resolve_name(_doc.load_prompt(), user_id)
    # persona.md doesn't exist yet and neither does a default -- fall back
    # to the template directly rather than sending an empty system prompt.
    tmpl = _template_path()
    if not _doc.path.is_file() and not _doc.default_path.is_file() and tmpl.is_file():
        raw = tmpl.read_text(encoding="utf-8")
        if promptdoc.START in raw and promptdoc.END in raw:
            return _resolve_name(raw.split(promptdoc.START, 1)[1].split(promptdoc.END, 1)[0].strip(), user_id)
        return _resolve_name(raw.strip(), user_id)
    return ""


def load_file() -> str:
    return _doc.load_file()


def approx_tokens() -> int:
    return _doc.approx_tokens()


def validate(text: str) -> str | None:
    return _doc.validate(text)


def save(text: str) -> tuple[bool, str]:
    return _doc.save(text)


def history() -> list[dict]:
    return _doc.history()


def history_text(name: str) -> str | None:
    return _doc.history_text(name)


def restore(name: str) -> tuple[bool, str]:
    return _doc.restore(name)
