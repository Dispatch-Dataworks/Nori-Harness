# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Nori's tool-usage guidance -- operating mechanics (when/how she uses her
tools), kept separate from persona.py's character content on purpose, same
split a sibling application uses. Thin wrapper around promptdoc.PromptDoc, same shape
as persona.py.
"""
from __future__ import annotations

from pathlib import Path

import promptdoc

_doc = promptdoc.PromptDoc("tool-usage")


def _template_path() -> Path:
    return _doc.path.parent / "tool-usage.template.md"


# No PATH/DEFAULT_PATH/TEMPLATE_PATH module attributes here on purpose (2026-09-23, the persona.py live-data
# incident: promptdoc.PromptDoc.path etc. are properties, re-read fresh from NORI_PROMPTS_DIR on every access, but
# freezing that value into a plain module attribute at import time defeats it just the same). Nothing outside this
# file ever referenced PATH/DEFAULT_PATH/TEMPLATE_PATH (confirmed by grep), so there was no public API to preserve
# here -- every use below just calls `_doc.path`/`_doc.default_path`/`_template_path()` directly.
def default_text() -> str:
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


def load_prompt() -> str:
    if _doc.load_prompt():
        return _doc.load_prompt()
    tmpl = _template_path()
    if not _doc.path.is_file() and not _doc.default_path.is_file() and tmpl.is_file():
        raw = tmpl.read_text(encoding="utf-8")
        if promptdoc.START in raw and promptdoc.END in raw:
            return raw.split(promptdoc.START, 1)[1].split(promptdoc.END, 1)[0].strip()
        return raw.strip()
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
