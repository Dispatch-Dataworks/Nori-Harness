#!/usr/bin/env python3
"""One-off: generate a thumbnail for every existing image under
data/generated/ that doesn't already have one -- the backfill half of
the /photos speed fix (2026-09-15, operator's own ask: a thumbnail
system that only covers new images doesn't fix the page he's
complaining about, since every photo already in his real history is
exactly what made that page slow).

Purely additive: only ever WRITES new files under data/generated/thumbs/
(via imagegen.generate_thumbnail(), the exact same function
image_thumb_get's on-demand path calls -- one code path, not a second
one that could drift). Never touches the database, never touches an
original. Safe to re-run any time, including right after this same run
-- every file it's already made is skipped in one is_file() check
(generate_thumbnail()'s own first line), so a second pass costs a stat
call per photo, not a re-encode.

Usage (from the repo root or this directory):

    NORI_LIVE=1 python3 nori/scripts/backfill_thumbnails.py
    NORI_DATA_DIR=/path/to/throwaway/data python3 nori/scripts/backfill_thumbnails.py

Same store.py guard every other one-off nori script refuses to run
without -- see store.py's own error text if neither is set.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent   # scripts/
NORI_DIR = HERE.parent                    # repo root
REPO_ROOT = NORI_DIR
sys.path.insert(0, str(NORI_DIR))


def _load_env(path: Path) -> None:
    """Same reasoning as the operator's own snapshot tooling -- imagegen's
    import chain reaches modules that read real secrets as module-level
    constants even though nothing this script calls needs them."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env(REPO_ROOT.parent / (REPO_ROOT.name + "-env") / "nori.env")

import imagegen  # noqa: E402 -- import triggers the NORI_LIVE/NORI_DATA_DIR guard (via store)


def main() -> int:
    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        print("Pillow isn't installed for this interpreter -- nothing to backfill with.\n"
             "See imagegen.generate_thumbnail()'s own docstring for why this app doesn't "
             "hard-depend on it.", file=sys.stderr)
        return 1

    # iterdir(), not rglob() -- GEN_DIR's own direct children are the real
    # originals; is_file() alone already excludes the thumbs/ subdirectory
    # (a directory, not a file) without needing to name it specifically.
    candidates = sorted(p for p in imagegen.GEN_DIR.iterdir() if p.is_file())
    made = skipped_already = failed = 0
    t0 = time.time()
    for p in candidates:
        file_id = p.name
        existed = imagegen.thumb_path(file_id).is_file()
        ok = imagegen.generate_thumbnail(file_id)
        if not ok:
            failed += 1
            print(f"  FAILED: {file_id} (unreadable/corrupt/unsupported -- will fall back "
                 f"to the original at serve time, same as any future failure)")
        elif existed:
            skipped_already += 1
        else:
            made += 1
    elapsed = time.time() - t0
    print(f"\n{len(candidates)} real photo(s) under {imagegen.GEN_DIR}")
    print(f"  {made} thumbnail(s) generated just now")
    print(f"  {skipped_already} already had one (untouched)")
    print(f"  {failed} failed -- original will be served in their place, always")
    print(f"  took {elapsed:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
