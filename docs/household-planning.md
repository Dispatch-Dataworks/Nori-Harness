# Household planning

Source: `nori/household.py` (inventory), `nori/meals.py` (meal
planning).

## The first two real purpose tools

Both are deliberately simple: `household.py` owns `household_items`
(name, quantity, status — one of `ok`/`low`/`out`) and `meals.py` owns
`meal_plan` (a date, a meal type — breakfast/lunch/dinner — and a
free-text description). Each is the only module with raw SQL against
its own table, the same one-module-per-table discipline the rest of
this app follows.

## Workspace-scoped, and deliberately member-callable

Both tools write shared, workspace-wide data (not per-user), and both
are callable by an ordinary member, not gated to admin the way a
Tier-C default would suggest — see [The enforcement model](enforcement-model.md)
for what a risk tier actually means. This was a deliberate choice, not
an oversight: any household member should be able to say "we're out of
milk" or plan tonight's dinner without needing an admin's own
permission. A shared grocery list is mundane, not consequential, and
gatekeeping it would defeat the point of a genuinely shared tool.

## They're also proactive-ping signal sources

Both modules register a function with
`scheduler.register_signal(...)` at import time — see
[Scheduler](scheduler.md#extending-it). Low or out-of-stock inventory
and an unplanned upcoming meal are exactly the kind of thing worth a
household getting an unprompted mention about, subject to the same
quiet-hours/recently-active gates every other proactive ping goes
through.

## They can be switched off

Trackers, the inventory and meal planning are optional features (`features.py`): **Settings -> Features** has a switch for each, per person, on by default. Off means the page and its links leave the "⋯" menu, the chat's quick-action bar and the board; a direct visit goes to Settings -> Features; its pings stop (the signal is never even called); and its tools leave Nori's tool list and refuse if called. What was saved is kept, and turning the switch on again brings all of it back. Nori can do the same through `update_settings` (`feature_meals_enabled`, `feature_inventory_enabled`, `feature_trackers_enabled`), so "turn off meal planning" in chat works.

## Extending it

A third household-planning tool (a chore rotation, a shopping list
with quantities, whatever comes next) fits the same shape: one table,
member-callable if the data is genuinely shared and low-stakes,
admin-gated only if there's a real reason it should be, and a signal
provider registered with the scheduler if it's the kind of thing worth
mentioning unprompted. Add it to `FEATURES` in `features.py` (one entry: key, label, pages, tools) and name it on the signal with `feature="<key>"`; the switch, the Settings -> Features row, the hidden links, the silenced ping, the withdrawn tools and Nori's ability to flip it all follow from that. See [Contributing](contributing.md).
