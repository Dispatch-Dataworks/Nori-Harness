// Core: pairing by code, 401/403/429 handling, pause, standing look, MV3 discipline, the manifest, and the
// commands that existed in v0.1.0 (which must behave as before). Run: node tests/test_browser_extension_core.js
"use strict";
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const { L, DIR, MANIFEST, UA, REGISTERED, sleep, waitFor, tab, makeEnv, suite, plain } = require("./browser_extension_harness");
const vectors = require("./bridge_vectors.json");
const { test, run } = suite("core");

const reg = (o) => Object.assign({}, REGISTERED, o || {});
const pair = (env, o) => env.message(Object.assign({ type: "pair", baseUrl: "https://nori.example/", code: "abcd 1234", name: "Desk PC" }, o || {}));

// ── lib.js: pairing + version helpers ──
test("pairing helpers: addresses, codes, the register body", () => {
  assert.strictEqual(L.normalizeBaseUrl(" https://nori.example/ ").url, "https://nori.example");
  assert.strictEqual(L.normalizeBaseUrl("https://nori.example/app/?x=1#y").url, "https://nori.example/app");
  assert.strictEqual(L.normalizeBaseUrl("http://127.0.0.1:8877").warning, "");
  assert.strictEqual(L.normalizeBaseUrl("http://localhost:8877").warning, "");
  assert.ok(L.normalizeBaseUrl("http://nori.example.com").warning.length > 0, "plain http to a real host warns");
  for (const bad of ["nori.example", "ftp://x.com", "javascript:alert(1)", "", "https://u:p@nori.example"]) assert.strictEqual(L.normalizeBaseUrl(bad).ok, false, bad);
  assert.strictEqual(L.normalizePairCode("abcd-1234"), "ABCD-1234");
  assert.strictEqual(L.normalizePairCode(" abcd 1234 "), "ABCD-1234");
  assert.strictEqual(L.normalizePairCode("ABCD1234"), "ABCD-1234");
  for (const bad of ["", "ABC-123", "ABCD-12345", "ABCD-12!4", null, undefined]) assert.strictEqual(L.normalizePairCode(bad), null, String(bad));
  assert.strictEqual(L.browserModel(UA), "Chrome 130 on Windows");
  assert.strictEqual(L.defaultDeviceName(UA), "Chrome on Windows");
  assert.deepStrictEqual(L.registerBody({ code: "ABCD-1234", name: "  Desk ", model: "Chrome 130 on Windows", v: "0.2.0", ua: UA }),
    { code: "ABCD-1234", kind: "browser", name: "Desk", v: "0.2.0", model: "Chrome 130 on Windows" });
  assert.strictEqual(L.registerBody({ code: "X", name: "", v: "0.2.0", ua: UA }).name, "Chrome on Windows");
  assert.ok(!("model" in L.registerBody({ code: "X", name: "n", v: "1" })));
});

test("version gating", () => {
  assert.strictEqual(L.compareVersions("0.2.0", "0.1.9"), 1);
  assert.strictEqual(L.compareVersions("0.2.0", "0.2.0"), 0);
  assert.strictEqual(L.compareVersions("0.2", "0.2.0"), 0);
  assert.strictEqual(L.compareVersions("0.10.0", "0.9.0"), 1);
  assert.strictEqual(L.compareVersions("1.0.0", "1.0.1"), -1);
  assert.strictEqual(L.compareVersions(undefined, "0.0.1"), -1);
  assert.strictEqual(L.chromeMajor(UA), 130);
  assert.strictEqual(L.chromeMajor("curl/8"), 0);
  assert.strictEqual(L.featureSupported("sidePanel", 130), true);
  assert.strictEqual(L.featureSupported("sidePanel", 115), false);
  assert.strictEqual(L.featureSupported("unknownFeature", 999), false);
  assert.strictEqual(MANIFEST.version, "0.2.1");
  assert.strictEqual(L.compareVersions(MANIFEST.version, "0.1.0"), 1);
});

test("settings parsing: only notifications default on; hidden hosts are never forgotten by a reply that omits them", () => {
  assert.deepStrictEqual(plain(L.parseSettings(undefined, null)), { present: false, notifications: true, activity: false, standing_look: false, hidden_hosts: [] });
  const s = L.parseSettings({ notifications: false, activity: true, standing_look: true, hidden_hosts: ["*.Bank.com", "bad host", 7] }, null);
  assert.deepStrictEqual(plain(s), { present: true, notifications: false, activity: true, standing_look: true, hidden_hosts: ["*.bank.com"] });
  assert.deepStrictEqual(L.parseSettings({ activity: true }, { hidden_hosts: ["a.com"] }).hidden_hosts, ["a.com"]);       // omitted: kept
  assert.deepStrictEqual(L.parseSettings({ hidden_hosts: [] }, { hidden_hosts: ["a.com"] }).hidden_hosts, []);             // an explicit empty list clears
  assert.strictEqual(L.parseSettings({ activity: "yes", standing_look: 1 }, null).activity, false);                       // only a real true counts
  assert.strictEqual(L.parseSettings({ activity: "yes", standing_look: 1 }, null).standing_look, false);
  assert.strictEqual(L.parseSettings([], null).present, false);
});

test("hidden-host matching agrees with the shared allowlist vectors (same matcher, opposite sense)", () => {
  for (const [host, pattern, expected] of vectors.host_matches) {
    assert.strictEqual(L.hostHidden(host, [pattern]), expected, JSON.stringify([host, pattern]));
    assert.strictEqual(L.hostMatches(host, pattern), expected);
  }
});

test("standing-look verdicts: click/type/scroll/anything else are refused with the exact words, even if the server lies", () => {
  for (const action of ["click", "type", "scroll", "navigate", "close", "evaluate", ""]) {
    assert.deepStrictEqual(L.lookOnlyVerdict({ action, look_only: true }, true), { ok: false, error: "a session isn't running; I can only look" }, action);
  }
  for (const action of ["tabs", "read", "screenshot", "open"]) {
    assert.strictEqual(L.lookOnlyVerdict({ action, look_only: true }, true).ok, true, action);
    assert.strictEqual(L.lookOnlyVerdict({ action, look_only: true }, false).ok, false, action + " needs standing look on");
    assert.strictEqual(L.lookOnlyVerdict({ action }, false).ok, true, "no look_only: not this rule's business");
  }
  assert.strictEqual(L.lookOnlyVerdict({ action: "click", look_only: "false" }, true).ok, false, "any truthy look_only is treated as look-only");
});

// ── pairing in the real worker ──
test("pairing by code: POST /bridge/register {code, kind:'browser', name, model, v} with no token, then the token is stored and used", async () => {
  const env = makeEnv({ storage: {} });
  const r = await pair(env);
  assert.strictEqual(r.ok, true);
  assert.strictEqual(r.name, "Desk");
  const c = env.registers()[0];
  assert.strictEqual(c.url, "https://nori.example/bridge/register");
  assert.deepStrictEqual(c.body, { code: "ABCD-1234", kind: "browser", name: "Desk PC", v: "0.2.1", model: "Chrome 130 on Windows" });
  assert.strictEqual(c.auth, undefined, "no bearer token on the register call");
  assert.strictEqual(env.storage.token, "nbr_newtoken_0123456789abcdef");
  assert.strictEqual(env.storage.baseUrl, "https://nori.example");
  assert.strictEqual(env.storage.name, "Desk");
  assert.ok(!JSON.stringify(env.storage).includes("ABCD"), "the one-time code is not kept");
  await waitFor(() => env.polls().length >= 1, 3000, "poll");
  assert.strictEqual(env.polls()[0].auth, "Bearer nbr_newtoken_0123456789abcdef");
});

test("pairing wipes everything from a previous pairing (ack, shown ids, settings, activity clock, backoff)", async () => {
  const env = makeEnv({ storage: reg({ lastAck: 500, shown: [498, 499, 500], approvals: { "nori-1": { id: 1 } }, serverSettings: { activity: true }, settingsTs: 1, activityState: { acc: { "a.com": 5 }, cur: null, since: null },
    activityPending: { ts: 1, host_seconds: [{ host: "a.com", seconds: 1 }] }, backoffUntil: Date.now() + 99999, authFailedToken: REGISTERED.token, paused: true, includePrivate: true }) });
  const r = await pair(env);
  assert.strictEqual(r.ok, true);
  assert.strictEqual(env.storage.lastAck, 0);
  assert.deepStrictEqual(plain(env.storage.shown), []);
  assert.deepStrictEqual(plain(env.storage.approvals), {});
  assert.strictEqual(env.storage.serverSettings, null);
  assert.strictEqual(env.storage.activityState, null);
  assert.strictEqual(env.storage.activityPending, null);
  assert.strictEqual(env.storage.backoffUntil, 0);
  assert.strictEqual(env.storage.authFailedToken, "");
  assert.strictEqual(env.storage.paused, true, "his own pause and private-window choices are not pairing state");
  assert.strictEqual(env.storage.includePrivate, true);
});

test("pairing refusals: bad address or code (nothing sent), wrong/used code, switched off, rate limited, a reply with no usable token, no network", async () => {
  let env = makeEnv({ storage: {} });
  for (const o of [{ code: "12" }, { code: "" }, { baseUrl: "nori.example" }, { baseUrl: "javascript:alert(1)" }]) {
    const r = await pair(env, o);
    assert.strictEqual(r.ok, false, JSON.stringify(o));
  }
  assert.strictEqual(env.registers().length, 0, "nothing is sent for input that can't be valid");
  const cases = [
    [{ status: 404, json: {} }, /wasn't accepted/],
    [{ status: 401, json: { error: "wrong code" } }, /wrong code/],
    [{ status: 403, json: {} }, /switched off/],
    [{ status: 429, json: {} }, /Too many tries/],
    [{ status: 200, json: { name: "x" } }, /no browser token|didn't contain/],
    [{ status: 200, json: { token: "npn_phone_token_0123456789", name: "x" } }, /didn't contain/],       // a phone token is not a browser token
    [{ status: 200, json: { token: "nbr_" } }, /didn't contain/],
    [{ throws: "ECONNREFUSED" }, /Couldn't reach/],
  ];
  for (const [route, rx] of cases) {
    env = makeEnv({ storage: {}, routes: { "/bridge/register": () => route } });
    const r = await pair(env);
    assert.strictEqual(r.ok, false);
    assert.match(r.error, rx);
    assert.ok(!env.storage.token, "no token saved on failure");
  }
});

test("only this extension's own pages may start a pairing", async () => {
  const env = makeEnv({ storage: {} });
  assert.strictEqual(await env.message({ type: "pair", baseUrl: "https://x.example", code: "ABCD-1234" }, { id: "other" }), undefined);
  assert.strictEqual(await env.message({ type: "pair", baseUrl: "https://x.example", code: "ABCD-1234" }, { id: "testext", url: "https://evil.example/" }), undefined);
  assert.strictEqual(await env.message({ type: "unknown" }), undefined);
  assert.strictEqual(env.registers().length, 0);
});

// ── the poll loop's status handling ──
test("not paired: it does nothing and says so", async () => {
  const env = makeEnv({ storage: {} });
  await waitFor(() => env.storage.status && env.storage.status.state === "not configured", 2000, "status");
  assert.strictEqual(env.calls.length, 0);
});

test("401: 'removed, re-pair'; it stops talking to the server until a new token is saved", async () => {
  const env = makeEnv({ storage: reg() });
  env.poll({}, 401);
  await waitFor(() => env.storage.status && env.storage.status.state === "removed, re-pair", 3000, "status");
  assert.strictEqual(env.storage.authFailedToken, REGISTERED.token);
  await sleep(300);
  assert.strictEqual(env.polls().length, 1, "no retry with the dead token");
  await pair(env, { baseUrl: "https://nori.example" });                       // a new pairing replaces it
  await waitFor(() => env.polls().length >= 2, 3000, "polling resumes");
  assert.strictEqual(env.polls()[1].auth, "Bearer nbr_newtoken_0123456789abcdef");
});

test("403: 'switched off on the server', backs off 60 s, and a restarted worker keeps honouring that", async () => {
  const env = makeEnv({ storage: reg(), fakeTime: true });
  env.poll({}, 403);
  await waitFor(() => env.storage.status && env.storage.status.state === "switched off on the server" && env.storage.backoffUntil > 0, 3000, "status and backoff");
  assert.ok(env.storage.backoffUntil >= env.time + 59000 && env.storage.backoffUntil <= env.time + 61000);
  await sleep(250);
  assert.strictEqual(env.polls().length, 1);
  env.stop();
  const env2 = makeEnv({ storage: env.storage, fakeTime: true, startTime: env.time });
  await sleep(250);
  assert.strictEqual(env2.polls().length, 0, "the restarted worker waits out the backoff");
  env2.advance(61000);
  await waitFor(() => env2.polls().length === 1, 3000, "polls after the backoff");
});

test("429: 'rate limited', honours Retry-After (clamped), otherwise 30 s", async () => {
  let env = makeEnv({ storage: reg(), fakeTime: true });
  env.pollRaw({ status: 429, json: {}, headers: { "Retry-After": "45" } });
  await waitFor(() => env.storage.status && env.storage.status.state === "rate limited" && env.storage.backoffUntil > 0, 3000, "status and backoff");
  assert.ok(Math.abs(env.storage.backoffUntil - (env.time + 45000)) < 1500);
  env = makeEnv({ storage: reg(), fakeTime: true });
  env.poll({}, 429);
  await waitFor(() => env.storage.status && env.storage.status.state === "rate limited" && env.storage.backoffUntil > 0, 3000, "status and backoff");
  assert.ok(Math.abs(env.storage.backoffUntil - (env.time + 30000)) < 1500);
  env = makeEnv({ storage: reg(), fakeTime: true });
  env.pollRaw({ status: 429, json: {}, headers: { "Retry-After": "999999" } });
  await waitFor(() => env.storage.status && env.storage.status.state === "rate limited" && env.storage.backoffUntil > 0, 3000, "status and backoff");
  assert.ok(env.storage.backoffUntil <= env.time + 301000);
});

test("a server error or no network is reported and retried later, not in a tight loop", async () => {
  let env = makeEnv({ storage: reg() });
  env.poll({}, 502);
  await waitFor(() => env.storage.status && env.storage.status.state === "can't reach Nori", 3000, "status");
  await sleep(200);
  assert.strictEqual(env.polls().length, 1);
  env = makeEnv({ storage: reg() });
  env.pollRaw({ throws: "ECONNREFUSED" });
  await waitFor(() => env.storage.status && env.storage.status.state === "can't reach Nori", 3000, "status");
  assert.match(env.storage.status.error, /ECONNREFUSED/);
  await sleep(200);
  assert.strictEqual(env.polls().length, 1);
});

test("the poll status line says whether a session is running", async () => {
  const env = makeEnv({ storage: reg() });
  env.poll({ active: true, session_until: 1792000000 });
  await waitFor(() => env.storage.status && env.storage.status.state === "session running", 3000, "running");
  const env2 = makeEnv({ storage: reg() });
  env2.poll({ active: false });
  await waitFor(() => env2.storage.status && env2.storage.status.state === "idle (no session)", 3000, "idle");
});

test("paused: the poll says so, no command is carried out, no message shown, no result sent", async () => {
  const env = makeEnv({ storage: reg({ paused: true }), tabs: [tab(1, { active: true, url: "https://example.com/" })] });
  env.poll({ command: { id: 9, action: "tabs", allowed: ["example.com"] }, messages: [{ id: 1, category: "chat", text: "hi" }] });
  await waitFor(() => env.polls().length >= 1, 3000, "poll");
  await sleep(200);
  assert.strictEqual(env.polls()[0].body.paused, true);
  assert.strictEqual(env.results().length, 0);
  assert.strictEqual(env.shown().length, 0);
  assert.strictEqual(env.storage.status.state, "paused");
});

// ── commands: v0.1.0 behaviour must be unchanged ──
const cmdEnv = async (envOpts, cmd, settings) => {
  const env = makeEnv(Object.assign({ storage: reg() }, envOpts));
  env.poll({ command: Object.assign({ id: 31, allowed: ["example.com", "*.example.com"] }, cmd), settings: settings || {} });
  await waitFor(() => env.results().length >= 1, 4000, "result");
  return { env, r: env.results()[0].body };
};
const TABS = [tab(1, { active: true, title: "Home", url: "https://www.example.com/home" }), tab(2, { title: "Bank", url: "https://mybank.com/acct" }), tab(3, { incognito: true, url: "https://www.example.com/private" }), tab(4, { url: "chrome://settings" })];

test("tabs: only allowlisted tabs are listed (not private, not hidden, not other sites), and the rest are only counted", async () => {
  const { r } = await cmdEnv({ tabs: TABS }, { action: "tabs" }, { hidden_hosts: ["www.example.com"] });
  assert.strictEqual(r.ok, true);
  assert.deepStrictEqual(r.result.tabs, []);                                       // www.example.com is hidden by the server's list
  assert.strictEqual(r.result.hidden, 4);
  const { r: r2 } = await cmdEnv({ tabs: TABS }, { action: "tabs" });
  assert.deepStrictEqual(r2.result.tabs.map((t) => t.id), [1]);                    // the private tab is skipped, mybank isn't allowlisted
  assert.strictEqual(r2.result.hidden, 3);
  const { r: r3 } = await cmdEnv({ tabs: TABS, storage: reg({ includePrivate: true }) }, { action: "tabs" });
  assert.deepStrictEqual(r3.result.tabs.map((t) => t.id), [1, 3]);
});

test("open: new tab on an allowed site; a site that isn't allowed, or is hidden, is refused", async () => {
  let { env, r } = await cmdEnv({}, { action: "open", args: { url: "https://www.example.com/x" } });
  assert.strictEqual(r.ok, true);
  assert.deepStrictEqual(env.actions.filter((a) => a.op === "create"), [{ op: "create", url: "https://www.example.com/x", active: true }]);
  ({ env, r } = await cmdEnv({}, { action: "open", args: { url: "https://evil.com/" } }));
  assert.strictEqual(r.ok, false);
  assert.match(r.error, /allowed-sites/);
  assert.deepStrictEqual(env.actions.filter((a) => a.op === "create"), []);
  ({ env, r } = await cmdEnv({}, { action: "open", args: { url: "https://www.example.com/x" } }, { hidden_hosts: ["*.example.com"] }));
  assert.strictEqual(r.ok, false);
  assert.match(r.error, /private/);
});

test("read/click/type need the tab on an allowed site; hidden sites and private windows are refused", async () => {
  for (const [id, rx] of [[2, /allowed/], [3, /private window/], [4, /allowed/], [77, /no such tab/]]) {
    const { r } = await cmdEnv({ tabs: TABS }, { action: "read", args: { tab_id: id } });
    assert.strictEqual(r.ok, false, "tab " + id);
    assert.match(r.error, rx);
  }
  const { r } = await cmdEnv({ tabs: TABS }, { action: "read", args: { tab_id: 1 } }, { hidden_hosts: ["*.example.com"] });
  assert.strictEqual(r.ok, false);
  assert.match(r.error, /keep private/);
  const { r: ok } = await cmdEnv({ tabs: TABS, pageCollect: { url: "https://www.example.com/home", title: "Home", text: "hi", elements: [{ ref: "e1", tag: "button", label: "Send", role: "button", type: "", href_host: "", name: "", id_attr: "", autocomplete: "", placeholder: "", is_submit: false, sensitive: false }] } }, { action: "read", args: { tab_id: 1 } });
  assert.strictEqual(ok.ok, true);
  assert.strictEqual(ok.result.elements[0].risky, true, "Send is flagged risky");
  assert.ok(!("name" in ok.result.elements[0]));
});

test("a risky click answers needs_approval and does nothing; an approved one for the same element runs; sensitive fields are never typed into", async () => {
  const send = { tag: "button", type: "", label: "Send", role: "", href_host: "", name: "", id_attr: "", autocomplete: "", placeholder: "", is_submit: false, currentHost: "www.example.com" };
  const script = (o) => (o.func.name === "pageDescribe" ? send : { ok: true });
  const { env, r } = await cmdEnv({ tabs: TABS, script }, { action: "click", args: { tab_id: 1, ref: "e1" } });
  assert.strictEqual(r.needs_approval, true);
  assert.strictEqual(r.element.fingerprint, L.fingerprint(send));
  assert.deepStrictEqual(env.scriptCalls.map((c) => c.name), ["pageDescribe"], "no click happened");
  const ok = await cmdEnv({ tabs: TABS, script }, { action: "click", approved: true, args: { tab_id: 1, ref: "e1", _expect: L.fingerprint(send) } });
  assert.strictEqual(ok.r.ok, true);
  assert.ok(ok.env.scriptCalls.some((c) => c.name === "pageClick"));
  const wrong = await cmdEnv({ tabs: TABS, script }, { action: "click", approved: true, args: { tab_id: 1, ref: "e1", _expect: "f00" } });
  assert.strictEqual(wrong.r.ok, false);
  const pw = { tag: "input", type: "password", label: "(sensitive field)", name: "", id_attr: "", autocomplete: "", placeholder: "", href_host: "", role: "", is_submit: false };
  const typed = await cmdEnv({ tabs: TABS, script: (o) => (o.func.name === "pageDescribe" ? pw : { ok: true }) }, { action: "type", args: { tab_id: 1, ref: "e1", text: "hunter2" } });
  assert.strictEqual(typed.r.ok, false);
  assert.match(typed.r.error, /never typed into/);
  assert.ok(!typed.env.scriptCalls.some((c) => c.name === "pageType"));
});

test("an action it doesn't know is refused", async () => {
  const { r } = await cmdEnv({}, { action: "format_disk" });
  assert.strictEqual(r.ok, false);
  assert.match(r.error, /unknown action/);
});

test("the last command's outcome is kept for the popup (action and result only: no URLs or arguments)", async () => {
  const { env } = await cmdEnv({ tabs: TABS }, { action: "open", args: { url: "https://evil.com/secret?token=1" } });
  await waitFor(() => env.storage.lastCommand, 3000, "noted");
  assert.strictEqual(env.storage.lastCommand.action, "open");
  assert.strictEqual(env.storage.lastCommand.ok, false);
  assert.ok(!JSON.stringify(env.storage.lastCommand).includes("token=1"));
});

// ── standing look ──
const LOOK = { standing_look: true };
const NOSESSION = (o) => Object.assign({ look_only: true }, o);

test("standing look: click and type are refused by the extension itself, touching nothing", async () => {
  for (const settings of [LOOK, { standing_look: false }, {}]) {
    for (const [action, args] of [["click", { tab_id: 1, ref: "e1" }], ["type", { tab_id: 1, ref: "e1", text: "x", submit: true }], ["scroll", { tab_id: 1, direction: "down" }]]) {
      const { env, r } = await cmdEnv({ tabs: TABS }, NOSESSION({ action, approved: true, args: Object.assign({ _expect: "f1" }, args) }), settings);
      assert.strictEqual(r.ok, false, action);
      assert.strictEqual(r.error, "a session isn't running; I can only look");
      assert.deepStrictEqual(env.scriptCalls, [], action + ": nothing injected into any page");
      assert.deepStrictEqual(env.actions.filter((a) => a.op !== "alarm" && a.op !== "idleInterval"), [], action + ": no tab was touched");
      assert.strictEqual(env.captures.length, 0);
    }
  }
});

test("standing look: tabs, read, screenshot and open work on allowlisted hosts", async () => {
  const lt = (id) => ({ tabs: [tab(1, { active: true, title: "Home", url: "https://www.example.com/home" }), tab(2, { title: "Other", url: "https://docs.example.com/x" })], id });
  let { r } = await cmdEnv(lt(), NOSESSION({ action: "tabs" }), LOOK);
  assert.strictEqual(r.ok, true);
  assert.deepStrictEqual(r.result.tabs.map((t) => t.id), [1, 2]);
  ({ r } = await cmdEnv(Object.assign(lt(), { pageCollect: { url: "https://www.example.com/home", title: "Home", text: "hello", elements: [] } }), NOSESSION({ action: "read", args: { tab_id: 1 } }), LOOK));
  assert.strictEqual(r.ok, true);
  assert.strictEqual(r.result.text, "hello");
  let res = await cmdEnv(lt(), NOSESSION({ action: "screenshot", args: { tab_id: 1 } }), LOOK);
  assert.strictEqual(res.r.ok, true);
  assert.strictEqual(res.env.captures.length, 1);
  assert.ok(!res.env.actions.some((a) => a.op === "update"), "it did not switch tabs to look");
  res = await cmdEnv(lt(), NOSESSION({ action: "screenshot", args: { tab_id: 2 } }), LOOK);          // a background tab: not brought to the front
  assert.strictEqual(res.r.ok, false);
  assert.match(res.r.error, /isn't the one in front/);
  assert.strictEqual(res.env.captures.length, 0);
  res = await cmdEnv(lt(), NOSESSION({ action: "open", args: { url: "https://www.example.com/new" } }), LOOK);
  assert.strictEqual(res.r.ok, true);
  assert.deepStrictEqual(res.env.actions.filter((a) => a.op === "create"), [{ op: "create", url: "https://www.example.com/new", active: true }]);
});

test("standing look: open ALWAYS makes a new tab and never navigates an existing one, even when told new_tab:false", async () => {
  const { env, r } = await cmdEnv({ tabs: [tab(1, { active: true, url: "https://www.example.com/home" })] }, NOSESSION({ action: "open", args: { url: "https://www.example.com/new", new_tab: false } }), LOOK);
  assert.strictEqual(r.ok, true);
  assert.deepStrictEqual(env.actions.filter((a) => a.op === "create").length, 1);
  assert.deepStrictEqual(env.actions.filter((a) => a.op === "update" && a.url), [], "no existing tab was navigated");
});

test("standing look only works on allowlisted hosts, and respects hidden hosts and private windows", async () => {
  let { env, r } = await cmdEnv({ tabs: TABS }, NOSESSION({ action: "open", args: { url: "https://evil.com/" } }), LOOK);
  assert.strictEqual(r.ok, false);
  assert.deepStrictEqual(env.actions.filter((a) => a.op === "create"), []);
  ({ r } = await cmdEnv({ tabs: TABS }, NOSESSION({ action: "read", args: { tab_id: 2 } }), LOOK));            // mybank isn't on the allowlist
  assert.strictEqual(r.ok, false);
  ({ r } = await cmdEnv({ tabs: TABS }, NOSESSION({ action: "read", args: { tab_id: 3 } }), LOOK));            // private window
  assert.strictEqual(r.ok, false);
  ({ r } = await cmdEnv({ tabs: TABS }, NOSESSION({ action: "read", args: { tab_id: 1 } }), { standing_look: true, hidden_hosts: ["*.example.com"] }));
  assert.strictEqual(r.ok, false);
  assert.match(r.error, /keep private/);
});

test("standing look is refused for look-only commands when the server's own setting says it is off (the extension re-checks)", async () => {
  const { r } = await cmdEnv({ tabs: TABS }, NOSESSION({ action: "tabs" }), { standing_look: false });
  assert.strictEqual(r.ok, false);
  assert.match(r.error, /standing look is switched off/);
});

test("a session command (no look_only) is unaffected by the standing-look setting", async () => {
  const { r } = await cmdEnv({ tabs: TABS }, { action: "tabs" }, { standing_look: false });
  assert.strictEqual(r.ok, true);
});

// ── MV3 discipline ──
test("MV3: the keep-alive alarm is 30 s, and a command that hangs is answered (not left to the server to time out)", async () => {
  const env = makeEnv({ storage: reg() });
  assert.deepStrictEqual(plain(env.actions.find((a) => a.op === "alarm")), { op: "alarm", name: "nori-keepalive", o: { periodInMinutes: 0.5 } });
  const hang = makeEnv({ storage: reg(), tabs: [tab(1, { active: true, url: "https://www.example.com/" })], captureHangs: true, captureTimeoutMs: 120 });
  hang.poll({ command: { id: 4, action: "screenshot", allowed: ["www.example.com"], args: { tab_id: 1 } } });
  await waitFor(() => hang.results().length === 1, 3000, "answered");
  assert.strictEqual(hang.results()[0].body.ok, false);
  assert.match(hang.results()[0].body.error, /screenshot in time/);
});

test("MV3: no wait in the worker exceeds ~28 s, and nothing waits on a bare timer longer than that", () => {
  const src = fs.readFileSync(path.join(DIR, "background.js"), "utf8");
  const poll = /const POLL_TIMEOUT_MS = (\d+)/.exec(src);
  assert.ok(Number(poll[1]) <= 28000, "the long poll's own timeout must stay under 30 s");
  for (const m of src.matchAll(/\bsleep\((\d+)\)/g)) assert.ok(Number(m[1]) <= 20000, "sleep(" + m[1] + ")");
  for (const m of src.matchAll(/\b(?:setTimeout|withTimeout)\([^;]*?,\s*(\d+)\)/g)) assert.ok(Number(m[1]) <= 28000, "timeout " + m[1]);
  for (const m of src.matchAll(/_MS = (\d+)/g)) assert.ok(Number(m[1]) <= 60000 || /FORBIDDEN|PENDING/.test(m[0]), m[0]);
});

test("MV3: a backoff is persisted, so waiting longer than a worker lives is just a number in storage", async () => {
  const env = makeEnv({ storage: reg(), fakeTime: true });
  env.poll({}, 403);
  await waitFor(() => env.storage.backoffUntil > 0, 3000, "persisted");
  assert.ok(env.storage.backoffUntil - env.time > 50000);
});

// ── the manifest and the files ──
test("manifest v0.2.1: the permissions, CSP and entry points", () => {
  assert.strictEqual(MANIFEST.manifest_version, 3);
  assert.strictEqual(MANIFEST.version, "0.2.1");
  assert.deepStrictEqual([...MANIFEST.permissions].sort(), ["activeTab", "alarms", "contextMenus", "idle", "notifications", "scripting", "sidePanel", "storage", "tabs"]);
  assert.deepStrictEqual(MANIFEST.host_permissions, ["<all_urls>"]);
  assert.strictEqual(MANIFEST.content_security_policy.extension_pages, "script-src 'self'; object-src 'self'");
  for (const banned of ["cookies", "history", "downloads", "debugger", "webRequest", "nativeMessaging", "clipboardRead", "bookmarks", "management"]) assert.ok(!MANIFEST.permissions.includes(banned), banned);
  assert.strictEqual(MANIFEST.background.service_worker, "background.js");
  assert.strictEqual(MANIFEST.side_panel.default_path, "panel.html");
  assert.strictEqual(MANIFEST.action.default_popup, "popup.html");
  assert.strictEqual(MANIFEST.options_ui.page, "options.html");
  for (const f of [MANIFEST.background.service_worker, MANIFEST.side_panel.default_path, MANIFEST.action.default_popup, MANIFEST.options_ui.page, ...Object.values(MANIFEST.icons), ...Object.values(MANIFEST.action.default_icon)]) {
    assert.ok(fs.existsSync(path.join(DIR, f)), f + " exists");
  }
  for (const f of Object.values(MANIFEST.icons)) assert.strictEqual(fs.readFileSync(path.join(DIR, f)).slice(0, 4).toString("hex"), "89504e47", f + " is a PNG");
});

test("every page loads only local scripts, no inline script, no remote anything", () => {
  for (const f of ["popup.html", "options.html", "panel.html"]) {
    const html = fs.readFileSync(path.join(DIR, f), "utf8");
    assert.ok(!/<script(?![^>]*\bsrc=)/i.test(html), f + " has an inline script");
    assert.ok(!/(?:src|href|action)=["']https?:/i.test(html), f + " loads or links something remote");
    for (const m of html.matchAll(/<script[^>]*src="([^"]+)"/g)) assert.ok(fs.existsSync(path.join(DIR, m[1])), f + " -> " + m[1]);
    for (const m of html.matchAll(/<link[^>]*href="([^"]+)"/g)) assert.ok(fs.existsSync(path.join(DIR, m[1])), f + " -> " + m[1]);
    assert.ok(/<script src="lib\.js">/.test(html), f + " loads lib.js");
  }
});

test("source hygiene: no eval, no innerHTML, no remote code, scripting only where it's meant to be, every file parses", () => {
  const { execFileSync } = require("child_process");
  for (const f of fs.readdirSync(DIR).filter((x) => x.endsWith(".js"))) {
    const src = fs.readFileSync(path.join(DIR, f), "utf8");
    for (const banned of ["eval(", "new Function", "innerHTML", "outerHTML", "document.write", "importScripts(\"http", "chrome.cookies", "chrome.history", "chrome.downloads", "chrome.debugger", "chrome.webRequest", "XMLHttpRequest"]) {
      assert.ok(!src.includes(banned), f + " uses " + banned);
    }
    if (!["background.js", "panel.js"].includes(f)) assert.ok(!src.replace(/\/\/.*$/gm, "").includes("executeScript"), f + " must not read pages");
    execFileSync(process.execPath, ["--check", path.join(DIR, f)]);
  }
  assert.ok(!/chrome\.tabs\.(create|update|remove)/.test(fs.readFileSync(path.join(DIR, "lib.js"), "utf8")));
  assert.ok(!/chrome\./.test(fs.readFileSync(path.join(DIR, "lib.js"), "utf8").replace(/\/\/.*$/gm, "")), "lib.js stays free of chrome.* calls");
});

run();
