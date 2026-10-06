// Nori browser bridge -- pure logic (no chrome.* calls), shared by the service
// worker and by Node tests (tests/test_browser_extension.js).
//
// This is the AUTHORITATIVE side of two rules: which sites may be touched
// (the allowlist the server sends with every command, checked against the live
// tab's real URL), and which actions must pause for the operator's approval.
// The server enforces the allowlist too for navigation; the extension is the
// one that sees what the tab really is.
(function (root) {
  "use strict";

  function hostMatches(host, pattern) {
    host = String(host || "").toLowerCase().replace(/\.$/, "");
    pattern = String(pattern || "").toLowerCase().trim().replace(/^\./, "");
    if (!host || !pattern) return false;
    if (pattern.startsWith("*.")) {
      const base = pattern.slice(2);
      return host === base || host.endsWith("." + base);
    }
    return host === pattern;
  }

  function hostAllowed(host, patterns) {
    if (!host) return false;
    return (patterns || []).some((p) => hostMatches(host, p));
  }

  function hostOf(url) {
    try {
      const u = new URL(url);
      if (u.protocol !== "http:" && u.protocol !== "https:") return null;
      return u.hostname.toLowerCase().replace(/\.$/, "") || null;
    } catch (e) {
      return null;
    }
  }

  // Fields that are never typed into and never read, approved or not.
  const SENSITIVE_RE = /(pass(word|wd|code)?|passcode|pin\b|cvv|cvc|card.?(number|num|no)|cc-|credit|ssn|social.?sec|routing|account.?(number|num)|iban|otp|one.?time|2fa|mfa|security.?code|secret)/i;

  // Buttons/links whose click does something that's hard to take back or
  // that speaks or spends on the operator's behalf.
  const RISKY_RE = /\b(send|submit|pay|buy|purchase|order|checkout|check out|confirm|delete|remove|erase|post|publish|transfer|withdraw|sign ?out|log ?out|unsubscribe|authori[sz]e|allow|accept|agree|donate|subscribe|reserve|book|apply|place|reply|forward|share|invite|grant|connect|install|download|upload|save)\b/i;

  function isSensitiveField(el) {
    if (!el) return false;
    if ((el.type || "").toLowerCase() === "password") return true;
    const ac = (el.autocomplete || "").toLowerCase();
    if (/(password|cc-|one-time-code)/.test(ac)) return true;
    return SENSITIVE_RE.test([el.name, el.id_attr, el.label, el.placeholder].filter(Boolean).join(" "));
  }

  // action: "click" | "type". el: the descriptor the page script produced.
  // ctx: {allowed: [patterns], currentHost, forceApproval, submit}
  // Returns {level: "ok" | "approval" | "blocked", reason}.
  function classify(action, el, ctx) {
    ctx = ctx || {};
    if (!el) return { level: "blocked", reason: "no such element" };
    if (action === "type" && isSensitiveField(el)) {
      return { level: "blocked", reason: "password, payment and one-time-code fields are never typed into" };
    }
    if (action === "click" && el.href_host) {
      const sameHost = el.href_host === ctx.currentHost;
      if (!sameHost && !hostAllowed(el.href_host, ctx.allowed)) {
        return { level: "blocked", reason: "that link goes to " + el.href_host + ", which isn't an allowed site" };
      }
    }
    if (ctx.forceApproval) {
      return { level: "approval", reason: "a page in this session looked like it was trying to instruct her" };
    }
    if (action === "type") {
      if (ctx.submit) return { level: "approval", reason: "typing and pressing Enter submits" };
      return { level: "ok", reason: "" };
    }
    if (action === "click") {
      if (el.is_submit) return { level: "approval", reason: "this submits a form" };
      if (RISKY_RE.test(el.label || "")) return { level: "approval", reason: "the label says " + JSON.stringify(String(el.label).slice(0, 40)) };
      return { level: "ok", reason: "" };
    }
    return { level: "ok", reason: "" };
  }

  // A stable-enough identity for "the same element the operator approved".
  function fingerprint(el) {
    const s = [el.tag, el.type, el.label, el.href_host, el.name, el.role].map((x) => String(x || "")).join("|");
    let h = 5381;
    for (let i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) | 0;
    return "f" + (h >>> 0).toString(16);
  }

  function describe(el) {
    const what = (el.role || el.tag || "element") + (el.label ? " " + JSON.stringify(String(el.label).slice(0, 50)) : "");
    return what;
  }

  const api = { hostMatches, hostAllowed, hostOf, isSensitiveField, classify, fingerprint, describe, SENSITIVE_RE, RISKY_RE };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.NoriLib = api;
})(typeof self !== "undefined" ? self : this);
