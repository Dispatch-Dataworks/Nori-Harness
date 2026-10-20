# History: searching, hiding and deleting messages

Source: `nori/conversation.py` (the operations and the readers), `nori/compaction.py`
(`invalidate_around`), `nori/server.py` (the History page and `POST /history/edit`).

The **History** page (menu → History) shows everything that was actually said, newest first, with
search that *jumps to where something happened in the real timeline* rather than listing matching
lines. You can pick messages there and do one of two different things to them.

## Hide from her vs. delete forever

| | Hide from her | Delete forever |
|---|---|---|
| The message stays in storage | **yes** — still on this page, marked "hidden from her" | **no** — the row is removed |
| Reversible | **yes** — *Restore* | no |
| Her context window, summaries, memory reflection, `search_history` | **never sees it** | never sees it (it doesn't exist) |
| Still shown in your chat | yes, dimmed, with a tooltip | no |
| An image message's stored picture and thumbnail | kept | **deleted** (unless another message still shows the same picture) |

Use *hide* to take something out of her head without losing your own record of it; use *delete* when it
should be gone.

## How to use it

1. Open **History**. Search by text, by "hours ago", or both — the page jumps to the newest match, with
   *↑ newer match / ↓ older match* links. Tick **hidden from her only** to see just the hidden messages.
2. Tick the rows you want (the checkbox is at the left of each row). **this page** ticks everything on the
   page. When a search or the hidden filter is active, **Apply to all N messages that match…** acts on
   *every* match (the server re-runs the search itself; the page's list isn't trusted), not just the ticked ones.
3. Press **Hide from her**, **Restore**, or **Delete forever** (which asks first, with the real count). The page
   returns to the same view with a one-line result.

The action bar is pinned to the bottom of the page so it stays in reach on a phone.

## What "she never sees it" covers — and doesn't

`conversation.py` is the only module that reads `messages` for the model, and every model-facing reader
excludes hidden rows (`_VISIBLE_TO_HER`):

- `recent()` — her raw context window and memory's reflection window. It excludes hidden messages **by default**;
  only the chat page's own display passes `include_pruned=True`, so a new caller is safe without doing anything.
- `before()` / `in_range()` — what compaction summarizes.
- `search()` — her `search_history` tool, including the context it pulls in around a match.
- `latest_user_message_after()` — a hidden message is not something she should go and answer.

The human views (`history_page`, `history_match_ids`, the chat display, the poll feed) deliberately do show them —
that is how you find them again.

**Summaries are rebuilt.** Compaction stores a summary per conversation session; one that covered a message you
change would still contain its text. So every hide, restore and delete drops the summaries around the affected
messages — the ones containing them *and the neighbour on each side* (re-partitioning can merge adjacent
sessions) — and the background compaction pass rebuilds them from the messages that remain.

**What it does not touch**, and you should know:

- **Memories she already saved.** Facts reflection extracted before you hid the message stay in her memory. Review
  *Settings → Memory* to remove them.
- **Earlier backups.** Deleting removes the row from the live database; automatic backups taken before still contain
  it until they age out (see [Backups](backups.md)). The delete confirmation says so.
- **Anything she already said to someone else** over a [peer connection](peer-agents.md).

## Audit

Every hide, restore and delete is recorded in `history_edits`: when, what, how many, the id range, and the search that
selected them — **never the message text** (a deletion has to actually delete).

## Safety properties (all enforced in code)

- `POST /history/edit` sits behind the signed-in-session and CSRF gates like every other form route.
- Every id is scoped to the signed-in account inside `conversation.set_pruned` / `delete_messages`; another account's
  ids match nothing, whatever is posted. Admins and members each manage only their own history.
- "Apply to all" is refused without a search/filter, and above `MAX_EDIT_IDS` (5000) matches.
- A deleted image's file id is validated as plain hex before it ever becomes a path.
- The redirect after an edit carries only a short allow-listed result code and known search parameters; it can only
  ever point at `/history`.

## Tests

`tests/test_nori_history_prune.py` — including a **guard** that fails when a function in `conversation.py` reads
`messages` and is not classified as either model-facing (it must then filter hidden rows) or human/bookkeeping, so a
future reader can't silently start showing hidden messages to her.
