# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""A small editable, versioned prompt fragment: atomic write, timestamped
history, validate before commit, reset to a shipped default. Retyped from
a sibling application/promptdoc.py — same shape, no shared code.

persona.py wraps this for Nori's persona specifically (it needs a
template-file fallback this doesn't). Reused as-is for any other editable
prompt fragment added later (tool-usage guidance, once there are tools to
write guidance about).
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent


# NORI_PROMPTS_DIR redirects these files for throwaway testing — mirrors the same convention
# a sibling application uses for its own prompts directory override, unrelated
# env var, no shared state. A FUNCTION, re-read on every call (2026-09-23, live-data incident: this used to be a
# module-level constant computed once at import, exactly the bug already diagnosed for a sibling application's store.DATA_DIR --
# a test file setting NORI_PROMPTS_DIR before its own `import persona` did nothing if `persona`/`promptdoc` had
# already been imported earlier in the SAME process with a different (or no) override, because the frozen constant
# was already baked in. That silently pointed a real multi-file nori test run at the LIVE prompts directory and
# overwrote the real persona.md and its baseline with test fixture text -- see OUTSTANDING.md and CHANGELOG.md,
# 2026-09-23. Every PromptDoc path below is now a property that calls this fresh each time, so an env var set at
# ANY point before use takes effect, not just before the first import anywhere in the process.
_REAL_PROMPTS_DIR = _HERE / "prompts"   # never returned unless NORI_LIVE says so, even if NORI_PROMPTS_DIR happens to name it explicitly


def _prompts_dir() -> Path:
    """Refuses to guess, same discipline as store.py's DATA_DIR (2026-09-23, added AFTER the incident: this used
    to silently default to the real prompts/ directory whenever NORI_PROMPTS_DIR was unset for any reason -- which
    is exactly what happened, silently, the day a test run overwrote the real persona.md. Now it requires EITHER
    NORI_PROMPTS_DIR (a throwaway path) OR NORI_LIVE (nori_ctl.ps1 already sets this for the real service) --
    unconfigured no longer means 'fall back to the real one', it means a loud refusal. A SECOND check catches the
    explicit case too: even a NORI_PROMPTS_DIR that happens to resolve to the real prompts/ directory is refused
    without NORI_LIVE -- see tests/test_nori_path_safety.py for the adversarial proof."""
    env = os.environ.get("NORI_PROMPTS_DIR")
    live = os.environ.get("NORI_LIVE")
    if env:
        p = Path(env).resolve()
        if p == _REAL_PROMPTS_DIR and not live:
            raise RuntimeError(
                "promptdoc.py: NORI_PROMPTS_DIR points at the REAL live prompts directory, and NORI_LIVE is not "
                "set -- refusing, even though it was named explicitly.")
        return p
    if live:
        return _REAL_PROMPTS_DIR
    raise RuntimeError(
        "promptdoc.py: no prompts directory configured -- refusing to guess. Set one of:\n"
        "  NORI_PROMPTS_DIR=<path>   a throwaway/test instance, your own scratch directory\n"
        "  NORI_LIVE=1               yes, this really is the live production server\n"
        "                            (nori_ctl.ps1 already sets this for you)\n"
        "This refuses rather than default to the real persona/tool-usage files: a test run without either "
        "has overwritten them before.")


START = "=== PROMPT STARTS ==="
END = "=== PROMPT ENDS ==="
MAX_BYTES = 32_000
_KEEP_HISTORY = 100


class PromptDoc:
    def __init__(self, slug: str):
        self.slug = slug
        self.hist_re = re.compile(rf"^{re.escape(slug)}-\d{{8}}-\d{{6}}(?:-\d+)?\.md$")

    # Every path below is a PROPERTY, computed fresh from _prompts_dir() on each access -- never cached on the
    # instance -- so a NORI_PROMPTS_DIR set after this object already exists (e.g. persona.py's module-level `_doc`,
    # created once at persona.py's own import time) still redirects every read/write correctly. This is the actual
    # fix: see _prompts_dir()'s own comment for the incident that made it necessary.
    @property
    def path(self) -> Path:
        return _prompts_dir() / f"{self.slug}.md"

    @property
    def default_path(self) -> Path:
        return _prompts_dir() / f"{self.slug}.default.md"

    @property
    def baseline_path(self) -> Path:
        return _prompts_dir() / f"{self.slug}.baseline.md"

    @property
    def baseline_meta_path(self) -> Path:
        return _prompts_dir() / f"{self.slug}.baseline.meta"

    @property
    def history_dir(self) -> Path:
        return _prompts_dir() / f"{self.slug}-history"

    # ── read ────────────────────────────────────────────────────────────
    def _raw(self) -> str:
        for p in (self.path, self.default_path):
            if p.is_file():
                return p.read_text(encoding="utf-8")
        return ""

    def default_text(self) -> str:
        return (self.default_path.read_text(encoding="utf-8")
                if self.default_path.is_file() else "")

    def is_default(self) -> bool:
        return self.load_file().strip() == self.default_text().strip()

    # ── baseline: the operator's own saved-good state -- distinct from
    # both the live file and the shipped default (2026-09-12, matching
    # a sibling application/persona.py's existing pattern, generalized here so every
    # PromptDoc-backed file gets it, not just one). Never versioned in
    # git -- this is the operator's own data, same category as the live
    # file itself, regardless of whether the live file is tracked. ──────
    def has_baseline(self) -> bool:
        return self.baseline_path.is_file()

    def baseline_text(self) -> str:
        return self.baseline_path.read_text(encoding="utf-8") if self.baseline_path.is_file() else ""

    def baseline_saved_ts(self) -> float | None:
        """When the operator marked this baseline: recorded explicitly at save time (a copied or restored file keeps its own, unrelated mtime), with the file's mtime only as
        the fallback for a baseline saved before this was recorded."""
        if not self.baseline_path.is_file():
            return None
        try:
            return float(json.loads(self.baseline_meta_path.read_text(encoding="utf-8"))["saved_ts"])
        except (OSError, ValueError, KeyError, TypeError):
            return self.baseline_path.stat().st_mtime

    def save_baseline(self) -> tuple[bool, str]:
        """Freeze the CURRENT live file as the reset/revert target. Overwrites
        any previous baseline -- a deliberate 'promote what I have now'
        action, not itself versioned (the live file's own history already
        covers that)."""
        text = self.load_file()
        if not text.strip():
            return False, "nothing live to save"
        self._atomic_write(self.baseline_path, text)
        self._atomic_write(self.baseline_meta_path, json.dumps({"saved_ts": time.time()}))
        return True, "saved"

    def revert_to_baseline(self) -> tuple[bool, str]:
        """Restore the live file to the saved baseline -- not a full reset,
        the one to reach for day to day."""
        t = self.baseline_text()
        if not t.strip():
            return False, "no baseline saved yet"
        return self.save(t)

    def restore_target_text(self) -> str:
        """What a 'reset' restores to: the baseline if one's ever been
        saved, otherwise the shipped default."""
        return self.baseline_text() if self.has_baseline() else self.default_text()

    def reset_to_preferred(self) -> tuple[bool, str]:
        t = self.restore_target_text()
        return self.save(t) if t.strip() else (False, "nothing to reset to")

    def load_prompt(self) -> str:
        """The text the model actually gets — between the markers, stripped."""
        raw = self._raw()
        out = raw.split(START, 1)[1].split(END, 1)[0].strip() if (START in raw and END in raw) else raw.strip()
        if not out and self.default_path.is_file():
            # A live file with nothing usable in it (edited on disk, bypassing validate) must never send her an empty prompt: fall back to the shipped default.
            raw = self.default_path.read_text(encoding="utf-8")
            out = raw.split(START, 1)[1].split(END, 1)[0].strip() if (START in raw and END in raw) else raw.strip()
        return out

    def load_file(self) -> str:
        """The whole file, for an editor textarea."""
        return self._raw()

    def approx_tokens(self) -> int:
        return len(self.load_prompt()) // 4

    # ── validate + write ────────────────────────────────────────────────
    def validate(self, text: str) -> str | None:
        if not text.strip():
            return "empty"
        if len(text.encode("utf-8")) > MAX_BYTES:
            return f"too large (> {MAX_BYTES} bytes)"
        if START not in text or END not in text:
            return f"must contain both {START!r} and {END!r} markers"
        if text.index(START) > text.index(END):
            return "markers are in the wrong order"
        if not text.split(START, 1)[1].split(END, 1)[0].strip():
            return "nothing between the markers -- she would be sent an empty prompt"
        return None

    def _atomic_write(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{self.slug}-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def _order_key(self, name: str) -> tuple:
        """Chronological order of a snapshot name: its timestamp, then its same-second counter as a NUMBER. A plain sort of the names is wrong within one second
        ("...-1.md" sorts before "....md"), which would list and prune the wrong version."""
        m = re.search(r"-(\d{8}-\d{6})(?:-(\d+))?\.md$", name)
        return (m.group(1), int(m.group(2) or 0)) if m else ("", 0)

    def _snapshot(self) -> str | None:
        if not self.path.is_file():
            return None
        self.history_dir.mkdir(parents=True, exist_ok=True)
        base = time.strftime(f"{self.slug}-%Y%m%d-%H%M%S")
        name, n = f"{base}.md", 1
        while (self.history_dir / name).exists():
            name = f"{base}-{n}.md"
            n += 1
        self._atomic_write(self.history_dir / name, self.path.read_text(encoding="utf-8"))
        files = sorted(self.history_dir.glob(f"{self.slug}-*.md"), key=lambda p: self._order_key(p.name))
        for old in files[:-_KEEP_HISTORY]:
            old.unlink(missing_ok=True)
        return name

    def save(self, text: str) -> tuple[bool, str]:
        text = text.replace("\r\n", "\n")
        err = self.validate(text)
        if err:
            return False, err
        prev = self._snapshot()
        self._atomic_write(self.path, text if text.endswith("\n") else text + "\n")
        if self.path.read_text(encoding="utf-8").strip() != text.strip():
            if prev:
                self._atomic_write(self.path, (self.history_dir / prev).read_text(encoding="utf-8"))
            return False, "readback mismatch — restored previous"
        return True, prev or "(no previous)"

    def reset_to_default(self) -> tuple[bool, str]:
        d = self.default_text()
        return self.save(d) if d.strip() else (False, "no default file")

    # ── history ─────────────────────────────────────────────────────────
    def history(self) -> list[dict]:
        if not self.history_dir.is_dir():
            return []
        out = []
        for p in sorted(self.history_dir.glob(f"{self.slug}-*.md"), key=lambda p: self._order_key(p.name), reverse=True):
            st = p.stat()
            out.append({"name": p.name, "ts": st.st_mtime, "bytes": st.st_size})
        return out

    def history_text(self, name: str) -> str | None:
        if not self.hist_re.match(name or ""):
            return None
        p = self.history_dir / name
        return p.read_text(encoding="utf-8") if p.is_file() else None

    def restore(self, name: str) -> tuple[bool, str]:
        txt = self.history_text(name)
        if txt is None:
            return False, "no such snapshot"
        return self.save(txt)
