# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Avatar builder (2026-10-28): describe her neutral face once, get the whole set.

A new instance shipped with the original character's two dozen portraits, so Iron wore Nori's face. This is how an admin gives an instance its own:

  1. Describe the neutral face in words. Three candidate portraits are drawn.
  2. Pick one. It becomes the reference every other portrait is drawn from (the image model is handed it as `input_references`, the same mechanism the
     selfie tool uses to stay recognisably her).
  3. The rest of the states in emotions.json are drawn from it, a few at a time, in the background (each takes 30-90 seconds). Any can be redrawn.
  4. Install: the current avatars are moved to a backup folder first, the new ones take their place, and the neutral face also becomes the selfie
     reference. Optionally her selfie style text is rewritten from the same description, because the default one describes the original character.
     Restore puts the backup back.

Everything before install happens in a draft folder under the data directory, so nothing she shows changes until the admin approves the whole set.

One build at a time per instance, kept as one JSON file plus the images (no schema). The work runs on a background thread; a process restart mid-build
leaves "interrupted" pictures that the next press of build picks up again. Image calls go through chat.generate_image (the image provider chosen in Settings -> Images: OpenRouter, OpenAI or xAI), and each is logged to media_log as purpose "avatar_builder" so the cost is visible in Settings -> Images. That purpose is left out of
today's household cap (medialog.spend_status): the admin is shown the estimate and presses the button, and one build would otherwise use up the day.

See docs/avatar-builder.md."""
from __future__ import annotations

import json
import os
import random
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import config
import emotion
import imagegen
import medialog
import store

AVATAR_DIR = Path(__file__).resolve().parent / "static" / "avatars"
BUILD_DIR = store.DATA_DIR / "avatar_build"
BACKUP_DIR = store.DATA_DIR / "avatar_backups"
EXTS = ("png", "jpg", "jpeg", "svg", "gif", "webp")  # the order server._AVATAR_EXTS resolves them in
CANDIDATES = 3
WORKERS = 3
DESC_MIN, DESC_MAX = 10, 600
PURPOSE = "avatar_builder"

# Appended to every request, whatever the admin wrote. Pronoun-free on purpose: it must not depend on what she is called. Worded "if the character is a
# person" because the first wording ("An adult, fully clothed.") told the image model the character was a human, and a robot came back as a man.
ADULT = ("If the character is a person, they are an adult and fully clothed. No nudity, no sexual or suggestive content, no graphic violence, gore or "
         "injury. Nothing depicting or implying a minor.")
SELFIE_CONSTRAINT = ("If the character is a person, they are fully clothed at all times and always shown as a clearly adult. No nudity, no sexual or "
                     "suggestive content of any kind. No graphic violence, gore, blood, or injury. Nothing depicting or implying a minor.")

_LOCK = threading.RLock()
_JOB: dict = {"thread": None}


# ── prompts ──────────────────────────────────────────────────────────────
def neutral_prompt(description: str) -> str:
    return (f"Head-and-shoulders portrait of {description.strip()}. A neutral, relaxed expression with the mouth closed, looking straight at the "
            f"camera. Plain light background, soft even lighting, 4:5 portrait framing. A single image of one character. {ADULT}")


def state_prompt(state: str, description: str = "") -> str:
    """One expression, drawn from the reference. The description is repeated here, not left to the reference alone: with only "the same character" the
    image model sometimes leaned on the words in the look ("eyebrows", "mouth") and drew a different, human, character."""
    who = f" ({description.strip()})" if description and description.strip() else ""
    return (f"The same character as in the reference image{who}: identical head or face, kind of being (a robot stays a robot, an animal stays an animal, "
            "a person stays the same person), age, hair or surface, skin or material, colours, clothing, camera distance, framing, plain light "
            "background and lighting. Do not change who or what the character is. Only the expression and pose change, and the expression is shown "
            "with the character's own features (a robot uses its eyes, lights, faceplate or posture rather than human brows or lips). "
            f"Head-and-shoulders portrait. Expression: {emotion.look(state)}. A single image of one character, not a collage, grid, contact sheet "
            f"or split image. {ADULT}")


def selfie_style_prefix(description: str) -> str:
    return (f"Image of the same character in every picture: {description.strip()}. Natural, well-lit, candid but composed framing, like an ordinary "
            "photo taken during the day, not a staged studio shoot or a stock image.")


def check_description(description: str, workspace_id: int) -> tuple[bool, str]:
    d = (description or "").strip()
    if len(d) < DESC_MIN:
        return False, f"describe the face in a sentence or two (at least {DESC_MIN} characters)"
    if len(d) > DESC_MAX:
        return False, f"that is {len(d)} characters; keep it under {DESC_MAX}"
    if config.get("workspace", workspace_id, "image_content_precheck_enabled"):
        flags = imagegen.prompt_flags(d)
        if flags:
            return False, f"the image service would very likely refuse that ({', '.join(flags)}); describe an adult, fully clothed character"
    return True, d


# ── readiness / cost ─────────────────────────────────────────────────────
def readiness(workspace_id: int) -> dict:
    """Can pictures be made at all? Needs an image provider that is connected (chat.image_backend: OpenRouter, OpenAI or xAI) and an image model name."""
    import chat
    model = (config.get("workspace", workspace_id, "image_model") or "").strip()
    b = chat.image_backend(workspace_id)
    return {"key": b["ok"], "model": model, "ready": b["ok"] and bool(model), "label": b["label"], "type": b["type"], "choices": chat.image_choices(workspace_id)}


def per_image_estimate(workspace_id: int) -> float:
    return float(config.get("workspace", workspace_id, "image_cost_per_request_usd"))


def other_states() -> list[str]:
    return [s for s in emotion.STATES if s != emotion.DEFAULT_STATE]


# ── state file ───────────────────────────────────────────────────────────
def _state_path() -> Path:
    return BUILD_DIR / "state.json"


def _load() -> dict | None:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _save(st: dict) -> None:
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _state_path().with_suffix(".tmp")
    tmp.write_text(json.dumps(st), encoding="utf-8")
    os.replace(tmp, _state_path())


def _update(fn) -> dict | None:
    """Read-modify-write under the lock: the three worker threads and the request handlers all go through here."""
    with _LOCK:
        st = _load()
        if st is None:
            return None
        fn(st)
        _save(st)
        return st


def _alive() -> bool:
    t = _JOB["thread"]
    return t is not None and t.is_alive()


def status() -> dict | None:
    """The current build, or None. A build that says it is busy while no thread is running (the process restarted mid-build) is repaired here: what was
    in flight becomes retryable."""
    with _LOCK:
        st = _load()
        if st is None:
            return None
        if st.get("busy") and not _alive():
            for c in st["candidates"]:
                if c["status"] == "running":
                    c.update(status="failed", error="interrupted by a restart")
            for s in st["states"].values():
                if s["status"] == "running":
                    s.update(status="pending", error="")
            st["busy"], st["job"] = False, None
            _save(st)
        return st


def counts(st: dict) -> dict:
    states = st["states"]
    done = sum(1 for s in states.values() if s["status"] == "done")
    return {"done": done, "failed": sum(1 for s in states.values() if s["status"] == "failed"),
            "total": len(states), "complete": bool(states) and done == len(states)}


def has_backup() -> bool:
    return latest_backup() is not None


def _new_state(description: str) -> dict:
    return {"id": uuid.uuid4().hex[:12], "description": description, "created_ts": time.time(), "busy": False, "job": None, "cancel": False,
            "candidates": [{"status": "pending", "file": "", "error": ""} for _ in range(CANDIDATES)], "chosen": None,
            "states": {}, "cost_usd": 0.0, "images": 0, "installed_ts": None}


# ── one picture ──────────────────────────────────────────────────────────
_EXT_FOR_MIME = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}


def _generate(workspace_id: int, user_id: int, prompt: str, references: list | None) -> dict:
    """One image call. {"ok": True, "bytes", "ext", "cost"} or {"ok": False, "error"}; always logged to media_log."""
    import chat
    model = (config.get("workspace", workspace_id, "image_model") or "").strip()
    seed = random.randint(1, 2_000_000_000)
    est = per_image_estimate(workspace_id)
    try:
        res = chat.generate_image(prompt, model=model, seed=seed, references=references, workspace_id=workspace_id)
    except Exception as exc:  # noqa: BLE001 -- a crash must end as a failed picture, not a slot stuck on "running"
        res = {"ok": False, "reason": f"image call failed: {exc}"}
    if res.get("ok"):
        mime = imagegen._sniff_strict(res["bytes"][:16])
        if mime not in _EXT_FOR_MIME:
            res = {"ok": False, "reason": "image API returned something that is not a PNG, JPEG or WebP image"}
    if not res.get("ok"):
        raw = res.get("reason", "image generation failed")
        medialog.log_image_gen(workspace_id=workspace_id, user_id=user_id, purpose=PURPOSE, prompt=prompt, final_prompt=prompt, model=model, seed=seed,
                               used_reference=bool(references), ok=False, error=raw)
        return {"ok": False, "error": imagegen.clean_image_error(raw)[0]}
    cost = res.get("cost")
    charged = float(cost) if isinstance(cost, (int, float)) else est
    medialog.log_image_gen(workspace_id=workspace_id, user_id=user_id, purpose=PURPOSE, prompt=prompt, final_prompt=prompt, model=model, seed=seed,
                           used_reference=bool(references), ok=True, cost_usd=round(charged, 4), cost_is_actual=cost is not None)
    return {"ok": True, "bytes": res["bytes"], "ext": _EXT_FOR_MIME[mime], "cost": charged}


def _write_image(stem: str, data: bytes, ext: str) -> str:
    """Save under a name that cannot clash with an older picture of the same stem in another format; returns the file name."""
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    for e in EXTS:
        (BUILD_DIR / f"{stem}.{e}").unlink(missing_ok=True)
    name = f"{stem}.{ext}"
    (BUILD_DIR / name).write_bytes(data)
    return name


def _cancelled() -> bool:
    st = _load()
    return st is None or bool(st.get("cancel"))


def _record(path_fn, result: dict, stem: str) -> None:
    """Put one result into the state file: status, file name or error, running cost."""
    def apply(st):
        slot = path_fn(st)
        if result["ok"]:
            slot.update(status="done", file=_write_image(stem, result["bytes"], result["ext"]), error="")
            st["cost_usd"] = round(st["cost_usd"] + result["cost"], 4)
            st["images"] += 1
        else:
            slot.update(status="failed", error=result["error"])
    _update(apply)


def _run_candidate(workspace_id: int, user_id: int, index: int) -> None:
    st = _load()
    if st is None or st.get("cancel"):
        return
    _update(lambda s: s["candidates"][index].update(status="running", error=""))
    res = _generate(workspace_id, user_id, neutral_prompt(st["description"]), None)
    _record(lambda s: s["candidates"][index], res, f"cand_{index}")


def _run_state(workspace_id: int, user_id: int, name: str, reference: tuple[bytes, str]) -> None:
    if _cancelled():
        return
    _update(lambda s: s["states"][name].update(status="running", error=""))
    res = _generate(workspace_id, user_id, state_prompt(name, (_load() or {}).get("description", "")), [reference])
    _record(lambda s: s["states"][name], res, name)


def _reference() -> tuple[bytes, str] | None:
    st = _load()
    if not st or not st["states"].get(emotion.DEFAULT_STATE, {}).get("file"):
        return None
    p = BUILD_DIR / st["states"][emotion.DEFAULT_STATE]["file"]
    if not p.is_file():
        return None
    data = p.read_bytes()
    return data, imagegen.sniff_mime(data[:16])


def _job_main(kind: str, workspace_id: int, user_id: int, work: list) -> None:
    """The background thread: runs `work` (a list of zero-argument callables) WORKERS at a time, then clears busy. A cancel while it runs wipes the draft."""
    try:
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            list(ex.map(lambda fn: fn(), work))
    except Exception as exc:  # noqa: BLE001 -- never leave busy set behind a crash
        print(f"avatar builder {kind} job crashed: {exc!r}", flush=True)
    finally:
        with _LOCK:
            st = _load()
            if st is not None and st.get("cancel"):
                shutil.rmtree(BUILD_DIR, ignore_errors=True)
            elif st is not None:
                st["busy"], st["job"] = False, None
                _save(st)


def _launch(kind: str, workspace_id: int, user_id: int, work: list) -> None:
    t = threading.Thread(target=_job_main, args=(kind, workspace_id, user_id, work), name=f"avatar-{kind}", daemon=True)
    _JOB["thread"] = t
    t.start()


def _begin(kind: str, mutate=None) -> tuple[bool, str, dict | None]:
    """Mark a job as running, refusing a second one. Returns (ok, message, state)."""
    with _LOCK:
        if _alive():
            return False, "a picture job is already running; wait for it to finish", None
        st = _load()
        if st is None:
            return False, "there is no build to work on", None
        st["busy"], st["job"], st["cancel"] = True, kind, False
        _save(st)
        return True, "", st


# ── public actions ───────────────────────────────────────────────────────
def start(workspace_id: int, user_id: int, description: str) -> tuple[bool, str]:
    """Begin a new build from a description: three candidate neutral portraits. Replaces any draft that is not being worked on."""
    r = readiness(workspace_id)
    if not r["ready"]:
        return False, "connect an image provider (OpenRouter, OpenAI or xAI) and choose an image model first"
    ok, d = check_description(description, workspace_id)
    if not ok:
        return False, d
    with _LOCK:
        if _alive():
            return False, "a picture job is already running; wait for it to finish"
        shutil.rmtree(BUILD_DIR, ignore_errors=True)
        _save(_new_state(d))
    _, _, _st = _begin("candidates")
    _launch("candidates", workspace_id, user_id, [lambda i=i: _run_candidate(workspace_id, user_id, i) for i in range(CANDIDATES)])
    return True, "drawing three options"


def more_candidates(workspace_id: int, user_id: int) -> tuple[bool, str]:
    """Three fresh candidates from the same description (before one is chosen)."""
    st = status()
    if st is None or st["chosen"] is not None:
        return False, "there is nothing to redraw"
    ok, msg, _ = _begin("candidates")
    if not ok:
        return False, msg
    _update(lambda s: [c.update(status="pending", error="") for c in s["candidates"]])
    _launch("candidates", workspace_id, user_id, [lambda i=i: _run_candidate(workspace_id, user_id, i) for i in range(CANDIDATES)])
    return True, "drawing three more options"


def pick(index: int) -> tuple[bool, str]:
    """Choose a candidate as her neutral face. Starts the (not yet drawn) rest of the set."""
    st = status()
    if st is None or st["busy"]:
        return False, "wait for the pictures to finish first"
    if st["chosen"] is not None:
        return False, "a face is already chosen"
    if not isinstance(index, int) or not 0 <= index < len(st["candidates"]) or st["candidates"][index]["status"] != "done":
        return False, "that option has no picture"
    cand = st["candidates"][index]

    def apply(s):
        s["chosen"] = index
        ext = cand["file"].rsplit(".", 1)[-1]
        shutil.copyfile(BUILD_DIR / cand["file"], BUILD_DIR / f"{emotion.DEFAULT_STATE}.{ext}")
        s["states"] = {emotion.DEFAULT_STATE: {"status": "done", "file": f"{emotion.DEFAULT_STATE}.{ext}", "error": ""}}
        for name in other_states():
            s["states"][name] = {"status": "pending", "file": "", "error": ""}
    _update(apply)
    return True, "chosen"


def unpick() -> tuple[bool, str]:
    """Go back to the three options (only before any other portrait was drawn)."""
    st = status()
    if st is None or st["busy"] or st["chosen"] is None:
        return False, "nothing to go back from"
    if any(s["status"] != "pending" for n, s in st["states"].items() if n != emotion.DEFAULT_STATE):
        return False, "the rest of the set is already drawn; discard the draft to start again"

    def apply(s):
        for e in EXTS:
            (BUILD_DIR / f"{emotion.DEFAULT_STATE}.{e}").unlink(missing_ok=True)
        s["chosen"], s["states"] = None, {}
    _update(apply)
    return True, "back to the options"


def build_set(workspace_id: int, user_id: int) -> tuple[bool, str]:
    """Draw every state that is not drawn yet (also the retry of failed or interrupted ones)."""
    st = status()
    if st is None or st["chosen"] is None:
        return False, "choose a neutral face first"
    if not readiness(workspace_id)["ready"]:
        return False, "connect an image provider (OpenRouter, OpenAI or xAI) and choose an image model first"
    ref = _reference()
    if ref is None:
        return False, "the chosen face's picture is missing; discard the draft and start again"
    todo = [n for n in other_states() if st["states"].get(n, {}).get("status") != "done"]
    if not todo:
        return False, "every picture is already drawn"
    ok, msg, _ = _begin("set")
    if not ok:
        return False, msg
    for n in todo:
        _update(lambda s, n=n: s["states"].setdefault(n, {}).update(status="pending", error=""))
    _launch("set", workspace_id, user_id, [lambda n=n: _run_state(workspace_id, user_id, n, ref) for n in todo])
    return True, f"drawing {len(todo)} portraits"


def redraw_all(workspace_id: int, user_id: int) -> tuple[bool, str]:
    """Draw every portrait except the neutral reference again (when the set drifted from the character). Costs a full set, so the page confirms first."""
    st = status()
    if st is None or st["chosen"] is None:
        return False, "choose a neutral face first"
    if st["busy"]:
        return False, "a picture job is already running; wait for it to finish"
    _update(lambda s: [s["states"][n].update(status="pending", error="") for n in other_states() if n in s["states"]])
    return build_set(workspace_id, user_id)


def redraw(workspace_id: int, user_id: int, state: str) -> tuple[bool, str]:
    st = status()
    if st is None or st["chosen"] is None:
        return False, "choose a neutral face first"
    if state not in other_states():
        return False, "pick one of the other portraits to redraw"
    ref = _reference()
    if ref is None:
        return False, "the chosen face's picture is missing"
    ok, msg, _ = _begin("redo")
    if not ok:
        return False, msg
    _update(lambda s: s["states"][state].update(status="pending", error=""))
    _launch("redo", workspace_id, user_id, [lambda: _run_state(workspace_id, user_id, state, ref)])
    return True, f"redrawing {state}"


def discard() -> tuple[bool, str]:
    """Throw the draft away. If pictures are being drawn, stop after the ones in flight."""
    with _LOCK:
        st = _load()
        if st is None:
            return True, "nothing to discard"
        if _alive() and st.get("busy"):
            st["cancel"] = True
            _save(st)
            return True, "stopping after the pictures already being drawn; the draft is deleted when they finish"
        shutil.rmtree(BUILD_DIR, ignore_errors=True)
    return True, "draft discarded"


def draft_file(name: str) -> Path | None:
    """A file of the draft by name, for the review page; None for anything that is not one."""
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return None
    st = _load()
    if st is None:
        return None
    allowed = {c["file"] for c in st["candidates"] if c["file"]} | {s["file"] for s in st["states"].values() if s["file"]}
    p = BUILD_DIR / name
    return p if name in allowed and p.is_file() else None


# ── install / restore ────────────────────────────────────────────────────
def _avatar_files(state: str) -> list[Path]:
    return [AVATAR_DIR / f"{state}.{e}" for e in EXTS if (AVATAR_DIR / f"{state}.{e}").is_file()]


def _source_path() -> Path:
    return imagegen._REFERENCE_PATH


def install(workspace_id: int, use_for_selfies: bool) -> tuple[bool, str]:
    """Replace the live avatars with the draft. Refused unless every portrait is drawn (a mix with the old character's would be worse than either)."""
    st = status()
    if st is None or st["busy"]:
        return False, "wait for the pictures to finish first"
    c = counts(st)
    if not c["complete"]:
        return False, f"{c['total'] - c['done']} portraits are not drawn yet"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    bak = BACKUP_DIR / stamp
    n = 1
    while bak.exists():
        n += 1
        bak = BACKUP_DIR / f"{stamp}-{n}"
    (bak / "avatars").mkdir(parents=True)
    meta: dict = {"ts": time.time(), "description": st["description"], "settings": {}}
    (bak / "meta.json").write_text(json.dumps(meta), encoding="utf-8")   # first, so a half-finished install can still be restored
    thumbs = AVATAR_DIR / "thumbs"
    for state in emotion.STATES:
        for p in _avatar_files(state):
            shutil.move(str(p), str(bak / "avatars" / p.name))
        (thumbs / f"{state}.jpg").unlink(missing_ok=True)
        slot = st["states"][state]
        shutil.copyfile(BUILD_DIR / slot["file"], AVATAR_DIR / slot["file"])
    src = _source_path()
    if src.is_file():
        (bak / "source").mkdir()
        shutil.move(str(src), str(bak / "source" / src.name))
    src.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(BUILD_DIR / st["states"][emotion.DEFAULT_STATE]["file"], src)
    if use_for_selfies:
        for key, value in (("image_style_prefix", selfie_style_prefix(st["description"])), ("image_clothed_constraint", SELFIE_CONSTRAINT)):
            meta["settings"][key] = config.get("workspace", workspace_id, key)
            config.set("workspace", workspace_id, key, value)
        (bak / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    _update(lambda s: s.update(installed_ts=time.time()))
    return True, "installed"


def latest_backup() -> Path | None:
    if not BACKUP_DIR.is_dir():
        return None
    dirs = sorted(d for d in BACKUP_DIR.iterdir() if d.is_dir() and not d.name.endswith(".restored") and (d / "meta.json").is_file())
    return dirs[-1] if dirs else None


def restore(workspace_id: int) -> tuple[bool, str]:
    """Put back what install moved aside: the avatars, the selfie reference and (if install changed them) the selfie text."""
    bak = latest_backup()
    if bak is None:
        return False, "there is no earlier set to restore"
    if _alive():
        return False, "wait for the pictures to finish first"
    meta = json.loads((bak / "meta.json").read_text(encoding="utf-8"))
    thumbs = AVATAR_DIR / "thumbs"
    for state in emotion.STATES:
        for p in _avatar_files(state):
            p.unlink()
        (thumbs / f"{state}.jpg").unlink(missing_ok=True)
    for p in (bak / "avatars").glob("*"):
        shutil.move(str(p), str(AVATAR_DIR / p.name))
    src = _source_path()
    src.unlink(missing_ok=True)
    for p in (bak / "source").glob("*"):
        src.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(p), str(src))
    for key, value in (meta.get("settings") or {}).items():
        config.set("workspace", workspace_id, key, value)
    bak.rename(bak.with_name(bak.name + ".restored"))
    return True, "restored"
