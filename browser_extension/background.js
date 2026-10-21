// Nori browser bridge -- service worker (v0.2.1). Long-polls the paired Nori server for commands and
// notifications and carries them out in this browser. See ../docs/computer-use.md and ../docs/devices.md.
//
// Safety it enforces itself (the server cannot override these):
//   * only tabs whose REAL url host is on the server-sent allowlist are listed or touched, re-checked
//     before AND after every action; hidden hosts (settings.hidden_hosts) and, unless the options say
//     otherwise, private windows are never listed, read, shared, timed or touched;
//   * risky clicks / typing-with-submit answer "needs approval" and do nothing until the server re-sends
//     the command marked approved for the SAME element;
//   * password / payment / one-time-code fields are never typed into or read;
//   * a command marked look_only (standing look, no session) can only list, read, screenshot or open a NEW
//     tab: click, type and scroll are refused here, whatever the server says;
//   * nothing is read from a page unless he clicks (a Send to Nori menu item, the popup's button, or Ask in
//     the side panel); browsing time is OFF unless the server's switch says so, and is hosts + seconds only;
//   * the Pause switch in the popup stops all of it locally.
//
// MV3 lifetime: Chrome stops a service worker ~30 s after it was last busy, so no wait here exceeds ~28 s,
// the 30 s alarm restarts the loop, and everything that must survive a restart (shown notification ids, the
// last ack, the activity clock, backoffs) lives in chrome.storage, never only in memory.
importScripts("lib.js");
const L = self.NoriLib;
const POLL_TIMEOUT_MS = 28000;
const LOAD_TIMEOUT_MS = 15000;
const CMD_TIMEOUT_MS = 22000;
const REPLY_TIMEOUT_MS = 20000;
const CAPTURE_TIMEOUT_MS = self.NORI_CAPTURE_TIMEOUT_MS || 12000;
const MIN_POLL_GAP_MS = self.NORI_MIN_POLL_GAP_MS != null ? self.NORI_MIN_POLL_GAP_MS : 1000;
const SLICE_MS = self.NORI_SLICE_MS || 5000;
const FORBIDDEN_BACKOFF_MS = 60000;
const SHARE_TIMEOUT_MS = 25000;

let running = false;
let reporting = false;
let actQueue = Promise.resolve();
const notifFailures = new Map();

const DEFAULTS = {
  baseUrl: "", token: "", name: "", paused: false, includePrivate: false, notify: true,
  serverSettings: null, settingsTs: 0, lastAck: 0, shown: [], approvals: {}, notifBlocked: false,
  backoffUntil: 0, authFailedToken: "", badgeUntil: 0,
  activityState: null, activityPending: null, lastActivityReport: 0,
};

const now = () => Date.now();
function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }
async function cfg() { return await chrome.storage.local.get(DEFAULTS); }
async function setStatus(s) { await chrome.storage.local.set({ status: { ...s, ts: now() } }); }
const hiddenOf = (c) => (c.serverSettings && Array.isArray(c.serverSettings.hidden_hosts) ? c.serverSettings.hidden_hosts : []);
const version = () => { try { return chrome.runtime.getManifest().version; } catch (e) { return "0.2.1"; } };

class Refuse extends Error {}

function withTimeout(promise, ms, message) {
  let timer;
  const t = new Promise((_, rej) => { timer = setTimeout(() => rej(new Refuse(message)), ms); });
  return Promise.race([promise, t]).finally(() => clearTimeout(timer));
}

// ───────────────────────── the long poll ─────────────────────────
async function loop() {
  if (running) return;
  running = true;
  try {
    for (;;) {
      try { await iteration(); } catch (e) { await sleep(5000); }     // storage hiccup or the like: never end the loop
    }
  } finally { running = false; }
}

async function iteration() {
  const c = await cfg();
  if (!c.baseUrl || !c.token) { await setStatus({ state: "not configured" }); return sleep(SLICE_MS); }
  if (c.authFailedToken && c.authFailedToken === c.token) return sleep(SLICE_MS);        // removed on the server: wait for a new pairing
  if (c.backoffUntil > now()) return sleep(Math.min(c.backoffUntil - now(), SLICE_MS));   // persisted, so a restarted worker keeps it
  const started = now();
  let busy = false;
  try {
    const v = version();
    const body = { v, ext: { v }, paused: !!c.paused, ack: c.lastAck | 0, tab_visible: !c.paused && (await noriTabVisible(c)) };
    const resp = await post(c, "/bridge/poll", body, POLL_TIMEOUT_MS);
    if (resp.status === 401) { await markRemoved(c); return; }
    if (resp.status === 403) { await setStatus({ state: "switched off on the server" }); await backoff(FORBIDDEN_BACKOFF_MS); return; }
    if (resp.status === 429) { await setStatus({ state: "rate limited" }); await backoff(retryAfterMs(resp)); return; }
    if (resp.status < 200 || resp.status >= 300) { await setStatus({ state: "can't reach Nori", error: "the server answered " + resp.status }); await backoff(10000); return; }
    const data = (await resp.json()) || {};
    const settings = L.parseSettings(data.settings, c.serverSettings);
    await chrome.storage.local.set({ serverSettings: settings, settingsTs: now() });
    if (data.paused || c.paused) { await setStatus({ state: "paused" }); await sleep(3000); return; }
    await setStatus({ state: data.active ? "session running" : "idle (no session)", session_until: data.session_until });
    if (Array.isArray(data.messages) && data.messages.length) { busy = true; await handleMessages(c, settings, data.messages); }
    if (data.command) { busy = true; await handle(c, data.command, settings); }
    try { await activityTick(false); } catch (e) { /* browsing time must never break the loop */ }
  } catch (e) {
    if (String(e).includes("AbortError")) return;           // a poll that ran its full time: poll again
    await setStatus({ state: "can't reach Nori", error: String(e).slice(0, 120) });
    return sleep(5000);
  }
  const gap = MIN_POLL_GAP_MS - (now() - started);          // a server that answers an empty poll at once must not be hammered
  if (!busy && gap > 0) await sleep(gap);
}

async function post(c, path, body, timeoutMs) {
  const ctl = new AbortController();
  const t = setTimeout(() => ctl.abort(), timeoutMs || 20000);
  try {
    return await fetch(c.baseUrl.replace(/\/+$/, "") + path, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: "Bearer " + c.token },
      body: JSON.stringify(body),
      signal: ctl.signal,
    });
  } finally { clearTimeout(t); }
}

async function backoff(ms) { await chrome.storage.local.set({ backoffUntil: now() + ms }); }
function retryAfterMs(resp) {
  try {
    const h = resp.headers && resp.headers.get && resp.headers.get("Retry-After");
    const s = Number(h);
    if (Number.isFinite(s) && s > 0) return Math.min(300, Math.max(5, s)) * 1000;
  } catch (e) { /* no header */ }
  return 30000;
}
// 401: this token is gone. Say so and stop talking to the server until a new pairing replaces it.
async function markRemoved(c) {
  await chrome.storage.local.set({ authFailedToken: c.token });
  await setStatus({ state: "removed, re-pair" });
}

async function noriTabVisible(c) {
  try {
    const w = await chrome.windows.getLastFocused();
    if (!w || !w.focused) return false;
    const [t] = await chrome.tabs.query({ active: true, windowId: w.id });
    return !!(t && L.sameOrigin(t.url || "", c.baseUrl));
  } catch (e) { return false; }
}

// ───────────────────────── notifications ─────────────────────────
function nCall(fn, ...args) {
  return new Promise((resolve, reject) => {
    try {
      fn(...args, (r) => {
        const err = chrome.runtime.lastError;
        if (err) reject(new Error(err.message || "notification error")); else resolve(r);
      });
    } catch (e) { reject(e); }
  });
}
const nCreate = (id, o) => nCall((...a) => chrome.notifications.create(...a), id, o);
const nUpdate = (id, o) => nCall((...a) => chrome.notifications.update(...a), id, o);
const nClear = (id) => nCall((...a) => chrome.notifications.clear(...a), id);

async function notificationsAllowed() {
  try { return (await nCall((...a) => chrome.notifications.getPermissionLevel(...a))) !== "denied"; } catch (e) { return true; }
}

// One notification per message, oldest first. The ack only moves past a message once it has been SHOWN (or was
// already shown: a replay), or deliberately dropped because notifications are switched off. A failure to show
// stops here, so the server re-sends it.
async function handleMessages(c0, settings, raw) {
  const msgs = L.normalizeMessages(raw);
  if (!msgs.length) return;
  const c = await cfg();                                  // fresh: an approval answered a moment ago must not be resurrected
  const on = c.notify !== false && settings.notifications !== false;
  const permitted = on ? await notificationsAllowed() : true;
  let shown = (c.shown || []).slice();
  let ack = c.lastAck | 0;
  let blocked = on && !permitted;
  for (const m of msgs) {
    if (L.ledgerHas(shown, m.id)) { ack = L.nextAck(ack, m.id); continue; }            // a replay: already shown
    if (!on || !permitted) { ack = L.nextAck(ack, m.id); continue; }                   // switched off here or in the browser/OS: dropped, not queued
    const spec = L.notificationSpec(m, { iconUrl: "icon128.png", hidden: hiddenOf(c) });
    try {
      await nCreate(spec.id, spec.options);
    } catch (e) {
      const n = (notifFailures.get(m.id) || 0) + 1;
      notifFailures.set(m.id, n);
      if (n >= 3) { ack = L.nextAck(ack, m.id); continue; }                            // never let one bad message wedge the rest
      await setStatus({ state: "can't show notifications", error: String(e).slice(0, 120) });
      await backoff(10000);
      break;
    }
    shown = L.ledgerAdd(shown, m.id);
    ack = L.nextAck(ack, m.id);
    const save = { shown, lastAck: ack };                                              // persisted before the next poll can ack it
    if (spec.approval) {
      const current = Object.assign({}, (await cfg()).approvals || {});                // read-modify-write: an answer given a moment ago stays answered
      current[spec.id] = { id: spec.approval.id, summary: spec.approval.summary, host: spec.approval.host, ts: now() };
      save.approvals = pruneApprovals(current);
    }
    await chrome.storage.local.set(save);
  }
  await chrome.storage.local.set({ lastAck: ack, notifBlocked: blocked });
}

function pruneApprovals(map) {
  const cutoff = now() - 24 * 3600 * 1000;
  const keep = Object.entries(map).filter(([, v]) => v && v.ts > cutoff).sort((a, b) => b[1].ts - a[1].ts).slice(0, 30);
  return Object.fromEntries(keep);
}

async function openNori() {
  const c = await cfg();
  if (!c.baseUrl) return;
  try {
    const tabs = await chrome.tabs.query({});
    const mine = tabs.filter((t) => L.sameOrigin(t.url || "", c.baseUrl));
    const t = mine.find((x) => x.active) || mine[0];
    if (t) {
      await chrome.tabs.update(t.id, { active: true });
      try { await chrome.windows.update(t.windowId, { focused: true }); } catch (e) { /* the window may be gone */ }
      return;
    }
  } catch (e) { /* fall through and open a new one */ }
  await chrome.tabs.create({ url: c.baseUrl, active: true });
}

chrome.notifications.onClicked.addListener((nid) => {
  if (!String(nid).startsWith("nori-")) return;
  nClear(nid).catch(() => {});
  openNori().catch(() => {});
});

chrome.notifications.onClosed.addListener((nid) => {
  if (!String(nid).startsWith("nori-")) return;
  cfg().then(async (c) => {
    if (c.approvals && c.approvals[nid]) { const a = Object.assign({}, c.approvals); delete a[nid]; await chrome.storage.local.set({ approvals: a }); }
  }).catch(() => {});
});

// Approve / Deny on an approval notification: the same decision as the chat card, sent with the bearer token.
chrome.notifications.onButtonClicked.addListener((nid, index) => { onApprovalButton(String(nid), index).catch(() => {}); });

async function onApprovalButton(nid, index) {
  if (!nid.startsWith("nori-")) return;
  const decision = L.decisionForButton(index);
  const c = await cfg();
  const a = c.approvals && c.approvals[nid];
  if (!a || !decision) return;                                  // only an approval we offered ourselves can be answered
  const note = async (message, extra) => { try { await nUpdate(nid, Object.assign({ message }, extra || {})); } catch (e) { /* gone */ } };
  const record = (ok, error) => chrome.storage.local.set({ lastApproval: { ok, decision, error: error || "", ts: now() } });
  if (!c.baseUrl || !c.token) return;
  if (c.paused) { await note("Nori is paused on this browser. Answer in the chat instead."); await record(false, "paused"); return; }
  let resp;
  try { resp = await post(c, "/bridge/approval", { id: a.id, decision }, 15000); }
  catch (e) { await note("Couldn't reach Nori. Try again, or answer in the chat."); await record(false, "could not reach the server"); return; }
  const data = await resp.json().catch(() => ({}));
  const drop = async () => { const m = Object.assign({}, (await cfg()).approvals); delete m[nid]; await chrome.storage.local.set({ approvals: m }); };
  if (resp.status >= 200 && resp.status < 300 && data.ok !== false) {
    await drop();
    await note(decision === "approve" ? "Approved." : "Denied.", { buttons: [], requireInteraction: false });
    await record(true);
    setTimeout(() => { nClear(nid).catch(() => {}); }, 4000);
    return;
  }
  const why = L.httpErrorText(resp.status, data.error, "approving from here");
  if (resp.status === 401) await markRemoved(c);
  if (resp.status === 404 || resp.status === 409 || resp.status === 410) {
    await drop();
    await note("This was already answered or has expired.", { buttons: [], requireInteraction: false });
  } else {
    await note("Not sent: " + why);                             // the buttons stay so he can try again
  }
  await record(false, why);
}

// ───────────────────────── commands from the server ─────────────────────────
async function noteCommand(cmd, ok, error, waiting) {
  try { await chrome.storage.local.set({ lastCommand: { action: String(cmd.action || "").slice(0, 30), ok, error: error || "", waiting: !!waiting, look_only: !!cmd.look_only, ts: now() } }); } catch (e) { /* ignore */ }
}

async function reply(c, id, payload) {
  try { await post(c, "/bridge/result", { id, ...payload }, REPLY_TIMEOUT_MS); } catch (e) { /* the server expires it */ }
}

async function handle(c, cmd, settings) {
  const allowed = L.sitePolicy(cmd);
  const pol = { hidden: settings.hidden_hosts || [], includePrivate: !!c.includePrivate, lookOnly: !!cmd.look_only, standingLook: settings.standing_look === true };
  let result;
  try {
    const out = await withTimeout(run(cmd, allowed, pol), CMD_TIMEOUT_MS, "the browser took too long to do that");
    if (out.needs_approval) { await reply(c, cmd.id, out); await noteCommand(cmd, true, "", true); return; }
    result = { ok: true, result: out.result, host: out.host };
  } catch (e) {
    result = { ok: false, error: e instanceof Refuse ? e.message : "browser error: " + String(e).slice(0, 200) };
  }
  await reply(c, cmd.id, result);
  await noteCommand(cmd, !!result.ok, result.error);
}

async function tabChecked(tabId, allowed, pol) {
  let tab;
  try { tab = await chrome.tabs.get(tabId); } catch (e) { throw new Refuse("no such tab"); }
  if (tab.incognito && !pol.includePrivate) throw new Refuse("that tab is in a private window, which isn't shared");
  const host = L.hostOf(tab.url || "");
  if (L.hostHidden(host, pol.hidden)) throw new Refuse("that tab is on a site you keep private");
  if (!L.hostAllowed(host, allowed)) throw new Refuse("that tab isn't on a site you allowed" + (host ? " (" + host + ")" : ""));
  return { tab, host };
}

async function run(cmd, allowed, pol) {
  const a = cmd.args || {};
  const look = L.lookOnlyVerdict(cmd, pol.standingLook);        // standing look: only tabs / read / screenshot / open (new tab)
  if (!look.ok) throw new Refuse(look.error);
  switch (cmd.action) {
    case "tabs": return await actTabs(allowed, pol);
    case "open": return await actOpen(a, allowed, pol);
    case "read": return await actRead(a, allowed, pol);
    case "click": return await actAct("click", cmd, allowed, pol);
    case "type": return await actAct("type", cmd, allowed, pol);
    case "scroll": return await actScroll(a, allowed, pol);
    case "screenshot": return await actScreenshot(a, allowed, pol);
    default: throw new Refuse("unknown action " + cmd.action);
  }
}

async function actTabs(allowed, pol) {
  const tabs = await chrome.tabs.query({});
  const visible = [];
  let hidden = 0;
  for (const t of tabs) {
    const host = L.hostOf(t.url || "");
    if (t.incognito && !pol.includePrivate) { hidden++; continue; }
    if (!L.hostHidden(host, pol.hidden) && L.hostAllowed(host, allowed)) visible.push({ id: t.id, host, title: (t.title || "").slice(0, 80), active: !!t.active });
    else hidden++;
  }
  return { result: { tabs: visible, hidden }, host: null };
}

function waitForLoad(tabId) {
  return new Promise((resolve) => {
    let done = false;
    const finish = () => { if (!done) { done = true; chrome.tabs.onUpdated.removeListener(l); resolve(); } };
    const l = (id, info) => { if (id === tabId && info.status === "complete") finish(); };
    chrome.tabs.onUpdated.addListener(l);
    setTimeout(finish, LOAD_TIMEOUT_MS);
    chrome.tabs.get(tabId).then((t) => { if (t.status === "complete") finish(); }).catch(finish);
  });
}

async function actOpen(a, allowed, pol) {
  const host = L.hostOf(a.url);
  if (L.hostHidden(host, pol.hidden)) throw new Refuse("that site is one you keep private");
  if (!L.hostAllowed(host, allowed)) throw new Refuse((host || "that address") + " isn't on your allowed-sites list");
  let tab;
  // Standing look never navigates an existing tab: it opens a NEW one, whatever new_tab says.
  if (a.new_tab === false && !pol.lookOnly) {
    const [active] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!active) throw new Refuse("no active tab to navigate");
    await tabChecked(active.id, allowed, pol);   // never navigate a tab that isn't itself an allowed one
    tab = await chrome.tabs.update(active.id, { url: a.url });
  } else {
    tab = await chrome.tabs.create({ url: a.url, active: true });
  }
  await waitForLoad(tab.id);
  const after = await chrome.tabs.get(tab.id);
  const afterHost = L.hostOf(after.url || "");
  if (!L.hostAllowed(afterHost, allowed) || L.hostHidden(afterHost, pol.hidden)) {
    await chrome.tabs.update(tab.id, { url: "about:blank" });
    throw new Refuse("the page redirected to " + (afterHost || "somewhere") + ", which isn't allowed, so I left it");
  }
  return { result: { tab_id: tab.id, url: after.url, title: (after.title || "").slice(0, 80) }, host: afterHost };
}

// ── functions injected into the page (self-contained; JSON in, JSON out) ──
function pageCollect() {
  const SENS = /(pass(word|wd|code)?|passcode|pin\b|cvv|cvc|card.?(number|num|no)|cc-|credit|ssn|social.?sec|routing|account.?(number|num)|iban|otp|one.?time|2fa|mfa|security.?code|secret)/i;
  const sel = 'a[href], button, input, textarea, select, [role=button], [role=link], [role=textbox], [role=checkbox], [role=menuitem], [contenteditable=""], [contenteditable=true]';
  const refs = new Map();
  window.__noriRefs = refs;
  const out = [];
  const labelOf = (el) => {
    let t = el.getAttribute("aria-label") || "";
    if (!t && el.labels && el.labels[0]) t = el.labels[0].innerText;
    if (!t) t = el.innerText || el.placeholder || el.title || el.getAttribute("name") || el.getAttribute("alt") || "";
    if (!t && (el.type === "submit" || el.type === "button")) t = el.value || "";
    return String(t).replace(/\s+/g, " ").trim().slice(0, 80);
  };
  for (const el of document.querySelectorAll(sel)) {
    if (out.length >= 120) break;
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    const st = getComputedStyle(el);
    if (st.visibility === "hidden" || st.display === "none") continue;
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute("type") || "").toLowerCase();
    const ref = "e" + (out.length + 1);
    refs.set(ref, el);
    let hrefHost = "";
    if (tag === "a" && el.href) { try { const u = new URL(el.href); if (u.protocol.startsWith("http")) hrefHost = u.hostname.toLowerCase(); } catch (e) {} }
    const label = labelOf(el);
    const sensitive = type === "password" || /(password|cc-|one-time-code)/i.test(el.getAttribute("autocomplete") || "") ||
      SENS.test([el.getAttribute("name"), el.id, label, el.placeholder].filter(Boolean).join(" "));
    out.push({
      ref, tag, type, role: el.getAttribute("role") || (tag === "a" ? "link" : tag === "button" ? "button" : tag === "select" ? "select" : (tag === "input" || tag === "textarea") ? "field" : tag),
      label: sensitive ? "(sensitive field)" : label,
      name: el.getAttribute("name") || "", id_attr: el.id || "", autocomplete: el.getAttribute("autocomplete") || "",
      placeholder: sensitive ? "" : (el.placeholder || ""), href_host: hrefHost,
      is_submit: type === "submit" || (tag === "button" && (type === "" || type === "submit") && !!el.closest("form")),
      sensitive,
    });
  }
  let text = "";
  try { text = (document.body && document.body.innerText) || ""; } catch (e) {}
  return { url: location.href, title: document.title, text: text.slice(0, 40000), elements: out };
}

function pageDescribe(ref) {
  const el = window.__noriRefs && window.__noriRefs.get(ref);
  if (!el || !el.isConnected) return { missing: true };
  const SENS = /(pass(word|wd|code)?|passcode|pin\b|cvv|cvc|card.?(number|num|no)|cc-|credit|ssn|social.?sec|routing|account.?(number|num)|iban|otp|one.?time|2fa|mfa|security.?code|secret)/i;
  const tag = el.tagName.toLowerCase();
  const type = (el.getAttribute("type") || "").toLowerCase();
  let label = el.getAttribute("aria-label") || (el.labels && el.labels[0] && el.labels[0].innerText) || el.innerText || el.placeholder || el.title || el.getAttribute("name") || "";
  if (!label && (type === "submit" || type === "button")) label = el.value || "";
  label = String(label).replace(/\s+/g, " ").trim().slice(0, 80);
  let hrefHost = "";
  if (tag === "a" && el.href) { try { const u = new URL(el.href); if (u.protocol.startsWith("http")) hrefHost = u.hostname.toLowerCase(); } catch (e) {} }
  const sensitive = type === "password" || /(password|cc-|one-time-code)/i.test(el.getAttribute("autocomplete") || "") ||
    SENS.test([el.getAttribute("name"), el.id, label, el.placeholder].filter(Boolean).join(" "));
  return { tag, type, label: sensitive ? "(sensitive field)" : label, name: el.getAttribute("name") || "", id_attr: el.id || "",
    autocomplete: el.getAttribute("autocomplete") || "", placeholder: el.placeholder || "", href_host: hrefHost,
    role: el.getAttribute("role") || "", is_submit: type === "submit" || (tag === "button" && (type === "" || type === "submit") && !!el.closest("form")),
    currentHost: location.hostname.toLowerCase() };
}

function pageClick(ref) {
  const el = window.__noriRefs && window.__noriRefs.get(ref);
  if (!el || !el.isConnected) return { ok: false, error: "that element is gone -- read the page again" };
  el.scrollIntoView({ block: "center" });
  el.click();
  return { ok: true };
}

function pageType(ref, text, submit) {
  const el = window.__noriRefs && window.__noriRefs.get(ref);
  if (!el || !el.isConnected) return { ok: false, error: "that element is gone -- read the page again" };
  el.scrollIntoView({ block: "center" });
  el.focus();
  if (el.isContentEditable) {
    document.execCommand("selectAll", false);
    document.execCommand("insertText", false, text);
  } else {
    const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value");
    if (setter && setter.set) setter.set.call(el, text); else el.value = text;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }
  if (submit) {
    const form = el.form;
    for (const t of ["keydown", "keypress", "keyup"]) el.dispatchEvent(new KeyboardEvent(t, { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true }));
    if (form && form.requestSubmit) { try { form.requestSubmit(); } catch (e) {} }
  }
  return { ok: true };
}

function pageScroll(direction) {
  const h = window.innerHeight * 0.8;
  if (direction === "top") window.scrollTo(0, 0);
  else if (direction === "bottom") window.scrollTo(0, document.documentElement.scrollHeight);
  else window.scrollBy(0, direction === "up" ? -h : h);
  return { ok: true, y: Math.round(window.scrollY), height: document.documentElement.scrollHeight };
}

async function inPage(tabId, func, args) {
  const [r] = await chrome.scripting.executeScript({ target: { tabId }, func, args: args || [] });
  return r && r.result;
}

async function actRead(a, allowed, pol) {
  const { tab, host } = await tabChecked(a.tab_id, allowed, pol);
  const page = await inPage(tab.id, pageCollect);
  if (!page) throw new Refuse("couldn't read that page");
  const after = await tabChecked(tab.id, allowed, pol);   // it may have navigated while we read
  const ctx = { allowed, currentHost: host };
  for (const el of page.elements) {
    const isField = el.tag === "input" || el.tag === "textarea" || el.tag === "select";
    // Flag what a click would pause for (buttons/links); fields are never "risky" to click,
    // and sensitive ones already read as "(sensitive field)".
    el.risky = !isField && L.classify("click", el, ctx).level !== "ok";
    delete el.name; delete el.id_attr; delete el.autocomplete; delete el.placeholder; delete el.is_submit; delete el.sensitive;
  }
  return { result: page, host: after.host };
}

async function actAct(kind, cmd, allowed, pol) {
  const a = cmd.args || {};
  const { tab, host } = await tabChecked(a.tab_id, allowed, pol);
  const el = await inPage(tab.id, pageDescribe, [a.ref]);
  if (!el || el.missing) throw new Refuse("that element is gone -- read the page again");
  const ctx = { allowed, currentHost: host, forceApproval: !!cmd.force_approval, submit: !!a.submit };
  const verdict = L.classify(kind, el, ctx);
  if (verdict.level === "blocked") throw new Refuse(verdict.reason);
  const fp = L.fingerprint(el);
  if (verdict.level === "approval") {
    const approved = cmd.approved && a._expect && a._expect === fp;
    if (cmd.approved && !approved) throw new Refuse("the page changed since you were asked -- read it again and re-request");
    if (!approved) {
      return { needs_approval: true, reason: verdict.reason, host,
               element: { describe: kind + " " + L.describe(el) + (a.submit ? " and press Enter" : ""), fingerprint: fp } };
    }
  }
  const res = kind === "click" ? await inPage(tab.id, pageClick, [a.ref]) : await inPage(tab.id, pageType, [a.ref, String(a.text || "").slice(0, 2000), !!a.submit]);
  if (!res || !res.ok) throw new Refuse((res && res.error) || "that didn't work");
  await sleep(700);
  const after = await chrome.tabs.get(tab.id);
  const afterHost = L.hostOf(after.url || "");
  if (!L.hostAllowed(afterHost, allowed)) {
    try { await chrome.tabs.goBack(tab.id); } catch (e) {}
    throw new Refuse("that took the tab to " + (afterHost || "another site") + ", which isn't allowed; I went back");
  }
  return { result: { ok: true, url: after.url, title: (after.title || "").slice(0, 80) }, host: afterHost };
}

async function actScroll(a, allowed, pol) {
  const { tab, host } = await tabChecked(a.tab_id, allowed, pol);
  const r = await inPage(tab.id, pageScroll, [a.direction]);
  return { result: r || { ok: true }, host };
}

async function actScreenshot(a, allowed, pol) {
  const { tab, host } = await tabChecked(a.tab_id, allowed, pol);
  if (pol.lookOnly) {
    // Standing look only looks at what is already in front; it never switches his tabs around.
    if (!tab.active) throw new Refuse("that tab isn't the one in front, and with no session I only look at what is showing");
  } else {
    await chrome.tabs.update(tab.id, { active: true });
    await sleep(250);
  }
  let quality = 60, url = "";
  for (;;) {
    url = await withTimeout(chrome.tabs.captureVisibleTab(tab.windowId, { format: "jpeg", quality }), CAPTURE_TIMEOUT_MS, "the browser did not produce a screenshot in time");
    if (url.length < 4_000_000 || quality <= 25) break;
    quality -= 15;
  }
  await tabChecked(tab.id, allowed, pol);   // still the allowed site after the capture
  return { result: { jpeg_b64: url.replace(/^data:image\/jpeg;base64,/, "") }, host };
}

// ───────────────────────── Send to Nori ─────────────────────────
function makeMenus() {
  try {
    chrome.contextMenus.removeAll(() => {
      for (const m of L.shareMenuSpec()) {
        try { chrome.contextMenus.create(m, () => { void chrome.runtime.lastError; }); } catch (e) { /* a menu is a convenience */ }
      }
    });
  } catch (e) { /* menus are a convenience */ }
}

async function clearBadge() {
  try { await chrome.action.setBadgeText({ text: "" }); await chrome.action.setTitle({ title: "Nori" }); await chrome.storage.local.set({ badgeUntil: 0 }); } catch (e) { /* only feedback */ }
}
async function badge(text, color, title) {
  try {
    await chrome.action.setBadgeBackgroundColor({ color });
    await chrome.action.setBadgeText({ text });
    await chrome.action.setTitle({ title: "Nori: " + String(title).slice(0, 100) });
    await chrome.storage.local.set({ badgeUntil: now() + 5000 });     // so a worker that dies in between still clears it
    setTimeout(clearBadge, 5000);
  } catch (e) { /* the badge is only feedback */ }
}

// req: {kind: page|selection|link|screenshot, dest, withText, note, tab, selection, linkUrl, pageUrl}
// Resolves {ok, error?}. Every refusal is shown (badge "x" + the popup's "last send") and nothing is sent.
async function doShare(req) {
  const c = await cfg();
  const dest = L.normDest(req.dest);
  const kind = req.kind;
  const fail = async (why) => {
    await chrome.storage.local.set({ lastShare: { ok: false, error: why, ts: now(), kind, dest } });
    await badge("x", "#b00020", why);
    return { ok: false, error: why };
  };
  if (L.SHARE_KINDS.indexOf(kind) < 0) return fail("unknown kind of share");
  let tab = req.tab;
  if (!tab) { try { tab = (await chrome.tabs.query({ active: true, lastFocusedWindow: true }))[0]; } catch (e) { /* none */ } }
  if (!tab) return fail("there is no tab to send");
  const hidden = hiddenOf(c);
  const pol = { configured: !!(c.baseUrl && c.token), paused: !!c.paused, includePrivate: !!c.includePrivate, hidden };
  const gate = L.gatePage(tab.url || req.pageUrl, tab.incognito, pol);
  if (!gate.ok) return fail(gate.error);
  const here = L.shareUrl(tab.url || req.pageUrl, hidden);
  if (!here.ok) return fail(here.error);
  const o = { kind, dest, url: here.url, title: tab.title, note: req.note };
  try {
    if (kind === "link") {
      const target = L.shareUrl(req.linkUrl, hidden);
      if (!target.ok) return fail(target.error);
      o.url = target.url;
      delete o.title;
    } else if (kind === "selection") {
      o.selection = req.selection;
    } else if (kind === "page" && req.withText) {
      const page = await withTimeout(inPage(tab.id, L.pageTextCollector, [L.LIM.PAGE_TEXT_SHARE]), 15000, "the page's text could not be read in time");
      const now2 = await chrome.tabs.get(tab.id);
      if (L.hostOf(now2.url || "") !== gate.host || (now2.incognito && !c.includePrivate)) return fail("the tab changed while I was reading it, so I didn't send it");
      o.pageText = page ? page.text : "";
    } else if (kind === "screenshot") {
      const shot = await captureForShare(tab, gate.host, c, hidden);
      if (shot.error) return fail(shot.error);
      o.jpegB64 = shot.b64;
    }
  } catch (e) {
    return fail(e instanceof Refuse ? e.message : "couldn't read that page (" + String(e).slice(0, 80) + ")");
  }
  const built = L.buildShareBody(o);
  if (!built.ok) return fail(built.error);
  let resp;
  try { resp = await post(c, "/bridge/share", built.body, SHARE_TIMEOUT_MS); }
  catch (e) { return fail("couldn't reach Nori"); }
  const data = await resp.json().catch(() => ({}));
  if (resp.status === 401) await markRemoved(c);
  if (resp.status >= 200 && resp.status < 300 && data.ok === true) {
    await chrome.storage.local.set({ lastShare: { ok: true, error: "", ts: now(), kind, dest } });
    await badge("ok", "#1b7f3b", "sent to Nori");
    return { ok: true };
  }
  return fail(L.httpErrorText(resp.status, data.error, "sharing"));
}

// A screenshot of the tab he is looking at, as a JPEG that fits under ~4 MB (quality steps down). Refused if the
// tab is not the one in front, or if anything about it changed (tab, address, window) while the capture ran.
async function captureForShare(tab, host, c, hidden) {
  const winId = tab.windowId;
  const [front] = await chrome.tabs.query({ active: true, windowId: winId });
  if (!front || front.id !== tab.id) return { error: "that tab isn't the one in front, so I can't take its picture" };
  const urlBefore = String(front.url || "").split("#")[0];
  let b64 = "";
  for (const quality of L.JPEG_QUALITIES) {
    const dataUrl = await withTimeout(chrome.tabs.captureVisibleTab(winId, { format: "jpeg", quality }), CAPTURE_TIMEOUT_MS, "the browser did not produce a screenshot in time");
    b64 = L.dataUrlToJpeg(dataUrl) || "";
    if (!b64) return { error: "the browser returned an unreadable picture" };
    if (b64.length < L.LIM.JPEG_B64) break;
  }
  if (b64.length >= L.LIM.JPEG_B64) return { error: "the screenshot is too large to send, even compressed" };
  const [afterFront] = await chrome.tabs.query({ active: true, windowId: winId });
  let afterTab = null;
  try { afterTab = await chrome.tabs.get(tab.id); } catch (e) { /* closed */ }
  const changed = !afterFront || afterFront.id !== tab.id || !afterTab || String(afterTab.url || "").split("#")[0] !== urlBefore;
  const gate = afterTab ? L.gatePage(afterTab.url, afterTab.incognito, { configured: true, paused: false, includePrivate: c.includePrivate, hidden }) : { ok: false };
  if (changed || !gate.ok || gate.host !== host) return { error: "the tab changed while I was taking the picture, so I didn't send it" };
  return { b64 };
}

chrome.contextMenus.onClicked.addListener((info, tab) => {
  const m = L.parseShareMenuId(info && info.menuItemId);
  if (!m) return;
  doShare({ kind: m.kind, dest: m.dest, withText: m.withText, tab, selection: info.selectionText, linkUrl: info.linkUrl, pageUrl: info.pageUrl }).catch(() => {});
});

// ───────────────────────── pairing by code ─────────────────────────
async function pairWithCode(req) {
  const base = L.normalizeBaseUrl(req.baseUrl);
  if (!base.ok) return { ok: false, error: base.error };
  const code = L.normalizePairCode(req.code);
  if (!code) return { ok: false, error: "A pairing code looks like XXXX-XXXX (8 letters and digits). Make one in Nori: Settings → Devices → Pair a browser." };
  const ua = typeof navigator !== "undefined" ? navigator.userAgent : "";
  const body = L.registerBody({ code, name: req.name, model: L.browserModel(ua), v: version(), ua });
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), 15000);
  let resp, data;
  try {
    resp = await fetch(base.url + "/bridge/register", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), signal: ctl.signal });
    data = await resp.json().catch(() => ({}));
  } catch (e) {
    return { ok: false, error: "Couldn't reach " + base.url + " (" + String(e).slice(0, 80) + ")" };
  } finally { clearTimeout(timer); }
  if (resp.status === 429) return { ok: false, error: "Too many tries: wait a few minutes, then pair again" };
  if (resp.status === 403) return { ok: false, error: L.cleanText(data && data.error, 160) || "Pairing is switched off on the server" };
  if (resp.status < 200 || resp.status >= 300) return { ok: false, error: L.cleanText(data && data.error, 160) || "That code wasn't accepted (it may be wrong, already used, or older than 15 minutes)" };
  const token = data && typeof data.token === "string" ? data.token : "";
  if (!/^nbr_[A-Za-z0-9_-]{16,}$/.test(token)) return { ok: false, error: "The server's answer didn't contain a browser token, so nothing was saved" };
  const name = L.cleanText(data.name, L.LIM.NAME) || body.name;
  await chrome.storage.local.set(Object.assign(L.freshPairState(), { baseUrl: base.url, token, name }));
  return { ok: true, name, warning: base.warning };
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  // Only this extension's own pages (popup, options, side panel) may ask for these.
  if (!sender || sender.id !== chrome.runtime.id || !msg || typeof msg !== "object") return false;
  if (sender.url && !String(sender.url).startsWith(chrome.runtime.getURL(""))) return false;
  if (msg.type === "pair") { pairWithCode(msg).then(sendResponse, (e) => sendResponse({ ok: false, error: String(e).slice(0, 120) })); return true; }
  if (msg.type === "share" && (msg.kind === "page" || msg.kind === "screenshot")) {
    doShare({ kind: msg.kind, dest: msg.dest, withText: !!msg.withText && msg.kind === "page", note: msg.note }).then(sendResponse, (e) => sendResponse({ ok: false, error: String(e).slice(0, 120) }));
    return true;
  }
  return false;
});

// ───────────────────────── passive browsing time ─────────────────────────
// Off unless the server's switch (seen in the last 3 minutes) says so AND he is not paused. The clock times the
// active tab of the focused window while he is not idle; only host names and seconds are ever kept or sent.
function serial(fn) {
  const p = actQueue.then(fn, fn);
  actQueue = p.catch(() => {});
  return p;
}

async function queryIdle() {
  try { return await chrome.idle.queryState(120); } catch (e) { return "active"; }
}

async function doRefresh(idleOverride) {
  const c = await cfg();
  const st = L.sharingState(c, now());
  if (!st.activity) {
    if (c.activityState || c.activityPending) await chrome.storage.local.set({ activityState: null, activityPending: null });
    return false;
  }
  const idle = idleOverride || (await queryIdle());
  let focused = false, tab = null;
  try {
    const w = await chrome.windows.getLastFocused();
    focused = !!(w && w.focused);
    if (focused) tab = (await chrome.tabs.query({ active: true, windowId: w.id }))[0] || null;
  } catch (e) { /* no window */ }
  const host = L.activityHost(tab, focused, idle, { includePrivate: c.includePrivate, hidden: st.hidden });
  const clock = new L.ActivityClock(c.activityState);
  clock.setCurrent(host, now());
  await chrome.storage.local.set({ activityState: clock.s });
  return true;
}

function activityRefresh(idleOverride) { return serial(() => doRefresh(idleOverride)); }

function activityReport(force) {
  return serial(async () => {
    if (reporting) return;
    reporting = true;
    try {
      await doRefresh();
      const c = await cfg();
      const st = L.sharingState(c, now());
      if (!st.activity) return;
      if (!force && now() - c.lastActivityReport < L.ACTIVITY.REPORT_EVERY_MS) return;
      const clock = new L.ActivityClock(c.activityState);
      const fresh = clock.drain(now(), st.hidden);
      let pending = c.activityPending;
      if (pending && now() - pending.ts > L.ACTIVITY.PENDING_MAX_AGE_MS) pending = null;     // too old to be worth sending: dropped, not queued
      const body = L.buildActivityBody(L.mergeHostSeconds(pending ? pending.host_seconds : [], fresh), st.hidden);
      if (!body.host_seconds.length) { await chrome.storage.local.set({ activityState: clock.s, activityPending: null, lastActivityReport: now() }); return; }
      await chrome.storage.local.set({ activityState: clock.s, activityPending: { ts: pending ? pending.ts : now(), host_seconds: body.host_seconds }, lastActivityReport: now() });
      let resp;
      try { resp = await post(c, "/bridge/activity", body, 15000); }
      catch (e) { return; }                                   // kept (young) for the next report
      if (resp.status >= 200 && resp.status < 300) {
        await chrome.storage.local.set({ activityPending: null, lastActivity: { ts: now(), ok: true, hosts: body.host_seconds } });
      } else if (resp.status === 401 || resp.status === 403) {
        // removed, or browsing time switched off on the server: nothing more is kept or sent
        const s = Object.assign({}, c.serverSettings || {}, resp.status === 403 ? { activity: false } : {});
        await chrome.storage.local.set({ activityPending: null, activityState: null, serverSettings: s, lastActivity: { ts: now(), ok: false, hosts: [] } });
        if (resp.status === 401) await markRemoved(c);
      }                                                       // 429 / 5xx: kept (young) and retried at the next report
    } finally { reporting = false; }
  });
}

async function activityTick(force) {
  await activityRefresh();
  await activityReport(!!force);
}

chrome.tabs.onActivated.addListener(() => { activityRefresh().catch(() => {}); });
chrome.tabs.onRemoved.addListener(() => { activityRefresh().catch(() => {}); });
chrome.tabs.onUpdated.addListener((id, info) => { if (info && (info.url || info.status === "complete")) activityRefresh().catch(() => {}); });
chrome.windows.onFocusChanged.addListener(() => { activityRefresh().catch(() => {}); });
try { chrome.idle.setDetectionInterval(120); } catch (e) { /* ignore */ }
chrome.idle.onStateChanged.addListener((state) => {
  serial(() => doRefresh(state)).then(() => { if (state === "active") activityReport(true).catch(() => {}); }).catch(() => {});
});

// ───────────────────────── keep the worker alive ─────────────────────────
async function housekeeping() {
  try {
    const c = await cfg();
    if (c.badgeUntil && c.badgeUntil < now()) await clearBadge();
  } catch (e) { /* ignore */ }
}
chrome.alarms.create("nori-keepalive", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener(() => { loop(); housekeeping(); activityTick(false).catch(() => {}); });
chrome.runtime.onStartup.addListener(() => { makeMenus(); loop(); });
chrome.runtime.onInstalled.addListener(() => {
  makeMenus();
  try { if (chrome.sidePanel && chrome.sidePanel.setPanelBehavior) chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: false }); } catch (e) { /* older Chrome */ }
  loop();
});
chrome.storage.onChanged.addListener((changes) => {
  if (changes.paused || changes.includePrivate) activityRefresh().catch(() => {});
});
loop();
