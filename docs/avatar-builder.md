# Avatar builder

Source: `nori/avatar_builder.py`, the page and routes in `nori/server.py` (`avatar_builder_page`, `avatar_build_post`), the per-state `look` text in `nori/emotions.json`.

A new instance used to ship with the original character's portraits, so every instance wore the same face. The builder gives an instance its own: describe her **neutral** face once and an image model draws the rest of the set of [emotion](emotions.md) portraits from it.

Open it from **Settings → Avatars → design her face**, or from the optional "Give her a face" card on the name-and-pronouns step of [first-run setup](onboarding.md). Admin only.

## What it needs first

An **image provider** and an **image model**: an OpenRouter key, or an OpenAI or xAI API key, connected under Settings → Model config (see [Image generation](image-generation.md) for what each provider is sent and why Claude, Copilot and the subscription sign-ins are not options). The page shows a "Connect an image model first" card until one is chosen: pick the provider and model there (or under Settings → Images). OpenRouter is used when nothing else is chosen; OpenAI and xAI are never picked silently, because their model names differ. The model must accept a reference picture, because every portrait after the first is drawn from the chosen face; the defaults (`bytedance-seed/seedream-5-0-lite`, `gpt-image-1`, `grok-imagine-image-2.0`) do.

The connected provider's key is what the image and embedding calls use (`chat.openrouter_key()` for OpenRouter). Before this they read only the environment, so an instance set up through the first-run wizard, which has no `.env` key, could chat but never draw.

## The steps

1. **Describe her.** A sentence or two: roughly how old she looks, hair, eyes, skin, clothing, and the style (a photograph, an illustration, anime). Describe an adult. The description is checked with the same local pre-check the image tools use (`imagegen.prompt_flags`, honouring Settings → Images), and every request carries a fixed text that the character is an adult and fully clothed.
2. **Choose a face.** Three candidate neutral portraits are drawn. Pick one, or draw three different ones from the same description.
3. **Draw the rest.** The other states in `emotions.json` (23 by default) are drawn from the chosen face, three at a time in the background (each takes 30 to 90 seconds, so the whole set is a few minutes). The chosen picture is sent as the image model's reference with a prompt that locks identity (same face, hair, clothing, framing, background) and changes only the expression. You can leave the page; it refreshes itself while pictures are being drawn and the work continues. A failed picture shows its reason and can be retried alone; **redraw** replaces any one picture, and **Redraw all** draws every expression again from the same neutral face (a full set's cost, confirmed first) if the set drifted from the character. Each expression prompt repeats your description and tells the model not to change what kind of being the character is, and the fixed safety text says "if the character is a person"; the first version said "an adult, fully clothed", which told the model the character was human and turned a described robot into a man.
4. **Install.** Only when every picture is drawn. The current avatars are moved to a backup folder first, the new ones take their place, and the neutral face also becomes the reference her selfies are drawn from (`static/avatars/source/neutral.png`). A checkbox (on by default) also rewrites her **selfie style text** (`image_style_prefix`, `image_clothed_constraint`) from the description, because the default text describes the original character and would otherwise be what every selfie is drawn from.
5. **Restore.** If you don't like it, **Restore the earlier avatars** puts back the avatars, the selfie reference and (if install changed them) the selfie text.

Nothing she shows changes before step 4: the draft lives under the data directory (`avatar_build/`), and the backups under `avatar_backups/`, one folder per install.

## Cost

Each picture is one image call. The page shows the estimate before each button (three options, or the remaining expressions) using Settings → Images → per-image cost estimate (the provider's real cost is used when it returns one). A whole build is about 27 pictures with one round of candidates; redraws and extra candidates add to that. The spend is logged to the image log (purpose `avatar_builder`) and counts toward the lifetime total, but **not toward today's household cap**: that cap guards what she spends on her own, and one build would otherwise use up the day's selfies. Nothing is ever drawn without the admin pressing a button.

## Design notes

- **One build at a time**, kept as `state.json` plus the images; no schema. If the process restarts mid-build, the pictures that were in flight come back as retryable.
- **Each state's look** is an optional `"look"` in `emotions.json` (a phrase such as "one eyebrow raised, a smirk pulled to one side"); a state without one still gets a vaguer picture. Adding a state is still just an entry plus a picture.
- **Avatars are revalidated, not cached blind.** `/avatar/<state>` used to be cached for an hour; it now sends an ETag from the file's modified time and size, so a newly installed set shows on the next page load.
- **The app icon follows the set.** The favicon, the home-screen icon and the notification icon are drawn from the installed neutral avatar (the untouched original the builder keeps, else `neutral.*`), and the icon links carry a version that changes with it, so install and restore update them too (`tests/test_nori_icons.py`). An installed PWA may keep its old home-screen icon until reinstalled.
- **Formats.** Whatever the model returns (PNG, JPEG or WebP, checked by the file's contents) is kept as is. Install removes every old file of a state in any format first, because `png` outranks the others and an old `happy.png` would hide a new `happy.webp`.
- The selfie tools still describe themselves in photographic terms ("a single ordinary photograph"); an illustrated or anime character works for the avatars, and selfies of one are a known rough edge.

## Tests

`tests/test_nori_avatar_builder.py`: the readiness rules and key resolution; description checks; the candidate, choose, build, retry, redraw and discard flow with a faked image call (including parallelism limits, references, prompts and failures); install, backup and restore (including format shadowing and the selfie text); the cap exclusion; the pages, admin-only access and draft file path safety; avatar revalidation.
