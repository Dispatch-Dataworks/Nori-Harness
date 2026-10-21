// Shared harness for tests/test_browser_extension_*.js (not a test itself). Runs the REAL browser_extension/
// background.js, and the real popup/options/panel scripts, against a fake `chrome` object, a fake network and a
// tiny fake DOM, in Node `vm` contexts. No real server, no real browser.
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const DIR = path.join(__dirname, "..", "browser_extension");
const L = require(path.join(DIR, "lib.js"));
const MANIFEST = JSON.parse(fs.readFileSync(path.join(DIR, "manifest.json"), "utf8"));
const UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36";
const EXT_ID = "testext";
const T0 = 1_800_000_000_000;
const REGISTERED = Object.freeze({ baseUrl: "https://nori.example", token: "nbr_testtoken_0123456789abcdef", name: "Desk" });   // frozen: copy it, never share it as a storage object
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const clone = (v) => (v === undefined ? undefined : JSON.parse(JSON.stringify(v)));
const plain = clone;

async function waitFor(cond, ms = 3000, what = "condition") {
  const end = Date.now() + ms;
  while (Date.now() < end) { if (await cond()) return; await sleep(5); }
  throw new Error("timed out waiting for " + what);
}

const tab = (id, extra) => Object.assign({ id, windowId: 1, active: false, title: "T" + id, url: "https://example.com/p" + id, pinned: false, incognito: false, status: "complete" }, extra || {});

function makeEnv(opts) {
  opts = opts || {};
  const storage = opts.storage || {};
  const listeners = {};
  const ev = (name) => ({ addListener(fn) { (listeners[name] = listeners[name] || []).push(fn); }, removeListener(fn) { listeners[name] = (listeners[name] || []).filter((x) => x !== fn); } });
  // Listeners in the worker start async work without returning it (Chrome ignores return values), so a fire() also gives that work a moment to settle.
  const fire = async (name, ...args) => { for (const fn of (listeners[name] || []).slice()) await fn(...args); await sleep(25); };
  const time = { now: opts.startTime || T0 };
  const calls = [];
  const queue = [];                                   // responses for /bridge/poll, in order
  const notifs = [];                                  // {op, id, options}
  const actions = [];                                 // tab actions the worker took
  const scriptCalls = [];
  const captures = [];
  const badges = [];
  const menus = [];
  const panelOpens = [];
  let stopped = false;
  let tabsNow = opts.tabs ? clone(opts.tabs) : [tab(1, { active: true, title: "Docs", url: "https://docs.example.com/guide?x=1#frag" })];
  let win = opts.win || { id: 1, focused: true, state: "normal" };
  const idle = { state: opts.idle || "active" };
  const state = { lastError: undefined };

  const pick = (keys) => {
    if (keys == null) return clone(storage);
    if (typeof keys === "string") return { [keys]: clone(storage[keys]) };
    if (Array.isArray(keys)) return Object.fromEntries(keys.filter((k) => k in storage).map((k) => [k, clone(storage[k])]));
    return Object.fromEntries(Object.keys(keys).map((k) => [k, k in storage ? clone(storage[k]) : clone(keys[k])]));
  };
  const setStorage = async (o) => {
    const changes = {};
    for (const [k, v] of Object.entries(o)) { changes[k] = { oldValue: storage[k], newValue: clone(v) }; storage[k] = clone(v); }
    await fire("storage.onChanged", changes, "local");
  };

  const chromeObj = {
    runtime: {
      id: EXT_ID,
      getManifest: () => MANIFEST,
      getURL: (p) => "chrome-extension://" + EXT_ID + "/" + (p || ""),
      get lastError() { return state.lastError; },
      onStartup: ev("runtime.onStartup"), onInstalled: ev("runtime.onInstalled"), onMessage: ev("runtime.onMessage"),
      sendMessage: (msg) => message(msg, { id: EXT_ID, url: "chrome-extension://" + EXT_ID + "/popup.html" }),
      openOptionsPage: () => { actions.push({ op: "openOptions" }); },
    },
    storage: { local: { get: async (k) => pick(k), set: setStorage, remove: async (k) => { for (const x of [].concat(k)) delete storage[x]; } }, onChanged: ev("storage.onChanged") },
    tabs: {
      query: async (q) => {
        q = q || {};
        let r = tabsNow.slice();
        if (q.active) r = r.filter((t) => t.active);
        if (q.windowId != null) r = r.filter((t) => t.windowId === q.windowId);
        if (q.currentWindow || q.lastFocusedWindow) r = r.filter((t) => t.windowId === win.id);
        return clone(r);
      },
      get: async (id) => { const t = tabsNow.find((x) => x.id === id); if (!t) throw new Error("No tab with id: " + id); return clone(t); },
      captureVisibleTab: async (winId, o) => {
        captures.push({ winId, quality: o && o.quality });
        if (opts.onCapture) await opts.onCapture(captures.length, { setTabs, getTabs: () => tabsNow, winId });
        if (opts.captureHangs) return await new Promise(() => {});
        const len = opts.captureLen ? opts.captureLen(o.quality) : 12;
        return "data:image/jpeg;base64," + "A".repeat(len);
      },
      create: async (o) => { actions.push(Object.assign({ op: "create" }, clone(o))); const t = tab(99, { url: o.url, title: "New", active: !!o.active }); tabsNow.push(t); return clone(t); },
      update: async (id, o) => {
        actions.push(Object.assign({ op: "update", id }, clone(o)));
        const t = tabsNow.find((x) => x.id === id);
        if (t && o.active) { for (const x of tabsNow) if (x.windowId === t.windowId) x.active = false; t.active = true; }
        if (t && o.url) t.url = o.url;
        return clone(t || {});
      },
      remove: async (id) => { actions.push({ op: "remove", id }); },
      goBack: async (id) => { actions.push({ op: "goBack", id }); },
      onCreated: ev("tabs.onCreated"), onRemoved: ev("tabs.onRemoved"), onActivated: ev("tabs.onActivated"), onUpdated: ev("tabs.onUpdated"),
    },
    windows: {
      getLastFocused: async () => clone(win),
      update: async (id, o) => { actions.push(Object.assign({ op: "windowUpdate", id }, clone(o))); return {}; },
      onFocusChanged: ev("windows.onFocusChanged"),
    },
    scripting: {
      executeScript: async (o) => {
        scriptCalls.push({ tabId: o.target.tabId, name: o.func.name, args: clone(o.args) });
        if (opts.scriptFails) throw new Error("Cannot access contents of the page");
        if (opts.script) return [{ result: await opts.script(o, tabsNow.find((t) => t.id === o.target.tabId)) }];
        if (o.func.name === "pageTextCollector") {
          const t = tabsNow.find((x) => x.id === o.target.tabId) || {};
          return [{ result: { title: t.title || "", url: t.url || "", text: opts.pageText != null ? opts.pageText : "Some visible text on the page." } }];
        }
        return [{ result: opts.pageCollect || { url: "", title: "", text: "", elements: [] } }];
      },
    },
    idle: { setDetectionInterval(s) { actions.push({ op: "idleInterval", s }); }, queryState: async () => idle.state, onStateChanged: ev("idle.onStateChanged") },
    alarms: { create(name, o) { actions.push({ op: "alarm", name, o }); }, onAlarm: ev("alarms.onAlarm") },
    contextMenus: {
      create: (m, cb) => { menus.push(m); if (cb) cb(); },
      removeAll: (cb) => { menus.length = 0; if (cb) cb(); },
      onClicked: ev("contextMenus.onClicked"),
    },
    action: {
      setBadgeText: async (o) => { badges.push(o.text); },
      setBadgeBackgroundColor: async () => {},
      setTitle: async () => {},
    },
    notifications: {
      create: (id, o, cb) => {
        if (opts.failCreate && opts.failCreate(id, notifs.filter((n) => n.op === "create-attempt" && n.id === id).length)) {
          notifs.push({ op: "create-attempt", id, options: clone(o) });
          state.lastError = { message: "notification create failed" };
          try { cb && cb(undefined); } finally { state.lastError = undefined; }
          return;
        }
        notifs.push({ op: "create-attempt", id, options: clone(o) });
        notifs.push({ op: "create", id, options: clone(o) });
        cb && cb(id);
      },
      update: (id, o, cb) => { notifs.push({ op: "update", id, options: clone(o) }); cb && cb(true); },
      clear: (id, cb) => { notifs.push({ op: "clear", id }); cb && cb(true); },
      getPermissionLevel: (cb) => cb(opts.notifPermission || "granted"),
      onClicked: ev("notifications.onClicked"), onButtonClicked: ev("notifications.onButtonClicked"), onClosed: ev("notifications.onClosed"),
    },
    sidePanel: { open: async (o) => { panelOpens.push(clone(o)); }, setPanelBehavior() {} },
  };
  if (opts.noSidePanel) delete chromeObj.sidePanel;

  function setTabs(t) { tabsNow = clone(t); }
  async function message(msg, sender) {
    sender = sender || { id: EXT_ID, url: "chrome-extension://" + EXT_ID + "/popup.html" };
    return await new Promise((resolve) => {
      let waited = false;
      for (const fn of listeners["runtime.onMessage"] || []) {
        const r = fn(clone(msg), sender, (x) => resolve(clone(x)));
        if (r === true) { waited = true; break; }
      }
      if (!waited) resolve(undefined);
    });
  }

  const routes = Object.assign({
    "/bridge/result": () => ({ status: 200, json: { ok: true } }),
    "/bridge/approval": () => ({ status: 200, json: { ok: true } }),
    "/bridge/share": () => ({ status: 200, json: { ok: true, id: 5 } }),
    "/bridge/ask": () => ({ status: 200, json: { ok: true, reply: "It is a page." } }),
    "/bridge/activity": () => ({ status: 200, json: { ok: true } }),
    "/bridge/register": () => ({ status: 200, json: { token: "nbr_newtoken_0123456789abcdef", name: "Desk" } }),
  }, opts.routes || {});

  const fetchFn = async (url, o) => {
    const u = new URL(url);
    const call = { url, path: u.pathname, body: o.body ? JSON.parse(o.body) : null, auth: (o.headers || {}).Authorization, method: o.method };
    calls.push(call);
    if (stopped) return await new Promise(() => {});
    const respond = (r) => ({ status: r.status || 200, headers: { get: (h) => (r.headers || {})[h] }, json: async () => { if (r.badJson) throw new Error("bad json"); return clone(r.json || {}); } });
    if (u.pathname.endsWith("/bridge/poll")) {
      const next = queue.shift();
      if (next && next.throws) throw new Error(next.throws);
      if (next) return respond(next);
      return await new Promise((_, rej) => { if (o.signal) o.signal.addEventListener("abort", () => rej(new Error("AbortError: aborted"))); });
    }
    const key = Object.keys(routes).find((k) => u.pathname.endsWith(k));
    if (!key) throw new Error("no fake route for " + u.pathname);
    const r = await (typeof routes[key] === "function" ? routes[key](call) : routes[key]);
    if (r && r.throws) throw new Error(r.throws);
    return respond(r);
  };

  class FakeDate extends Date {
    constructor(...a) { if (a.length) super(...a); else super(time.now); }
    static now() { return time.now; }
  }
  const ctx = vm.createContext({
    chrome: chromeObj, fetch: fetchFn, console, setTimeout, clearTimeout, setInterval, clearInterval, AbortController, URL,
    Date: opts.fakeTime ? FakeDate : Date, JSON, Promise, Error, Math, Object, Array, String, Number, Set, Map, Boolean, RegExp,
    navigator: { userAgent: UA },
  });
  ctx.self = ctx;
  ctx.NORI_MIN_POLL_GAP_MS = 0;
  ctx.NORI_SLICE_MS = 40;
  if (opts.captureTimeoutMs) ctx.NORI_CAPTURE_TIMEOUT_MS = opts.captureTimeoutMs;
  ctx.importScripts = (f) => vm.runInContext(fs.readFileSync(path.join(DIR, f), "utf8"), ctx, { filename: f });
  vm.runInContext(fs.readFileSync(path.join(DIR, "background.js"), "utf8"), ctx, { filename: "background.js" });

  const of = (p) => calls.filter((c) => c.path.endsWith(p));
  return {
    chrome: chromeObj, ctx, storage, calls, queue, notifs, actions, scriptCalls, captures, badges, menus, panelOpens, idle, routes, listeners,
    fire, message, setTabs, getTabs: () => clone(tabsNow), setWin: (w) => { win = w; }, setIdle: (s) => { idle.state = s; },
    advance: (ms) => { time.now += ms; }, setTime: (t) => { time.now = t; }, get time() { return time.now; },
    stop: () => { stopped = true; },
    polls: () => of("/bridge/poll"), results: () => of("/bridge/result"), shares: () => of("/bridge/share"), asks: () => of("/bridge/ask"),
    activities: () => of("/bridge/activity"), approvals: () => of("/bridge/approval"), registers: () => of("/bridge/register"),
    shown: () => notifs.filter((n) => n.op === "create"),
    poll: (json, status) => queue.push({ status: status || 200, json: json || {} }),
    pollRaw: (r) => queue.push(r),
    after: (what, ms) => waitFor(what, ms || 3000, "state"),
  };
}

// ── a minimal fake DOM for the popup / options / panel scripts ──
class El {
  constructor(id, tag) {
    this.id = id; this.tagName = (tag || "div").toUpperCase(); this.children = []; this.listeners = {};
    this.textContent = ""; this.value = ""; this.checked = false; this.disabled = false; this.hidden = false;
    this.className = ""; this.dataset = {}; this.placeholder = ""; this.scrollHeight = 0;
  }
  addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); }
  appendChild(c) { this.children.push(c); return c; }
  scrollTo() {}
  async trigger(type, extra) { for (const f of this.listeners[type] || []) await f(Object.assign({ preventDefault() {}, key: "", ctrlKey: false, metaKey: false }, extra || {})); }
}

// Loads real page scripts (lib.js + the named script) into a context that shares the env's fake chrome.
function loadPage(env, script, opts) {
  opts = opts || {};
  const els = {};
  const state = { closed: false, confirm: true };
  const document = {
    getElementById: (id) => els[id] || (els[id] = new El(id)),
    createElement: (tag) => new El("", tag),
    addEventListener() {},
  };
  const ctx = vm.createContext({
    chrome: env.chrome, fetch: opts.fetch || env.ctx.fetch, console, setTimeout, clearTimeout, AbortController, URL, JSON, Promise, Error, Math, Object, Array, String, Number, Date, Set, Map,
    document, navigator: { userAgent: UA },
  });
  ctx.self = ctx; ctx.window = ctx;
  ctx.close = () => { state.closed = true; };
  const run = (f) => vm.runInContext(fs.readFileSync(path.join(DIR, f), "utf8"), ctx, { filename: f });
  run("lib.js");
  run(script);
  return { els, el: (id) => document.getElementById(id), state, ctx, document, log: () => (els.log ? els.log.children.map((c) => c.className + " | " + c.textContent) : []) };
}

function suite(title) {
  const tests = [];
  return {
    test: (name, fn) => tests.push([name, fn]),
    async run() {
      let failed = 0;
      for (const [name, fn] of tests) {
        try { await fn(); console.log("ok   " + name); }
        catch (e) { failed++; console.log("FAIL " + name + "\n     " + String((e && e.stack) || e).split("\n").slice(0, 18).join("\n     ")); }
      }
      console.log(`\n${title}: ${tests.length - failed}/${tests.length} passed`);
      process.exit(failed ? 1 : 0);
    },
  };
}

module.exports = { DIR, L, MANIFEST, UA, EXT_ID, T0, REGISTERED, sleep, waitFor, tab, makeEnv, loadPage, suite, plain, clone, El };
