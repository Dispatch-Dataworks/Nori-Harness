// Send to Nori: the pure builders/gates in lib.js, the page-text collector, and the real worker's context-menu and
// popup flows against a fake chrome + network. Run: node tests/test_browser_extension_share.js
"use strict";
const assert = require("assert");
const vm = require("vm");
const { L, EXT_ID, REGISTERED, sleep, waitFor, tab, makeEnv, suite, plain } = require("./browser_extension_harness");
const { test, run } = suite("share");

const POL = (o) => Object.assign({ configured: true, paused: false, includePrivate: false, hidden: [] }, o || {});
const TAB = tab(5, { active: true, title: "A good read", url: "https://blog.example.com/post?id=7&utm=x#section" });

// ── gating ──
test("gatePage: every refusal, and the one yes", () => {
  assert.strictEqual(L.gatePage("https://blog.example.com/x", false, POL()).ok, true);
  assert.strictEqual(L.gatePage("https://blog.example.com/x", false, POL()).host, "blog.example.com");
  assert.strictEqual(L.gatePage("https://x.com/", false, POL({ configured: false })).code, "unconfigured");
  assert.strictEqual(L.gatePage("https://x.com/", false, POL({ paused: true })).code, "paused");
  assert.strictEqual(L.gatePage("https://x.com/", true, POL()).code, "private");
  assert.strictEqual(L.gatePage("https://x.com/", true, POL({ includePrivate: true })).ok, true);
  for (const bad of ["chrome://settings", "file:///C:/x.pdf", "javascript:alert(1)", "about:blank", "data:text/html,hi", "blob:https://x.com/a", "", null, undefined, "not a url", "ftp://x.com/"]) {
    assert.strictEqual(L.gatePage(bad, false, POL()).code, "nonweb", String(bad));
  }
  assert.strictEqual(L.gatePage("https://user:pw@x.com/", false, POL()).code, "credentials");
});

test("hidden hosts: exact, wildcard (and its apex), case, trailing dot; look-alikes are not hidden", () => {
  const hidden = ["mybank.com", "*.corp.example", "Private.Site"];
  for (const h of ["https://mybank.com/a", "https://MYBANK.com./a", "https://a.corp.example/", "https://corp.example/", "https://x.y.corp.example/", "https://private.site/"]) {
    assert.strictEqual(L.gatePage(h, false, POL({ hidden })).code, "hidden", h);
  }
  for (const h of ["https://www.mybank.com/", "https://notmybank.com/", "https://mybank.com.evil.io/", "https://evilcorp.example/", "https://corp.example.com/"]) {
    assert.strictEqual(L.gatePage(h, false, POL({ hidden })).ok, true, h);
  }
  assert.strictEqual(L.hostHidden("a.mybank.com", ["*.mybank.com"]), true);
  assert.strictEqual(L.hostHidden(null, ["*.x.com"]), false);
  assert.strictEqual(L.hostHidden("x.com", null), false);
  assert.deepStrictEqual(L.sanitizeHostList(["*.a.com", " B.com ", 5, "", "bad host", "x/y", "*.*.z"]), ["*.a.com", "b.com"]);
  assert.strictEqual(L.sanitizeHostList("nope"), null);
});

test("shareUrl: the query stays, the fragment goes, credentials and hidden hosts are refused", () => {
  assert.strictEqual(L.shareUrl("https://a.example.com/p?q=1#f").url, "https://a.example.com/p?q=1");
  assert.strictEqual(L.shareUrl("https://u:p@a.com/").ok, false);
  assert.strictEqual(L.shareUrl("https://x.bank.com/", ["*.bank.com"]).ok, false);
  assert.strictEqual(L.shareUrl("javascript:alert(1)").ok, false);
  assert.strictEqual(L.shareUrl("https://a.com/" + "x".repeat(3000)).ok, false);
  assert.strictEqual(L.shareUrl("http://localhost:3000/a").ok, true);
});

// ── body builders ──
test("page: title and address only, unless the page text was chosen; text is capped at 8000", () => {
  const plainPage = L.buildShareBody({ kind: "page", dest: "chat", url: "https://a.com/x", title: "T\u0000itle" });
  assert.deepStrictEqual(plainPage.body, { kind: "page", dest: "chat", url: "https://a.com/x", title: "T itle" });
  assert.ok(!("text" in plainPage.body));
  const withText = L.buildShareBody({ kind: "page", dest: "note", url: "https://a.com/x", title: "T", pageText: "word ".repeat(10000) });
  assert.ok(withText.body.text.length <= 8000);
  assert.strictEqual(withText.body.dest, "note");
});

test("selection: capped at 2000 characters even when it is enormous, and an empty one is refused", () => {
  const t0 = Date.now();
  const huge = L.buildShareBody({ kind: "selection", dest: "chat", url: "https://a.com/", title: "T", selection: "lorem ipsum\n".repeat(900000) });
  assert.ok(Date.now() - t0 < 1500, "must not choke on a 10 MB selection");
  assert.ok(huge.body.text.length <= 2000 && huge.body.text.length > 1000);
  assert.strictEqual(L.buildShareBody({ kind: "selection", selection: "  \n\t " }).ok, false);
  assert.strictEqual(L.buildShareBody({ kind: "selection", selection: "\u0000\u202e\u200b" }).ok, false);
  assert.ok(!/[\u202e\u200b]/.test(L.buildShareBody({ kind: "selection", selection: "a\u202eb\u200bc" }).body.text));
});

test("link, screenshot, dest, note and unknown kinds", () => {
  assert.deepStrictEqual(L.buildShareBody({ kind: "link", dest: "task", url: "https://o.com/x?y=1" }).body, { kind: "link", dest: "task", url: "https://o.com/x?y=1" });
  assert.strictEqual(L.buildShareBody({ kind: "link", dest: "task" }).ok, false);
  const s = L.buildShareBody({ kind: "screenshot", dest: "chat", url: "https://a.com/", title: "T", jpegB64: "QUJD" });
  assert.strictEqual(s.body.jpeg_b64, "QUJD");
  assert.strictEqual(s.body.mime, "image/jpeg");
  assert.strictEqual(L.buildShareBody({ kind: "screenshot", jpegB64: "" }).ok, false);
  assert.strictEqual(L.buildShareBody({ kind: "screenshot", jpegB64: "A".repeat(4000000) }).ok, false);
  assert.strictEqual(L.buildShareBody({ kind: "page", dest: "everyone", url: "https://a.com/" }).body.dest, "chat");     // an unknown destination falls back to chat
  assert.strictEqual(L.buildShareBody({ kind: "page", dest: undefined, url: "https://a.com/" }).body.dest, "chat");
  assert.strictEqual(L.buildShareBody({ kind: "page", dest: "note", url: "https://a.com/", note: "  remember this\u0000 " }).body.note, "remember this");
  assert.ok(L.buildShareBody({ kind: "page", url: "https://a.com/", note: "n".repeat(5000) }).body.note.length <= 1000);
  assert.strictEqual(L.buildShareBody({ kind: "email" }).ok, false);
  assert.strictEqual(L.buildShareBody(null).ok, false);
});

test("data URLs from captureVisibleTab", () => {
  assert.strictEqual(L.dataUrlToJpeg("data:image/jpeg;base64,QUJD"), "QUJD");
  assert.strictEqual(L.dataUrlToJpeg("data:image/png;base64,QUJD"), null);
  assert.strictEqual(L.dataUrlToJpeg("data:image/jpeg;base64,<script>"), null);
  assert.strictEqual(L.dataUrlToJpeg(undefined), null);
});

test("the menu: five parents, three destinations each (chat first), ids round-trip, nothing else parses", () => {
  const spec = L.shareMenuSpec();
  const parents = spec.filter((m) => !m.parentId);
  assert.strictEqual(parents.length, 5);
  for (const p of parents) {
    const kids = spec.filter((m) => m.parentId === p.id);
    assert.deepStrictEqual(kids.map((k) => k.id.split("|")[2]), ["chat", "note", "task"]);
    assert.deepStrictEqual(kids[0].contexts, p.contexts);
  }
  assert.ok(!spec.some((m) => m.contexts.includes("image")), "no image sharing");
  assert.deepStrictEqual(L.parseShareMenuId("nori|selection|task"), { kind: "selection", withText: false, dest: "task" });
  assert.deepStrictEqual(L.parseShareMenuId("nori|pagetext|note"), { kind: "page", withText: true, dest: "note" });
  for (const bad of ["nori|page|everyone", "nori|image|chat", "nori-root|page", "claire-page", "", null, undefined]) assert.strictEqual(L.parseShareMenuId(bad), null, String(bad));
});

// ── page-text cleaning ──
test("cleanPageText: markup, invisible characters and control characters go; whitespace collapses; it is capped", () => {
  const dirty = "Hello\u200b \u202eworld<script>steal()</script> and <style>body{}</style>more <!-- hidden --> text\u0000\u001b[31m\n\n\n\n\nlast  line\t\tend";
  const out = L.cleanPageText(dirty, 30000);
  assert.ok(!/script|steal|body\{|hidden|\u200b|\u202e|\u0000|\u001b/.test(out), out);
  assert.ok(!/\n\n\n/.test(out));
  assert.ok(/Hello/.test(out) && /more/.test(out) && /last line end/.test(out));
  assert.ok(L.cleanPageText("x".repeat(500000), 30000).length <= 30000);
  assert.ok(L.cleanPageText("<script>" + "a".repeat(100), 100) === "");                    // an unterminated script swallows the rest
  assert.strictEqual(L.cleanPageText(null, 10), "");
  assert.strictEqual(L.cleanPageText("", 10), "");
});

// pageTextCollector is serialised into the page, so it is run here exactly as Chrome would: from its source
// text, in a context that has only a fake document.
const T = (v) => ({ nodeType: 3, nodeValue: v });
const E = (tag, kids, style, extra) => Object.assign({ nodeType: 1, tagName: tag, childNodes: kids || [], style: style || {} }, extra || {});
function collect(body, max) {
  const g = { document: { body, title: "Title", documentElement: body }, location: { href: "https://x.test/p?q=1" }, getComputedStyle: (n) => n.style };
  return vm.runInNewContext("(" + L.pageTextCollector.toString() + ")(" + (max || 30000) + ")", g);
}

test("the page-text collector reads visible text nodes only: never a script, a style, a form field, or a hidden element", () => {
  const body = E("BODY", [
    E("H1", [T("Headline")]),
    E("P", [T("Visible paragraph "), E("B", [T("bold")])]),
    E("SCRIPT", [T("var apiKey='SCRIPT-SECRET'")]),
    E("STYLE", [T(".x{color:red}")]),
    E("NOSCRIPT", [T("NOSCRIPT-TEXT")]),
    E("FORM", [
      E("LABEL", [T("Password")]),
      E("INPUT", [], {}, { type: "password", value: "hunter2-PASSWORD" }),
      E("INPUT", [], {}, { type: "text", value: "TYPED-IN-VALUE" }),
      E("TEXTAREA", [T("DRAFT-MESSAGE-TEXT")]),
      E("SELECT", [E("OPTION", [T("OPTION-TEXT")])]),
    ]),
    E("DIV", [T("display none text")], { display: "none" }),
    E("DIV", [T("visibility hidden text")], { visibility: "hidden" }),
    E("IFRAME", [T("IFRAME-TEXT")]),
    E("svg", [T("SVG-TEXT")]),
    E("TEMPLATE", [T("TEMPLATE-TEXT")]),
    E("DIV", [E("SPAN", [T("nested visible")])]),
  ]);
  const r = collect(body);
  const text = L.cleanPageText(r.text, 30000);
  for (const bad of ["SCRIPT-SECRET", "color:red", "NOSCRIPT-TEXT", "hunter2", "TYPED-IN-VALUE", "DRAFT-MESSAGE-TEXT", "OPTION-TEXT", "display none", "visibility hidden", "IFRAME-TEXT", "SVG-TEXT", "TEMPLATE-TEXT"]) {
    assert.ok(!text.includes(bad), "leaked: " + bad);
  }
  for (const good of ["Headline", "Visible paragraph bold", "nested visible", "Password"]) assert.ok(text.includes(good), "missing: " + good);
  assert.strictEqual(r.title, "Title");
});

test("the page-text collector stops early on a gigantic page and respects its cap", () => {
  const kids = Array.from({ length: 50000 }, (_, i) => E("P", [T("paragraph number " + i + " " + "z".repeat(50))]));
  const r = collect(E("BODY", kids), 2000);
  assert.ok(r.text.length < 2000 * 3 + 200, "stopped early: " + r.text.length);
  assert.ok(L.cleanPageText(r.text, 2000).length <= 2000);
  assert.strictEqual(collect(E("BODY", []), 10).text, "");
});

// ── the real worker ──
async function installed(env) { await env.fire("runtime.onInstalled", { reason: "install" }); }
const click = (env, id, extra, t) => env.fire("contextMenus.onClicked", Object.assign({ menuItemId: id }, extra || {}), t || TAB);
const lastShare = (env) => env.storage.lastShare;

test("on install the worker builds the Send to Nori menu", async () => {
  const env = makeEnv({ storage: Object.assign({}, REGISTERED) });
  await installed(env);
  await waitFor(() => env.menus.length === L.shareMenuSpec().length, 3000, "menus");
  assert.deepStrictEqual(env.menus.map((m) => m.id), L.shareMenuSpec().map((m) => m.id));
});

test("Send this page (chat): title + address (query kept, fragment gone); no page text is read", async () => {
  const env = makeEnv({ storage: Object.assign({}, REGISTERED) });
  await click(env, "nori|page|chat");
  await waitFor(() => env.shares().length === 1, 3000, "share");
  const s = env.shares()[0];
  assert.strictEqual(s.url, "https://nori.example/bridge/share");
  assert.strictEqual(s.auth, "Bearer " + REGISTERED.token);
  assert.deepStrictEqual(s.body, { kind: "page", dest: "chat", url: "https://blog.example.com/post?id=7&utm=x", title: "A good read" });
  assert.deepStrictEqual(env.scriptCalls, [], "the page must not be read for a plain page share");
  await waitFor(() => lastShare(env) && lastShare(env).ok === true, 3000, "noted");
  await waitFor(() => env.badges.includes("ok"), 3000, "badge ok");
});

test("Send this page with its text: read once, capped at 8000, destination honoured", async () => {
  const env = makeEnv({ storage: Object.assign({}, REGISTERED), tabs: [TAB], pageText: "sentence. ".repeat(5000) });
  await click(env, "nori|pagetext|note");
  await waitFor(() => env.shares().length === 1, 3000, "share");
  const b = env.shares()[0].body;
  assert.strictEqual(b.dest, "note");
  assert.ok(b.text && b.text.length <= 8000 && b.text.length > 7000);
  assert.strictEqual(env.scriptCalls.length, 1);
  assert.strictEqual(env.scriptCalls[0].name, "pageTextCollector");
  assert.deepStrictEqual(env.scriptCalls[0].args, [8000]);
});

test("a page whose text can't be read is reported, nothing is sent", async () => {
  const env = makeEnv({ storage: Object.assign({}, REGISTERED), tabs: [TAB], scriptFails: true });
  await click(env, "nori|pagetext|chat");
  await waitFor(() => lastShare(env) && lastShare(env).ok === false, 3000, "failure noted");
  assert.strictEqual(env.shares().length, 0);
  assert.match(lastShare(env).error, /couldn't read/);
});

test("selection: the selected text is sent, capped at 2000, with the page's title and address", async () => {
  const env = makeEnv({ storage: Object.assign({}, REGISTERED) });
  await click(env, "nori|selection|chat", { selectionText: "line one\nline two " + "x".repeat(50000) });
  await waitFor(() => env.shares().length === 1, 3000, "share");
  const b = env.shares()[0].body;
  assert.strictEqual(b.kind, "selection");
  assert.ok(b.text.startsWith("line one\nline two"));
  assert.ok(b.text.length <= 2000);
  assert.strictEqual(b.title, "A good read");
  assert.strictEqual(b.url, "https://blog.example.com/post?id=7&utm=x");
});

test("link: the link is what is sent, to the chosen destination", async () => {
  const env = makeEnv({ storage: Object.assign({}, REGISTERED) });
  await click(env, "nori|link|task", { linkUrl: "https://other.example.org/x?y=1#f" });
  await waitFor(() => env.shares().length === 1, 3000, "share");
  assert.deepStrictEqual(env.shares()[0].body, { kind: "link", dest: "task", url: "https://other.example.org/x?y=1" });
});

test("screenshot: JPEG quality steps down until it fits under ~4 MB", async () => {
  const env = makeEnv({ storage: Object.assign({}, REGISTERED), tabs: [TAB], captureLen: (q) => (q > 30 ? 4500000 : 1000) });
  await click(env, "nori|screenshot|chat");
  await waitFor(() => env.shares().length === 1, 3000, "share");
  assert.deepStrictEqual(env.captures.map((c) => c.quality), [60, 45, 30]);
  const b = env.shares()[0].body;
  assert.strictEqual(b.kind, "screenshot");
  assert.strictEqual(b.mime, "image/jpeg");
  assert.ok(b.jpeg_b64.length < 4000000);
  assert.strictEqual(b.title, "A good read");
});

test("screenshot: a picture that never fits is refused, not sent", async () => {
  const env = makeEnv({ storage: Object.assign({}, REGISTERED), tabs: [TAB], captureLen: () => 4500000 });
  await click(env, "nori|screenshot|chat");
  await waitFor(() => lastShare(env) && lastShare(env).ok === false, 3000, "refused");
  assert.match(lastShare(env).error, /too large/);
  assert.strictEqual(env.shares().length, 0);
});

test("screenshot: refused if the active tab changed while capturing (or the page moved to another address)", async () => {
  const tabs = [TAB, tab(6, { title: "Other", url: "https://other.example/" })];
  let env = makeEnv({ storage: Object.assign({}, REGISTERED), tabs, onCapture: (n, h) => h.setTabs([Object.assign({}, TAB, { active: false }), tab(6, { active: true, title: "Other", url: "https://other.example/" })]) });
  await click(env, "nori|screenshot|chat");
  await waitFor(() => lastShare(env) && lastShare(env).ok === false, 3000, "refused");
  assert.match(lastShare(env).error, /changed/);
  assert.strictEqual(env.shares().length, 0);
  env = makeEnv({ storage: Object.assign({}, REGISTERED), tabs, onCapture: (n, h) => h.setTabs([Object.assign({}, TAB, { url: "https://mybank.com/account" })]) });
  await env.chrome.storage.local.set({ serverSettings: { hidden_hosts: ["mybank.com"] } });
  await click(env, "nori|screenshot|chat");
  await waitFor(() => lastShare(env) && lastShare(env).ok === false, 3000, "refused 2");
  assert.strictEqual(env.shares().length, 0);
});

test("screenshot: refused when the clicked tab isn't the one in front", async () => {
  const env = makeEnv({ storage: Object.assign({}, REGISTERED), tabs: [Object.assign({}, TAB, { active: false }), tab(6, { active: true })] });
  await click(env, "nori|screenshot|chat");
  await waitFor(() => lastShare(env) && lastShare(env).ok === false, 3000, "refused");
  assert.strictEqual(env.captures.length, 0, "no capture was even taken");
});

test("refused (and shown as a failure, badge x) from hidden hosts incl. wildcard, hidden link targets, private windows, paused, unpaired and non-web pages", async () => {
  const withHidden = (hidden) => Object.assign({}, REGISTERED, { serverSettings: { hidden_hosts: hidden, notifications: true }, settingsTs: Date.now() });
  const cases = [
    [{ storage: withHidden(["blog.example.com"]) }, "nori|page|chat", {}, TAB],
    [{ storage: withHidden(["*.example.com"]) }, "nori|page|chat", {}, TAB],                              // wildcard
    [{ storage: withHidden(["*.example.com"]) }, "nori|selection|chat", { selectionText: "x" }, TAB],
    [{ storage: withHidden(["bank.com"]) }, "nori|link|chat", { linkUrl: "https://bank.com/x" }, TAB],     // the link target is hidden
    [{ storage: withHidden(["*.bank.com"]) }, "nori|link|chat", { linkUrl: "https://www.bank.com/x" }, TAB],
    [{ storage: Object.assign({}, REGISTERED) }, "nori|page|chat", {}, Object.assign({}, TAB, { incognito: true })],
    [{ storage: Object.assign({}, REGISTERED, { paused: true }) }, "nori|page|chat", {}, TAB],
    [{ storage: {} }, "nori|page|chat", {}, TAB],
    [{ storage: Object.assign({}, REGISTERED) }, "nori|page|chat", {}, Object.assign({}, TAB, { url: "chrome://settings" })],
    [{ storage: Object.assign({}, REGISTERED) }, "nori|link|chat", { linkUrl: "javascript:alert(1)" }, TAB],
    [{ storage: Object.assign({}, REGISTERED) }, "nori|page|chat", {}, Object.assign({}, TAB, { url: "https://user:pw@blog.example.com/" })],
    [{ storage: Object.assign({}, REGISTERED) }, "nori|selection|chat", { selectionText: "  \n " }, TAB],                   // nothing selected
  ];
  for (const [envOpts, id, info, t] of cases) {
    const env = makeEnv(envOpts);
    await click(env, id, info, t);
    await waitFor(() => lastShare(env) && lastShare(env).ok === false, 3000, "failure for " + JSON.stringify([id, info]));
    assert.strictEqual(env.shares().length, 0, JSON.stringify([id, info]));
    assert.deepStrictEqual(env.scriptCalls, [], "nothing may be read when refusing");
    await waitFor(() => env.badges.includes("x"), 3000, "x badge");
  }
});

test("private windows are shared once the option is on", async () => {
  const env = makeEnv({ storage: Object.assign({}, REGISTERED, { includePrivate: true }) });
  await click(env, "nori|page|chat", {}, Object.assign({}, TAB, { incognito: true }));
  await waitFor(() => env.shares().length === 1, 3000, "share");
});

test("server answers are shown honestly: 429, 403, 413, 500, a network failure; 401 marks the browser removed", async () => {
  const cases = [
    [{ status: 429, json: { error: "give her a moment" } }, /give her a moment/],
    [{ status: 403, json: {} }, /switched off/],
    [{ status: 413, json: {} }, /too large/],
    [{ status: 500, json: {} }, /problem \(500\)/],
    [{ status: 200, json: { ok: false, error: "no" } }, /no/],
    [{ throws: "connection refused" }, /couldn't reach/],
    [{ status: 401, json: {} }, /removed|pair/],
  ];
  for (const [route, rx] of cases) {
    const env = makeEnv({ storage: Object.assign({}, REGISTERED), routes: { "/bridge/share": () => route } });
    await click(env, "nori|page|chat");
    await waitFor(() => lastShare(env) && lastShare(env).ok === false, 3000, "failure " + JSON.stringify(route));
    assert.match(lastShare(env).error, rx);
    await waitFor(() => env.badges.includes("x"), 3000, "x badge");
    if (route.status === 401) assert.strictEqual(env.storage.authFailedToken, REGISTERED.token);
  }
});

test("the 'ok'/'x' badge is cleared after 5 s even if the worker was restarted in between (alarm housekeeping)", async () => {
  const env = makeEnv({ storage: Object.assign({}, REGISTERED), fakeTime: true });
  await click(env, "nori|page|chat");
  await waitFor(() => env.badges.includes("ok"), 3000, "ok badge");
  assert.ok(env.storage.badgeUntil >= env.time + 4000);
  env.advance(6000);
  await env.fire("alarms.onAlarm", { name: "nori-keepalive" });
  await waitFor(() => env.badges[env.badges.length - 1] === "", 3000, "cleared");
  assert.strictEqual(env.storage.badgeUntil, 0);
});

test("the popup button: 'Send this page' goes through the same gates (and only from this extension's own pages)", async () => {
  const env = makeEnv({ storage: Object.assign({}, REGISTERED), tabs: [TAB] });
  const r = await env.message({ type: "share", kind: "page", dest: "task", withText: false, note: "for later" });
  assert.deepStrictEqual(plain(r), { ok: true });
  assert.deepStrictEqual(env.shares()[0].body, { kind: "page", dest: "task", url: "https://blog.example.com/post?id=7&utm=x", title: "A good read", note: "for later" });
  const before = env.shares().length;
  assert.strictEqual(await env.message({ type: "share", kind: "page" }, { id: "some-other-extension" }), undefined);          // a foreign sender
  assert.strictEqual(await env.message({ type: "share", kind: "page" }, { id: EXT_ID, url: "https://evil.example/page.html" }), undefined);   // a web page pretending
  assert.strictEqual(await env.message({ type: "share", kind: "selection", selection: "x" }, { id: EXT_ID, url: "chrome-extension://" + EXT_ID + "/popup.html" }), undefined);   // popup may send a page or a screenshot only
  await sleep(50);
  assert.strictEqual(env.shares().length, before);
  const hidden = makeEnv({ storage: Object.assign({}, REGISTERED, { serverSettings: { hidden_hosts: ["*.example.com"] } }), tabs: [TAB] });
  const r2 = await hidden.message({ type: "share", kind: "page", dest: "chat" });
  assert.strictEqual(r2.ok, false);
  assert.strictEqual(hidden.shares().length, 0);
});

run();
