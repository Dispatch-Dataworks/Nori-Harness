// Nori browser bridge -- service worker. Long-polls the paired Nori server for
// commands and carries them out in this browser. See ../docs/computer-use.md.
//
// Safety it enforces itself (the server cannot override these):
//   * only tabs whose REAL url host is on the server-sent allowlist are listed
//     or touched, re-checked before AND after every action;
//   * risky clicks / typing-with-submit answer "needs approval" and do nothing
//     until the server re-sends the command marked approved for the SAME element;
//   * password / payment / one-time-code fields are never typed into;
//   * the Pause switch in the popup stops all of it locally.
importScripts("lib.js");
const L = self.NoriLib;
const POLL_TIMEOUT_MS = 35000;
const LOAD_TIMEOUT_MS = 15000;

let running = false;

async function cfg() {
  return await chrome.storage.local.get({ baseUrl: "", token: "", paused: false });
}

async function setStatus(s) {
  await chrome.storage.local.set({ status: { ...s, ts: Date.now() } });
}

async function loop() {
  if (running) return;
  running = true;
  for (;;) {
    const c = await cfg();
    if (!c.baseUrl || !c.token) {
      await setStatus({ state: "not configured" });
      await sleep(5000);
      continue;
    }
    try {
      const resp = await post(c, "/bridge/poll", { paused: !!c.paused, ext: { v: chrome.runtime.getManifest().version } }, POLL_TIMEOUT_MS);
      if (resp.status === 401) { await setStatus({ state: "token rejected" }); await sleep(10000); continue; }
      if (resp.status === 403) { await setStatus({ state: "switched off on the server" }); await sleep(10000); continue; }
      if (resp.status === 429) { await setStatus({ state: "rate limited" }); await sleep(15000); continue; }
      const data = await resp.json();
      if (data.paused || c.paused) { await setStatus({ state: "paused" }); continue; }
      await setStatus({ state: data.active ? "session running" : "idle (no session)", session_until: data.session_until });
      if (data.command) await handle(c, data.command);
    } catch (e) {
      await setStatus({ state: "can't reach Nori", error: String(e).slice(0, 120) });
      await sleep(5000);
    }
  }
}

function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

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

async function reply(c, id, payload) {
  try { await post(c, "/bridge/result", { id, ...payload }, 30000); } catch (e) { /* the server expires it */ }
}

class Refuse extends Error {}

async function handle(c, cmd) {
  const allowed = cmd.allowed || [];
  try {
    const out = await run(cmd, allowed);
    if (out.needs_approval) return await reply(c, cmd.id, out);
    await reply(c, cmd.id, { ok: true, result: out.result, host: out.host });
  } catch (e) {
    await reply(c, cmd.id, { ok: false, error: e instanceof Refuse ? e.message : "browser error: " + String(e).slice(0, 200) });
  }
}

async function tabChecked(tabId, allowed) {
  let tab;
  try { tab = await chrome.tabs.get(tabId); } catch (e) { throw new Refuse("no such tab"); }
  const host = L.hostOf(tab.url || "");
  if (!L.hostAllowed(host, allowed)) throw new Refuse("that tab isn't on a site you allowed" + (host ? " (" + host + ")" : ""));
  return { tab, host };
}

async function run(cmd, allowed) {
  const a = cmd.args || {};
  switch (cmd.action) {
    case "tabs": return await actTabs(allowed);
    case "open": return await actOpen(a, allowed);
    case "read": return await actRead(a, allowed);
    case "click": return await actAct("click", cmd, allowed);
    case "type": return await actAct("type", cmd, allowed);
    case "scroll": return await actScroll(a, allowed);
    case "screenshot": return await actScreenshot(a, allowed);
    default: throw new Refuse("unknown action " + cmd.action);
  }
}

async function actTabs(allowed) {
  const tabs = await chrome.tabs.query({});
  const visible = [];
  let hidden = 0;
  for (const t of tabs) {
    const host = L.hostOf(t.url || "");
    if (L.hostAllowed(host, allowed)) visible.push({ id: t.id, host, title: (t.title || "").slice(0, 80), active: !!t.active });
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

async function actOpen(a, allowed) {
  const host = L.hostOf(a.url);
  if (!L.hostAllowed(host, allowed)) throw new Refuse((host || "that address") + " isn't on your allowed-sites list");
  let tab;
  if (a.new_tab === false) {
    const [active] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!active) throw new Refuse("no active tab to navigate");
    await tabChecked(active.id, allowed);   // never navigate a tab that isn't itself an allowed one
    tab = await chrome.tabs.update(active.id, { url: a.url });
  } else {
    tab = await chrome.tabs.create({ url: a.url, active: true });
  }
  await waitForLoad(tab.id);
  const after = await chrome.tabs.get(tab.id);
  const afterHost = L.hostOf(after.url || "");
  if (!L.hostAllowed(afterHost, allowed)) {
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

async function actRead(a, allowed) {
  const { tab, host } = await tabChecked(a.tab_id, allowed);
  const page = await inPage(tab.id, pageCollect);
  if (!page) throw new Refuse("couldn't read that page");
  const after = await tabChecked(tab.id, allowed);   // it may have navigated while we read
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

async function actAct(kind, cmd, allowed) {
  const a = cmd.args || {};
  const { tab, host } = await tabChecked(a.tab_id, allowed);
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

async function actScroll(a, allowed) {
  const { tab, host } = await tabChecked(a.tab_id, allowed);
  const r = await inPage(tab.id, pageScroll, [a.direction]);
  return { result: r || { ok: true }, host };
}

async function actScreenshot(a, allowed) {
  const { tab, host } = await tabChecked(a.tab_id, allowed);
  await chrome.tabs.update(tab.id, { active: true });
  await sleep(250);
  let quality = 60, url = "";
  for (;;) {
    url = await chrome.tabs.captureVisibleTab(tab.windowId, { format: "jpeg", quality });
    if (url.length < 4_000_000 || quality <= 25) break;
    quality -= 15;
  }
  await tabChecked(tab.id, allowed);   // still the allowed site after the capture
  return { result: { jpeg_b64: url.replace(/^data:image\/jpeg;base64,/, "") }, host };
}

// Keep the worker alive and the loop running.
chrome.alarms.create("nori-keepalive", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener(() => { loop(); });
chrome.runtime.onStartup.addListener(() => { loop(); });
chrome.runtime.onInstalled.addListener(() => { loop(); });
chrome.storage.onChanged.addListener(() => { /* config changes are picked up on the next poll */ });
loop();
