# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Per-user working folder -- files and folders a user can create, and Nori
can list/read/write/move/delete. Built from the operator's own brief and
the proposal it was based on.

The only module with raw SQL against `work_files`. That table tracks
exactly the two things a bare filesystem can't tell you -- who created a
file (`created_by`, 'user' or 'nori' -- never trusted from the model,
only ever set by the one write path below) and a cached vision caption
for an image, so a captioning call runs once, not once per read.
Everything else (size, whether something's a folder, mtime) is read live
off disk on every call, same philosophy /admin/avatars already uses: the
filesystem is the ground truth, not a counter that can drift. Folders get
no row at all -- nothing about "who made this empty folder" needs
remembering, since deleting an empty folder destroys no content
regardless of who made it (see delete_file).

── Path safety -- the actual point of this module ──────────────────────
_resolve() is the ONE containment chokepoint. Every operation below calls
it and nothing else in this module (or anywhere else) ever builds a path
onto disk from user- or model-supplied input. It rejects absolute paths
and drive letters BEFORE ever joining them (Path(root) / "/etc/passwd" or
Path(root) / "C:\\Windows" silently REPLACES the root during a naive
join -- that has to be caught first, not after), then resolves the final
candidate against the real filesystem (collapsing every ../, following
any symlink in the chain) and checks that the resolved path still sits
inside the resolved root. That containment check is what actually holds
against traversal, absolute paths, and a symlink dropped into the tree by
something outside the app -- never a string-filter on the input, which is
exactly the kind of check that gets bypassed.

── Case-insensitivity, on purpose, on every platform ────────────────────
The operator asked for name comparison to be case-insensitive regardless
of platform -- Windows already behaves this way on disk, Linux (where
this runs once containerized) does not. _resolve() walks one path segment
at a time and snaps to an existing case-insensitive match at every level,
not just the final name, so the app's own behavior is identical on both --
this app's logic decides collisions, not whichever filesystem happens to
be underneath it today.

── The provenance rule -- what the TOOL can and can't touch ─────────────
delete_file only ever deletes a file `nori` created; write_file only ever
overwrites a file `nori` created (creating a brand-new file is always
fine). A user-created file is untouchable by either operation via the
tool path -- she can *say* a file should go, but removing or overwriting
something the operator placed is a human action, done through the web UI,
which has no such restriction (it's the operator's own data). Deleting an
EMPTY folder is allowed regardless of provenance -- there's nothing inside
to lose, and no recursive folder delete exists anywhere in this module.

── Untrusted content, reusing the reader/actor split exactly ────────────
read_file's text path hands the raw content to ingest.summarize_untrusted()
-- the SAME function triage_email already uses, not a second
implementation -- before anything reaches a tool-capable context. An
image's cached vision caption gets the same treatment: a photo can
contain rendered text just as capable of smuggling an instruction as an
email body is, so the caption is ingested too, every read (cheap -- no
image tokens spent again), even though the caption itself is cached.
"""
from __future__ import annotations

import os
import re
import shutil
import time
import unicodedata
import uuid
from pathlib import Path

import config
import ingest
import store

# FUNCTIONS, not module constants (2026-09-23: store.DATA_DIR is now re-resolved live on every access -- freezing
# its value into a plain attribute here at import time would defeat that fix; see store.py's own comment).
def _workfiles_dir() -> Path:
    return store.DATA_DIR / "workfiles"


def _quarantine_dir() -> Path:
    # Download quarantine staging -- deliberately OUTSIDE WORKFILES_DIR entirely (2026-09-15, see
    # download_image()'s own docstring for why): a path under here is structurally unreachable through _resolve(),
    # not merely unlisted, so nothing any other tool does can read a fetched file before its scan has passed.
    return store.DATA_DIR / "quarantine"


# External callers (server.py, tests) keep writing workfiles.WORKFILES_DIR/QUARANTINE_DIR unchanged -- __getattr__
# resolves each fresh on every access.
_MODULE_PATHS = {"WORKFILES_DIR": _workfiles_dir, "QUARANTINE_DIR": _quarantine_dir}


def __getattr__(name: str):
    if name in _MODULE_PATHS:
        return _MODULE_PATHS[name]()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

# Where a web-sourced file lands once it passes scanning -- a separate
# subfolder of the working folder, never mixed with what the user
# placed directly or what she wrote/generated herself (operator's own
# ask, 2026-09-15).
DOWNLOADS_SUBFOLDER = "downloads"

# All operator policy knobs, like NORI_TOOL_RATE_LIMIT -- not per-user
# preferences, so plain env constants rather than config.py settings.
MAX_FILE_MB = float(os.environ.get("NORI_WORKFILE_MAX_FILE_MB", "25"))
MAX_USER_MB = float(os.environ.get("NORI_WORKFILE_MAX_USER_MB", "250"))
MAX_USER_COUNT = int(os.environ.get("NORI_WORKFILE_MAX_USER_COUNT", "500"))
MAX_TOTAL_MB = float(os.environ.get("NORI_WORKFILE_MAX_TOTAL_MB", "5000"))

# Reserved on Windows -- treated as reserved on every platform this runs
# on, so a folder created on Linux doesn't break if the operator ever
# moves the volume to a Windows host, or vice versa.
_RESERVED_NAMES = {"con", "prn", "aux", "nul",
                  *(f"com{d}" for d in "123456789"), *(f"lpt{d}" for d in "123456789")}
_BAD_CHARS = set('\\/:*?"<>|')

_IMAGE_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
              ".gif": "image/gif", ".webp": "image/webp"}

# Real byte-signature sniffing for the download tool's own allowlist
# (2026-09-15) -- never trusting the URL's extension or the server's
# claimed Content-Type, same reasoning as everywhere else this app
# validates a file by its actual bytes. Keyed to the same extensions
# _IMAGE_MIME already knows, so a sniffed file lands under a real,
# recognized extension rather than whatever the model asked for.
_IMAGE_SIGS = {b"\xff\xd8\xff": ".jpg", b"\x89PNG\r\n\x1a\n": ".png",
              b"GIF87a": ".gif", b"GIF89a": ".gif", b"RIFF": ".webp"}


def _sniff_image_ext(head: bytes) -> str | None:
    for sig, ext in _IMAGE_SIGS.items():
        if head.startswith(sig):
            if sig == b"RIFF":
                return ".webp" if head[8:12] == b"WEBP" else None
            return ext
    return None

READ_TEXT_MAX_CHARS = 200_000  # capped before the ingest pass ever sees it (summary mode only -- see read_file)
# One page of a raw read (2026-10-02). Raw reads used to stop dead at the
# first 4,000 characters of a file with no way to continue -- a sub-agent
# "granted exact access" to a chapter saw its first screenful and nothing
# else. Now an offset walks the whole file; this is how much one call
# returns. Bigger than ingest.PRESERVE_CAP on purpose (that cap exists for
# a different caller's single-shot reads): fewer calls per file, against a
# per-job tool-call budget. Each page is screened on its own.
RAW_PAGE_CHARS = int(os.environ.get("NORI_RAW_PAGE_CHARS", "8000"))


class WorkfileError(Exception):
    pass


def _validate_name(name: str) -> str:
    name = unicodedata.normalize("NFC", name or "")
    if not name or name in (".", ".."):
        raise WorkfileError("invalid name")
    if len(name) > 200:
        raise WorkfileError("name too long")
    if any(c in _BAD_CHARS for c in name) or any(ord(c) < 32 for c in name):
        raise WorkfileError('names can\'t contain \\ / : * ? " < > | or control characters')
    if name != name.rstrip(". "):
        raise WorkfileError("name can't end in a dot or space")
    if name.split(".")[0].lower() in _RESERVED_NAMES:
        raise WorkfileError(f"{name!r} is a reserved name on some platforms -- pick another")
    return name


def _existing_case(parent: Path, name: str) -> str:
    """If parent already has an entry matching `name` case-insensitively,
    return ITS real on-disk casing; otherwise return `name` unchanged (this
    is the name a brand-new entry would get). Enforced at every path
    segment by _resolve(), not just the final one."""
    if not parent.is_dir():
        return name
    low = name.lower()
    for entry in os.listdir(parent):
        if entry.lower() == low:
            return entry
    return name


def _user_root(user_id: int) -> Path:
    root = _workfiles_dir() / str(user_id)
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def _resolve(user_id: int, rel_path: str) -> Path:
    """The one containment chokepoint -- see module docstring. Returns the
    real, resolved path; raises WorkfileError for anything unsafe. Does
    NOT require the path to exist -- callers check that themselves."""
    rel_path = (rel_path or "").strip().replace("\\", "/")
    if os.path.isabs(rel_path) or re.match(r"^[A-Za-z]:", rel_path):
        raise WorkfileError("invalid path")
    root = _user_root(user_id)
    parts = [p for p in rel_path.split("/") if p not in ("", ".")]
    if ".." in parts:
        raise WorkfileError("invalid path")
    cur = root
    for part in parts:
        real = _existing_case(cur, part)
        if (cur / real).exists():
            part = real  # snap to whatever's already there, regardless of requested case
        else:
            part = _validate_name(part)  # a genuinely new segment -- validate its name
        cur = cur / part
    final = cur.resolve()
    if final != root and root not in final.parents:
        raise WorkfileError("path escapes the working folder")
    return final


def _rel_key(user_id: int, path: Path) -> str:
    root = _user_root(user_id)
    if path == root:
        return ""
    return str(path.relative_to(root)).replace(os.sep, "/")


def _dir_usage(path: Path) -> tuple[int, int]:
    """(total_bytes, entry_count) -- walked fresh off disk every call, on
    purpose. See module docstring: no maintained counter that can drift."""
    if not path.is_dir():
        return 0, 0
    total = count = 0
    for dirpath, dirnames, filenames in os.walk(path):
        count += len(dirnames) + len(filenames)
        for f in filenames:
            try:
                total += (Path(dirpath) / f).stat().st_size
            except OSError:
                pass
    return total, count


def _user_usage_mb(user_id: int) -> tuple[float, int]:
    total, count = _dir_usage(_user_root(user_id))
    return total / (1024 * 1024), count


def _system_usage_mb() -> float:
    total, _ = _dir_usage(_workfiles_dir())
    return total / (1024 * 1024)


def _check_quota(user_id: int, added_bytes: int, *, new_entry: bool, extra_entries: int = 0) -> str | None:
    added_mb = added_bytes / (1024 * 1024)
    if added_mb > MAX_FILE_MB:
        return f"that file is over the {MAX_FILE_MB:.0f} MB per-file limit"
    used_mb, count = _user_usage_mb(user_id)
    if used_mb + added_mb > MAX_USER_MB:
        return f"this would exceed your {MAX_USER_MB:.0f} MB working-folder limit"
    if (new_entry or extra_entries) and count + extra_entries + (1 if new_entry else 0) > MAX_USER_COUNT:
        return f"you're at the {MAX_USER_COUNT}-item limit for the working folder"
    if _system_usage_mb() + added_mb > MAX_TOTAL_MB:
        return ("this instance's total working-folder storage limit has been reached -- "
               "ask the operator to free up space")
    return None


# ── metadata (work_files: file rows only, never folders) ────────────────
def _meta_row(user_id: int, rel: str) -> dict | None:
    row = store.read(lambda c: c.execute(
        "SELECT * FROM work_files WHERE user_id=? AND rel_path=?", (user_id, rel)).fetchone())
    return dict(row) if row else None


def _meta_all(user_id: int) -> dict[str, dict]:
    rows = store.read(lambda c: c.execute(
        "SELECT * FROM work_files WHERE user_id=?", (user_id,)).fetchall())
    return {r["rel_path"]: dict(r) for r in rows}


def _meta_upsert(user_id: int, rel: str, *, created_by: str, source_url: str | None = None) -> None:
    now = time.time()
    def _w(c):
        row = c.execute("SELECT id, created_by, created_ts FROM work_files WHERE user_id=? AND rel_path=?",
                        (user_id, rel)).fetchone()
        if row:
            if source_url is not None:
                c.execute("UPDATE work_files SET updated_ts=?, source_url=? WHERE id=?", (now, source_url, row["id"]))
            else:
                c.execute("UPDATE work_files SET updated_ts=? WHERE id=?", (now, row["id"]))
        else:
            c.execute("INSERT INTO work_files(user_id, rel_path, created_by, created_ts, updated_ts, source_url) "
                     "VALUES (?,?,?,?,?,?)", (user_id, rel, created_by, now, now, source_url))
    store.write(_w)


def _meta_delete(user_id: int, rel: str) -> None:
    store.write(lambda c: c.execute(
        "DELETE FROM work_files WHERE user_id=? AND rel_path=?", (user_id, rel)))


def _meta_rename(user_id: int, old_rel: str, new_rel: str) -> None:
    """Single-file rename, or a folder move cascading over every
    descendant file row it contains (folders themselves have no row)."""
    def _w(c):
        c.execute("UPDATE work_files SET rel_path=? WHERE user_id=? AND rel_path=?",
                 (new_rel, user_id, old_rel))
        c.execute("UPDATE work_files SET rel_path = ? || substr(rel_path, ?) "
                 "WHERE user_id=? AND rel_path LIKE ?",
                 (new_rel, len(old_rel) + 1, user_id, old_rel + "/%"))
    store.write(_w)


# ── core operations ──────────────────────────────────────────────────────
def resolve_for_download(session: dict, path: str) -> Path | None:
    """The one path server.py's download endpoint is allowed to touch --
    goes through the same _resolve() chokepoint as everything else, never
    a raw join in server.py itself. None if there's nothing safe to serve."""
    try:
        target = _resolve(session["user_id"], path)
    except WorkfileError:
        return None
    return target if target.is_file() else None


# ── in-browser preview (2026-10-02, operator's own ask: "preview documents
# (txt, md, yml, json etc), as well as images and videos") ────────────────
# Stored files are untrusted (see files_download's forced-attachment note
# in server.py), so previewing is deliberately NOT "serve it inline under
# its own content-type". Three narrow, separately-validated paths instead:
#   - text: read here, decoded here, handed back as a plain string for the
#     page to HTML-escape into a <pre>. Never served raw, so an .html or
#     .svg file previews as its source text and can't execute.
#   - image: only png/jpeg/gif/webp, and only if the REAL leading bytes
#     say so (SVG is excluded on purpose -- it can carry script).
#   - video: only mp4/m4v/mov/webm/ogv, and only if the real container
#     signature matches the extension.
# The Content-Type for image/video always comes from what was verified
# here, never from the filename alone.
PREVIEW_TEXT_MAX_BYTES = 512 * 1024

# Cheap, name-only: decides whether the LISTING offers a preview link. The
# preview itself re-validates against the real bytes (preview_info), and
# will also sniff text for names not on this list.
_TEXT_EXTS = frozenset({
    ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv", ".json", ".jsonl", ".ndjson",
    ".yml", ".yaml", ".toml", ".ini", ".cfg", ".conf", ".env", ".properties", ".xml", ".svg",
    ".html", ".htm", ".css", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".py", ".sh", ".bash", ".zsh",
    ".ps1", ".bat", ".cmd", ".sql", ".tex", ".bib", ".java", ".c", ".h", ".cpp", ".hpp", ".cs",
    ".go", ".rs", ".rb", ".php", ".pl", ".lua", ".swift", ".kt", ".r", ".diff", ".patch",
    ".gitignore", ".editorconfig", ".lock"})
_TEXT_NAMES = frozenset({"readme", "license", "licence", "dockerfile", "makefile", "changelog",
                         "notice", "authors", "procfile", "gemfile", "rakefile"})
_VIDEO_MIME = {".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime",
               ".webm": "video/webm", ".ogv": "video/ogg"}


def preview_kind_for_name(name: str) -> str | None:
    """"image" / "video" / "text" by filename alone, or None (no preview
    link offered). A hint for the listing only -- never a security
    decision; see preview_info."""
    low = name.lower()
    ext = os.path.splitext(low)[1]
    if ext in _IMAGE_MIME:
        return "image"
    if ext in _VIDEO_MIME:
        return "video"
    # `low in _TEXT_EXTS` covers dotfiles: splitext(".gitignore") is
    # (".gitignore", ""), i.e. no extension at all.
    if ext in _TEXT_EXTS or low in _TEXT_EXTS or low in _TEXT_NAMES:
        return "text"
    return None


def _video_signature_ok(head: bytes, ext: str) -> bool:
    if ext in (".mp4", ".m4v", ".mov"):
        return head[4:8] == b"ftyp"
    if ext == ".webm":
        return head.startswith(b"\x1a\x45\xdf\xa3")
    if ext == ".ogv":
        return head.startswith(b"OggS")
    return False


def preview_info(session: dict, path: str) -> dict:
    """The one decision point for what a file may be previewed AS, made
    from its real bytes. Returns {"kind": "image"|"video"|"text"|"binary",
    "name", "path", "size", "mime", "file"} or {"error"}. "file" is the
    already-contained, resolved Path -- the only thing the raw-serving
    route is allowed to stream, so it can't be pointed at anything this
    function didn't classify."""
    user_id = session["user_id"]
    try:
        target = _resolve(user_id, path)
    except WorkfileError as exc:
        return {"error": str(exc)}
    if not target.exists():
        return {"error": "no such file"}
    if not target.is_file():
        return {"error": "that's a folder, not a file"}
    try:
        with open(target, "rb") as f:
            head = f.read(8192)
        size = target.stat().st_size
    except OSError:
        return {"error": "couldn't read that file"}
    ext = os.path.splitext(target.name.lower())[1]
    base = {"name": target.name, "path": _rel_key(user_id, target), "size": size, "file": target}

    img_ext = _sniff_image_ext(head)
    if img_ext is not None and ext in _IMAGE_MIME:
        return {**base, "kind": "image", "mime": _IMAGE_MIME[img_ext]}
    if ext in _VIDEO_MIME and _video_signature_ok(head, ext):
        return {**base, "kind": "video", "mime": _VIDEO_MIME[ext]}
    if ext in _IMAGE_MIME or ext in _VIDEO_MIME:
        return {**base, "kind": "binary", "mime": "application/octet-stream"}  # name lies about the bytes
    if b"\x00" in head:
        return {**base, "kind": "binary", "mime": "application/octet-stream"}
    return {**base, "kind": "text", "mime": "text/plain"}


def preview_text(session: dict, path: str) -> dict:
    """preview_info plus, for a text file, its decoded content (capped at
    PREVIEW_TEXT_MAX_BYTES; `truncated` says plainly when it was). The
    caller must HTML-escape it -- this returns a plain string, not
    markup."""
    info = preview_info(session, path)
    if "error" in info or info["kind"] != "text":
        return info
    with open(info["file"], "rb") as f:
        raw = f.read(PREVIEW_TEXT_MAX_BYTES + 1)
    truncated = len(raw) > PREVIEW_TEXT_MAX_BYTES
    text = raw[:PREVIEW_TEXT_MAX_BYTES].decode("utf-8-sig", errors="replace")
    return {**info, "text": text, "truncated": truncated}


LIST_MAX_RECURSIVE_ENTRIES = 1000  # backstop against dumping a huge tree in one call


def _entry(user_id: int, p: Path, meta: dict[str, dict]) -> dict:
    rel = _rel_key(user_id, p)
    is_dir = p.is_dir()
    row = meta.get(rel)
    return {"name": p.name, "path": rel, "kind": "folder" if is_dir else "file",
            "size_bytes": 0 if is_dir else p.stat().st_size,
            "created_by": None if is_dir else (row["created_by"] if row else "user"),
            "modified_ts": p.stat().st_mtime}


def list_files(session: dict, path: str = "", *, recursive: bool = False) -> dict:
    """Single-level by default -- one call, immediate children only, same
    as an ordinary file browser. recursive=True (2026-09-15, operator's
    own ask, after single-level-only made a several-folders-deep file
    genuinely hard to discover -- not impossible, since passing `path`
    already lets a caller descend one level at a time, but impractical:
    finding one file nested under an unfamiliar 4-level, 280-file tree by
    listing one folder at a time is exactly what looked, from the
    outside, like "she can't see nested files") walks the WHOLE subtree
    in one call instead, still just names/kinds/sizes -- no content, so
    this carries none of read_file's screening obligations. Bounded by
    LIST_MAX_RECURSIVE_ENTRIES so a huge tree can't flood one tool
    result; `hit_cap` says plainly when it did, same shape as
    search_files' own cap."""
    user_id = session["user_id"]
    try:
        target = _resolve(user_id, path)
    except WorkfileError as exc:
        return {"error": str(exc)}
    if not target.exists():
        return {"error": "no such folder"}
    if not target.is_dir():
        return {"error": "that's a file, not a folder"}
    meta = _meta_all(user_id)
    used_mb, count = _user_usage_mb(user_id)
    base = {"path": _rel_key(user_id, target), "usage_mb": round(used_mb, 2),
            "item_count": count, "limit_mb": MAX_USER_MB, "limit_count": MAX_USER_COUNT}

    if not recursive:
        entries = [_entry(user_id, target / name, meta) for name in sorted(os.listdir(target), key=str.lower)]
        return {**base, "entries": entries, "recursive": False}

    entries = []
    hit_cap = False
    for dirpath, dirnames, filenames in os.walk(target):  # followlinks=False by default -- no escape via symlink
        dirnames.sort(key=str.lower)
        names = sorted(dirnames, key=str.lower) + sorted(filenames, key=str.lower)
        for name in names:
            if len(entries) >= LIST_MAX_RECURSIVE_ENTRIES:
                hit_cap = True
                break
            entries.append(_entry(user_id, Path(dirpath) / name, meta))
        if hit_cap:
            break
    entries.sort(key=lambda e: e["path"].lower())
    return {**base, "entries": entries, "recursive": True, "hit_cap": hit_cap}


def _raw_page_end(text: str, start: int) -> int:
    """Where the page beginning at `start` ends: RAW_PAGE_CHARS later, pulled
    back to the last whitespace in its final fifth so a page doesn't end
    mid-word, never shorter than that fifth. Contiguous by construction --
    the next page starts exactly where this one ends, with nothing
    trimmed or skipped between them."""
    hard = min(len(text), start + RAW_PAGE_CHARS)
    if hard >= len(text):
        return len(text)
    floor = start + (RAW_PAGE_CHARS * 4) // 5
    cut = max(text.rfind(chr(10), floor, hard), text.rfind(" ", floor, hard))
    return cut + 1 if cut >= floor else hard


def read_file(session: dict, path: str, *, preserve_content: bool = False, offset: int = 0) -> dict:
    """preserve_content=False (the default, and the ONLY mode the
    registered `read_file` tool below ever calls this with) is the
    documented promise that tool's schema makes: "you get back a summary,
    never the raw content directly." That's a deliberate containment
    boundary, not an oversight -- read_file's content is untrusted the
    same way an email is, and a live turn has real tools (peer_send,
    email, the works) an injected instruction could try to reach through.

    offset (2026-10-02) only means anything with preserve_content=True: the
    character position to start the page at. The result carries
    total_chars, offset and next_offset (None on the last page), and
    `truncated` is True exactly when there is more after this page -- so
    a caller walks a file by passing next_offset back until it's None.
    offset == total_chars is a valid empty final page; past it is an
    error. A page whose screening call failed is an error too, not
    "content": it must be retried, and never counted as read.

    preserve_content=True is the deliberate, narrow exception (2026-09-14,
    operator's own ask, after a security-review sub-agent kept stalling
    on nothing but ~200-character gists of a 268KB file): real text back,
    still screened for a suspicious-instruction flag, still capped
    (PRESERVE_CAP chars, `truncated` says when there was more). ONLY
    jobs.py's sub-agent dispatch path calls this with preserve_content=True,
    and only when a specific job was explicitly granted raw_file_access --
    never from the tools._REGISTRY-registered lambda below, so a live turn
    can never reach this mode no matter what a model asks for. See
    jobs.py's own module docstring for why that path's risk profile is
    different (a sandboxed, read-only, side-effect-free sub-agent whose
    own output still gets screened again before it ever reaches a turn
    that DOES have real tools)."""
    user_id = session["user_id"]
    try:
        target = _resolve(user_id, path)
    except WorkfileError as exc:
        return {"error": str(exc)}
    if not target.exists() or target.is_dir():
        return {"error": "no such file"}
    size = target.stat().st_size
    if size > MAX_FILE_MB * 1024 * 1024:
        return {"error": f"file is over the {MAX_FILE_MB:.0f} MB read limit -- download it instead"}
    rel = _rel_key(user_id, target)
    ext = target.suffix.lower()

    if ext in _IMAGE_MIME:
        caption = _get_or_caption_image(user_id, rel, target, _IMAGE_MIME[ext])
        if caption is None:
            return {"kind": "image",
                    "message": "vision is off for this account -- enable it on the files page "
                              "to let her look at images"}
        ingested = ingest.summarize_untrusted(caption, kind="image description")
        return {"kind": "image", **ingested}

    raw = target.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return {"kind": "binary", "message": "no text extraction available for this file type yet "
                                             "-- download it to view it"}
    if not preserve_content:
        ingested = ingest.summarize_untrusted(text[:READ_TEXT_MAX_CHARS], kind="file")
        return {"kind": "text", "path": rel, "total_chars": len(text), **ingested}

    total = len(text)
    try:
        offset = int(offset)
    except (TypeError, ValueError):
        return {"error": "offset must be a whole number of characters"}
    if offset < 0:
        return {"error": "offset can't be negative"}
    if offset > total:
        return {"error": f"offset {offset} is past the end of the file ({total} characters)"}
    end = _raw_page_end(text, offset)
    page = text[offset:end]
    # Only the page goes to the screening model (the old path sent up to
    # 200K characters through it to return 4,000). The content returned is
    # that page verbatim, deterministically -- never the model's words.
    ingested = ingest.summarize_untrusted(page, kind="file", preserve_content=True) if page else {
        "category": "informational", "priority": "normal", "suggested_action": "", "suspicious": False}
    if ingested.get("screening_failed"):
        return {"error": "couldn't screen this page right now -- retry the same offset",
                "offset": offset, "total_chars": total}
    more = end < total
    return {"kind": "text", **ingested, "content": page, "path": rel, "offset": offset, "total_chars": total,
            "next_offset": end if more else None, "truncated": more}


# ── search (2026-09-14, operator's own ask) ──────────────────────────────
# Grep-shaped, scoped to the working folder: a term or regex in, matching
# file paths + line numbers + a few lines of real surrounding text out.
# Built because read_file's summary-only shape makes finding one specific
# thing in a large codebase impractical -- nine calls each returning a
# ~200-character gist of a whole file never gets near a token-validation
# check buried in a 268KB file. General availability (registered as an
# ordinary tool below, not sub-agent-only) on purpose: a handful of
# targeted lines is a materially smaller reveal than a whole file, and
# she asked for exactly this herself while diagnosing the stall.
#
# Matched lines are real file content -- untrusted the same way a
# read_file result is. Screening every match separately would mean a real
# model call per match (up to SEARCH_MAX_MATCHES of them); instead the
# WHOLE assembled result goes through the same ingest.summarize_untrusted()
# pass read_file uses, once per search call, in preserve_content mode --
# real text back, still capped, still flagged if anything in the batch
# looks like an injection attempt. SEARCH_MAX_MATCHES is sized so the
# assembled blob comfortably fits under PRESERVE_CAP in the ordinary
# case; `truncated` says plainly when it didn't.
SEARCH_MAX_MATCHES = 12
SEARCH_CONTEXT_LINES = 2
SEARCH_MAX_FILES_SCANNED = 2000  # backstop; MAX_USER_COUNT already keeps real usage well under


def search_files(session: dict, pattern: str, path: str = "") -> dict:
    user_id = session["user_id"]
    pattern = (pattern or "").strip()
    if not pattern:
        return {"error": "pattern can't be empty"}
    try:
        root = _resolve(user_id, path)
    except WorkfileError as exc:
        return {"error": str(exc)}
    if not root.exists():
        return {"error": "no such folder"}
    if not root.is_dir():
        return {"error": "that's a file, not a folder -- pass its parent folder as path"}
    try:
        rx = re.compile(pattern, re.IGNORECASE)
    except re.error:
        rx = re.compile(re.escape(pattern), re.IGNORECASE)  # not a valid regex -- search for it literally

    raw_matches: list[tuple[str, int, str]] = []
    files_scanned = 0
    for dirpath, _dirnames, filenames in os.walk(root):  # followlinks=False by default -- no escape via a symlink
        for name in sorted(filenames, key=str.lower):
            if len(raw_matches) >= SEARCH_MAX_MATCHES or files_scanned >= SEARCH_MAX_FILES_SCANNED:
                break
            fp = Path(dirpath) / name
            if fp.suffix.lower() in _IMAGE_MIME:
                continue
            try:
                if fp.stat().st_size > MAX_FILE_MB * 1024 * 1024:
                    continue
                lines = fp.read_bytes().decode("utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            files_scanned += 1
            for i, line in enumerate(lines):
                if len(raw_matches) >= SEARCH_MAX_MATCHES:
                    break
                if rx.search(line):
                    lo, hi = max(0, i - SEARCH_CONTEXT_LINES), min(len(lines), i + SEARCH_CONTEXT_LINES + 1)
                    raw_matches.append((_rel_key(user_id, fp), i + 1, "\n".join(lines[lo:hi])))
        if len(raw_matches) >= SEARCH_MAX_MATCHES or files_scanned >= SEARCH_MAX_FILES_SCANNED:
            break

    if not raw_matches:
        return {"kind": "search", "match_count": 0, "hit_cap": False, "suspicious": False,
                "content": "no matches"}

    blob = "\n---\n".join(f"{p}:{ln}\n{snippet}" for p, ln, snippet in raw_matches)
    ingested = ingest.summarize_untrusted(blob, kind="file search results", preserve_content=True)
    return {"kind": "search", "match_count": len(raw_matches),
            "hit_cap": len(raw_matches) >= SEARCH_MAX_MATCHES,
            "suspicious": ingested.get("suspicious", False),
            "truncated": ingested.get("truncated", False),
            "content": ingested.get("content") or ingested.get("summary") or "(screening returned nothing)"}


def read_image_bytes(session: dict, path: str) -> dict:
    """Raw image bytes from the user's own working folder, for use as an
    image-GENERATION reference (2026-09-14, imagine_image's own
    working-folder option) -- deliberately NOT read_file's text path.
    read_file screens/summarizes because its content might reach a
    tool-calling model's own context as language; an image used purely as
    a visual anchor for another image call is never read as language, only
    handed to the image API as pixels, so that screening doesn't apply
    here. Still fully contained by the same _resolve() and MAX_FILE_MB
    cap as every other read in this module -- nothing about the
    containment story changes, only what happens to the bytes afterward."""
    user_id = session["user_id"]
    try:
        target = _resolve(user_id, path)
    except WorkfileError as exc:
        return {"error": str(exc)}
    if not target.exists() or target.is_dir():
        return {"error": "no such file"}
    ext = target.suffix.lower()
    if ext not in _IMAGE_MIME:
        return {"error": "that's not an image file (png/jpg/jpeg/gif/webp)"}
    size = target.stat().st_size
    if size > MAX_FILE_MB * 1024 * 1024:
        return {"error": f"file is over the {MAX_FILE_MB:.0f} MB limit"}
    return {"ok": True, "bytes": target.read_bytes(), "mime": _IMAGE_MIME[ext]}


DOWNLOAD_MAX_BYTES = int(MAX_FILE_MB * 1024 * 1024)


def download_image(session: dict, url: str, filename: str) -> dict:
    """Fetch an image from the web and save it into the user's own
    downloads/ folder -- a separate area, distinct from what he placed
    directly or what she wrote/generated herself (operator's own ask,
    2026-09-15). This is what makes imagine_image's working-folder
    reference actually reachable when the reference is a web image:
    download it here first (landing at "downloads/<name>"), then point
    imagine_image at that path.

    The race this closes (operator's own framing, verbatim reasoning):
    Defender's own passive scanning would eventually catch a bad file,
    but she operates fast enough to download and open one before the
    passive scanner reacts. So the fetched bytes are staged in
    QUARANTINE_DIR -- outside WORKFILES_DIR entirely, structurally
    unreachable through _resolve() and therefore through
    list_files/read_file/search_files/etc, not merely unlisted or
    named oddly -- scanned there with scanner.scan(), and only written
    into downloads/ (through the SAME _write() chokepoint every other
    file in this app goes through) after a clean verdict. Nothing this
    function does is ever readable by any other tool before that.

    Scanner boundary, stated plainly per the operator's own explicit
    instruction: a clean verdict means the bytes don't match a known
    malware signature. It says NOTHING about whether the file's
    CONTENT is safe to trust as instructions or data -- a different
    problem, ingest.py's own reader/actor split, not touched here.
    Not live for an image (never read as language), but stated so it's
    never silently assumed for whatever this is extended to later."""
    import webtools
    import scanner

    user_id = session["user_id"]
    stem = Path((filename or "").strip().replace("\\", "/").rsplit("/", 1)[-1]).stem
    try:
        stem = _validate_name(stem)
    except WorkfileError as exc:
        return {"error": str(exc)}

    fetched = webtools.fetch_binary(url, agent="download_image", max_bytes=DOWNLOAD_MAX_BYTES)
    if not fetched.get("ok"):
        return {"error": fetched.get("reason") or "couldn't fetch that URL"}
    if fetched.get("truncated"):
        return {"error": f"that file is over the {MAX_FILE_MB:.0f} MB download limit"}
    data = fetched["bytes"]

    ext = _sniff_image_ext(data[:16])
    if ext is None:
        webtools.log_download_result(url, ok=False, verdict="rejected", agent="download_image",
                                     detail="not a recognized image format")
        return {"error": "that URL didn't return a recognizable image (jpeg/png/gif/webp)"}

    staging_dir = _quarantine_dir() / str(user_id)
    staging_dir.mkdir(parents=True, exist_ok=True)
    staging_path = staging_dir / f"{uuid.uuid4().hex}{ext}"
    staging_path.write_bytes(data)
    try:
        result = scanner.scan(staging_path)
        if not scanner.allowed(result):
            reason = (f"the file was flagged by the virus scanner ({result.threat})" if result.threat
                      else "the file couldn't be verified safe by the virus scanner and was refused"
                           if result.verdict == "unavailable" else "the file was flagged by the virus scanner")
            webtools.log_download_result(url, ok=False, verdict=result.verdict, agent="download_image",
                                         detail=result.threat or result.detail)
            return {"error": reason, "scan_verdict": result.verdict}

        rel_path = f"{DOWNLOADS_SUBFOLDER}/{stem}{ext}"
        res = _write(user_id, rel_path, data, created_by="nori", source_url=url)
        if "error" in res:
            webtools.log_download_result(url, ok=False, verdict=result.verdict, agent="download_image",
                                         detail=res["error"])
            return res
        webtools.log_download_result(url, ok=True, verdict=result.verdict, agent="download_image",
                                     detail=res["path"])
        return {"ok": True, "path": res["path"], "size_bytes": res["size_bytes"], "scan_verdict": result.verdict}
    finally:
        staging_path.unlink(missing_ok=True)


def _get_or_caption_image(user_id: int, rel: str, path: Path, mime: str) -> str | None:
    row = _meta_row(user_id, rel)
    if row and row.get("vision_description"):
        return row["vision_description"]
    if not config.get("user", user_id, "workfile_vision_enabled"):
        return None
    import chat
    try:
        caption = chat.vision(
            "Describe what's in this image in a few plain, factual sentences -- objects, any "
            "visible text, general composition. Not creative, not speculative.",
            [(path.read_bytes(), mime)])
    except chat.ModelError:
        return None
    now = time.time()
    def _w(c):
        r = c.execute("SELECT id FROM work_files WHERE user_id=? AND rel_path=?", (user_id, rel)).fetchone()
        if r:
            c.execute("UPDATE work_files SET vision_description=?, vision_ts=? WHERE id=?", (caption, now, r["id"]))
        else:
            c.execute("INSERT INTO work_files(user_id, rel_path, created_by, created_ts, updated_ts, "
                     "vision_description, vision_ts) VALUES (?,?,?,?,?,?,?)",
                     (user_id, rel, "user", now, now, caption, now))
    store.write(_w)
    return caption


def _write(user_id: int, rel_path: str, data: bytes, *, created_by: str, human_action: bool = False,
          source_url: str | None = None) -> dict:
    try:
        target = _resolve(user_id, rel_path)
    except WorkfileError as exc:
        return {"error": str(exc)}
    if target.is_dir():
        return {"error": "that's a folder"}
    existed = target.exists()
    if existed and not human_action:
        row = _meta_row(user_id, _rel_key(user_id, target))
        if not row or row["created_by"] != "nori":
            return {"error": "a file already exists there that you didn't create -- "
                             "write to a different name, or ask them to move or delete the original first"}
    added = len(data) - (target.stat().st_size if existed else 0)
    # Parent folders this write would have to create count toward the
    # item limit too (a folder upload makes several) -- they're entries
    # _dir_usage already counts, so not counting them here let a bulk
    # upload sail past MAX_USER_COUNT.
    root = _user_root(user_id)
    missing_dirs, anc = 0, target.parent
    while anc != root and not anc.exists():
        missing_dirs += 1
        anc = anc.parent
    err = _check_quota(user_id, max(added, 0), new_entry=not existed, extra_entries=missing_dirs)
    if err:
        return {"error": err}
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + f".tmp{os.getpid()}")
    tmp.write_bytes(data)
    os.replace(tmp, target)  # atomic on the same volume -- no torn/partial file visible to a reader
    rel = _rel_key(user_id, target)
    _meta_upsert(user_id, rel, created_by=created_by, source_url=source_url)
    return {"ok": True, "path": rel, "size_bytes": len(data), "replaced": existed}


def validate_write_folder(path: str) -> tuple[str | None, str]:
    """Normalize + syntax-check a per-agent write scope (a folder path
    relative to a user's working folder). Returns (normalized, "") or
    (None, error). Purely syntactic -- the folder is relative to whichever
    user dispatches the job, so there's no single real directory to check
    it against when an admin saves it; containment is enforced for real,
    per write, by _check_write_scope. "" (no scope) is valid and means
    anywhere in the working folder."""
    raw = (path or "").strip().replace("\\", "/")
    if not raw.strip("/"):
        return "", ""
    if os.path.isabs(raw) or raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        return None, "write folder must be a relative path inside the working folder"
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if ".." in parts:
        return None, "write folder can't contain .."
    try:
        for part in parts:
            _validate_name(part)
    except WorkfileError as exc:
        return None, f"write folder: {exc}"
    return "/".join(parts), ""


def _check_write_scope(session: dict, target: Path) -> dict | None:
    """session["_write_root"] (set only by jobs.py from a sub-agent's own
    write_folder, never from anything the model can supply) confines
    writes and new folders to that folder. Enforced here, at the same
    chokepoint every write already goes through, rather than in the job
    loop -- a check in only one caller is a check the next caller forgets.
    Returns an error dict to hand straight back to the model, or None."""
    scope = session.get("_write_root")
    if not scope:
        return None
    try:
        root = _resolve(session["user_id"], scope)
    except WorkfileError as exc:
        return {"error": str(exc)}
    if target == root or root in target.parents:
        return None
    return {"error": f"this sub-agent can only write inside {scope}/ -- that path is outside it; "
                     f"save the file under {scope}/ instead",
            "write_folder": scope}


def _next_version_path(user_id: int, target: Path) -> Path | None:
    """notes.txt -> notes.v2.txt, then notes.v3.txt, ... -- the first
    name that doesn't already exist, so a version is NEVER overwritten
    either (an earlier job's v2 stays put; the next write is v3). Returns
    None if a valid name can't be built (e.g. the base name is already at
    the length limit), leaving the caller to fall back to _write's own
    refusal."""
    for n in range(2, 1000):
        name = f"{target.stem}.v{n}{target.suffix}"
        try:
            _validate_name(name)
        except WorkfileError:
            return None
        if _existing_case(target.parent, name) == name and not (target.parent / name).exists():
            return target.parent / name
    return None


def write_file(session: dict, path: str, content: str) -> dict:
    """Tool-facing: she writes a text file. Always attributed to her --
    the provenance rule (write_file can't overwrite a user's file) is
    enforced inside _write for every non-human caller.

    session["_versioned_writes"] (2026-10-02, operator's own ask; set only
    by jobs.py for a sub-agent job, never from anything the model can
    supply -- tool args can't reach the session) changes what happens on
    that refusal: instead of an error, the write lands next to the
    original as name.v2.ext (v3, v4... -- never overwriting an earlier
    version either) and the result says so. Her own live-turn write_file
    is unchanged: it still refuses, with the same error as before."""
    user_id = session["user_id"]
    data = (content or "").encode("utf-8")
    if session.get("_write_root"):
        try:
            scoped_target = _resolve(user_id, path)
        except WorkfileError as exc:
            return {"error": str(exc)}
        refused = _check_write_scope(session, scoped_target)
        if refused:
            return refused
    if session.get("_versioned_writes"):
        try:
            target = _resolve(user_id, path)
        except WorkfileError as exc:
            return {"error": str(exc)}
        if target.is_file():
            row = _meta_row(user_id, _rel_key(user_id, target))
            if not row or row["created_by"] != "nori":
                versioned = _next_version_path(user_id, target)
                if versioned is not None:
                    result = _write(user_id, _rel_key(user_id, versioned), data, created_by="nori")
                    if result.get("ok"):
                        result["versioned_from"] = _rel_key(user_id, target)
                        result["note"] = (f"{result['versioned_from']} already exists and wasn't created by "
                                          f"you, so this was saved as {result['path']} instead -- the "
                                          f"original is untouched.")
                    return result
    return _write(user_id, path, data, created_by="nori")


def upload_file(session: dict, path: str, data: bytes) -> dict:
    """UI-facing: the operator's own upload. human_action=True skips the
    overwrite-protection check -- it's their data, their call, always."""
    return _write(session["user_id"], path, data, created_by="user", human_action=True)


def create_folder(session: dict, path: str) -> dict:
    user_id = session["user_id"]
    try:
        target = _resolve(user_id, path)
    except WorkfileError as exc:
        return {"error": str(exc)}
    refused = _check_write_scope(session, target)
    if refused:
        return refused
    if target.is_file():
        return {"error": "a file already exists there"}
    if target.is_dir():
        return {"ok": True, "path": _rel_key(user_id, target), "already_existed": True}
    _, count = _user_usage_mb(user_id)
    if count + 1 > MAX_USER_COUNT:
        return {"error": f"you're at the {MAX_USER_COUNT}-item limit for the working folder"}
    target.mkdir(parents=True, exist_ok=True)
    return {"ok": True, "path": _rel_key(user_id, target)}


def _meta_delete_tree(user_id: int, rel: str) -> None:
    """Every work_files row under a deleted folder. substr comparison
    rather than LIKE: a folder name can legitimately contain % or _, and
    those would act as wildcards in a LIKE pattern."""
    prefix = rel + "/"
    store.write(lambda c: c.execute(
        "DELETE FROM work_files WHERE user_id=? AND substr(rel_path, 1, ?) = ?",
        (user_id, len(prefix), prefix)))


def folder_stats(session: dict, path: str) -> dict:
    """{"files", "folders", "bytes"} under a folder (not counting the
    folder itself) -- for the files page's delete confirmation, so it can
    say exactly what a recursive delete is about to remove. Never part of
    any model-facing tool."""
    try:
        target = _resolve(session["user_id"], path)
    except WorkfileError:
        return {"files": 0, "folders": 0, "bytes": 0}
    files = folders = size = 0
    if target.is_dir():
        for dirpath, dirnames, filenames in os.walk(target):
            folders += len(dirnames)
            files += len(filenames)
            for f in filenames:
                try:
                    size += (Path(dirpath) / f).stat().st_size
                except OSError:
                    pass
    return {"files": files, "folders": folders, "bytes": size}


def _delete(user_id: int, rel_path: str, *, human_action: bool = False, recursive: bool = False) -> dict:
    try:
        target = _resolve(user_id, rel_path)
    except WorkfileError as exc:
        return {"error": str(exc)}
    if target == _user_root(user_id):
        return {"error": "can't delete the working folder itself"}
    if not target.exists():
        return {"error": "no such file or folder"}
    if target.is_dir():
        if any(target.iterdir()):
            # Recursive delete is a human-only act (the files page's own
            # confirmed button): human_action AND recursive must both be
            # set, so no model-facing tool -- delete_file passes neither --
            # can ever reach the rmtree below, whatever it's asked for.
            if not (human_action and recursive):
                return {"error": "that folder isn't empty"}
            stats = folder_stats({"user_id": user_id}, rel_path)
            rel = _rel_key(user_id, target)
            shutil.rmtree(target)  # symlinks inside are unlinked, never followed
            _meta_delete_tree(user_id, rel)
            return {"ok": True, **stats}
        target.rmdir()
        return {"ok": True}
    rel = _rel_key(user_id, target)
    if not human_action:
        row = _meta_row(user_id, rel)
        if not row or row["created_by"] != "nori":
            return {"error": "that file was placed by them, not you -- "
                             "ask them to delete it, or delete it yourself on the files page"}
    target.unlink()
    _meta_delete(user_id, rel)
    return {"ok": True}


def delete_file(session: dict, path: str) -> dict:
    return _delete(session["user_id"], path)


def delete_file_ui(session: dict, path: str, *, recursive: bool = False) -> dict:
    """The files page's own delete -- human_action skips the provenance
    check. recursive=True (2026-10-02, operator's own ask) additionally
    allows removing a non-empty folder and everything in it; the page
    only sends it from a confirm-dialog'd form that states what's inside."""
    return _delete(session["user_id"], path, human_action=True, recursive=recursive)


def move_file(session: dict, from_path: str, to_path: str) -> dict:
    user_id = session["user_id"]
    try:
        src = _resolve(user_id, from_path)
        dst = _resolve(user_id, to_path)
    except WorkfileError as exc:
        return {"error": str(exc)}
    if not src.exists():
        return {"error": "no such file or folder"}
    if dst.exists() and dst != src:
        return {"error": "something already exists at the destination"}
    old_rel, new_rel = _rel_key(user_id, src), _rel_key(user_id, dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    _meta_rename(user_id, old_rel, new_rel)
    return {"ok": True, "path": new_rel}


# ── tool registration ────────────────────────────────────────────────────
def _register_tools() -> None:
    import tools  # local: same reasoning as every other subsystem module

    tools.register(tools.Tool(
        "list_files",
        {"type": "function", "function": {
            "name": "list_files",
            "description": ("List the user's working folder (or a subfolder within it). By default "
                            "shows only the immediate contents of that one folder -- a 'folder' entry "
                            "means there's more inside it, and you list it again with that entry's own "
                            "path to see what. Set recursive=true to see the WHOLE subtree (every "
                            "nested file and folder) in one call instead of listing level by level -- "
                            "use this when you're looking for a specific file somewhere in a folder "
                            "you don't already know the shape of."),
            "parameters": {"type": "object", "properties": {
                "path": {"type": "string", "description": "subfolder path, or omit for the top level"},
                "recursive": {"type": "boolean", "default": False,
                             "description": "list every file and folder nested underneath, not just "
                                            "the immediate contents"}}}}},
        lambda session, **kw: list_files(session, **kw), min_role="member", data_scope="self", risk_tier="A"))

    tools.register(tools.Tool(
        "read_file",
        {"type": "function", "function": {
            "name": "read_file",
            "description": ("Read a file from the user's working folder. The content is untrusted "
                            "(same as an email) -- you get back a summary, never the raw content "
                            "directly. Images are described, not shown."),
            "parameters": {"type": "object", "properties": {
                "path": {"type": "string"}}, "required": ["path"]}}},
        lambda session, **kw: read_file(session, **kw), min_role="member", data_scope="self", risk_tier="A"))

    tools.register(tools.Tool(
        "search_files",
        {"type": "function", "function": {
            "name": "search_files",
            "description": ("Search text files in the user's working folder (or a subfolder) for a "
                            "term or regex -- returns matching file paths, line numbers, and a few "
                            "lines of real surrounding text, screened the same way read_file's "
                            "content is. Reach for this before read_file when you're looking for "
                            "something specific rather than reading a whole file -- far more useful "
                            "for finding one thing in a large file or across many files."),
            "parameters": {"type": "object", "properties": {
                "pattern": {"type": "string", "description": "text or regex to search for"},
                "path": {"type": "string", "description": "subfolder to search, or omit for the "
                        "whole working folder"}},
                "required": ["pattern"]}}},
        lambda session, **kw: search_files(session, **kw), min_role="member", data_scope="self", risk_tier="A"))

    tools.register(tools.Tool(
        "write_file",
        {"type": "function", "function": {
            "name": "write_file",
            "description": ("Create a text file in the user's working folder, or overwrite one you "
                            "created yourself. Can't overwrite a file the user placed there "
                            "(in some contexts that's saved automatically as name.v2.ext "
                            "beside it instead -- the result says when it was)."),
            "parameters": {"type": "object", "properties": {
                "path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"]}}},
        lambda session, **kw: write_file(session, **kw), min_role="member", data_scope="self", risk_tier="B"))

    tools.register(tools.Tool(
        "create_folder",
        {"type": "function", "function": {
            "name": "create_folder",
            "description": "Create a folder (and any missing parent folders) in the working folder.",
            "parameters": {"type": "object", "properties": {
                "path": {"type": "string"}}, "required": ["path"]}}},
        lambda session, **kw: create_folder(session, **kw), min_role="member", data_scope="self", risk_tier="B"))

    tools.register(tools.Tool(
        "move_file",
        {"type": "function", "function": {
            "name": "move_file",
            "description": "Move or rename a file or folder within the user's working folder.",
            "parameters": {"type": "object", "properties": {
                "from_path": {"type": "string"}, "to_path": {"type": "string"}},
                "required": ["from_path", "to_path"]}}},
        lambda session, **kw: move_file(session, **kw), min_role="member", data_scope="self", risk_tier="B"))

    tools.register(tools.Tool(
        "delete_file",
        {"type": "function", "function": {
            "name": "delete_file",
            "description": ("Delete a file you created yourself, or an EMPTY folder (any folder, "
                            "regardless of who made it -- an empty folder has nothing to lose). "
                            "Can't delete a file the user placed there."),
            "parameters": {"type": "object", "properties": {
                "path": {"type": "string"}}, "required": ["path"]}}},
        lambda session, **kw: delete_file(session, **kw), min_role="member", data_scope="self", risk_tier="B"))

    tools.register(tools.Tool(
        "download_image",
        {"type": "function", "function": {
            "name": "download_image",
            "description": (
                "Fetch an image from a URL and save it into your downloads/ folder -- a separate "
                "area from the rest of your working folder, for anything that came from the "
                "internet. Use this when you need a reference image from the web for imagine_image "
                "(fetch it here first, then pass \"downloads/<name>\" as imagine_image's own "
                "reference_path). The file is virus-scanned before it's saved -- if it's flagged, "
                "or if the scanner isn't available, the download is refused and you're told why. "
                "A scan only checks for known malware -- it says nothing about whether an image's "
                "contents are safe to trust, the same as any other file here. Images only "
                "(jpeg/png/gif/webp); anything else is rejected."),
            "parameters": {"type": "object", "properties": {
                "url": {"type": "string", "description": "the image URL to fetch"},
                "filename": {"type": "string",
                            "description": "a plain base name for the saved file (no folders, no "
                                           "extension needed -- the real one is picked from the "
                                           "actual image data)"}},
                "required": ["url", "filename"]}}},
        lambda session, **kw: download_image(session, **kw), min_role="member", data_scope="self", risk_tier="B"))


_register_tools()
