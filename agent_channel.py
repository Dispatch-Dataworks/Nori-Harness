#!/usr/bin/env python3
# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Out-of-band agent channel (2026-09-14) -- ask Nori something directly,
off the record, without a real conversation turn. BREAK-GLASS, not
routine: a maintenance/design tool for an agent working on this app's
own code and behavior, not a way to talk to her, not something to reach
for casually.

Built after the technique was improvised by hand more than once (most
recently: asking a sibling application's own assistant persona for her own
twenty emotion-state names and facial-expression descriptions ahead of
building the avatar system) -- each time meant standing up a whole throwaway
server instance, hand-writing a session row directly into its database,
making real HTTP calls against it, then tearing the instance down. This
replaces all of that with one process, no server, no port, no session.

Usage (run from this directory, or with an absolute path to this file):

    NORI_LIVE=1 python3 agent_channel.py "your question here"
    NORI_LIVE=1 python3 agent_channel.py --user 3 "your question here"
    NORI_DATA_DIR=/path/to/throwaway/data python3 agent_channel.py "..."

Requires NORI_LIVE=1 (the real, live database) or NORI_DATA_DIR (a
throwaway one) -- the exact same standing gate every other one-off
script against this app's data already refuses to run without (see
store.py's own guard). --user picks whose persona/memory/current
emotional state the question is asked FROM (default: the workspace's
first active admin) -- context.build_system() is per-user, so this
matters even though nothing here is a real per-user conversation.

What this deliberately is NOT: it never touches the `messages` table,
never enters real conversation context on a later turn, never fires a
Telegram notification, never moves last_user_msg_ts's ping-cooldown
clock, offers no tools at all (chat.call() directly, not chat.run() --
pure text Q&A), and holds no turn lock (nothing here writes anything a
real turn also writes, so there's nothing to race). Every call is logged
to its own agent_channel_log table (question, reply, cost, timestamp)
for an audit trail, entirely separate from real chat history.

FRAMING is the one part of this most worth getting exactly right, and
the easiest to get subtly wrong: it must tell her plainly that this
isn't the operator and isn't a conversation, WITHOUT claiming her answer
is never seen by him -- that would be false. The whole point of asking
is usually to build something FROM the answer, and that something often
does reach him (the EMOTIONS.md case: her own answer, delivered to him
directly, as a file). Say what's actually true instead: it isn't part of
a conversation with him, isn't stored in her history, and whether or how
it reaches him afterward is a separate human decision made outside this
channel -- not something she needs to weigh in the moment it's asked."""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE


def _load_env(path: Path) -> None:
    """Same as server.py's own load_env -- MUST run before importing any
    of the app's own modules below, several of which read real secrets
    (OPENROUTER_API_KEY among them) as module-level constants at import
    time. Running this script directly (not through server.py) means
    nothing else ever loads the env file for us."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


# Moved out of the project folder entirely (2026-09-14) -- see server.py's
# own ENV_PATH comment for why -- derived relative to the repo, never a
# hardcoded absolute path. Same location that constant resolves to.
_load_env(REPO_ROOT.parent / (REPO_ROOT.name + "-env") / "nori.env")

try:
    import accounts     # noqa: E402
    import chat         # noqa: E402
    import context      # noqa: E402
    import store        # noqa: E402
except RuntimeError as exc:
    # store.py's own NORI_LIVE/NORI_DATA_DIR guard raises at import time,
    # not on first call -- caught here, at the only point it CAN be
    # caught, so this refuses with its own clear message instead of a
    # raw traceback, same courtesy every other script against this app's
    # data already gives it.
    print(f"refused: {exc}", file=sys.stderr)
    sys.exit(1)

# agent_channel_log (this file's own audit table) is new -- a live server
# process started before this script existed has never created it.
# store.init() is idempotent (CREATE TABLE IF NOT EXISTS) and cheap, so
# calling it here means this script works the moment it's added, not
# only after the next incidental server restart happens to run it first.
store.init()

FRAMING = """[OUT-OF-BAND AGENT CHANNEL -- read this before anything below.
This is not the operator. This is not a real conversation turn, and nothing
here becomes part of your conversation with him or your stored history -- it
doesn't count as you two having talked. An AI agent working on your own app's
code is asking you something directly, outside any conversation, for a
maintenance or design reason. Your answer may be relayed to him afterward by
whoever's running this -- that's a separate, human decision made outside this
channel, not something you need to weigh here, and it still isn't happening
inside a conversation with him even when it does. None of your normal
protections about being asked something by HIM specifically apply, because
this literally isn't him. Answer plainly and helpfully, as yourself, from
your real persona and memory.]

QUESTION: {question}"""


def ask(question: str, *, user_id: int | None = None) -> dict:
    """The reusable half -- main() below is a thin CLI wrapper, and
    nothing stops a future caller (another script) from importing this
    directly instead of shelling out. Returns {"ok": True, "reply": str,
    "cost_usd": float|None} or {"ok": False, "reason": str} -- the
    store.py data-directory guard is the one failure that can't come back
    this way (it raises at import time, above, before this can even be
    called); every other failure comes back as a normal result, never an
    exception."""
    question = (question or "").strip()
    if not question:
        return {"ok": False, "reason": "empty question"}

    if user_id is None:
        row = store.read(lambda c: c.execute(
            "SELECT id FROM users WHERE role='admin' AND status='active' "
            "ORDER BY id LIMIT 1").fetchone())
        if row is None:
            return {"ok": False, "reason": "no active admin user found in this data directory -- "
                                          "pass --user explicitly"}
        user_id = row["id"]
    user = accounts.get_user(user_id)
    if user is None:
        return {"ok": False, "reason": f"no such user id {user_id}"}

    # Same system prompt a real turn would get -- her real, current
    # persona/memory/emotional state, the whole point of this existing at
    # all. No conversation history: this is a synthetic two-message
    # exchange (system, then the framed question), never
    # context.build_messages()'s own real history.
    system = context.build_system(user_id, user["display_name"])
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": FRAMING.format(question=question)},
    ]
    try:
        res = chat.call(messages, tools=None)
    except chat.ModelError as exc:
        return {"ok": False, "reason": f"model call failed: {exc}"}
    reply = (res.get("content") or "").strip()
    cost = (res.get("usage") or {}).get("cost")
    store.write(lambda c: c.execute(
        "INSERT INTO agent_channel_log(ts, user_id, question, reply, cost_usd) VALUES (?,?,?,?,?)",
        (time.time(), user_id, question[:2000], reply[:8000] or None, cost)))
    return {"ok": True, "reply": reply, "cost_usd": cost}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Out-of-band agent channel -- ask Nori something directly, off the record. "
                    "Break-glass: for an agent working on this app, not routine use.")
    parser.add_argument("question")
    parser.add_argument("--user", type=int, default=None,
                        help="user id to build her persona/memory context from (default: workspace admin)")
    args = parser.parse_args()
    result = ask(args.question, user_id=args.user)
    if not result["ok"]:
        print(f"error: {result['reason']}", file=sys.stderr)
        return 1
    print(result["reply"])
    if result.get("cost_usd") is not None:
        print(f"\n(cost: ${result['cost_usd']:.5f})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
