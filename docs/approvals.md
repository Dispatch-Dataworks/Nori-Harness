# Approvals: one list for everything waiting on you

Some actions wait for a person to say yes before they run:
- an infrastructure change: `ssh_exec`, an `ssh_named` restart or update, a Proxmox, Cloudflare or GitHub write ([Infrastructure](infra.md));
- a risky click or typed text in your browser or on your phone ([Computer use](computer-use.md), [Devices](devices.md));
- something another agent asked for, from a peer at trust level *prompt* ([Peer agents](peer-agents.md)).

Each module keeps its own table, expiry and checks. `approvals.py` puts them in one list (2026-11-03). Wherever an approval shows up, it uses the same card and the same Approve / Deny route:

| Where | What you see |
|---|---|
| **The chat, when it happens** | A card under the message that asks, with Approve and Deny. It reaches the tab you are typing in while the turn is still running: the page checks for new cards every 5 seconds while it waits for a reply, and the reply brings back everything posted during the turn. Before this, a card posted mid-turn showed only after a reload. |
| **The board, while it is still waiting** | A *Waiting for your OK* section at the top of the board lists every approval still pending, from every module. Each one shows the same buttons, when it was asked, when it expires, and a link to its card in the chat. Each one counts toward the board badge. |
| **Your phone or browser notifications** | Approve and Deny on the notification. The button sends back a number. A browser or phone command keeps its own id. Any other request is sent as `approvals.HANDLE_BASE` plus the id of the chat message that carries its card. Before this change, an infrastructure request sent its bare id, so the button approved whichever browser command had the same number, and the infrastructure request was never resolved. |
| **Settings** | Settings → Infrastructure, Settings → Devices and Settings → Peers still list their own pending requests. |

A decision made in one place reaches the others:
- The chat card asks `/poll` for the current status of every card it still shows as pending (`cards=source:id,...`). A card approved on the board, approved from a phone, or expired changes within 15 seconds.
- Every module runs an approved action exactly once, so pressing the chat card's button after the board's does nothing.

**The agent reads how its own requests ended.** The "Done:" note only says that a change ran. The agent reads the output with `infra_requests`: its latest requests, or one by `request_id`, showing pending, done (with output), failed, denied or expired. The output comes back as a labelled tool result, the same one a change allowed to run without asking returns at once. It is never written into the chat as the agent's own words. The tool description and the "awaiting approval" answer tell the agent to check there before asking again.

**Adding a kind of approval** means calling `approvals.register(source, label=..., pending=..., resolve=..., status=...)` at import and posting the chat message with `meta=approvals.card(source, id, summary, host)`. The board, the card's live status and the notification buttons need nothing else. Tests: `tests/test_nori_approvals.py`.
