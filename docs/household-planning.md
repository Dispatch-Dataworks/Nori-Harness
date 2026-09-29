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

## Extending it

A third household-planning tool (a chore rotation, a shopping list
with quantities, whatever comes next) fits the same shape: one table,
member-callable if the data is genuinely shared and low-stakes,
admin-gated only if there's a real reason it should be, and a signal
provider registered with the scheduler if it's the kind of thing worth
mentioning unprompted. See [Contributing](contributing.md).
