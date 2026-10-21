// Passive browsing time: the clock and report builders in lib.js, and the real worker (fake time, fake chrome).
// Run: node tests/test_browser_extension_activity.js
"use strict";
const assert = require("assert");
const { L, T0, REGISTERED, sleep, waitFor, tab, makeEnv, suite, plain } = require("./browser_extension_harness");
const { test, run } = suite("activity");

const S = 1000, MIN = 60 * S, HOUR = 60 * MIN;

// ── the clock ──
test("time accrues to the host in front, and hands over on a switch", () => {
  const c = new L.ActivityClock();
  c.setCurrent("a.example", T0);
  c.setCurrent("b.example", T0 + 40 * S);
  const out = c.drain(T0 + 100 * S, []);
  assert.deepStrictEqual(out, [{ host: "b.example", seconds: 60 }, { host: "a.example", seconds: 40 }]);
  assert.deepStrictEqual(c.drain(T0 + 100 * S, []), []);                          // drained
});

test("nothing accrues while nothing is being timed (idle, locked, another application in front)", () => {
  const c = new L.ActivityClock();
  c.setCurrent("a.example", T0);
  c.setCurrent(null, T0 + 30 * S);                                                // went idle
  c.setCurrent("a.example", T0 + 200 * S);                                        // came back
  const out = c.drain(T0 + 230 * S, []);
  assert.deepStrictEqual(out, [{ host: "a.example", seconds: 60 }]);              // 30 s + 30 s, not the 170 s idle
});

test("a report is capped at 120 s per host", () => {
  const c = new L.ActivityClock();
  c.setCurrent("a.example", T0);
  for (let i = 1; i <= 10; i++) c.setCurrent("a.example", T0 + i * 60 * S);       // ten minutes of ticks, never drained
  assert.deepStrictEqual(c.drain(T0 + 600 * S, []), [{ host: "a.example", seconds: 120 }]);
});

test("a ten-hour gap (asleep, worker killed, lid shut) counts for nothing, not for ten hours", () => {
  const c = new L.ActivityClock();
  c.setCurrent("a.example", T0);
  assert.deepStrictEqual(c.drain(T0 + 10 * HOUR, []), []);
  const c2 = new L.ActivityClock(plain(c.s));                                     // and across a save/load
  c2.setCurrent("a.example", T0 + 10 * HOUR);
  assert.deepStrictEqual(c2.drain(T0 + 10 * HOUR + 30 * S, []), [{ host: "a.example", seconds: 30 }]);
  const gap = new L.ActivityClock();
  gap.setCurrent("a.example", T0);
  gap.setCurrent("a.example", T0 + 60 * S);                                       // a normal tick
  gap.setCurrent("a.example", T0 + 60 * S + L.ACTIVITY.GAP_MAX_MS + 1);           // then a silence longer than the limit
  assert.deepStrictEqual(gap.drain(T0 + 60 * S + L.ACTIVITY.GAP_MAX_MS + 1, []), [{ host: "a.example", seconds: 60 }]);
});

test("a clock that goes backwards adds nothing", () => {
  const c = new L.ActivityClock();
  c.setCurrent("a.example", T0);
  c.setCurrent("a.example", T0 - 50 * S);
  assert.deepStrictEqual(c.drain(T0 - 50 * S, []), []);
});

test("the state survives being saved and loaded (a restarted worker)", () => {
  const c = new L.ActivityClock();
  c.setCurrent("a.example", T0);
  c.setCurrent("a.example", T0 + 30 * S);
  const r = new L.ActivityClock(plain(c.s));
  assert.deepStrictEqual(r.drain(T0 + 60 * S, []), [{ host: "a.example", seconds: 60 }]);
  assert.strictEqual(new L.ActivityClock({ junk: 1 }).s.cur, null);
  assert.strictEqual(new L.ActivityClock(null).s.cur, null);
});

test("hidden hosts are dropped when draining, even if they were timed before they became hidden", () => {
  const c = new L.ActivityClock();
  c.setCurrent("www.mybank.com", T0);
  c.setCurrent("ok.example", T0 + 30 * S);
  assert.deepStrictEqual(c.drain(T0 + 60 * S, ["*.mybank.com"]), [{ host: "ok.example", seconds: 30 }]);
});

test("which host is timed: active tab of the focused window, active user, not private, not hidden, web only", () => {
  const pol = { includePrivate: false, hidden: ["*.bank.com"] };
  const t = (url, extra) => Object.assign({ url, incognito: false }, extra);
  assert.strictEqual(L.activityHost(t("https://Docs.Example.com/path?q=1"), true, "active", pol), "docs.example.com");
  assert.strictEqual(L.activityHost(t("https://docs.example.com/"), false, "active", pol), null);        // another application
  assert.strictEqual(L.activityHost(t("https://docs.example.com/"), true, "idle", pol), null);
  assert.strictEqual(L.activityHost(t("https://docs.example.com/"), true, "locked", pol), null);
  assert.strictEqual(L.activityHost(t("https://docs.example.com/", { incognito: true }), true, "active", pol), null);
  assert.strictEqual(L.activityHost(t("https://docs.example.com/", { incognito: true }), true, "active", { includePrivate: true, hidden: [] }), "docs.example.com");
  assert.strictEqual(L.activityHost(t("https://www.bank.com/"), true, "active", pol), null);
  for (const u of ["chrome://settings", "file:///C:/x", "about:blank", "javascript:1", "", undefined]) assert.strictEqual(L.activityHost(t(u), true, "active", pol), null, String(u));
  assert.strictEqual(L.activityHost(null, true, "active", pol), null);
});

test("the report body is hosts and seconds ONLY: titles, URLs, paths and junk can't get in", () => {
  const body = L.buildActivityBody([
    { host: "ok.example", seconds: 30 },
    { host: "evil.example/secret/path?token=1", seconds: 30 },
    { host: "https://evil.example/", seconds: 30 },
    { host: "has space.example", seconds: 30 },
    { host: "Title: My Bank Statement", seconds: 30 },
    { host: "a.example", seconds: 9999 },
    { host: "b.example", seconds: -5 },
    { host: "c.example", seconds: NaN },
    { host: "d.example", seconds: 0.2 },
    { host: "hidden.bank.com", seconds: 20 },
    { host: 12, seconds: 5 }, null, "x",
    { host: "e.example", seconds: 12.6, title: "LEAK-TITLE", url: "https://e.example/LEAK-PATH" },
  ], ["*.bank.com"]);
  assert.deepStrictEqual(Object.keys(body), ["host_seconds"]);
  assert.deepStrictEqual(body.host_seconds, [{ host: "ok.example", seconds: 30 }, { host: "a.example", seconds: 120 }, { host: "e.example", seconds: 13 }]);
  for (const e of body.host_seconds) assert.deepStrictEqual(Object.keys(e), ["host", "seconds"]);
  assert.ok(!/LEAK|secret|token|bank/.test(JSON.stringify(body)));
  assert.deepStrictEqual(L.buildActivityBody(null, []).host_seconds, []);
});

test("mergeHostSeconds sums and keeps the 120 s cap", () => {
  assert.deepStrictEqual(L.mergeHostSeconds([{ host: "a", seconds: 100 }, { host: "b", seconds: 5 }], [{ host: "a", seconds: 50 }, { host: "c", seconds: 1 }]),
    [{ host: "a", seconds: 120 }, { host: "b", seconds: 5 }, { host: "c", seconds: 1 }]);
});

test("sharingState: browsing time needs the server's switch (seen recently), a pairing, and no pause", () => {
  const st = (o) => L.sharingState(Object.assign({ baseUrl: "https://n", token: "nbr_x", serverSettings: { activity: true, standing_look: true, notifications: true }, settingsTs: T0 }, o), T0 + 10 * S);
  assert.strictEqual(st().activity, true);
  assert.strictEqual(st({ serverSettings: { activity: false } }).activity, false);
  assert.strictEqual(st({ serverSettings: {} }).activity, false);
  assert.strictEqual(st({ serverSettings: null }).activity, false);
  assert.strictEqual(st({ paused: true }).activity, false);
  assert.strictEqual(st({ token: "" }).activity, false);
  assert.strictEqual(st({ settingsTs: T0 - 10 * MIN }).activity, false);              // stale: the server hasn't confirmed lately
  assert.strictEqual(st({ settingsTs: 0 }).activity, false);
  assert.strictEqual(st({ authFailedToken: "nbr_x" }).activity, false);              // removed on the server
  assert.strictEqual(st({ paused: true }).notifications, false);
  assert.strictEqual(st({ notify: false }).notifications, false);
});

// ── the real worker ──
const DOCS = tab(1, { active: true, title: "Quarterly PRIVATE-TITLE", url: "https://docs.example.com/secret/path?token=ABC#frag" });
const base = (o) => Object.assign({ fakeTime: true, storage: Object.assign({}, REGISTERED), tabs: [DOCS] }, o || {});
async function enable(env, settings) {
  env.poll({ settings: Object.assign({ activity: true, notifications: true, hidden_hosts: [] }, settings || {}) });
  await waitFor(() => env.storage.activityState && "cur" in env.storage.activityState && env.storage.lastActivityReport, 3000, "clock started and first tick done");
}
const tick = async (env) => { await env.fire("alarms.onAlarm", { name: "nori-keepalive" }); };
const fresh = (env) => env.chrome.storage.local.set({ settingsTs: env.time });

test("off by default: nothing is timed or sent, whatever happens", async () => {
  const env = makeEnv(base());
  env.poll({ settings: { notifications: true } });                                    // the server says nothing about activity
  await waitFor(() => env.storage.serverSettings, 3000, "settings stored");
  for (let i = 0; i < 4; i++) { env.advance(60 * S); await fresh(env); await tick(env); }
  await env.fire("idle.onStateChanged", "active");
  await sleep(60);
  assert.strictEqual(env.activities().length, 0);
  assert.ok(!env.storage.activityState, "no clock is kept while it is off");
  assert.ok(env.actions.some((a) => a.op === "idleInterval" && a.s === 120), "idle detection interval is 120 s");
});

test("on: a report after 60 s carries the host and seconds, and nothing else (no title, path, query or fragment)", async () => {
  const env = makeEnv(base());
  await enable(env);
  env.advance(60 * S);
  await fresh(env);
  await tick(env);
  await waitFor(() => env.activities().length === 1, 3000, "report");
  const a = env.activities()[0];
  assert.strictEqual(a.url, "https://nori.example/bridge/activity");
  assert.strictEqual(a.auth, "Bearer " + REGISTERED.token);
  assert.deepStrictEqual(a.body, { host_seconds: [{ host: "docs.example.com", seconds: 60 }] });
  const raw = JSON.stringify(a.body);
  for (const leak of ["PRIVATE-TITLE", "secret", "path", "token", "ABC", "frag", "Quarterly", "https"]) assert.ok(!raw.includes(leak), "leaked " + leak);
  await waitFor(() => env.storage.lastActivity && env.storage.lastActivity.ok, 3000, "noted");
  assert.deepStrictEqual(plain(env.storage.lastActivity.hosts), [{ host: "docs.example.com", seconds: 60 }]);
  assert.strictEqual(env.storage.activityPending, null);
});

test("going idle stops the clock; coming back reports straight away with only the active time", async () => {
  const env = makeEnv(base());
  await enable(env);
  env.advance(30 * S);
  env.setIdle("idle");
  await env.fire("idle.onStateChanged", "idle");
  env.advance(100 * S);                                                                // away from the keyboard
  await fresh(env);
  env.setIdle("active");
  await env.fire("idle.onStateChanged", "active");
  await waitFor(() => env.activities().length === 1, 3000, "report on idle -> active");
  assert.deepStrictEqual(env.activities()[0].body.host_seconds, [{ host: "docs.example.com", seconds: 30 }]);
});

test("a locked screen counts as idle", async () => {
  const env = makeEnv(base());
  await enable(env);
  env.advance(20 * S);
  env.setIdle("locked");
  await env.fire("idle.onStateChanged", "locked");
  env.advance(5 * MIN);
  await fresh(env);
  env.setIdle("active");
  await env.fire("idle.onStateChanged", "active");
  await waitFor(() => env.activities().length === 1, 3000, "report");
  assert.deepStrictEqual(env.activities()[0].body.host_seconds, [{ host: "docs.example.com", seconds: 20 }]);
});

test("another application in front (browser window not focused) stops the clock", async () => {
  const env = makeEnv(base());
  await enable(env);
  env.advance(15 * S);
  env.setWin({ id: 1, focused: false, state: "normal" });
  await env.fire("windows.onFocusChanged", -1);
  env.advance(90 * S);
  await fresh(env);
  env.setWin({ id: 1, focused: true, state: "normal" });
  await env.fire("windows.onFocusChanged", 1);
  env.advance(10 * S);
  await fresh(env);
  await env.fire("idle.onStateChanged", "active");
  await waitFor(() => env.activities().length === 1, 3000, "report");
  assert.deepStrictEqual(env.activities()[0].body.host_seconds, [{ host: "docs.example.com", seconds: 25 }]);
});

test("switching tabs: each host gets its own time; hidden hosts (wildcard), private windows and non-web pages get none", async () => {
  const tabs = [DOCS, tab(2, { url: "https://www.mybank.com/acct" }), tab(3, { incognito: true, url: "https://private.example/" }), tab(4, { url: "chrome://settings" }), tab(5, { url: "https://mail.example.org/inbox" })];
  const env = makeEnv(base({ tabs }));
  await enable(env, { hidden_hosts: ["*.mybank.com"] });
  const goto = async (id, secs) => {
    env.setTabs(tabs.map((t) => Object.assign({}, t, { active: t.id === id })));
    await env.fire("tabs.onActivated", { tabId: id, windowId: 1 });
    env.advance(secs * S);
  };
  env.advance(10 * S);                                                                  // docs 10
  await goto(2, 20); await goto(3, 20); await goto(4, 20); await goto(5, 15);          // bank, private, chrome://, mail 15
  await fresh(env);
  await tick(env);
  await waitFor(() => env.activities().length === 1, 3000, "report");
  assert.deepStrictEqual(env.activities()[0].body.host_seconds, [{ host: "mail.example.org", seconds: 15 }, { host: "docs.example.com", seconds: 10 }]);
  assert.ok(!/bank|private|settings/.test(JSON.stringify(env.activities()[0].body)));
});

test("private windows are timed only once the option is on", async () => {
  const env = makeEnv(base({ tabs: [tab(3, { active: true, incognito: true, url: "https://private.example/" })], storage: Object.assign({}, REGISTERED, { includePrivate: true }) }));
  await enable(env);
  env.advance(60 * S);
  await fresh(env);
  await tick(env);
  await waitFor(() => env.activities().length === 1, 3000, "report");
  assert.deepStrictEqual(env.activities()[0].body.host_seconds, [{ host: "private.example", seconds: 60 }]);
});

test("a 10-hour gap with no events reports nothing (and sends nothing at all)", async () => {
  const env = makeEnv(base());
  await enable(env);
  env.advance(10 * HOUR);
  await fresh(env);
  await tick(env);
  await sleep(80);
  assert.strictEqual(env.activities().length, 0, "an empty report is not sent");
  assert.deepStrictEqual(plain(env.storage.activityState.acc), {}, "the ten hours were not credited to anything");
});

test("pause stops everything at once: no more time kept, nothing sent, the clock state is cleared", async () => {
  const env = makeEnv(base());
  await enable(env);
  env.advance(50 * S);
  await env.chrome.storage.local.set({ paused: true });
  await waitFor(() => !env.storage.activityState, 3000, "cleared on pause");
  env.advance(5 * MIN);
  await fresh(env);
  await tick(env);
  await env.fire("idle.onStateChanged", "active");
  await sleep(80);
  assert.strictEqual(env.activities().length, 0);
  await env.chrome.storage.local.set({ paused: false });
  await sleep(40);
  env.advance(60 * S);
  await fresh(env);
  await tick(env);
  await sleep(60);
  assert.ok(!env.activities().some((a) => a.body.host_seconds.some((h) => h.seconds > 60)), "the time before the pause is gone, not resumed");
});

test("if the server's setting was not confirmed in the last 3 minutes the worker stops timing", async () => {
  const env = makeEnv(base());
  await enable(env);
  env.advance(4 * MIN);                                                                // no poll has refreshed the settings
  await tick(env);
  await sleep(60);
  assert.strictEqual(env.activities().length, 0);
  assert.ok(!env.storage.activityState);
});

test("the server turning it off (settings.activity false in a poll) clears the clock; 403 on the report does too", async () => {
  const env = makeEnv(base({ routes: { "/bridge/activity": () => ({ status: 403, json: {} }) } }));
  await enable(env);
  env.advance(60 * S);
  await fresh(env);
  await tick(env);
  await waitFor(() => env.activities().length === 1, 3000, "report");
  await waitFor(() => env.storage.serverSettings && env.storage.serverSettings.activity === false, 3000, "switched off locally");
  assert.ok(!env.storage.activityState && !env.storage.activityPending);
  env.advance(60 * S);
  await fresh(env);
  await tick(env);
  await sleep(60);
  assert.strictEqual(env.activities().length, 1, "no further report after a 403");
  const env2 = makeEnv(base());
  env2.poll({ settings: { activity: true } });
  env2.poll({ settings: { activity: false } });                                        // the server switches it off on the next poll
  await waitFor(() => env2.storage.serverSettings && env2.storage.serverSettings.activity === false && !env2.storage.activityState, 3000, "clock cleared");
  env2.advance(60 * S);
  await tick(env2);
  await sleep(60);
  assert.strictEqual(env2.activities().length, 0);
});

test("a report that failed is kept for a few minutes (merged into the next), then dropped, never queued forever", async () => {
  // young: kept and merged
  let env = makeEnv(base({ routes: { "/bridge/activity": () => ({ status: 500, json: {} }) } }));
  await enable(env);
  env.advance(60 * S);
  await fresh(env);
  await tick(env);
  await waitFor(() => env.activities().length === 1, 3000, "first (failing) report");
  assert.deepStrictEqual(plain(env.storage.activityPending.host_seconds), [{ host: "docs.example.com", seconds: 60 }]);
  env.routes["/bridge/activity"] = () => ({ status: 200, json: { ok: true } });
  env.advance(60 * S);
  await fresh(env);
  await tick(env);
  await waitFor(() => env.activities().length === 2, 3000, "second report");
  assert.deepStrictEqual(env.activities()[1].body.host_seconds, [{ host: "docs.example.com", seconds: 120 }]);   // 60 kept + 60 new, still capped at 120
  assert.strictEqual(env.storage.activityPending, null);
  // old: dropped
  env = makeEnv(base());
  await enable(env);
  await env.chrome.storage.local.set({ activityPending: { ts: env.time - 6 * MIN, host_seconds: [{ host: "old.example", seconds: 50 }] } });
  env.advance(60 * S);
  await fresh(env);
  await tick(env);
  await waitFor(() => env.activities().length === 1, 3000, "report");
  assert.deepStrictEqual(env.activities()[0].body.host_seconds, [{ host: "docs.example.com", seconds: 60 }]);
  assert.ok(!JSON.stringify(env.activities()[0].body).includes("old.example"));
});

test("hidden hosts that arrive after time was recorded are scrubbed from a pending report too", async () => {
  const env = makeEnv(base());
  await enable(env);
  await env.chrome.storage.local.set({ activityPending: { ts: env.time - MIN, host_seconds: [{ host: "www.mybank.com", seconds: 50 }] }, serverSettings: { activity: true, notifications: true, hidden_hosts: ["*.mybank.com"] } });
  env.advance(60 * S);
  await fresh(env);
  await tick(env);
  await waitFor(() => env.activities().length === 1, 3000, "report");
  assert.ok(!JSON.stringify(env.activities()[0].body).includes("mybank"));
});

test("a report is never sent more than once a minute (apart from idle -> active)", async () => {
  const env = makeEnv(base());
  await enable(env);
  env.advance(60 * S);
  await fresh(env);
  await tick(env);
  await waitFor(() => env.activities().length === 1, 3000, "first");
  for (let i = 0; i < 3; i++) { env.advance(10 * S); await fresh(env); await tick(env); }
  await sleep(60);
  assert.strictEqual(env.activities().length, 1);
});

run();
