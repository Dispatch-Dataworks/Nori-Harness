# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Launcher shim: runs server.py in THIS process (same PID, so nori_ctl.ps1's
pidfile logic is unchanged) and records any crash to .nori.boot.err.

Why it exists (2026-09-18): server.py only redirects stdout/stderr to
.nori.log/.nori.err at the top of main(), AFTER every import. A crash
before that -- any import-time failure, the live-data guard in store.py
refusing to open a data directory -- printed its traceback to the hidden
launch process's own stderr, i.e. nowhere. After a hard power loss both
apps' launches died within seconds and left no trace anywhere a person
could read, so "why did it die" was unanswerable. nori_ctl.ps1 reads this
file (only what was written since ITS launch) into its own outcome log.

Deliberately tiny and dependency-free: it has to work when everything else
is what's broken.
"""
import os
import runpy
import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
BOOT_ERR = HERE / ".nori.boot.err"


def _note(msg: str) -> None:
    try:
        with BOOT_ERR.open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} pid={os.getpid()} {msg}\n")
        if BOOT_ERR.stat().st_size > 200_000:
            tail = BOOT_ERR.read_text(encoding="utf-8", errors="replace")[-100_000:]
            BOOT_ERR.write_text(tail, encoding="utf-8")
    except OSError:
        pass  # this must never be the thing that fails


if __name__ == "__main__":
    try:
        runpy.run_path(str(HERE / "server.py"), run_name="__main__")
    except SystemExit as exc:
        if exc.code not in (0, None):
            _note(f"server.py exited with code {exc.code!r}")
        raise
    except BaseException:
        _note("server.py crashed:\n" + traceback.format_exc())
        raise
