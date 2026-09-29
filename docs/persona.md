# The persona editor

Source: `nori/persona_admin.py`, `nori/persona.py`, `nori/promptdoc.py`. Where you change who she is,
and how you get back if a change goes wrong.

## What the persona is

The persona is her character text: voice, manner, how playful or plain she is, when she drops the
personality, what she will and won't do in conversation. It is sent with every reply, near the top
of the prompt (see [Architecture](architecture.md#how-the-prompt-is-assembled)). How she *uses her
tools* is a separate, mechanical document (`prompts/tool-usage.md`) kept apart on purpose, and is not
edited from this page.

## Editing it

**Settings → Administration → Persona.** The page shows the live text in an editor, an estimate of
its size in tokens, and where the text is coming from. **Save persona** validates and writes it; the
change applies on her very next reply, to everyone in the household. No restart.

- **A fresh install needs nothing.** With no `prompts/persona.md`, she uses the shipped default
  (`prompts/persona.default.md`), which is written to work for anyone: it names no operator, assumes
  nothing about who the user is, and ends with a short "not yet written" section that tells her the
  specifics of your relationship (what you call her, how formal to be, running jokes) are yours to add.
  Your first save creates `prompts/persona.md`. That file is yours: it is git-ignored, and an upgrade
  never overwrites it.
- **Validation.** The text must contain the two marker lines (`=== PROMPT STARTS ===` and
  `=== PROMPT ENDS ===`), in order, with something between them, and be under 32 KB. Only the text
  between the markers is sent; anything outside them is for your own notes. A save that fails
  validation changes nothing and the editor keeps what you typed, with the reason shown above it.
- **Writes are atomic** and read back after writing; a mismatch restores the previous text.

## Three layers, and the ways back

Editing the persona changes what she is, and a bad edit can make her worse rather than merely
different. So there are three layers, and they are deliberately distinct:

1. **The shipped default** (`prompts/persona.default.md`): what a fresh install gets. It never
   changes, no page action ever writes to it, and it is always available.
2. **Your baseline**: a last-known-good you mark on purpose with **Save the current text as my
   baseline**. It is set by that one action and nothing else: editing, resetting or restoring never
   creates or moves it, so it cannot drift into whatever you typed last. It is stored with the date
   you saved it, and the page shows how old it is. Saving a new baseline replaces the old one
   (after a confirmation that names the old one's date).
3. **The live text**: what she is running on, freely edited.

There are three ways back, each a separate card on the page and each going through a **preview**: a
line diff of the live text against what it would become, shown before anything changes.

- **Back to my baseline.**
- **Back to the shipped default.**
- **Back one edit.** The version the last save replaced. Doing it twice puts you back where you
  started; to go further back, open any version from the history list (up to the last 100 saves) and
  restore that.

Every restore keeps the text it replaces in the history, so a restore is never a one-way door.

Your baseline survives everything else: an edit, a reset, a restore of any kind, a rejected save, a
hundred more saves than the history keeps, and a restore of the context-tuning baseline (which is
separate and independent, see [Context & tuning](context.md#baseline-and-going-back)). The baseline
is the file `prompts/persona.baseline.md` with its date beside it; both are yours, git-ignored, and
backed up with the rest of `prompts/`.

If you cannot use the browser at all, deleting `prompts/persona.md` returns her to the shipped
default on the next reply. Her prompt can never be empty: if the live file is somehow emptied by hand
(bypassing validation), she falls back to the shipped default rather than being sent nothing.

## Who can change it

Only an administrator, from this page, and only with a valid session and CSRF token, like every other
administration page. It is deliberately **not a tool**: she has no way to edit her own persona, and
neither does a sub-agent, a tool you generated, an MCP server, or a connected peer agent. That is
enforced structurally rather than by convention: the persona-editing code is imported by exactly one
place (the admin routes), no module that registers a model-callable tool can import it, and no
setting the settings tool can read or write names a prompt. The tests
(`tests/test_nori_persona_admin.py`) fail if that changes. See
[Enforcement model](enforcement-model.md) for what "enforced" means across the app.

## What it doesn't do

- It does not record *who* made an edit; history is a list of versions and times. In a household with
  several administrators, agree who edits.
- It does not preview her behaviour. The composition breakdown on the
  [context tuning](context.md) page shows how many tokens the persona takes each turn; to see what an
  edit does, talk to her.
- Two administrators saving at the same moment: the later save wins, and the earlier text is in the
  history.
