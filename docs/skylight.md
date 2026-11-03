# Skylight

[Skylight](https://www.ourskylight.com) is a family wall calendar: a frame in the kitchen showing the
household's calendar, grocery and to-do lists, chore chart, rewards and meal plan. Connect it and every
member's assistant can read and update all of it, the way anyone in the house can tap the frame.

Code: `skylight.py` (everything Nori-specific) on top of `skylight_calendar/`, a vendored copy of the
[skylight-calendar](https://github.com/benjaminarthurt/Skylight-Calendar) library (MIT; see its
`VENDORED.md` for the commit and how to update it). Tests: `tests/test_nori_skylight.py`.

> Skylight publishes no API. The library talks to the private one its own web and mobile apps use, so
> this can stop working whenever Skylight changes their app. When a request is refused (HTTP 422), the
> assistant is told what Skylight said and what was sent, and **recent activity** on the settings page
> keeps the start of it, so the library can be corrected from a real capture.

## Connecting

**Settings → Skylight** (admins only). One connection per household, because a frame belongs to the
household, not to one person. Two ways to sign in:

- **Email and password.** Nori uses them once, to sign in on Skylight's own login form, and keeps
  only the session Skylight hands back (an access token and a refresh token), encrypted at rest like
  every other credential (see [Auth & roles](auth-and-roles.md#secrets-at-rest)). The password is not
  stored, not logged and not shown again.
- **On Skylight's own page, without giving Nori the password.** "Open Skylight's sign-in page" gives a
  link; sign in there, and you land on `https://ourskylight.com/welcome?code=…`. Paste that address back
  within 15 minutes. (Skylight's sign-in only ever sends people to its own web app, so the address has
  to be copied by hand.) A pasted address from a different sign-in attempt is refused.

Connecting turns the tools on. An account with more than one frame gets a picker. The switch on the same
page turns the tools off without signing out; **disconnect** forgets the session on Nori's side.
Nori has no way to end the session on Skylight's side (the library knows no sign-out call).

If the saved session ever stops working (Skylight refuses to refresh it), the tools switch themselves
off, the assistant says an admin needs to reconnect, and the page shows that Skylight signed Nori out.
Signing in again fixes it.

## What the assistant can do

| Tool | Does |
|---|---|
| `skylight_calendar` | events from today (or a start date) for up to 31 days, with who each is for |
| `skylight_add_event` | put an event on the frame; a date alone is all-day, `end` for an all-day event is its last day |
| `skylight_update_event` | change the title, time, people, place or notes of an event |
| `skylight_delete_event` | remove an event (marked consequential: memory is re-checked first; see [Memory](memory.md)) |
| `skylight_lists` | every list, or one, with what is still on it |
| `skylight_add_to_list` | add items (the grocery list unless another is named); items already on it are skipped |
| `skylight_check_off` | check items off by name or id, or put them back with `undo` |
| `skylight_chores` | today's chores (or the next few days'), optionally one person's, done or not |
| `skylight_add_chore` | a one-off chore for the people named, or up for grabs |
| `skylight_complete_chore` | mark a chore done (points go to whoever it belongs to), or not done |
| `skylight_meals` / `skylight_set_meal` | the meal plan, and planning a meal (Breakfast, Lunch, Dinner… as the frame names them) |
| `skylight_rewards` | each person's points and the rewards they are saving for |
| `skylight_family` | the family members' names (what `who` takes) and the frame's time zone |

Times are the household's own wall clock, in the frame's time zone: `2026-11-05T15:30` means 3:30 pm
wherever the frame is, and is sent to Skylight with that zone's offset (Skylight reads a time without an
offset as UTC). People are named as they appear on the frame; an unknown name is refused before anything
is sent, with the names that do exist.

Not included on purpose: deleting lists, chores, rewards or profiles; redeeming rewards (a parent's act);
recurring chores (the library's payload for them is reconstructed, not captured, so it is not offered
until it has been checked against a real frame); device settings (brightness, sleep).

Nori's own [inventory and meal plan](household-planning.md) are separate and unchanged. A household that
lives on its Skylight can simply use the Skylight tools; nothing syncs between the two.

## Safety

- **One chokepoint.** `skylight._call()` is the only place a Skylight client is built. Requests go to
  `app.ourskylight.com` and nowhere else (the transport refuses any other host or plain `http`), one at a
  time, and every call is logged (`skylight_log`, the last 2000; shown on the settings page).
- **One at a time, on purpose.** Skylight rotates the refresh token each time it is used. Two
  refreshes racing would leave one of them holding a dead token and sign Nori out, so calls are
  serialised and a refreshed session is saved before the next call starts.
- **Who.** Members and admins (data scope: the household). Not peer-requestable and in no sub-agent's
  roster: a connected peer or a background job cannot reach the family's Skylight.
- **Not screened.** Event titles, list items and chore names reach the model as typed, the same flagged
  gap the Google/Outlook calendar list carries (see [Content screening](content-screening.md)). Most of it
  is typed by the family, but a Skylight shows calendars synced in from elsewhere, so an invite someone
  else wrote can appear. Every read labels its contents as information, not instructions, and nothing is
  acted on by itself: any change is a tool call the person can see in the chat.

## Storage

- `skylight_connection`: one row per household, `data_enc` (encrypted JSON: the session, the account
  email, the frames and the one in use, its time zone, when and by whom it was connected).
- `skylight_log`: when, who, which action, on what, whether it worked.
- `skylight_enabled` (workspace setting): the switch.
