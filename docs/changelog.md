# Changelog

A skim-able, chronological distillation of what shipped.

## 2026-09-25 — 0.9.0-beta: the repo's public home, a configurable instance name, an emotion off switch

The repo's home is decided: `https://github.com/Dispatch-Dataworks/Nori-Harness`.
Two things the operator asked for before tagging this beta and going public. **A
configurable assistant name and avatars** — the harness/product stays
Nori everywhere (this repo, `server_version`, the
Nori-PACI User-Agent); what's configurable is what an operator's own
instance is called, read fresh at render time everywhere she's named
(persona, the chat header, notifications, PACI's own outbound
`agent_name`) — never baked into a file or frozen at peer-connect time.
Avatars needed no new mechanism: dropping a file into
`static/avatars/<state>` already worked with no code change. **A real
off switch for the emotion function** — when disabled, nothing
emotion-related reaches her context, the UI, or the turn: `get_state()`
always reports neutral, the precheck reminder line drops out with no
gap, and `set_emotion` disappears from her tool list entirely.

## 2026-09-11 — foundations

Initial build: core chat loop, per-user working folder, the dark
mobile-first UI shell, async send with an optimistic UI and a
server-side per-account lock, avatar system, chat scroll behavior.

## 2026-09-12 — memory, voice, tools, and the first peer protocol

Memory reflection; household inventory and meal-plan tools; the voice
layer (text-to-speech and speech-to-text) designed and built; visible
tool-call lines in the chat view; the MCP client; a pre-commit secret
scanner added and versioned, not left as a manual habit; a
deterministic backstop for a leaked (narrated instead of called) tool
call; conversation history search. **PACI, the peer-agent protocol,
was designed and built as a general capability** this same day —
connection, authentication, loop prevention, passive delivery — see
[Peer agents](peer-agents.md); the protocol specification itself is
maintained in its own separate repository (see [NOTICE](../NOTICE)).

## 2026-09-13 — peer-protocol hardening, live-data safety

Two real, separately diagnosed peer-message delivery incidents (lost
messages from a malformed field; false-negative timeouts on an
already-successful delivery) — both fixed at the protocol layer, both
documented in the PACI specification §7 as the incidents that motivated the
fix. Working-memory metadata (a self-initiated message records *why*
it was sent — see [Confabulation](confabulation.md)). The `NORI_LIVE`/
`NORI_DATA_DIR` live-data guard, added after a real incident where a
verification script ran against production data by accident. Per-turn
cost logging; a push-to-talk voice conversation mode.

## 2026-09-14 — the out-of-band agent channel, image generation

`agent_channel.py`, a side channel independent of the main chat
turn. `generate_image_selfie`/`imagine_image` — see
[Image generation](image-generation.md).

## 2026-09-15 — downloads, scanning, Home Assistant, general scheduling

A real download tool built behind a swappable virus-scanner interface
(quarantine → scan → promote — see [The working folder](working-folder.md)).
Home Assistant integration — entity discovery/exposure split, control,
history (see [Home Assistant](home-assistant.md)). The general-purpose
scheduler for one-off and recurring tasks (see [Scheduler](scheduler.md)).
A fourth tracked confabulation instance, from a real toggle gap — see
[Confabulation](confabulation.md).

## 2026-09-16 — the board: tasks, notes, trackers, reminders

All four board item types built this day, each following the same
one-table-plus-one-event-log pattern; categories generalized from a
hardcoded list into a managed set; peers wired into all four. See
[Tasks, notes, reminders, trackers](tasks-notes-reminders-trackers.md).

## 2026-09-17 — board polish

Read/needs-attention flags on board items; trackers moved onto their
own page rather than living inside Settings.

## 2026-09-18 — a five-phase design pass, Google & Microsoft, backups

A five-phase visual/structural redesign pass across the whole app.
Gmail, Google Calendar, Google Contacts, Google Drive, Outlook,
Outlook Contacts, OneDrive (two accounts), and SharePoint all
connected — see [Google & Microsoft](google-and-microsoft.md). An
app-wide timezone audit and fix. Daily encrypted backups, both apps —
see [Backups](backups.md).

## 2026-09-19 — integration health, a documented register split, her own address

[Integration health](integration-health.md) checks built across every
outward connection. A standing principle stated explicitly: Nori is
agent-aware and reasons about her own machinery out loud; the other
self-hosted persona app in this repository never breaks frame — a
deliberate difference between the two personas, not an inconsistency
to reconcile. Settings navigation rebuilt with a full theming pass.
History-page search fixed to actually scroll to and center a result.
Nori given the ability to read her own live URL and enumerate her real
page routes — the direct fix for a fifth, differently-shaped
confabulation instance (inventing a plausible *feature* rather than
hiding ignorance of a *status*) — see [Confabulation](confabulation.md).

## 2026-09-19 — reply_requested's busy path: queued and logged, not dropped

A real incident — several `reply_requested` asks to a peer that appeared
to go unanswered — turned out to be undiagnosable from the data alone:
a busy-drop and a granted-turn-that-chose-silence looked identical
after the fact, because neither side ever recorded which had happened.
Fixed on both apps: a compulsion blocked by a busy turn lock now queues
instead of dropping, runs the moment the lock frees, collapses several
queued asks from the same peer into one deferred turn rather than
stacking a backlog, and re-checks each message's own freshness against
`expiry_hours` before running so a stale ask can't compel a turn on
old content. Every decision — granted, queued, run-from-queue,
dropped-as-expired, dropped-for-another-reason — is now logged with
its own timestamp and surfaced on the peer's own settings row. A
second, real gap found and fixed along the way: a message flagged
suspicious by screening could still be granted the synchronous
exception, because the grant decision was computed before screening
ran — the PACI specification §9.5's own claim that flagged content never gets
the exception wasn't actually true of either app's code until this
fix. The PACI specification bumped to v1.0; see [Peer agents](peer-agents.md#busy-means-queued-not-dropped).

## 2026-09-17 — topic-triggered memory activation

Instead of relying on her to remember to call `recall()`, an incoming
message's own topics are now matched against stored memory automatically
and injected before she decides what to say or do — see
[Memory](memory.md#topic-triggered-activation--she-doesnt-have-to-remember-to-call-recall).
Keyword+stemming matching is the default; a new `safety_tier` flag (set
deliberately, by the model's own `remember`/`update_memory` call or the
operator on the memory settings tab, never inferred from content) marks
a fact as a real boundary or constraint, and that tier gets a second,
embedding-based semantic pass on top — added specifically because a
real recall measurement against Nori's own history found keyword
matching alone missed a genuine paraphrase (2 of 7 real safety/
constraint memories recalled naively, 5 of 7 with stemming, the
remaining miss a zero-overlap paraphrase). Before a consequential tool
call's result is folded back into the turn (an MCP connection toggle,
smart-home control, a settings change, messaging someone), the safety-
tier semantic pass runs unconditionally, never gated on keyword
confidence — a confident keyword hit can still be the wrong memory. An
unreachable embedding provider is surfaced as degraded, logged and
stated plainly in context, never a silent all-clear.

## 2026-09-17 — image generation's timeout raised, from real data

`chat.IMAGE_TIMEOUT_S` raised 60s → 120s after measuring real
`.nori.log` `TIMING` data, not guessed: genuine successes reached
54.2s, and 27% of real calls were already hitting the old ceiling and
failing. Confirmed this was a genuine provider-latency issue, not the
2026-09-13 IPv6/DNS-latency incident recurring — `imagegen.py` already
inherits `server.py`'s process-wide IPv4-only DNS fix through
`chat.openrouter_image()`, the only path it uses. A real timeout now
tags its own reason string so `clean_image_error()` can tell it apart
from a content-policy refusal or a generic failure, and says so
plainly rather than leaving her to guess (or claim it worked). Confirmed
with a real held lock, not assumed, that a slow generation call
deferring a queued `reply_requested` or a second message from the
operator is `turns.py`'s existing queue-not-drop behavior working as
designed, not a new gap — see
[Image generation](image-generation.md#timeout-and-what-happens-when-generation-runs-long).

## 2026-09-17 — the boot-persistence tasks were never actually registered

Traced why nori came back down after a reboot on the original
deployment host: a scheduled-task registration script was correct but
had never been successfully run — every attempt hit an elevation wall
that tooling couldn't click through (confirmed directly, not assumed:
an interactive UAC prompt gets silently cancelled with no human able
to approve it). Someone with real admin access has to run the
registration themselves, once — see
[Deployment & supervision](deployment-and-watchdog.md). A related,
real bug found and fixed the same day: `nori_ctl.ps1` used to trust a
bare recorded PID with no cross-check, so a reused PID after a reboot
could read as "still running" and mask a real outage; and `start`
didn't check whether something else already had the port before
launching, which could let two servers bind the same port at once.
Both verified for real (a deliberately corrupted pidfile, a
deliberately concurrent start), not just read off the code.

## 2026-09-17 — the crash-loop, root-caused: an unlocked dual supervisor

Corrected an earlier same-day conclusion: the boot-persistence
Scheduled Tasks *were* registered and *were* firing — an empty
`Get-ScheduledTask` result was a blind spot at that query's own
privilege level, not evidence of absence (confirmed by calibrating the
same query against a known-real task before trusting it empty; see
[Deployment & supervision](deployment-and-watchdog.md)). Fixed what
was blocking real diagnosis first: `.nori.log`/`.nori.err`
now stamp every line with its own timestamp — before this
a raw traceback carried no way to tell when it happened. Root cause of
the actual crash-loop: no fatal traceback exists anywhere in the
error log for the whole incident, which points to the process
being killed from outside its own code rather than crashing from
within it — an in-process watchdog and an OS-level scheduled task ran
completely uncoordinated, each able to force-kill
whatever the other just started. Not yet fixed; not caused by anything
shipped the same day.

## 2026-09-18 — the in-process watchdog retired; one supervisor now

Fixed the crash-loop found the day before: the in-process watchdog
deleted outright rather than locked against the OS-level scheduled
task also calling `ensure` — a lock would have papered over a
redundancy that shouldn't have existed, and the in-process watchdog
dies with the very process it's meant to guard, exactly when it's
needed. The OS-level scheduled task is now the only supervisor,
shortened from 3 minutes to 60 seconds (matching the retired
watchdog's own cadence, so retiring it gives up as little
responsiveness as reasonably possible — see
[Deployment & supervision](deployment-and-watchdog.md#one-supervisor-not-two-2026-09-18--a-real-crash-loop-root-caused)).
`Stop-One` (every `_ctl.ps1` script's kill path) re-verifies the
target's start-time ticks immediately before killing, not just when
the pidfile was last read — closes the same class of bug as the
PID-reuse fix, applied to the kill side specifically. The
settings-page status dot (read literally, "watchdog") now reflects
the supervisor's own check-ins instead of going permanently stale.
**Verified for real, not assumed:** force-killed nori's live server
process directly and watched the already-registered scheduled task
bring it back on its own next tick, with exactly one process
listening afterward — not via this session invoking anything itself
(confirmed separately that `Start-ScheduledTask` hits the same
privilege wall as registering one). A genuinely leftover in-process
watchdog from before this change (deleting the `.ps1` file doesn't
kill an already-running instance of it) turned out to still be alive
on both apps mid-verification, fired concurrently with the real
task's own tick, and resolved cleanly — then was properly retired by
running the updated `_ctl.ps1 restart`.

## This documentation project

This `docs/` folder itself: one file per system, a quick start, setup,
architecture, the enforcement-model distinction, an honest account of
the confabulation pattern, and a contributor's guide — see
[the index](README.md) for the full list. Existing root-level docs
(`INSTALL.md`, `INTEGRATIONS.md`, `BACKUPS.md`, `PACI-SPEC.md`) were
swept to genericize any reference to a specific named peer persona,
since this folder documents PACI as a capability for connecting *a*
peer, not a specific relationship. `WEBSEARCH.md` moved into this
folder outright rather than staying linked from the root. Going
forward, a change to any system documented here isn't finished until
its page is updated to match.

**Follow-up (same project, next pass):** `INSTALL.md`'s,
`INTEGRATIONS.md`'s, and `BACKUPS.md`'s actual Nori-relevant content —
the parts that were previously just linked in from the repository
root — moved into this folder outright, integrated into its own voice
rather than deferred to an outside file: quick start/setup/deployment
absorbed the relevant parts of `INSTALL.md` (including a real,
previously-undocumented gap, the OS-level Scheduled Task supervision
layer beyond the in-process watchdog); [Google & Microsoft](google-and-microsoft.md)
absorbed `INTEGRATIONS.md`'s full Cloud-console walkthrough; [Backups](backups.md)
absorbed `BACKUPS.md`'s complete included/excluded table, encryption,
and restore procedure.

## 2026-09-19 — peer-context visibility gaps, closed

Follow-up to a read-only investigation into "are peer messages actually
in context on an ordinary turn": unread peer content now rides along
on every turn that answers the operator directly too (live send/retry/
voice, and every orphan-sweep answering a message that arrived
mid-turn), not just a peer-motivated one — his own explicit, informed
choice, reversing part of a 2026-09-13 protection for exactly that
case. Two more gaps found and fixed alongside it: the recent-exchanges
background-awareness block was received-only (an agent could see an
incoming peer message with no memory of having already answered it —
now includes her own sent messages, attributed "you told X"), and its
cap was shared across every connected peer combined rather than per
peer (a chatty peer could evict a quieter one's messages regardless of
recency — now a windowed, per-peer cap). Window widened 3h → 12h, cap
5 per peer (was 3, shared) — see [Peer agents](peer-agents.md#recent-exchange-background-awareness)
for the full mechanism and its real, measured cost. The PACI
specification bumped to v1.1 (§13, reference-implementation notes only — no
wire-protocol change).

## 2026-09-19 — read state split in two; read receipts built

Same-day follow-up, operator's own refinement: "read in a peer
initiated turn is not read in a user initiated turn... those should
still be unread until she sees them in context talking to me." The
single `presented_ts` flag above turned out to still be overloaded --
whichever kind of turn (peer-motivated or one answering him directly)
reached a pending message first silently satisfied it for the OTHER
kind too. Split into two independent columns, `presented_peer_ts` /
`presented_user_ts`, each stamped only by a turn of its own matching
kind — see [Peer agents](peer-agents.md#read-state-is-two-dimensional-not-one-flag).
The PACI specification bumped to v1.2.

Built the same day, on top of that split: **read receipts**
(the PACI specification §7.1, v1.3) — a connected peer can now tell her, per
message, that it was actually seen (`presented`, in their own
peer-motivated turn) and/or surfaced to their own operator
(`surfaced`) -- confirmation the previous "read state" work only ever
gave her *locally*, never across the wire. Deliberately never a
message and never able to trigger a turn (the PACI specification §9.4's
discipline, extended) -- but, once received, folded into the same
recent-exchanges background awareness a sent message's own line
already gets, which is the actual point: an agent knowing its own
words landed. Off by default per peer (`receipt_granularity`,
`/admin/peers`) because the "surfaced" stage is evidence a human was
actually present on the other side, not just a fact about their
agent -- see [Peer agents](peer-agents.md#read-receipts--confirmation-that-a-message-actually-landed)
for the full mechanism and the privacy reasoning.

## 2026-09-19 — round cap raised; a real silent-turn diagnosability gap closed

Follow-up to a real question about why a peer sometimes gets no reply
at all: hitting a peer-motivated turn's own round budget produces a
fallback reply that, on that kind of turn, is discarded exactly like
any other unsent reply — before today, that made "ran out of rounds"
indistinguishable from "ran fine, genuinely chose silence." `tool_
rounds_proactive` raised 4 → 8 on Nori (trust_level=full reinstates 11
tools a peer-motivated turn otherwise can't see, and the old budget
left no slack for using one). More importantly: every
peer-motivated turn that actually exhausts its round budget now writes
a real, timestamped, visible record — same place and shape as the
existing `reply_requested` decisions log — so this specific cause of
silence is never invisible again, whether or not it turns out to be
the explanation for any particular quiet turn. See
[Peer agents](peer-agents.md#a-round-limit-hitting-is-now-visible-never-invisible-silence)
for the full mechanism.

## 2026-09-18 — the supervisor reported "ok" while both apps were down

After a hard power loss the sweep logged both apps "ok" while neither was
listening. Root causes, all fixed: `ensure` exited 0 after logging "STILL
DOWN"; start counted "process exists after 2s" as success; boot-time
`start-all` and the periodic supervisor raced each other; import-time
crashes vanished before log redirection; the sweep stamped every line with
its own start time and trusted the ctl exit code. Now: success means a
listening socket plus a real `/healthz`; a control lock; `boot.py` captures
early crashes; the sweep verifies independently and exits non-zero when
something's down; every outcome is recorded in `.supervision.jsonl`. Also
shipped: model-call failures on peer-motivated turns and refused peer sends
now leave durable records, and all "why was there no reply / why was it
down" sources are presented as one timeline on the peers page. See
[Deployment](deployment-and-watchdog.md#what-ok-means-the-supervisors-honesty-contract-2026-09-18)
and [Peer agents](peer-agents.md#one-place-to-look-why-was-there-no-reply--or-why-was-it-down).

**Known limitation recorded (same day):** on the original bare-metal Windows deployment, a SYSTEM-launched instance died at import (`cryptography` lives only in the interactive user's site-packages), so it didn't return after a reboot until started from a user session. Deliberately deferred to the repo-split/Docker work rather than patched in place — a container image installs its own dependencies, so this class of bug doesn't apply there; now loudly logged rather than silent for anyone still running it the original way. See [Deployment](deployment-and-watchdog.md).

## 2026-09-20 — a persona editor; her own past output no longer a template

**Persona editing in the settings pages** (Settings → Administration → Persona): validation, a version
history that keeps the text every save replaces, a baseline of your own, and a reset to the shipped
default; administrator-only and not reachable by any tool she has. The shipped default persona and
tool-usage guidance were also made neutral about who the user is. See [The persona editor](persona.md).
Context tuning already existed (Settings → Administration → Context tuning) and is unchanged.

**Her own past output is scrubbed before it is shown back to her** (`own_output.py`). Found by reading
her real history: two replies that were a copy of the app's own bracketed photo annotation, one tool
call written as prose, and app-written fallback lines stored as her words, all of which were rendered
back to her verbatim on later turns. Also fixed: what she had sent to a connected peer was shown back
to her as a Python dict (`{'text': '...'}`) rather than as her words. See
[Context & tuning](context.md#her-own-past-output-is-a-template).

**Baselines for both (2026-09-20, later).** The persona and the context tuning each now have three layers: the
shipped default, a dated last-known-good baseline you set deliberately, and the live value; three
clearly separate ways back (baseline, default, one edit back) each behind a preview of exactly what
would change; and independence between the two. The baseline is never set by an edit or a restore and
survives resets, restores, migrations and history pruning. Also fixed: two saves in the same second
were listed and pruned in the wrong order. See [The persona editor](persona.md) and
[Context & tuning](context.md#baseline-and-going-back).

## 2026-09-21 — image content pre-check can be turned off

Admins can now switch off the local image content pre-check (the keyword screen that stops an obviously
refusable prompt before it costs an image call) under **Settings → Images**. It stays on by default. With it off,
the prompt's content rules, the image provider's own checks and the shared image budget all still apply, and a real
provider refusal is still reported back plainly. See [Image generation](image-generation.md).
