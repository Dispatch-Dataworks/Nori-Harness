// The pages: side panel ("Ask about this page"), popup and options, run for real against a fake DOM and the same
// fake chrome/network as the worker. Run: node tests/test_browser_extension_panel.js
"use strict";
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const { L, DIR, REGISTERED, sleep, waitFor, tab, makeEnv, loadPage, suite, plain } = require("./browser_extension_harness");
const { test, run } = suite("panel + popup + options");

const reg = (o) => Object.assign({}, REGISTERED, o || {});
const PAGE = tab(1, { active: true, title: "The Weather Report", url: "https://news.example.com/weather?city=ny#top" });

// ── lib.js helpers the panel relies on ──
test("buildAskBody: question and text cleaned and capped; describeAsk names title, host and the character count", () => {
  const b = L.buildAskBody({ question: "  What is this?\u0000 ", url: "https://a.com/x?y=1", title: "T\n\nitle", text: "word ".repeat(20000) });
  assert.strictEqual(b.ok, true);
  assert.deepStrictEqual(Object.keys(b.body), ["question", "url", "title", "text"]);
  assert.strictEqual(b.body.question, "What is this?");
  assert.strictEqual(b.body.title, "T itle");
  assert.ok(b.body.text.length <= 30000);
  assert.strictEqual(b.chars, b.body.text.length);
  assert.strictEqual(L.buildAskBody({ question: "   " }).ok, false);
  assert.ok(L.buildAskBody({ question: "q".repeat(9000), text: "" }).body.question.length <= 2000);
  assert.strictEqual(L.describeAsk("The Weather Report", "news.example.com", 1234), "Sending \u201cThe Weather Report\u201d (news.example.com) with 1234 characters of page text");
  assert.ok(/with 1 character of/.test(L.describeAsk("t", "h", 1)));
});

test("httpErrorText: honest wording for each status", () => {
  assert.match(L.httpErrorText(401), /removed on the server/);
  assert.match(L.httpErrorText(403, "", "asking"), /asking is switched off/);
  assert.match(L.httpErrorText(429), /moment/);
  assert.match(L.httpErrorText(500), /\(500\)/);
  assert.strictEqual(L.httpErrorText(400, "the page text was empty"), "the page text was empty");
});

// ── side panel ──
async function panelEnv(envOpts) {
  const env = makeEnv(Object.assign({ storage: reg(), tabs: [PAGE], pageText: "It will rain on Tuesday. " + "Details. ".repeat(50) }, envOpts || {}));
  const page = loadPage(env, "panel.js");
  await sleep(30);
  return { env, page };
}
const ask = async (page, q) => { page.el("q").value = q; await page.el("form").trigger("submit"); };
const noReads = (env) => { assert.deepStrictEqual(env.scriptCalls, [], "no page text may be read"); assert.strictEqual(env.asks().length, 0, "nothing may be sent"); };

test("opening the panel reads nothing and sends nothing; it only names the tab (title and host)", async () => {
  const { env, page } = await panelEnv();
  assert.match(page.el("page").textContent, /The Weather Report/);
  assert.match(page.el("page").textContent, /news\.example\.com/);
  assert.ok(!page.el("page").textContent.includes("city=ny"), "the address isn't even shown, let alone sent");
  noReads(env);
  await env.fire("tabs.onActivated", { tabId: 1, windowId: 1 });                        // tab changes: still no reading
  await env.fire("tabs.onUpdated", 1, { title: "x" }, PAGE);
  await sleep(30);
  noReads(env);
  assert.strictEqual(env.calls.length > 0 ? env.calls.filter((c) => c.path.endsWith("/bridge/ask")).length : 0, 0);
});

test("Ask: reads the visible text once (cap 30,000), shows what is sent, POSTs /bridge/ask, shows the reply", async () => {
  const { env, page } = await panelEnv({ routes: { "/bridge/ask": () => ({ status: 200, json: { ok: true, reply: "Rain on Tuesday." } }) } });
  await ask(page, "When will it rain?");
  await waitFor(() => env.asks().length === 1, 3000, "ask");
  assert.strictEqual(env.scriptCalls.length, 1);
  assert.strictEqual(env.scriptCalls[0].name, "pageTextCollector");
  assert.deepStrictEqual(env.scriptCalls[0].args, [30000]);
  const a = env.asks()[0];
  assert.strictEqual(a.url, "https://nori.example/bridge/ask");
  assert.strictEqual(a.auth, "Bearer " + REGISTERED.token);
  assert.deepStrictEqual(Object.keys(a.body), ["question", "url", "title", "text"]);
  assert.strictEqual(a.body.question, "When will it rain?");
  assert.strictEqual(a.body.url, "https://news.example.com/weather?city=ny");
  assert.strictEqual(a.body.title, "The Weather Report");
  assert.ok(a.body.text.startsWith("It will rain on Tuesday."));
  await waitFor(() => page.log().some((l) => l.startsWith("bubble nori")), 3000, "reply shown");
  assert.deepStrictEqual(page.log(), ["bubble user | When will it rain?", "bubble nori | Rain on Tuesday."]);
  const sent = page.el("sent").textContent;
  assert.ok(sent.startsWith("Sent \u201cThe Weather Report\u201d (news.example.com) with " + a.body.text.length + " characters of page text"), sent);
  assert.strictEqual(page.el("ask").disabled, false);
  assert.strictEqual(page.el("q").value, "");
});

test("while it is being sent the panel already says what is going out (title, host, N characters)", async () => {
  let release;
  const gate = new Promise((r) => { release = r; });
  const { env, page } = await panelEnv({ routes: { "/bridge/ask": async () => { await gate; return { status: 200, json: { ok: true, reply: "ok" } }; } } });
  const first = ask(page, "Q?");                                                         // not awaited: the handler waits for the network
  await waitFor(() => env.asks().length === 1, 3000, "request in flight");
  assert.match(page.el("sent").textContent, /^Sending \u201cThe Weather Report\u201d \(news\.example\.com\) with \d+ characters of page text/);
  assert.strictEqual(page.el("ask").disabled, true);
  await ask(page, "a second question while waiting");                                    // ignored: one at a time
  await sleep(40);
  assert.strictEqual(env.scriptCalls.length, 1);
  assert.strictEqual(env.asks().length, 1);
  release();
  await first;
  assert.strictEqual(page.el("ask").disabled, false);
});

test("a huge page is cut to 30,000 characters and the panel says exactly how many were sent", async () => {
  const { env, page } = await panelEnv({ pageText: "lorem ipsum dolor ".repeat(40000) });
  await ask(page, "Summarise");
  await waitFor(() => env.asks().length === 1, 3000, "ask");
  const n = env.asks()[0].body.text.length;
  assert.ok(n <= 30000 && n > 29000);
  await waitFor(() => /^Sent/.test(page.el("sent").textContent), 3000, "sent line");
  assert.ok(page.el("sent").textContent.includes(n + " characters of page text"));
});

test("a reply is shown as plain text, never as markup", async () => {
  const evil = '<img src=x onerror="alert(1)"><script>steal()</script>**bold**';
  const { page } = await panelEnv({ routes: { "/bridge/ask": () => ({ status: 200, json: { ok: true, reply: evil } }) } });
  await ask(page, "x");
  await waitFor(() => page.log().length === 2, 3000, "reply");
  assert.strictEqual(page.els.log.children[1].textContent, evil);
  assert.ok(!fs.readFileSync(path.join(DIR, "panel.js"), "utf8").includes("innerHTML"));
});

test("refused without reading or sending: hidden host (wildcard), private window, paused, unpaired, non-web page, credentials in the address", async () => {
  const cases = [
    [{ storage: reg({ serverSettings: { hidden_hosts: ["*.example.com"] } }) }, /private/],
    [{ storage: reg({ serverSettings: { hidden_hosts: ["news.example.com"] } }) }, /private/],
    [{ tabs: [Object.assign({}, PAGE, { incognito: true })] }, /private windows/],
    [{ storage: reg({ paused: true }) }, /paused/],
    [{ storage: {} }, /isn't paired/],
    [{ tabs: [Object.assign({}, PAGE, { url: "chrome://settings" })] }, /ordinary web pages/],
    [{ tabs: [Object.assign({}, PAGE, { url: "https://u:pw@news.example.com/" })] }, /name or password/],
    [{ tabs: [Object.assign({}, PAGE, { url: "file:///C:/secret.pdf" })] }, /ordinary web pages/],
  ];
  for (const [o, rx] of cases) {
    const { env, page } = await panelEnv(o);
    await ask(page, "What is this?");
    await sleep(30);
    noReads(env);
    assert.ok(page.log().some((l) => l.startsWith("bubble system err") && rx.test(l)), JSON.stringify(o) + " -> " + page.log().join(" / "));
    assert.ok(page.log().every((l) => !l.startsWith("bubble user")), "the question isn't even echoed as sent");
  }
  const { env, page } = await panelEnv({ tabs: [Object.assign({}, PAGE, { incognito: true })], storage: reg({ includePrivate: true }) });
  await ask(page, "private ok?");
  await waitFor(() => env.asks().length === 1, 3000, "allowed once the option is on");
});

test("an empty question does nothing", async () => {
  const { env, page } = await panelEnv();
  await ask(page, "   ");
  noReads(env);
  assert.match(page.el("sent").textContent, /Type a question/);
});

test("errors are reported honestly and the page text is not claimed to be delivered when it wasn't", async () => {
  const cases = [
    [{ status: 401, json: {} }, /removed on the server/],
    [{ status: 403, json: {} }, /switched off/],
    [{ status: 429, json: {} }, /moment/],
    [{ status: 500, json: {} }, /\(500\)/],
    [{ status: 200, json: { ok: false, error: "she can't read that" } }, /she can't read that/],
    [{ status: 200, json: { ok: true } }, /server answered 200/],
    [{ throws: "ECONNRESET" }, /Couldn't reach Nori/],
  ];
  for (const [route, rx] of cases) {
    const { env, page } = await panelEnv({ routes: { "/bridge/ask": () => route } });
    await ask(page, "Q?");
    await waitFor(() => page.log().some((l) => l.startsWith("bubble system err")), 3000, "error shown");
    assert.ok(page.log().some((l) => rx.test(l)), JSON.stringify(route) + " -> " + page.log().join(" / "));
    assert.ok(!page.log().some((l) => l.startsWith("bubble nori")));
    assert.strictEqual(page.el("ask").disabled, false, "the panel is usable again");
    if (route.status === 401) assert.strictEqual(env.storage.authFailedToken, REGISTERED.token);
  }
});

test("a page that can't be read (blocked by the browser) or that changes while being read is reported and nothing is sent", async () => {
  let { env, page } = await panelEnv({ scriptFails: true });
  await ask(page, "Q?");
  await waitFor(() => page.log().some((l) => /can't be read/.test(l)), 3000, "blocked");
  assert.strictEqual(env.asks().length, 0);
  ({ env, page } = await panelEnv({ script: async (o) => { env2.setTabs([Object.assign({}, PAGE, { url: "https://news.example.com/other" })]); return { title: "", url: "", text: "text" }; } }));
  var env2 = env;
  await ask(page, "Q?");
  await waitFor(() => page.log().some((l) => /changed while I was reading/.test(l)), 3000, "changed");
  assert.strictEqual(env.asks().length, 0);
});

test("the panel never reads a page on its own: the only executeScript call sits inside the submit handler", () => {
  const src = fs.readFileSync(path.join(DIR, "panel.js"), "utf8");
  assert.strictEqual((src.match(/executeScript/g) || []).length, 1);
  const handlerStart = src.indexOf("async function onAsk");
  const handlerEnd = src.indexOf("$(\"form\").addEventListener");
  assert.ok(src.indexOf("executeScript") > handlerStart && src.indexOf("executeScript") < handlerEnd);
  assert.ok(src.indexOf("onAsk") > 0 && /addEventListener\("submit", onAsk\)/.test(src));
});

// ── popup ──
async function popupEnv(envOpts) {
  const env = makeEnv(Object.assign({ storage: reg(), tabs: [PAGE] }, envOpts || {}));
  const page = loadPage(env, "popup.js");
  await sleep(40);
  return { env, page };
}
// The pages run on the real clock here, so "fresh" means now.
const fresh = (env, extra) => env.chrome.storage.local.set(Object.assign({ settingsTs: Date.now() }, extra || {}));

test("popup (not paired): says so, can't send, can't pause", async () => {
  const { page } = await popupEnv({ storage: {} });
  assert.match(page.el("state").textContent, /Not paired/);
  assert.strictEqual(page.el("send").disabled, true);
  assert.strictEqual(page.el("pause").disabled, true);
  assert.match(page.el("send-msg").textContent, /isn't paired/);
  assert.strictEqual(page.el("sh-notif").textContent, "desktop notifications: off");
});

test("popup: 'what is being shared right now' comes from real state, not from claims", async () => {
  const { env, page } = await popupEnv();
  await fresh(env, { serverSettings: { notifications: true, activity: false, standing_look: false, hidden_hosts: [] }, status: { state: "idle (no session)", ts: 1 } });
  await sleep(40);
  assert.strictEqual(page.el("state").textContent, "idle (no session)");
  assert.strictEqual(page.el("sh-notif").textContent, "desktop notifications: on");
  assert.strictEqual(page.el("sh-activity").textContent, "browsing time sharing: off");
  assert.strictEqual(page.el("sh-look").textContent, "standing look: off");
  assert.strictEqual(page.el("sh-private").textContent, "private windows: not included");
  await fresh(env, { serverSettings: { notifications: true, activity: true, standing_look: true, hidden_hosts: [] },
    lastActivity: { ts: Date.now() - 30000, ok: true, hosts: [{ host: "docs.example.com", seconds: 55 }] } });
  await sleep(40);
  assert.strictEqual(page.el("sh-activity").textContent, "browsing time sharing: on");
  assert.strictEqual(page.el("sh-activity").className, "line on");
  assert.strictEqual(page.el("sh-look").textContent, "standing look: on");
  assert.strictEqual(page.el("last-activity").hidden, false);
  assert.match(page.el("last-activity").textContent, /docs\.example\.com 55 s/);
  assert.ok(!/https?:|\//.test(page.el("last-activity").textContent.replace("host names and seconds only", "")), "host names and seconds only");
  await env.chrome.storage.local.set({ settingsTs: Date.now() - 10 * 60000 });             // the server hasn't confirmed lately -> off
  await sleep(40);
  assert.strictEqual(page.el("sh-activity").textContent, "browsing time sharing: off");
  assert.strictEqual(page.el("sh-look").textContent, "standing look: off");
  assert.strictEqual(page.el("last-activity").hidden, true);
  await fresh(env, { notify: false });
  await sleep(40);
  assert.strictEqual(page.el("sh-notif").textContent, "desktop notifications: off");
  await fresh(env, { notify: true, notifBlocked: true });
  await sleep(40);
  assert.strictEqual(page.el("sh-notif").textContent, "desktop notifications: off");
  await fresh(env, { notifBlocked: false, includePrivate: true });
  await sleep(40);
  assert.strictEqual(page.el("sh-private").textContent, "private windows: included");
});

test("popup: pause switches everything off at once and shows it; Resume brings it back", async () => {
  const { env, page } = await popupEnv();
  await fresh(env, { serverSettings: { notifications: true, activity: true, standing_look: true } });
  await sleep(40);
  assert.strictEqual(page.el("pause").textContent, "Pause");
  await page.el("pause").trigger("click");
  await sleep(40);
  assert.strictEqual(env.storage.paused, true);
  assert.match(page.el("state").textContent, /Paused/);
  assert.strictEqual(page.el("pause").textContent, "Resume");
  for (const id of ["sh-notif", "sh-activity", "sh-look"]) assert.ok(/: off$/.test(page.el(id).textContent), id);
  assert.strictEqual(page.el("send").disabled, true);
  await page.el("pause").trigger("click");
  await sleep(40);
  assert.strictEqual(env.storage.paused, false);
});

test("popup: the last command and the last send are shown from what really happened", async () => {
  const { env, page } = await popupEnv();
  await env.chrome.storage.local.set({
    lastCommand: { action: "click", ok: false, error: "a session isn't running; I can only look", look_only: true, ts: Date.now() - 120000 },
    lastShare: { ok: true, kind: "page", dest: "note", ts: Date.now() - 5000 },
  });
  await sleep(40);
  assert.strictEqual(page.el("last-command").textContent, "(standing look) click \u2014 refused or failed: a session isn't running; I can only look, 2 min ago");
  assert.strictEqual(page.el("last-share").textContent, "Sent page to note, just now");
  await env.chrome.storage.local.set({ lastCommand: { action: "click", ok: true, waiting: true, ts: Date.now() }, lastShare: { ok: false, error: "private windows are not shared", ts: Date.now() } });
  await sleep(40);
  assert.match(page.el("last-command").textContent, /waiting for your approval/);
  assert.match(page.el("last-share").textContent, /Not sent: private windows are not shared/);
});

test("popup: 'Send this page' goes through the worker with the chosen destination, text option and note", async () => {
  const { env, page } = await popupEnv({ pageText: "page words ".repeat(1000) });
  page.el("dest").value = "task";
  await page.el("dest").trigger("change");
  assert.strictEqual(env.storage.shareDest, "task");
  page.el("note").value = "follow up";
  await page.el("send").trigger("click");
  await waitFor(() => env.shares().length === 1, 3000, "share");
  assert.deepStrictEqual(env.shares()[0].body, { kind: "page", dest: "task", url: "https://news.example.com/weather?city=ny", title: "The Weather Report", note: "follow up" });
  assert.deepStrictEqual(env.scriptCalls, []);
  await waitFor(() => /^Sent/.test(page.el("send-msg").textContent), 3000, "confirmation");
  assert.strictEqual(page.el("note").value, "");
  page.el("withtext").checked = true;
  await page.el("withtext").trigger("change");
  assert.match(page.el("send-what").textContent, /8,000 characters/);
  await page.el("send").trigger("click");
  await waitFor(() => env.shares().length === 2, 3000, "share with text");
  assert.ok(env.shares()[1].body.text.length <= 8000 && env.shares()[1].body.text.length > 100);
});

test("popup: a refused send says why (hidden site, private window) and the button is off", async () => {
  let { page } = await popupEnv({ storage: reg({ serverSettings: { hidden_hosts: ["*.example.com"] } }) });
  assert.strictEqual(page.el("send").disabled, true);
  assert.match(page.el("send-msg").textContent, /keep private/);
  ({ page } = await popupEnv({ tabs: [Object.assign({}, PAGE, { incognito: true })] }));
  assert.strictEqual(page.el("send").disabled, true);
  assert.match(page.el("send-msg").textContent, /private windows/);
  ({ page } = await popupEnv({ tabs: [Object.assign({}, PAGE, { url: "chrome://extensions" })] }));
  assert.strictEqual(page.el("send").disabled, true);
});

test("popup: Open side panel (hidden when the browser has none) and Options", async () => {
  let { env, page } = await popupEnv();
  assert.strictEqual(page.el("panel").hidden, false);
  await page.el("panel").trigger("click");
  await sleep(20);
  assert.deepStrictEqual(env.panelOpens, [{ windowId: 1 }]);
  assert.strictEqual(page.state.closed, true);
  await page.el("options").trigger("click");
  assert.ok(env.actions.some((a) => a.op === "openOptions"));
  ({ page } = await popupEnv({ noSidePanel: true }));
  assert.strictEqual(page.el("panel").hidden, true);
});

// ── options ──
async function optionsEnv(envOpts) {
  const env = makeEnv(Object.assign({ storage: reg({ name: "Desk" }), tabs: [PAGE] }, envOpts || {}));
  const page = loadPage(env, "options.js");
  await sleep(40);
  return { env, page };
}

test("options: shows the current pairing, never puts the token back on screen, local switches reflect storage", async () => {
  const { page } = await optionsEnv({ storage: reg({ name: "Desk", notify: false, includePrivate: true }) });
  assert.strictEqual(page.el("url").value, "https://nori.example");
  assert.strictEqual(page.el("name").value, "Desk");
  assert.strictEqual(page.el("token").value, "");
  assert.strictEqual(page.el("notify").checked, false);
  assert.strictEqual(page.el("private").checked, true);
  assert.match(page.el("paired").textContent, /Paired as .Desk. with https:\/\/nori\.example/);
  assert.ok(!page.el("paired").textContent.includes(REGISTERED.token));
  assert.strictEqual(page.el("paired").hidden, false);
});

test("options: pair with a code -> /bridge/register -> token stored, code field cleared, success shown", async () => {
  const { env, page } = await optionsEnv({ storage: {} });
  page.el("url").value = "https://nori.example/";
  page.el("code").value = "wxyz-2345";
  page.el("name").value = "Laptop";
  await page.el("pair").trigger("click");
  await waitFor(() => env.registers().length === 1, 3000, "register");
  assert.deepStrictEqual(env.registers()[0].body, { code: "WXYZ-2345", kind: "browser", name: "Laptop", v: "0.2.1", model: "Chrome 130 on Windows" });
  await waitFor(() => env.storage.token, 3000, "token stored");
  await sleep(30);
  assert.strictEqual(page.el("code").value, "");
  assert.match(page.el("pair-msg").textContent, /Paired as/);
  assert.match(page.el("pair-msg").className, /ok/);
  assert.strictEqual(page.el("pair").disabled, false);
});

test("options: a refused pairing shows the reason and stores nothing", async () => {
  const { env, page } = await optionsEnv({ storage: {}, routes: { "/bridge/register": () => ({ status: 404, json: {} }) } });
  page.el("url").value = "https://nori.example";
  page.el("code").value = "AAAA-BBBB";
  await page.el("pair").trigger("click");
  await waitFor(() => /wasn't accepted/.test(page.el("pair-msg").textContent), 3000, "message");
  assert.match(page.el("pair-msg").className, /err/);
  assert.ok(!env.storage.token);
  page.el("code").value = "x";
  await page.el("pair").trigger("click");
  await waitFor(() => /XXXX-XXXX/.test(page.el("pair-msg").textContent), 3000, "format hint");
});

test("options: the old 'paste a token' path still works, and wipes the previous pairing's bookkeeping", async () => {
  const { env, page } = await optionsEnv({ storage: reg({ lastAck: 77, shown: [77], serverSettings: { activity: true } }) });
  page.el("url").value = "http://127.0.0.1:8877/";
  page.el("token").value = "nbr_pasted_token_0123456789";
  await page.el("save").trigger("click");
  await sleep(30);
  assert.strictEqual(env.storage.baseUrl, "http://127.0.0.1:8877");
  assert.strictEqual(env.storage.token, "nbr_pasted_token_0123456789");
  assert.strictEqual(env.storage.lastAck, 0);
  assert.deepStrictEqual(plain(env.storage.shown), []);
  assert.strictEqual(env.storage.serverSettings, null);
  assert.match(page.el("save-msg").textContent, /Saved/);
  assert.strictEqual(page.el("token").value, "");
  page.el("token").value = "npn_phone_token";
  await page.el("save").trigger("click");
  assert.match(page.el("save-msg").textContent, /nbr_/);
  assert.strictEqual(env.storage.token, "nbr_pasted_token_0123456789", "a wrong kind of token isn't saved");
  page.el("url").value = "nori.example";
  page.el("token").value = "nbr_x";
  await page.el("save").trigger("click");
  assert.match(page.el("save-msg").textContent, /http/);
});

test("options: plain http to a non-local address shows a warning", async () => {
  const { page } = await optionsEnv({ storage: {} });
  page.el("url").value = "http://nori.example.com";
  await page.el("url").trigger("input");
  assert.strictEqual(page.el("url-warn").hidden, false);
  page.el("url").value = "https://nori.example.com";
  await page.el("url").trigger("input");
  assert.strictEqual(page.el("url-warn").hidden, true);
});

test("options: the notification and include-private switches are saved at once", async () => {
  const { env, page } = await optionsEnv();
  page.el("notify").checked = false;
  await page.el("notify").trigger("change");
  assert.strictEqual(env.storage.notify, false);
  page.el("private").checked = true;
  await page.el("private").trigger("change");
  assert.strictEqual(env.storage.includePrivate, true);
});

test("options: Remove takes two clicks, forgets the token and everything tied to it, and keeps his own pause/private choices", async () => {
  const { env, page } = await optionsEnv({ storage: reg({ name: "Desk", lastAck: 5, shown: [5], paused: true, includePrivate: true, activityState: { acc: {}, cur: null, since: null } }) });
  await page.el("remove").trigger("click");
  assert.ok(env.storage.token, "one click only arms it");
  assert.match(page.el("remove").textContent, /again/);
  await page.el("remove").trigger("click");
  await sleep(30);
  assert.strictEqual(env.storage.token, "");
  assert.strictEqual(env.storage.name, "");
  assert.strictEqual(env.storage.lastAck, 0);
  assert.strictEqual(env.storage.activityState, null);
  assert.strictEqual(env.storage.paused, true);
  assert.strictEqual(env.storage.includePrivate, true);
  assert.match(page.el("remove-msg").textContent, /Also remove it in Nori/);
  assert.strictEqual(page.el("paired").hidden, true);
  await waitFor(() => env.storage.status && env.storage.status.state === "not configured" || true, 100, "n/a");
});

run();
