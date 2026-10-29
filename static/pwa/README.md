# PWA icons

**These three files are only the fallback now.** The server draws the app icon (the browser-tab favicon, the apple-touch icon, the installed-app and notification icons) from the
neutral avatar that is installed (the avatar builder's untouched original in `static/avatars/source/` if there is one, else `static/avatars/neutral.*`), so installing a new avatar set
changes it, and the icon links carry `?v=<avatar version>` so browsers fetch the new one. The shipped files below are served only when there is no usable neutral avatar.

`icon-192.png`, `icon-512.png`, `icon-180.png` (apple-touch-icon) --
generated from `static/avatars/source/neutral.png` (the untouched,
full-resolution original, not the already-resized 900×900 served
avatar), via a plain LANCZOS resize. `neutral` was chosen
deliberately: an install icon shouldn't carry a specific mood.

The 512px icon is declared `purpose: "any maskable"` in the manifest --
checked, not assumed: composited it against a circular mask at the
~80%-safe-zone radius Android actually applies, and the face, eyes, and
hair all stay comfortably inside it. If `neutral.png` is ever
regenerated, redo that check before trusting the same claim again --
nothing here re-verifies it automatically.

To regenerate after a new `neutral` avatar lands:
```python
from PIL import Image
src = Image.open("static/avatars/source/neutral.png").convert("RGB")
for name, size in {"icon-192.png": 192, "icon-512.png": 512, "icon-180.png": 180}.items():
    src.resize((size, size), Image.LANCZOS).save(f"static/pwa/{name}", "PNG", optimize=True)
```
