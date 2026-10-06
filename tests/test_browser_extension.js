// Tests for browser_extension/lib.js -- the extension's authoritative allowlist
// and approval rules. Run: node tests/test_browser_extension.js
"use strict";
const assert = require("assert");
const path = require("path");
const L = require(path.join(__dirname, "..", "browser_extension", "lib.js"));
const vectors = require("./bridge_vectors.json");

let n = 0;
function t(name, fn) { fn(); n++; }

t("host matching agrees with the shared vectors", () => {
  for (const [host, pattern, expected] of vectors.host_matches) {
    assert.strictEqual(L.hostMatches(host, pattern), expected, JSON.stringify([host, pattern]));
  }
});

t("hostAllowed / hostOf", () => {
  assert.strictEqual(L.hostAllowed("a.example.com", ["x.com", "*.example.com"]), true);
  assert.strictEqual(L.hostAllowed("other.com", ["x.com", "*.example.com"]), false);
  assert.strictEqual(L.hostAllowed(null, ["*.example.com"]), false);
  assert.strictEqual(L.hostAllowed("example.com", []), false);
  assert.strictEqual(L.hostOf("https://User:pw@Mail.Example.com:8443/x"), "mail.example.com");
  assert.strictEqual(L.hostOf("chrome://settings"), null);
  assert.strictEqual(L.hostOf("javascript:alert(1)"), null);
  assert.strictEqual(L.hostOf("file:///etc/passwd"), null);
  assert.strictEqual(L.hostOf("about:blank"), null);
  assert.strictEqual(L.hostOf("garbage"), null);
});

const ctx = (o) => Object.assign({ allowed: ["*.example.com"], currentHost: "www.example.com", forceApproval: false, submit: false }, o || {});

t("sensitive fields are blocked for typing, however they're named", () => {
  const cases = [
    { tag: "input", type: "password", label: "Secret thing" },
    { tag: "input", type: "text", autocomplete: "current-password", label: "x" },
    { tag: "input", type: "text", autocomplete: "cc-number", label: "x" },
    { tag: "input", type: "text", autocomplete: "one-time-code", label: "x" },
    { tag: "input", type: "text", name: "cardnumber", label: "x" },
    { tag: "input", type: "text", label: "Card number" },
    { tag: "input", type: "text", label: "CVV" },
    { tag: "input", type: "text", id_attr: "ssn" },
    { tag: "input", type: "text", placeholder: "Enter your PIN" },
    { tag: "input", type: "text", label: "Routing number" },
    { tag: "input", type: "text", name: "otp" },
  ];
  for (const el of cases) {
    assert.strictEqual(L.classify("type", el, ctx()).level, "blocked", JSON.stringify(el));
    assert.strictEqual(L.classify("type", el, ctx({ forceApproval: true })).level, "blocked", "force can't unblock " + JSON.stringify(el));
  }
});

t("ordinary fields can be typed into without approval", () => {
  for (const el of [{ tag: "input", type: "text", label: "Search" }, { tag: "textarea", label: "Message body" }, { tag: "input", type: "email", label: "Email" }]) {
    assert.strictEqual(L.classify("type", el, ctx()).level, "ok", JSON.stringify(el));
  }
});

t("typing then pressing Enter needs approval", () => {
  assert.strictEqual(L.classify("type", { tag: "input", type: "text", label: "Search" }, ctx({ submit: true })).level, "approval");
});

t("risky buttons need approval; harmless ones don't", () => {
  for (const label of ["Send", "Submit", "Pay now", "Buy it now", "Place order", "Delete", "Remove item", "Confirm", "Sign out",
                       "Log out", "Publish", "Post", "Transfer", "Unsubscribe", "Accept all", "Allow", "Reply", "Share", "Save", "Download"]) {
    assert.strictEqual(L.classify("click", { tag: "button", label }, ctx()).level, "approval", label);
  }
  for (const label of ["Next", "Menu", "Open", "Home", "Show more", "Search", "Cancel", "Expand"]) {
    assert.strictEqual(L.classify("click", { tag: "button", label }, ctx()).level, "ok", label);
  }
});

t("any submit button needs approval whatever its label", () => {
  assert.strictEqual(L.classify("click", { tag: "button", label: "Go", is_submit: true }, ctx()).level, "approval");
});

t("a link to a site that isn't allowed is blocked; same-host and allowed-host links are fine", () => {
  assert.strictEqual(L.classify("click", { tag: "a", label: "Offsite", href_host: "evil.com" }, ctx()).level, "blocked");
  assert.strictEqual(L.classify("click", { tag: "a", label: "Same", href_host: "www.example.com" }, ctx()).level, "ok");
  assert.strictEqual(L.classify("click", { tag: "a", label: "Sub", href_host: "mail.example.com" }, ctx()).level, "ok");
  assert.strictEqual(L.classify("click", { tag: "a", label: "Same host but not in list", href_host: "same.test" }, ctx({ currentHost: "same.test", allowed: [] })).level, "ok");
});

t("forceApproval (a flagged page this session) makes every click and type need approval", () => {
  assert.strictEqual(L.classify("click", { tag: "a", label: "Next", href_host: "www.example.com" }, ctx({ forceApproval: true })).level, "approval");
  assert.strictEqual(L.classify("type", { tag: "input", type: "text", label: "Search" }, ctx({ forceApproval: true })).level, "approval");
});

t("a blocked link outranks forceApproval (still blocked, not merely paused)", () => {
  assert.strictEqual(L.classify("click", { tag: "a", label: "x", href_host: "evil.com" }, ctx({ forceApproval: true })).level, "blocked");
});

t("a missing element is blocked", () => {
  assert.strictEqual(L.classify("click", null, ctx()).level, "blocked");
});

t("fingerprints identify the same element and differ for different ones", () => {
  const a = { tag: "button", type: "submit", label: "Send", role: "" };
  assert.strictEqual(L.fingerprint(a), L.fingerprint({ ...a }));
  assert.notStrictEqual(L.fingerprint(a), L.fingerprint({ ...a, label: "Send all" }));
  assert.notStrictEqual(L.fingerprint(a), L.fingerprint({ ...a, href_host: "evil.com" }));
});

t("describe is short and labelled", () => {
  assert.strictEqual(L.describe({ tag: "button", label: "Send" }), 'button "Send"');
  assert.ok(L.describe({ tag: "a", label: "x".repeat(200) }).length < 80);
});

console.log(`ok - ${n} test groups passed`);
