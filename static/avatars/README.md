# Avatars

Drop a real image in here named after the emotion it's for, and it's used
automatically — no code or config change, no restart-required registration
step for a state that's already in `emotions.json`. `server.py`'s
`/avatar/<state>` route checks this folder first, in this exact order —
`.png`, `.jpg`, `.jpeg`, `.svg`, `.gif`, `.webp` — and only falls back to a
generated placeholder (a plain colored circle) when none of those exist.
(That order was previously mis-stated in this file as ending `...gif,
webp, svg` — corrected here to match what the code actually checks.)

```
static/avatars/
  panic.png
  happy.png
  amused.png
  ...
```

Filenames are lowercase, exactly the state's name from `emotions.json`,
no other characters — `panic.png`, not `Panic.PNG` or `panic-1.png`. If
both `panic.png` and `panic.svg` exist, `.png` wins (extension order
above); nothing warns you about the shadowed file, so don't leave both.

The valid state names are the source of truth in `../../emotions.json` —
add a state there and a matching avatar file here; nothing else needs to
change for an *existing* state name. Adding a brand new state name that
isn't in `emotions.json` yet needs an entry there (see that file's own
`_comment`) **and a server restart** — the state list is fixed at
startup, in the tool's schema among other places. Dropping in a file for
a state that's already listed is picked up immediately, no restart.

No size/aspect/transparency requirement is enforced — whatever bytes are
in the file are served as-is, with a matching `Content-Type`. In
practice: it's rendered as a 20–28px circle (`border-radius:50%` on an
`<img>` with equal width/height), so a **square source image is the safe
choice** — a non-square one gets squeezed into that box before the crop,
which will look distorted. Any resolution works (the browser scales it
down); transparency is fine and renders against the page background
rather than a colored circle.

Verify what's actually resolving to a real file vs. a placeholder at
**`/admin/avatars`** (admin only) — every configured state, its live
image, and whether it found a real file or fell back, read fresh off
disk on every page load. That's the fast way to confirm a batch of drops
actually took rather than silently falling back on a filename mismatch.

This folder is empty in the repo on purpose (there's nothing to ship
until real art exists) — everyone sees placeholders until they add their
own.
