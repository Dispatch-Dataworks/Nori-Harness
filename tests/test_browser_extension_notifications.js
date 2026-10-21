// Notifications: pure bookkeeping in lib.js, and the real service worker against a fake chrome + network.
// Run: node tests/test_browser_extension_notifications.js
"use strict";
const assert = require("assert");
const { L, REGISTERED, sleep, waitFor, tab, makeEnv, suite, plain } = require("./browser_extension_harness");
const { test, run } = suite("notifications");

const msg = (id, extra) => Object.assign({ id, category: "chat", title: "Nori", text: "hello " + id, ts: 1, approval: null }, extra || {});
const APPROVAL = (id, a) => msg(id, { category: "approval", text: "", approval: Object.assign({ id: 12, summary: 'click button "Send"', host: "x.com" }, a || {}) });

// ── lib.js ──
test("normalizeMessages: bad ids dropped, duplicates collapsed, oldest first, at most 20, unknown category tolerated", () => {
  const raw = [msg(5), msg(3), msg(3), { id: "7", text: "string id" }, { id: -1 }, { id: 1.5 }, null, "x", msg(4, { category: "weird" }), { text: "no id" }];
  const out = L.normalizeMessages(raw);
  assert.deepStrictEqual(out.map((m) => m.id), [3, 4, 5]);
  assert.strictEqual(out.find((m) => m.id === 4).category, "ping");
  const many = Array.from({ length: 50 }, (_, i) => msg(100 - i));
  const cut = L.normalizeMessages(many);
  assert.strictEqual(cut.length, 20);
  assert.strictEqual(cut[0].id, 51);                                           // oldest first
  assert.deepStrictEqual(L.normalizeMessages("nope"), []);
  assert.deepStrictEqual(L.normalizeMessages(undefined), []);
});

test("normalizeMessages: text is cleaned and capped, an approval block only counts for category approval", () => {
  const out = L.normalizeMessages([msg(1, { text: "line\u0000‮ one\n\n\n\nline two " + "x".repeat(2000), title: "N\u0007ori".repeat(40) }),
    msg(2, { category: "chat", approval: { id: 9, summary: "x", host: "y" } }), APPROVAL(3, { id: "bad" }), APPROVAL(4)]);
  assert.ok(out[0].text.length <= L.LIM.NOTIF_TEXT);
  assert.ok(!/[\u0000‮]/.test(out[0].text));
  assert.ok(!/\n\n\n/.test(out[0].text));
  assert.ok(out[0].title.length <= L.LIM.NOTIF_TITLE);
  assert.strictEqual(out[1].approval, null);
  assert.strictEqual(out[2].approval, null);                                   // a malformed approval id is not offered
  assert.strictEqual(out[3].approval.id, 12);
});

test("the ledger remembers ids, is bounded, and the ack only ever goes up", () => {
  let ids = [];
  for (let i = 1; i <= 400; i++) ids = L.ledgerAdd(ids, i);
  assert.strictEqual(ids.length, L.SHOWN_MAX);
  assert.ok(L.ledgerHas(ids, 400));
  assert.ok(!L.ledgerHas(ids, 1));
  assert.strictEqual(L.ledgerAdd([1, 2], 2).length, 2);                        // adding an id twice doesn't duplicate it
  assert.strictEqual(L.nextAck(10, 5), 10);
  assert.strictEqual(L.nextAck(10, 12), 12);
  assert.strictEqual(L.nextAck(undefined, 3), 3);
});

test("category -> notification options", () => {
  const spec = (m, o) => L.notificationSpec(L.normalizeMessages([m])[0], o);
  const chat = spec(msg(1));
  assert.strictEqual(chat.id, "nori-1");
  assert.strictEqual(chat.options.type, "basic");
  assert.strictEqual(chat.options.buttons, undefined);
  assert.strictEqual(chat.options.requireInteraction, false);
  assert.strictEqual(chat.options.title, "Nori");
  assert.strictEqual(chat.options.message, "hello 1");
  assert.ok(spec(msg(2, { category: "reminder" })).options.priority > chat.options.priority);
  for (const c of ["ping", "reminder", "job", "peer"]) assert.strictEqual(spec(msg(3, { category: c })).options.buttons, undefined, c);
  const ap = spec(APPROVAL(4));
  assert.deepStrictEqual(ap.options.buttons.map((b) => b.title), ["Approve", "Deny"]);
  assert.strictEqual(ap.options.requireInteraction, true);
  assert.ok(ap.options.priority >= 2);
  assert.ok(/click button/.test(ap.options.message) && /x\.com/.test(ap.options.message));
  assert.strictEqual(ap.approval.id, 12);
  assert.strictEqual(L.decisionForButton(0), "approve");
  assert.strictEqual(L.decisionForButton(1), "deny");
  assert.strictEqual(L.decisionForButton(2), null);
});

test("an approval on a hidden site gets no buttons and none of the details", () => {
  const s = L.notificationSpec(L.normalizeMessages([APPROVAL(9, { host: "www.mybank.com", summary: 'click "Pay 500"' })])[0], { hidden: ["*.mybank.com"] });
  assert.strictEqual(s.options.buttons, undefined);
  assert.strictEqual(s.approval, null);
  assert.ok(!/mybank|Pay/.test(s.options.message));
});

test("an approval with no usable details still notifies, without buttons", () => {
  const s = L.notificationSpec(L.normalizeMessages([APPROVAL(9, { id: null })])[0]);
  assert.strictEqual(s.options.buttons, undefined);
  assert.ok(s.options.message.length > 0);
});

// ── the real worker ──
const poll1 = (env) => env.polls()[0].body;

test("the poll carries the version, ack, paused and tab_visible=false when no Nori tab is in front", async () => {
  const env = makeEnv({ storage: Object.assign({}, REGISTERED) });
  await waitFor(() => env.polls().length >= 1, 3000, "first poll");
  const p = env.polls()[0];
  assert.strictEqual(p.url, "https://nori.example/bridge/poll");
  assert.strictEqual(p.auth, "Bearer " + REGISTERED.token);
  assert.strictEqual(p.body.ack, 0);
  assert.strictEqual(p.body.paused, false);
  assert.strictEqual(p.body.tab_visible, false);
  assert.strictEqual(p.body.v, "0.2.1");
  assert.strictEqual(p.body.ext.v, "0.2.1");
});

test("tab_visible is true only when the focused window's active tab is on the Nori origin", async () => {
  const cases = [
    [{ tabs: [tab(1, { active: true, url: "https://nori.example/chat" })] }, true],
    [{ tabs: [tab(1, { active: true, url: "https://nori.example/chat" })], win: { id: 1, focused: false } }, false],     // another application in front
    [{ tabs: [tab(1, { active: true, url: "https://other.example/" }), tab(2, { url: "https://nori.example/chat" })] }, false],   // Nori tab is in the background
    [{ tabs: [tab(1, { active: true, url: "https://nori.example.evil.com/" })] }, false],                                      // look-alike origin
    [{ tabs: [tab(1, { active: true, url: "http://nori.example/chat" })] }, false],                                           // different scheme = different origin
  ];
  for (const [o, expected] of cases) {
    const env = makeEnv(Object.assign({ storage: Object.assign({}, REGISTERED) }, o));
    await waitFor(() => env.polls().length >= 1, 3000, "poll");
    assert.strictEqual(poll1(env).tab_visible, expected, JSON.stringify(o));
  }
});

test("poll -> notification -> ack: one notification per message, id nori-<id>, ack only after showing", async () => {
  const env = makeEnv({ storage: Object.assign({}, REGISTERED) });
  env.poll({ messages: [msg(902, { text: "Take the bins out", category: "reminder" }), msg(903, { text: "All done", category: "job" })], settings: { notifications: true } });
  await waitFor(() => env.polls().length >= 2, 3000, "second poll");
  assert.deepStrictEqual(env.shown().map((n) => n.id), ["nori-902", "nori-903"]);
  assert.strictEqual(env.shown()[0].options.message, "Take the bins out");
  assert.strictEqual(env.polls()[0].body.ack, 0);                              // not acked before it was shown
  assert.strictEqual(env.polls()[1].body.ack, 903);                            // acked after
  assert.deepStrictEqual(plain(env.storage.shown), [902, 903]);
  assert.strictEqual(env.storage.lastAck, 903);
});

test("a message id replayed by the server is not shown twice, and is still acked", async () => {
  const env = makeEnv({ storage: Object.assign({}, REGISTERED) });
  env.poll({ messages: [msg(10), msg(11)] });
  env.poll({ messages: [msg(10), msg(11), msg(12)] });                          // the ack was lost: 10 and 11 come again
  await waitFor(() => env.polls().length >= 3, 3000, "poll 3");
  assert.deepStrictEqual(env.shown().map((n) => n.id), ["nori-10", "nori-11", "nori-12"]);
  assert.strictEqual(env.polls()[2].body.ack, 12);
});

test("a restarted worker doesn't show what it already showed (the ledger and ack are in chrome.storage)", async () => {
  const env1 = makeEnv({ storage: Object.assign({}, REGISTERED) });
  env1.poll({ messages: [msg(20), msg(21)] });
  await waitFor(() => env1.polls().length >= 2, 3000, "poll 2");
  env1.stop();                                                                  // the worker is killed
  const env2 = makeEnv({ storage: env1.storage });                              // a fresh worker, same storage
  env2.poll({ messages: [msg(20), msg(21), msg(22)] });
  await waitFor(() => env2.polls().length >= 2, 3000, "restarted worker polled twice");
  assert.strictEqual(env2.polls()[0].body.ack, 21);                             // remembered across the restart
  assert.deepStrictEqual(env2.shown().map((n) => n.id), ["nori-22"]);
  assert.strictEqual(env2.polls()[1].body.ack, 22);
});

test("if a notification can't be shown it is not acked, the rest wait, and the loop backs off", async () => {
  const env = makeEnv({ storage: Object.assign({}, REGISTERED), failCreate: (id) => id === "nori-31" });
  env.poll({ messages: [msg(30), msg(31), msg(32)] });
  await waitFor(() => env.storage.status && /can't show notifications/.test(env.storage.status.state) && env.storage.backoffUntil > 0, 3000, "status and backoff");
  assert.deepStrictEqual(env.shown().map((n) => n.id), ["nori-30"]);           // 32 was not shown past the failure
  assert.strictEqual(env.storage.lastAck, 30);                                   // acked only up to what was shown
  assert.ok(env.storage.backoffUntil > Date.now());                              // and it is persisted, so a restarted worker keeps the pause
});

test("one message that can never be shown is skipped after 3 tries instead of wedging the queue", async () => {
  const env = makeEnv({ storage: Object.assign({}, REGISTERED, {}), failCreate: (id) => id === "nori-41" });
  for (let i = 0; i < 3; i++) env.poll({ messages: [msg(40), msg(41), msg(42)] });
  for (let i = 1; i <= 3; i++) {
    await waitFor(() => env.notifs.filter((n) => n.op === "create-attempt" && n.id === "nori-41").length >= i, 3000, "attempt " + i);
    await sleep(40);
    await env.chrome.storage.local.set({ backoffUntil: 0 });                      // skip the 10 s wait
  }
  await waitFor(() => env.storage.lastAck === 42, 3000, "42 acked");
  assert.ok(env.shown().some((n) => n.id === "nori-42"));
  assert.ok(!env.shown().some((n) => n.id === "nori-41"));
});

test("notifications switched off on the server, or by the local option, show nothing but are not redelivered", async () => {
  for (const [serverOn, localOn] of [[false, true], [true, false]]) {
    const env = makeEnv({ storage: Object.assign({}, REGISTERED, { notify: localOn }) });
    env.poll({ messages: [msg(50), msg(51)], settings: { notifications: serverOn } });
    await waitFor(() => env.polls().length >= 2, 3000, "poll 2");
    assert.strictEqual(env.shown().length, 0);
    assert.strictEqual(env.polls()[1].body.ack, 51);
    assert.deepStrictEqual(plain(env.storage.shown || []), []);
  }
});

test("if the browser/OS blocks notifications they are dropped (and acked, so no hot loop) and the popup can say so", async () => {
  const env = makeEnv({ storage: Object.assign({}, REGISTERED), notifPermission: "denied" });
  env.poll({ messages: [msg(60)] });
  await waitFor(() => env.polls().length >= 2, 3000, "poll 2");
  assert.strictEqual(env.shown().length, 0);
  assert.strictEqual(env.polls()[1].body.ack, 60);
  assert.strictEqual(env.storage.notifBlocked, true);
  assert.strictEqual(L.sharingState(env.storage, Date.now()).notifications, false);
});

test("paused: nothing is shown and nothing is acked", async () => {
  const env = makeEnv({ storage: Object.assign({}, REGISTERED, { paused: true }) });
  env.poll({ messages: [msg(70)], settings: { notifications: true } });
  await waitFor(() => env.polls().length >= 1, 3000, "poll");
  await sleep(150);
  assert.strictEqual(env.polls()[0].body.paused, true);
  assert.strictEqual(env.shown().length, 0);
  assert.strictEqual(env.storage.lastAck || 0, 0);
});

test("hostile messages (script-ish text, huge text, control characters) become inert, capped notifications", async () => {
  const env = makeEnv({ storage: Object.assign({}, REGISTERED) });
  env.poll({ messages: [msg(80, { text: "<img src=x onerror=alert(1)>\u0000\u001b[31m" + "A".repeat(100000), title: "x".repeat(5000) })] });
  await waitFor(() => env.shown().length === 1, 3000, "shown");
  const o = env.shown()[0].options;
  assert.ok(o.message.length <= L.LIM.NOTIF_TEXT);
  assert.ok(o.title.length <= L.LIM.NOTIF_TITLE);
  assert.ok(!/[\u0000-\u0008\u001b]/.test(o.message));
});

// ── approval buttons ──
async function withApproval(envOpts, mutate) {
  const env = makeEnv(Object.assign({ storage: Object.assign({}, REGISTERED) }, envOpts || {}));
  env.poll({ messages: [APPROVAL(902)], settings: { notifications: true, hidden_hosts: [] } });
  await waitFor(() => env.shown().length === 1 && env.storage.approvals && env.storage.approvals["nori-902"], 3000, "approval shown and remembered");
  if (mutate) await mutate(env);
  return env;
}

test("Approve posts {id, decision:'approve'} with the bearer token, then updates the notification", async () => {
  const env = await withApproval();
  assert.deepStrictEqual(env.shown()[0].options.buttons.map((b) => b.title), ["Approve", "Deny"]);
  await env.fire("notifications.onButtonClicked", "nori-902", 0);
  await waitFor(() => env.approvals().length === 1, 3000, "approval POST");
  const a = env.approvals()[0];
  assert.strictEqual(a.url, "https://nori.example/bridge/approval");
  assert.deepStrictEqual(a.body, { id: 12, decision: "approve" });
  assert.strictEqual(a.auth, "Bearer " + REGISTERED.token);
  await waitFor(() => env.notifs.some((n) => n.op === "update" && /Approved/.test(n.options.message)), 3000, "updated");
  assert.deepStrictEqual(env.notifs.find((n) => n.op === "update").options.buttons, []);
  assert.strictEqual(env.storage.lastApproval.ok, true);
  assert.deepStrictEqual(plain(env.storage.approvals), {});                       // answered: can't be answered twice
  await env.fire("notifications.onButtonClicked", "nori-902", 0);
  await sleep(60);
  assert.strictEqual(env.approvals().length, 1);
});

test("Deny posts decision:'deny'", async () => {
  const env = await withApproval();
  await env.fire("notifications.onButtonClicked", "nori-902", 1);
  await waitFor(() => env.approvals().length === 1, 3000, "approval POST");
  assert.deepStrictEqual(env.approvals()[0].body, { id: 12, decision: "deny" });
  await waitFor(() => env.notifs.some((n) => n.op === "update" && /Denied/.test(n.options.message)), 3000, "updated");
});

test("only an approval this worker itself offered can be answered; other notification ids and button numbers do nothing", async () => {
  const env = await withApproval();
  await env.fire("notifications.onButtonClicked", "nori-999", 0);
  await env.fire("notifications.onButtonClicked", "someone-elses", 0);
  await env.fire("notifications.onButtonClicked", "nori-902", 5);
  await sleep(80);
  assert.strictEqual(env.approvals().length, 0);
});

test("the approval button is refused while paused", async () => {
  const env = await withApproval({}, async (e) => { await e.chrome.storage.local.set({ paused: true }); });
  await env.fire("notifications.onButtonClicked", "nori-902", 0);
  await waitFor(() => env.notifs.some((n) => n.op === "update" && /paused/i.test(n.options.message)), 3000, "paused note");
  assert.strictEqual(env.approvals().length, 0);
});

test("approval answers: already resolved (409) clears the buttons; a server error keeps them so he can retry; 401 marks the browser removed", async () => {
  let env = await withApproval({ routes: { "/bridge/approval": () => ({ status: 409, json: { error: "already answered" } }) } });
  await env.fire("notifications.onButtonClicked", "nori-902", 0);
  await waitFor(() => env.notifs.some((n) => n.op === "update" && /already answered|expired/.test(n.options.message)), 3000, "409 note");
  env = await withApproval({ routes: { "/bridge/approval": () => ({ status: 500, json: {} }) } });
  await env.fire("notifications.onButtonClicked", "nori-902", 0);
  await waitFor(() => env.notifs.some((n) => n.op === "update" && /Not sent/.test(n.options.message)), 3000, "500 note");
  assert.ok(env.storage.approvals["nori-902"], "kept for a retry");
  env = await withApproval({ routes: { "/bridge/approval": () => ({ status: 401, json: {} }) } });
  await env.fire("notifications.onButtonClicked", "nori-902", 0);
  await waitFor(() => env.storage.status && env.storage.status.state === "removed, re-pair", 3000, "removed");
});

test("an approval on a hidden site is shown without buttons and can't be answered from the notification", async () => {
  const env = makeEnv({ storage: Object.assign({}, REGISTERED, { serverSettings: { hidden_hosts: ["*.mybank.com"], notifications: true }, settingsTs: Date.now() }) });
  env.poll({ messages: [APPROVAL(5, { host: "www.mybank.com", summary: 'click "Pay"' })], settings: { hidden_hosts: ["*.mybank.com"] } });
  await waitFor(() => env.shown().length === 1, 3000, "shown");
  assert.strictEqual(env.shown()[0].options.buttons, undefined);
  assert.ok(!/mybank|Pay/.test(env.shown()[0].options.message));
  await env.fire("notifications.onButtonClicked", "nori-5", 0);
  await sleep(60);
  assert.strictEqual(env.approvals().length, 0);
});

// ── clicking ──
test("clicking a notification focuses an existing Nori tab, else opens the chat", async () => {
  let env = makeEnv({ storage: Object.assign({}, REGISTERED), tabs: [tab(1, { active: true, url: "https://other.example/" }), tab(7, { windowId: 2, url: "https://nori.example/chat" })] });
  await waitFor(() => env.polls().length >= 1, 3000, "poll");
  await env.fire("notifications.onClicked", "nori-1");
  await waitFor(() => env.actions.some((a) => a.op === "windowUpdate"), 3000, "focused");
  assert.ok(env.actions.some((a) => a.op === "update" && a.id === 7 && a.active === true));
  assert.ok(env.actions.some((a) => a.op === "windowUpdate" && a.id === 2 && a.focused === true));
  assert.ok(!env.actions.some((a) => a.op === "create"));
  assert.ok(env.notifs.some((n) => n.op === "clear" && n.id === "nori-1"));
  env = makeEnv({ storage: Object.assign({}, REGISTERED), tabs: [tab(1, { active: true, url: "https://other.example/" })] });
  await waitFor(() => env.polls().length >= 1, 3000, "poll");
  await env.fire("notifications.onClicked", "nori-1");
  await waitFor(() => env.actions.some((a) => a.op === "create"), 3000, "created");
  assert.deepStrictEqual(env.actions.find((a) => a.op === "create"), { op: "create", url: "https://nori.example", active: true });
  await env.fire("notifications.onClicked", "someone-elses");                  // not ours: ignored
});

run();
