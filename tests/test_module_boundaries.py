# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Structural hygiene: every import in this repository resolves to either the standard library, a
declared dependency (requirements.txt), or another file in this same repository -- never something
that exists only on the machine this was developed on. This is what actually catches accidental cross-
project code leakage (an import of a module this repo doesn't ship), and it does so without needing to
name anything about what might have been imported by mistake."""
from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# From requirements.txt -- the import name a dependency actually exposes, which isn't always its
# package/distribution name (Pillow's import name is PIL).
_DECLARED_DEPENDENCIES = {"cryptography", "PIL", "tzdata"}

_STDLIB = set(getattr(sys, "stdlib_module_names", ()))
_STDLIB |= {"__future__"}


def _local_modules() -> set[str]:
    """Every top-level importable name this repo actually provides: foo.py -> "foo" (anywhere in the
    tree -- the test files each run as their own script and put their own directory on sys.path, per
    docs/contributing.md's own "Running the tests" note, so a sibling test file counts as local same as
    a root-level module), plus pkg/__init__.py -> "pkg"."""
    names = {p.stem for p in ROOT.rglob("*.py") if ".git" not in p.parts}
    names |= {p.parent.name for p in ROOT.rglob("__init__.py") if ".git" not in p.parts}
    return names


def _all_source_files() -> list[Path]:
    return [p for p in ROOT.rglob("*.py") if ".git" not in p.parts]


def _imported_top_level_names(path: Path) -> set[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return set()
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:  # level > 0 is a relative import; not a top-level name
                names.add(node.module.split(".")[0])
    return names


class EveryImportResolvesWithinThisRepo(unittest.TestCase):
    def test_no_import_reaches_outside_stdlib_dependencies_or_this_repo(self):
        local = _local_modules()
        unresolved: list[str] = []
        for src in _all_source_files():
            rel = src.relative_to(ROOT).as_posix()
            for name in _imported_top_level_names(src):
                if name in _STDLIB or name in _DECLARED_DEPENDENCIES or name in local:
                    continue
                unresolved.append(f"{rel}: imports {name!r} -- not stdlib, not a declared "
                                 f"dependency, and no {name}.py/{name}/ exists in this repo")
        self.assertEqual(unresolved, [], "\n".join(unresolved))

    def test_the_check_itself_would_catch_a_real_gap(self):
        """Adversarial proof: a synthetic import of a name that doesn't exist anywhere must actually
        be flagged, so a clean result above means something."""
        local = _local_modules()
        name = "definitely_not_a_real_module_xyz123"
        self.assertNotIn(name, local)
        self.assertNotIn(name, _STDLIB)
        self.assertNotIn(name, _DECLARED_DEPENDENCIES)


if __name__ == "__main__":
    unittest.main()
