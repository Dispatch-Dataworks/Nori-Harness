# Onboarding

A fresh instance has two stages: a **first-run setup** that the admin does once (a model for her to think with, then her name and pronouns), and then a **getting-to-know-you conversation** that she leads. After both she is simply their assistant.

## First-run setup (admin)

The first time the admin opens the chat on an instance that has never been set up, they are sent to **/first-run** instead of an empty chat. It has three steps, shown along the top, and each shows as done because the data says so (so it resumes where they left off, and works for an instance that already got a model from `OPENROUTER_API_KEY`):

1. **Connect a provider.** The same providers as Settings -> Model config: an OpenRouter, Anthropic, OpenAI or xAI API key, or a Claude, ChatGPT/Codex, GitHub Copilot or xAI subscription signed in with its code or device flow.
2. **Choose models.** Browse a connected provider's catalogue, add models, press **test** to check each one really answers (a wrong key or a model name the provider doesn't know shows up now, not in her first message), then set the **primary** and up to three **fallbacks**. The first model added in the wizard becomes the primary automatically. The page says plainly when there is no primary, when there is no fallback, and when the fallback sits on the same provider as the primary (which covers a model being down, not the provider being down).
3. **Her name and pronouns.** Her name (shown in the chat header and notifications, and signed on peer messages) and her pronouns: **she/her** (the default), he/him or they/them. The pronouns are in the name header of every prompt (`Your pronouns are she/her: that is how the people around you refer to you.`); the app's own copy stays she/her.

On the name step there is also an optional **Give her a face** card that opens the [avatar builder](avatar-builder.md) (describe her once, an image model draws the whole set of expressions, reviewed before it is installed). It needs an OpenRouter key and a few minutes, runs in the background, and is also in Settings -> Avatars, so it can be done now or later.

Her name is also what the browser tab, the installed-app label (manifest and the iOS home-screen title), the login page and invitations show; only the public /about page keeps the product name. Pressing **Save, and meet her** finishes setup (refused while she has no model, so the admin can't land on a chat that can't answer) and goes to the chat, where she introduces herself. **Skip setup for now** stops the redirect (the chat then says what is missing). It can be reopened from **Settings -> About -> open setup**; it is safe to reopen, it shows what is done.

An instance that already has conversation history is never sent through it: the first time the app looks, it marks such an instance complete. Members are never redirected; until the admin has finished, a member sees a card saying the admin needs to finish setup. The state is three workspace settings (`setup_complete`, `setup_identity`, `assistant_pronouns`); none is in the settings tool's allowlist, so no model call can change them. The code is `firstrun.py`, the page is `server.first_run_page`, and `tests/test_nori_firstrun.py` pins it.

## Seeing it again (Settings -> About, admin)

- **Open setup**: reopens the wizard. It changes nothing until something is pressed, and shows what is already done. **Meet her again** (just below it) restarts only her introduction conversation for the person who presses it.
- **Start over**: for seeing the whole first-time experience on an instance that already has history. It **permanently deletes every member's conversation history** (with its stored summaries and pictures), forgets all onboarding progress and re-opens setup, so the wizard resumes at her name and pronouns and then she introduces herself. It **keeps** accounts, providers and keys, models and the fallback order, her name and pronouns, memories, her persona, files and every other setting. It is admin-only, needs the word `reset` typed, and if a backup key is configured it takes an encrypted backup first and deletes nothing if that backup fails (without a key it says so and takes none). To also start with a clean slate for the rest, clear Settings -> Memory and Settings -> Persona by hand. For a truly blank install (no provider, no accounts), remove the data volume instead. Code: `firstrun.reset`, `conversation.delete_all_for_workspace`, `server.start_over_post`; tests in `tests/test_nori_startover_titles.py`.

## The conversation

Once setup is done she speaks first: she introduces herself, asks what to call them and what they want to call her, confirms their timezone, gets to know them (saving what she learns as memories), drafts her own persona for the admin to approve, asks when it's OK to message them, and gives a tour of the settings and features. Every step is skippable, and she stops when asked.

## What the user sees

1. **No model yet.** If setup was skipped and there is still no model, the chat shows a welcome card pointing the admin to **/first-run** (a member is told to ask the admin). She can't introduce herself until a provider is connected.
2. **First chat after setup.** The page asks the server to have her speak first (`POST /onboarding/begin`) and she opens with a short introduction and her first question.
3. **The conversation** goes roughly: (her name and pronouns are already settled in setup, so she doesn't ask) what to call them → timezone → getting to know them and what they expect of her → ping and notification preferences → (admin) her persona → the tour → a recap and finish. One question at a time, in her own voice.
4. **Skip any time.** "Skip" or "later" ends it. An unrelated request is answered first. It also ends by itself after three days or sixty of her replies, so it can't nag.
5. **Again later:** **Settings → About → Meet her again**.

Invited members get a short version: what to call them, timezone, their own preferences, a tour of what's theirs. Her name and persona are shared by the whole household, so only the admin's version covers them.

An instance that already has history is **never greeted**: anyone with messages is marked done the first time onboarding is looked at, so an upgrade changes nothing for existing users.

## How it works

| Piece | Where |
|---|---|
| State per user: `pending → active → done / skipped`, the steps completed, an unapplied persona draft | table `onboarding`, `onboarding.py` |
| The guide: while active, every chat turn carries it as the **last system message** with a checklist computed from real data (name set? timezone confirmed? how many memories saved since it started? persona draft or applied? tour stops shown) so she doesn't repeat finished steps | `onboarding.guide_message`, injected in `server._run_turn` |
| Her tools, which exist **only while it is active** for that user (`enabled(session)`), the shared ones admin-only | `onboarding_set_name`, `onboarding_set_timezone`, `onboarding_propose_persona` (a draft only), `onboarding_tour`, `onboarding_finish` |
| Memories | the existing `remember` tool; the checklist counts `memory_events` since the start |
| Ping and notification preferences | the existing `update_settings` tool |
| The tour | `onboarding_tour`: a curated list of stops (where it lives in Settings, what it is) with each one's **live state** (on/off, counts) read from config; admin-only stops are hidden from members |
| The empty-state card and the begin call | `chat_page`, `CHAT_JS` (`beginOnboarding`) |

## The persona: she proposes, the admin applies

The persona file is shared by every member and shapes every reply. [It has always been deliberately not tool-writable](persona.md#who-can-change-it), and that still holds:

1. Her current persona is already in her instructions, so she builds on it (keep what works, change tone and relationship).
2. `onboarding_propose_persona(text)` stores a **draft** and puts it in the chat **verbatim** as a card with **Apply this persona** and **Discard** buttons. Nothing is live. The text must use `{{ASSISTANT_NAME}}` (so renaming her later keeps working) and not contain the file's marker lines. A new proposal replaces the old one; only the newest card is actionable.
3. She can't apply it. **Only the admin can**, by pressing the button: an authenticated, CSRF-checked POST (`/onboarding/persona`) that saves through `persona_admin` exactly like a Settings → Persona save, which snapshots the previous persona. **Settings → Persona** restores it (or "reset to default" if there was no custom persona before).
4. Because the onboarding tool module imports none of the persona code, and the only route that settles a draft is that admin POST, a prompt injection can't change her persona. `tests/test_nori_persona_admin.py` pins this (the single persona-named tool is the draft one; the module has no reference to the persona code; only the admin route settles a draft).

## What is enforced, and what isn't

Enforced in code: tools exist only while active and the shared ones only for the admin; name and timezone are validated; no model-callable path can change the persona (only the admin's button does); existing users are never started; it expires. Not enforceable: how well she follows the guide. In a live run against the real model the opening, naming, timezone, memory saves, settings changes (a "don't nag, 8am-9pm" request became ping window 8-21 with pings left on), the persona draft and the tour all worked, and the tour answered from the tool's data rather than guessing once the guide said so. She sometimes jumps ahead of the suggested order, so the guide states the order and the rules; it is a prompt, not a state machine.

## Implementation notes

- A turn with **no conversation yet** (the opening message, or a ping on an empty history) has only system text, which the OpenAI-OAuth endpoint rejects ("input must be provided") and Anthropic too. The adapters now send the last system part as a user-role note from the app in that case (`chat._messages_to_responses_input`, `_call_openai_oauth`, `_messages_to_anthropic`).
- The guide is a system message after the conversation, which the adapters keep in place (see the mid-conversation fix in `docs/scheduler.md` / the adapters' comments).

## Tests

`tests/test_nori_onboarding.py` (state, legacy users, expiry, the guide for admin and member, tool gating, name/timezone validation, the persona draft and card, the admin Apply/Discard route, the tour, the begin endpoint, the welcome card, the page flag), `tests/test_nori_persona_admin.py` (the persona invariants, including this exception) and `tests/test_nori_responses_strict.py` (the empty-conversation request shapes).
