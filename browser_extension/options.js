// Options: pair with a one-time code (or paste a token), two local switches, and remove.
const $ = (id) => document.getElementById(id);
const L = self.NoriLib;
const KEYS = { baseUrl: "", token: "", name: "", notify: true, includePrivate: false };
let removeArmed = false;

function say(id, text, kind) { const el = $(id); el.textContent = text; el.className = "msg" + (kind ? " " + kind : ""); }

function urlWarning() {
  const b = L.normalizeBaseUrl($("url").value);
  $("url-warn").hidden = !(b.ok && b.warning);
  $("url-warn").textContent = b.ok ? b.warning : "";
}

async function load() {
  const c = await chrome.storage.local.get(KEYS);
  $("url").value = c.baseUrl;
  $("name").value = c.name;
  $("name").placeholder = L.defaultDeviceName(typeof navigator !== "undefined" ? navigator.userAgent : "");
  $("token").value = "";                                   // never put the stored token back on screen
  $("notify").checked = c.notify !== false;
  $("private").checked = !!c.includePrivate;
  const paired = $("paired");
  paired.hidden = !c.token;
  paired.textContent = c.token ? "Paired" + (c.name ? " as “" + c.name + "”" : "") + " with " + c.baseUrl : "";
  urlWarning();
}

$("url").addEventListener("input", urlWarning);

$("pair").addEventListener("click", async () => {
  $("pair").disabled = true;
  say("pair-msg", "Pairing…");
  let r;
  try { r = await chrome.runtime.sendMessage({ type: "pair", baseUrl: $("url").value, code: $("code").value, name: $("name").value }); }
  catch (e) { r = { ok: false, error: "couldn't reach the extension's background worker" }; }
  $("pair").disabled = false;
  if (r && r.ok) {
    $("code").value = "";                                  // a code is single-use; don't keep it around
    say("pair-msg", "Paired as “" + r.name + "”. The popup shows whether it connected.", "ok");
  } else {
    say("pair-msg", (r && r.error) || "Pairing failed", "err");
    return;                                                // keep what he typed so he can fix it
  }
  load();
});

$("save").addEventListener("click", async () => {
  const b = L.normalizeBaseUrl($("url").value);
  const token = $("token").value.trim();
  if (!b.ok) { say("save-msg", b.error, "err"); return; }
  if (!token.startsWith("nbr_")) { say("save-msg", "That doesn't look like a Nori bridge token (it starts with nbr_).", "err"); return; }
  await chrome.storage.local.set(Object.assign(L.freshPairState(), { baseUrl: b.url, token }));   // nothing from an earlier pairing carries over
  $("token").value = "";
  say("save-msg", "Saved. The popup shows whether it connected.", "ok");
  load();
});

$("notify").addEventListener("change", () => chrome.storage.local.set({ notify: $("notify").checked }));
$("private").addEventListener("change", () => chrome.storage.local.set({ includePrivate: $("private").checked }));

$("remove").addEventListener("click", async () => {
  if (!removeArmed) {
    removeArmed = true;
    $("remove").textContent = "Click again to remove";
    say("remove-msg", "This forgets the token in this browser.");
    return;
  }
  removeArmed = false;
  $("remove").textContent = "Remove this browser";
  await chrome.storage.local.set(Object.assign(L.freshPairState(), { token: "", name: "" }));
  say("remove-msg", "Removed from this browser. Also remove it in Nori (Settings → Devices) so the old token stops working.", "ok");
  load();
});

load();
