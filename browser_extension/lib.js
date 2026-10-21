// Nori browser bridge -- pure logic (no chrome.* calls), shared by the service
// worker, the popup/options/side panel pages and the Node tests (tests/test_browser_extension*.js).
//
// This is the AUTHORITATIVE side of the rules that decide what may be touched
// and what may leave this browser:
//   * which sites may be touched by Nori's commands (the allowlist the server
//     sends with every command, checked against the live tab's real URL) and
//     which actions must pause for the operator's approval;
//   * which sites are NEVER shared, read or timed (settings.hidden_hosts, with
//     wildcards) and that private (incognito) windows are skipped unless the
//     options page says otherwise;
//   * what a share / an "ask about this page" / a browsing-time report may contain;
//   * how notifications are de-duplicated and acknowledged.
// The server enforces what it can; the extension is the one that sees what the
// tab really is, so it re-checks everything itself.
(function (root) {
  "use strict";

  const VERSION_FALLBACK = "0.2.0";

  // ───────────────────────── hosts ─────────────────────────
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

  // "hidden" uses the same matcher as the allowlist (wildcards included), but the sense is inverted:
  // a hidden host is never shared, read, timed or controlled.
  function hostHidden(host, patterns) {
    if (!host) return false;
    return (Array.isArray(patterns) ? patterns : []).some((p) => hostMatches(host, p));
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

  function sameOrigin(a, b) {
    try { return new URL(a).origin === new URL(b).origin && new URL(a).origin !== "null"; } catch (e) { return false; }
  }

  // A list of host patterns from the server, made safe to keep. null when the input isn't a list at all
  // (the caller then keeps what it had, which is the safe direction: never forget a hidden site).
  function sanitizeHostList(raw) {
    if (!Array.isArray(raw)) return null;
    const out = [];
    for (const p of raw) {
      if (typeof p !== "string") continue;
      const s = p.trim().toLowerCase();
      if (!s || s.length > 253 || !/^(\*\.)?[a-z0-9._-]+$/.test(s)) continue;
      out.push(s);
      if (out.length >= 500) break;
    }
    return out;
  }

  // ───────────────────────── bridge commands (unchanged rules) ─────────────────────────
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

  // Standing look: with look_only the server has no session running. The extension itself refuses everything
  // except these four, so a wrong or hostile server cannot make it click or type without a session.
  const LOOK_ACTIONS = ["tabs", "read", "screenshot", "open"];
  const LOOK_REFUSAL = "a session isn't running; I can only look";
  function lookOnlyVerdict(cmd, standingLookOn) {
    if (!cmd || !cmd.look_only) return { ok: true };
    if (LOOK_ACTIONS.indexOf(cmd.action) < 0) return { ok: false, error: LOOK_REFUSAL };
    if (standingLookOn !== true) return { ok: false, error: "standing look is switched off, and no session is running" };
    return { ok: true };
  }

  // ───────────────────────── text cleaning ─────────────────────────
  const LIM = {
    SELECTION: 2000, PAGE_TEXT_SHARE: 8000, PAGE_TEXT_ASK: 30000, QUESTION: 2000, TITLE: 200, URL: 2000,
    NOTE: 1000, JPEG_B64: 4000000, NOTIF_TEXT: 400, NOTIF_TITLE: 80, NAME: 60,
  };
  // Invisible characters used to hide text from a reader (zero-width space, direction overrides, the
  // "tag" block). The joiners U+200C/U+200D are kept: emoji and some scripts need them.
  const ZERO_WIDTH_RE = /[​‎‏‪-‮⁠-⁤⁦-⁩﻿]|[\u{e0000}-\u{e007f}]/gu;

  // One line: control characters and runs of whitespace become single spaces.
  function cleanText(s, max) {
    s = String(s == null ? "" : s);
    if (s.length > (max || 120) * 4) s = s.slice(0, (max || 120) * 4);
    return s.replace(ZERO_WIDTH_RE, "").replace(/[\u0000-\u001f\u007f-\u009f]+/g, " ").replace(/\s+/g, " ").trim().slice(0, max || 120);
  }

  // Several lines: newlines kept (at most one blank line in a row), other control characters dropped.
  function cleanMultiline(s, max) {
    s = String(s == null ? "" : s);
    if (s.length > max * 4) s = s.slice(0, max * 4);
    s = s.replace(/\r\n?/g, "\n").replace(ZERO_WIDTH_RE, "")
      .replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f-\u009f]/g, " ")
      .replace(/[ \t ]+/g, " ").replace(/ ?\n ?/g, "\n").replace(/\n{3,}/g, "\n\n").trim();
    return s.slice(0, max);
  }

  // Text taken off a page. The injected collector only ever returns text nodes of visible, non-form elements,
  // but this is the second line of defence on a plain string: markup that slipped in is removed, invisible
  // formatting characters (used to hide instructions) are dropped, whitespace is collapsed, and it is capped.
  function cleanPageText(raw, max) {
    max = max || LIM.PAGE_TEXT_ASK;
    let s = String(raw == null ? "" : raw);
    if (s.length > max * 4) s = s.slice(0, max * 4);
    s = s.replace(/<!--[\s\S]*?-->/g, " ")
      .replace(/<(script|style|noscript|template)\b[\s\S]*?<\/\1\s*>/gi, " ")
      .replace(/<(script|style|noscript|template)\b[\s\S]*$/gi, " ");      // an unterminated block swallows the rest
    return cleanMultiline(s, max);
  }

  // Injected into a page by chrome.scripting.executeScript({func, args:[maxChars]}). It must be fully
  // self-contained (it is serialised). It reads TEXT NODES ONLY, so the value of any input (password or not)
  // can never be part of the output; it also skips script, style, templates, form controls (input, textarea,
  // select), iframes, svg, and anything not visible. Returns {title, url, text}; the caller cleans and caps.
  function pageTextCollector(maxChars) {
    var SKIP = { SCRIPT: 1, STYLE: 1, NOSCRIPT: 1, TEMPLATE: 1, IFRAME: 1, FRAME: 1, OBJECT: 1, EMBED: 1, CANVAS: 1, SVG: 1, MATH: 1,
      INPUT: 1, TEXTAREA: 1, SELECT: 1, OPTION: 1, DATALIST: 1, HEAD: 1, TITLE: 1, META: 1, LINK: 1, AUDIO: 1, VIDEO: 1, PICTURE: 1 };
    var BLOCK = { DIV: 1, P: 1, LI: 1, UL: 1, OL: 1, TR: 1, TABLE: 1, H1: 1, H2: 1, H3: 1, H4: 1, H5: 1, H6: 1, BR: 1, SECTION: 1, ARTICLE: 1,
      HEADER: 1, FOOTER: 1, MAIN: 1, NAV: 1, ASIDE: 1, PRE: 1, BLOCKQUOTE: 1, FORM: 1, DL: 1, DT: 1, DD: 1, FIGURE: 1, FIGCAPTION: 1 };
    var cap = Math.max(1, Math.min(Number(maxChars) || 30000, 100000));
    var stopAt = cap * 3;
    var out = [], total = 0, visited = 0;
    var base = document.body || document.documentElement;
    if (!base) return { title: String(document.title || ""), url: String(location.href || ""), text: "" };
    var stack = [base];
    while (stack.length && total < stopAt && visited < 300000) {
      var node = stack.pop();
      visited++;
      if (typeof node === "string") { out.push(node); total += node.length; continue; }
      var type = node.nodeType;
      if (type === 3) {
        var v = node.nodeValue || "";
        if (v) { out.push(v); total += v.length; }
        continue;
      }
      if (type !== 1) continue;
      var tag = String(node.tagName || "").toUpperCase();
      if (SKIP[tag]) continue;
      try {
        var cs = getComputedStyle(node);
        if (cs && (cs.display === "none" || cs.visibility === "hidden" || cs.visibility === "collapse")) continue;
      } catch (e) { /* no style: treat as visible */ }
      var isBlock = !!BLOCK[tag];
      if (isBlock) stack.push("\n");
      var kids = node.childNodes || [];
      for (var i = kids.length - 1; i >= 0; i--) stack.push(kids[i]);
      if (node.shadowRoot && node.shadowRoot.childNodes) {
        var sk = node.shadowRoot.childNodes;
        for (var j = sk.length - 1; j >= 0; j--) stack.push(sk[j]);
      }
      if (isBlock) stack.push("\n");
    }
    return { title: String(document.title || ""), url: String(location.href || ""), text: out.join("") };
  }

  // ───────────────────────── gating: where may anything be read or sent from? ─────────────────────────
  // pol: {configured, paused, includePrivate, hidden:[patterns]}. Returns {ok, host} or {ok:false, error, code}.
  function gatePage(url, incognito, pol) {
    pol = pol || {};
    const no = (code, error) => ({ ok: false, code, error });
    if (!pol.configured) return no("unconfigured", "this browser isn't paired with Nori yet");
    if (pol.paused) return no("paused", "Nori is paused on this browser");
    if (incognito && !pol.includePrivate) return no("private", "private windows are not shared (you can change that in the options)");
    let u;
    try { u = new URL(String(url || "")); } catch (e) { return no("nonweb", "only ordinary web pages (http or https) can be sent"); }
    if (u.protocol !== "http:" && u.protocol !== "https:") return no("nonweb", "only ordinary web pages (http or https) can be sent");
    if (u.username || u.password) return no("credentials", "an address with a name or password in it is not sent");
    const host = u.hostname.toLowerCase().replace(/\.$/, "");
    if (!host) return no("nonweb", "only ordinary web pages (http or https) can be sent");
    if (hostHidden(host, pol.hidden)) return no("hidden", "that is a site you keep private, so it is not sent");
    return { ok: true, host };
  }

  // An address he is sending on purpose: kept whole (the query stays: it is often the point of the page),
  // minus the fragment. Web addresses only, no name or password in it, never a hidden site.
  function shareUrl(raw, hidden) {
    const no = (error) => ({ ok: false, error });
    let u;
    try { u = new URL(String(raw == null ? "" : raw).trim()); } catch (e) { return no("that has no ordinary web address to send"); }
    if (u.protocol !== "http:" && u.protocol !== "https:") return no("that has no ordinary web address to send");
    if (u.username || u.password) return no("an address with a name or password in it is not sent");
    const host = u.hostname.toLowerCase().replace(/\.$/, "");
    if (!host) return no("that has no ordinary web address to send");
    if (hostHidden(host, hidden)) return no("that is a site you keep private, so it is not sent");
    u.hash = "";
    const href = u.href;
    if (href.length > LIM.URL) return no("that address is too long to send");
    return { ok: true, url: href, host };
  }

  // ───────────────────────── Send to Nori ─────────────────────────
  const SHARE_KINDS = ["page", "selection", "link", "screenshot"];
  const SHARE_DESTS = ["chat", "note", "task"];
  function normDest(d) { return SHARE_DESTS.indexOf(d) >= 0 ? d : "chat"; }

  // o: {kind, dest, url, title, selection, pageText, jpegB64, note}. Returns {ok, body} | {ok:false, error}.
  // Page: title + url only, unless pageText is supplied (the user chose "include page text"), then capped at 8000.
  function buildShareBody(o) {
    o = o || {};
    if (SHARE_KINDS.indexOf(o.kind) < 0) return { ok: false, error: "unknown kind of share" };
    const body = { kind: o.kind, dest: normDest(o.dest) };
    if (o.url) body.url = String(o.url).slice(0, LIM.URL);
    if (o.kind === "page") {
      body.title = cleanText(o.title, LIM.TITLE);
      if (o.pageText != null && o.pageText !== false) {
        const t = cleanPageText(o.pageText, LIM.PAGE_TEXT_SHARE);
        if (t) body.text = t;
      }
    } else if (o.kind === "selection") {
      const t = cleanMultiline(o.selection, LIM.SELECTION);
      if (!t) return { ok: false, error: "nothing is selected" };
      body.text = t;
      body.title = cleanText(o.title, LIM.TITLE);
    } else if (o.kind === "link") {
      if (!body.url) return { ok: false, error: "that link has no address" };
    } else if (o.kind === "screenshot") {
      const b = String(o.jpegB64 || "");
      if (!b) return { ok: false, error: "the screenshot is empty" };
      if (b.length >= LIM.JPEG_B64) return { ok: false, error: "the screenshot is too large to send" };
      body.jpeg_b64 = b;
      body.mime = "image/jpeg";
      body.title = cleanText(o.title, LIM.TITLE);
    }
    if (body.title === "") delete body.title;
    const note = cleanMultiline(o.note, LIM.NOTE);
    if (note) body.note = note;
    return { ok: true, body };
  }

  // chrome.tabs.captureVisibleTab returns a data URL.
  function dataUrlToJpeg(dataUrl) {
    const m = /^data:image\/jpeg;base64,([A-Za-z0-9+/=]+)$/.exec(String(dataUrl || ""));
    return m ? m[1] : null;
  }
  const JPEG_QUALITIES = [60, 45, 30, 20];

  // Context menus: a parent per kind, three children per parent (chat is first and the default).
  const MENU_KINDS = [
    { kind: "page", title: "Send this page to Nori", contexts: ["page", "frame", "action"] },
    { kind: "pagetext", title: "Send this page and its text to Nori", contexts: ["page", "action"] },
    { kind: "selection", title: "Send this selection to Nori", contexts: ["selection"] },
    { kind: "link", title: "Send this link to Nori", contexts: ["link"] },
    { kind: "screenshot", title: "Send a screenshot of this tab to Nori", contexts: ["page", "action"] },
  ];
  const DEST_TITLES = { chat: "In the chat", note: "As a note", task: "As a task" };
  function shareMenuSpec() {
    const items = [];
    for (const k of MENU_KINDS) {
      items.push({ id: "nori-root|" + k.kind, title: k.title, contexts: k.contexts });
      for (const d of SHARE_DESTS) items.push({ id: "nori|" + k.kind + "|" + d, parentId: "nori-root|" + k.kind, title: DEST_TITLES[d], contexts: k.contexts });
    }
    return items;
  }
  function parseShareMenuId(id) {
    const m = /^nori\|(page|pagetext|selection|link|screenshot)\|(chat|note|task)$/.exec(String(id || ""));
    if (!m) return null;
    return { kind: m[1] === "pagetext" ? "page" : m[1], withText: m[1] === "pagetext", dest: m[2] };
  }

  // ───────────────────────── Ask about this page (side panel) ─────────────────────────
  // The text is cleaned and capped at 30,000 characters. Returns {ok, body, chars} | {ok:false, error}.
  function buildAskBody(o) {
    o = o || {};
    const question = cleanMultiline(o.question, LIM.QUESTION);
    if (!question) return { ok: false, error: "type a question first" };
    const text = cleanPageText(o.text, LIM.PAGE_TEXT_ASK);
    const url = o.url ? String(o.url).slice(0, LIM.URL) : "";
    return { ok: true, chars: text.length, body: { question, url, title: cleanText(o.title, LIM.TITLE), text } };
  }

  // What the side panel tells the user it is about to send (and the same words afterwards).
  function describeAsk(title, host, chars) {
    return "Sending “" + cleanText(title, 80) + "” (" + host + ") with " + chars + " character" + (chars === 1 ? "" : "s") + " of page text";
  }

  // A friendly reason for a failed call (the server's own words are kept when it gave any).
  function httpErrorText(status, serverError, what) {
    const own = cleanText(serverError, 200);
    if (status === 401) return "Nori no longer recognises this browser (removed on the server): pair it again in the options";
    if (status === 403) return own || (what || "that") + " is switched off on the server";
    if (status === 413) return own || "that was too large for the server to accept";
    if (status === 429) return own || "Nori asked for a moment between requests: try again shortly";
    if (status >= 500) return own || "the server had a problem (" + status + ")";
    return own || "the server answered " + status;
  }

  // ───────────────────────── Passive browsing time ─────────────────────────
  const ACTIVITY = {
    CAP_S: 120,                 // per host, per report
    GAP_MAX_MS: 180000,         // a longer silence between two looks at the clock counts for nothing (asleep, killed, lid closed)
    PENDING_MAX_AGE_MS: 300000, // a report that could not be sent is dropped after this
    REPORT_EVERY_MS: 60000,
    MAX_HOSTS: 200,
    FRESH_MS: 180000,           // the server's switch must have been seen this recently
  };
  const REPORT_HOST_RE = /^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?$/;

  // Only ever holds host names and milliseconds. `cur` is the host being timed right now (null: nothing is
  // being timed: another application is in front, the screen is locked, or he is idle).
  class ActivityClock {
    constructor(state) { this.s = state && state.acc && typeof state.acc === "object" ? state : { acc: {}, cur: null, since: null }; }
    _settle(now) {
      const s = this.s;
      if (s.cur && s.since != null) {
        const el = now - s.since;
        if (el > 0 && el <= ACTIVITY.GAP_MAX_MS) {
          if (s.acc[s.cur] != null || Object.keys(s.acc).length < ACTIVITY.MAX_HOSTS) s.acc[s.cur] = (s.acc[s.cur] || 0) + el;
        }
      }
      s.since = now;
    }
    // host: the host of the active tab of the focused window while he is active, else null.
    setCurrent(host, now) { this._settle(now); this.s.cur = host || null; this.s.since = now; }
    // The seconds to report, hosts only. Hidden hosts are dropped here too (the list may have changed since).
    drain(now, hidden) {
      this._settle(now);
      const list = [];
      for (const host of Object.keys(this.s.acc)) {
        const sec = Math.min(ACTIVITY.CAP_S, Math.round(this.s.acc[host] / 1000));
        if (sec >= 1 && REPORT_HOST_RE.test(host) && !hostHidden(host, hidden)) list.push({ host, seconds: sec });
      }
      this.s.acc = {};
      list.sort((a, b) => b.seconds - a.seconds);
      return list.slice(0, 100);
    }
    reset() { this.s = { acc: {}, cur: null, since: null }; }
  }

  // Merge two host lists (a report that failed + new time), capped per host.
  function mergeHostSeconds(a, b) {
    const m = new Map();
    for (const e of [].concat(a || [], b || [])) {
      if (!e || typeof e.host !== "string") continue;
      m.set(e.host, Math.min(ACTIVITY.CAP_S, (m.get(e.host) || 0) + (Number(e.seconds) || 0)));
    }
    return Array.from(m, ([host, seconds]) => ({ host, seconds }));
  }

  // The request body. Strictly {host_seconds:[{host, seconds}]}: anything that is not a plain host name, or
  // not a whole number of seconds from 1 to 120, is dropped, so a title / URL / path can never get through.
  function buildActivityBody(list, hidden) {
    const out = [];
    for (const e of list || []) {
      if (!e || typeof e.host !== "string") continue;
      const host = e.host.toLowerCase();
      const sec = Math.round(Number(e.seconds));
      if (!REPORT_HOST_RE.test(host) || hostHidden(host, hidden)) continue;
      if (!Number.isFinite(sec) || sec < 1) continue;
      out.push({ host, seconds: Math.min(ACTIVITY.CAP_S, sec) });
      if (out.length >= 100) break;
    }
    return { host_seconds: out };
  }

  // The host to time right now: the active tab of the focused window, only while he is active.
  // tab: {url, incognito} | null. Returns a host string or null.
  function activityHost(tab, windowFocused, idleState, pol) {
    if (!tab || !windowFocused || idleState !== "active") return null;
    if (tab.incognito && !pol.includePrivate) return null;
    const host = hostOf(tab.url || "");
    if (!host || hostHidden(host, pol.hidden)) return null;
    return host;
  }

  // ───────────────────────── Server settings / what is being shared ─────────────────────────
  function parseSettings(raw, prev) {
    prev = prev || {};
    const r = raw && typeof raw === "object" && !Array.isArray(raw) ? raw : null;
    const hidden = r ? sanitizeHostList(r.hidden_hosts) : null;
    return {
      present: !!r,
      notifications: r ? r.notifications !== false : true,     // notifications are the one thing on by default
      activity: !!r && r.activity === true,                    // everything else is off unless the server says so
      standing_look: !!r && r.standing_look === true,
      hidden_hosts: hidden || (Array.isArray(prev.hidden_hosts) ? prev.hidden_hosts : []),
    };
  }

  // st: the stored state (paused, baseUrl, token, notify, notifBlocked, serverSettings, settingsTs).
  // Everything the popup says about "what is shared right now" comes from this, and so do the worker's gates.
  function sharingState(st, now) {
    st = st || {};
    const configured = !!(st.baseUrl && st.token);
    const s = st.serverSettings || {};
    const fresh = !!st.settingsTs && now - st.settingsTs >= 0 && now - st.settingsTs < ACTIVITY.FRESH_MS;
    const live = configured && !st.paused && !(st.authFailedToken && st.authFailedToken === st.token);
    return {
      configured,
      paused: !!st.paused,
      notifications: live && st.notify !== false && s.notifications !== false && !st.notifBlocked,
      activity: live && fresh && s.activity === true,
      standingLook: live && fresh && s.standing_look === true,
      hidden: Array.isArray(s.hidden_hosts) ? s.hidden_hosts : [],
      includePrivate: !!st.includePrivate,
    };
  }

  // What a pairing or a "remove" resets: nothing from a previous server/account may carry over.
  function freshPairState() {
    return {
      lastAck: 0, shown: [], approvals: {}, serverSettings: null, settingsTs: 0, authFailedToken: "", backoffUntil: 0,
      notifBlocked: false, activityState: null, activityPending: null, lastActivity: null, lastCommand: null, lastShare: null, status: null,
    };
  }

  // ───────────────────────── Notifications ─────────────────────────
  const CATEGORIES = {
    chat: { label: "Reply", priority: 0 },
    ping: { label: "From Nori", priority: 0 },
    reminder: { label: "Reminder", priority: 1 },
    job: { label: "Finished", priority: 0 },
    approval: { label: "Needs your approval", priority: 2 },
    peer: { label: "Message", priority: 0 },
  };
  const MAX_MESSAGES = 20;
  const SHOWN_MAX = 300;

  function validApproval(a) {
    if (!a || typeof a !== "object") return null;
    if (!Number.isInteger(a.id) || a.id <= 0) return null;
    const host = typeof a.host === "string" ? a.host.trim().toLowerCase().slice(0, 253) : "";
    return { id: a.id, summary: cleanText(a.summary, 160), host };
  }

  // The messages from a poll, made safe: valid integer ids only, no duplicates within the poll, oldest first, at most 20.
  function normalizeMessages(raw) {
    if (!Array.isArray(raw)) return [];
    const seen = new Set();
    const out = [];
    for (const m of raw) {
      if (!m || typeof m !== "object") continue;
      const id = m.id;
      if (!Number.isInteger(id) || id <= 0 || seen.has(id)) continue;
      seen.add(id);
      const category = Object.prototype.hasOwnProperty.call(CATEGORIES, m.category) ? m.category : "ping";
      out.push({
        id, category,
        title: cleanText(m.title, LIM.NOTIF_TITLE) || "Nori",
        text: cleanMultiline(m.text, LIM.NOTIF_TEXT),
        approval: category === "approval" ? validApproval(m.approval) : null,
      });
    }
    out.sort((a, b) => a.id - b.id);
    return out.slice(0, MAX_MESSAGES);
  }

  // The ledger of notification ids already shown (kept in chrome.storage so a restarted worker doesn't repeat them).
  function ledgerHas(ids, id) { return Array.isArray(ids) && ids.indexOf(id) >= 0; }
  function ledgerAdd(ids, id, max) {
    const out = (Array.isArray(ids) ? ids : []).filter((x) => x !== id);
    out.push(id);
    return out.slice(-(max || SHOWN_MAX));
  }
  function nextAck(prev, id) { return Math.max(Number(prev) || 0, Number(id) || 0); }

  // Category -> chrome.notifications options. For an approval on a hidden site the details are withheld and no
  // buttons are offered (he opens Nori to decide, with the full picture). Returns {id, options, approval}.
  function notificationSpec(msg, o) {
    o = o || {};
    const cat = CATEGORIES[msg.category] || CATEGORIES.ping;
    const id = "nori-" + msg.id;
    let message = msg.text;
    let approval = null;
    if (msg.category === "approval") {
      const a = msg.approval;
      const hiddenSite = !!(a && a.host && hostHidden(a.host, o.hidden));
      if (!a) {
        message = message || "Nori needs your approval. Open Nori to answer.";
      } else if (hiddenSite) {
        message = "Nori needs your approval for something on a site you keep private. Open Nori to review it.";
      } else {
        message = message || ("Nori wants to " + (a.summary || "do something") + (a.host ? " on " + a.host : "") + ".");
        approval = a;
      }
    }
    const options = {
      type: "basic",
      iconUrl: o.iconUrl || "icon128.png",
      title: msg.title || "Nori",
      message: message || "(no text)",
      contextMessage: cat.label,
      priority: cat.priority,
      requireInteraction: msg.category === "approval",
    };
    if (approval) options.buttons = [{ title: "Approve" }, { title: "Deny" }];
    return { id, options, approval };
  }

  function decisionForButton(index) { return index === 0 ? "approve" : index === 1 ? "deny" : null; }

  // ───────────────────────── Pairing ─────────────────────────
  function normalizeBaseUrl(raw) {
    const s = String(raw == null ? "" : raw).trim();
    if (!/^https?:\/\//i.test(s)) return { ok: false, error: "The address must start with http:// or https://" };
    let u;
    try { u = new URL(s); } catch (e) { return { ok: false, error: "That doesn't look like a web address" }; }
    if (u.username || u.password) return { ok: false, error: "Leave the name and password out of the address" };
    if (!u.hostname) return { ok: false, error: "That address has no host in it" };
    u.search = ""; u.hash = "";
    const url = u.href.replace(/\/+$/, "");
    const plain = u.protocol === "http:" && !/^(localhost|127\.0\.0\.1|\[::1\])$/.test(u.hostname) && !/\.localhost$/.test(u.hostname);
    return { ok: true, url, warning: plain ? "This address is not encrypted (http). Your pairing token would cross the network in the clear: use https unless this is a trusted private network." : "" };
  }

  // XXXX-XXXX in, XXXX-XXXX out (or null). Case, spaces and a missing dash are forgiven.
  function normalizePairCode(raw) {
    const s = String(raw == null ? "" : raw).toUpperCase().replace(/[\s-]/g, "");
    if (!/^[A-Z0-9]{8}$/.test(s)) return null;
    return s.slice(0, 4) + "-" + s.slice(4);
  }

  function chromeMajor(ua) {
    const m = /\b(?:Chrome|Chromium|Edg)\/(\d+)/.exec(String(ua || ""));
    return m ? Number(m[1]) : 0;
  }
  function platformOf(ua) {
    ua = String(ua || "");
    return /Windows/.test(ua) ? "Windows" : /Mac OS X|Macintosh/.test(ua) ? "macOS" : /CrOS/.test(ua) ? "ChromeOS" : /Android/.test(ua) ? "Android" : /Linux/.test(ua) ? "Linux" : "";
  }
  function browserModel(ua) {
    const maj = chromeMajor(ua);
    const p = platformOf(ua);
    return maj ? ("Chrome " + maj + (p ? " on " + p : "")) : (p || "");
  }
  function defaultDeviceName(ua) {
    const p = platformOf(ua);
    return p ? "Chrome on " + p : "Chrome";
  }

  // POST /bridge/register {code, kind:"browser", name, model?, v}
  function registerBody(o) {
    const body = { code: o.code, kind: "browser", name: cleanText(o.name, LIM.NAME) || defaultDeviceName(o.ua), v: String(o.v || VERSION_FALLBACK) };
    const model = cleanText(o.model, 80);
    if (model) body.model = model;
    return body;
  }

  // ───────────────────────── Version gating ─────────────────────────
  function compareVersions(a, b) {
    const pa = String(a || "0").split(".").map((x) => parseInt(x, 10) || 0);
    const pb = String(b || "0").split(".").map((x) => parseInt(x, 10) || 0);
    for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
      const d = (pa[i] || 0) - (pb[i] || 0);
      if (d) return d < 0 ? -1 : 1;
    }
    return 0;
  }
  // The oldest Chrome each feature needs (MV3 service workers: 88; sidePanel API: 114; sidePanel.open(): 116).
  const MIN_CHROME = { sidePanel: 116, serviceWorker: 88, notificationButtons: 59 };
  function featureSupported(feature, major) { return !!MIN_CHROME[feature] && major >= MIN_CHROME[feature]; }

  // ───────────────────────── Words for the popup ─────────────────────────
  function agoText(ms) {
    const s = Math.max(0, Math.round(ms / 1000));
    if (s < 10) return "just now";
    if (s < 60) return s + " s ago";
    const m = Math.round(s / 60);
    if (m < 60) return m + " min ago";
    const h = Math.round(m / 60);
    if (h < 48) return h + " h ago";
    return Math.round(h / 24) + " days ago";
  }
  function describeLastCommand(lc, now) {
    if (!lc || !lc.action) return "No command yet";
    const how = lc.waiting ? "waiting for your approval" : lc.ok ? "done" : "refused or failed" + (lc.error ? ": " + cleanText(lc.error, 120) : "");
    return (lc.look_only ? "(standing look) " : "") + lc.action + " — " + how + ", " + agoText(now - (lc.ts || now));
  }
  function describeLastShare(ls, now) {
    if (!ls) return "Nothing sent yet";
    return (ls.ok ? "Sent " + (ls.kind || "") + (ls.dest ? " to " + ls.dest : "") : "Not sent: " + cleanText(ls.error, 140)) + ", " + agoText(now - (ls.ts || now));
  }

  const api = {
    hostMatches, hostAllowed, hostHidden, hostOf, sameOrigin, sanitizeHostList, isSensitiveField, classify, fingerprint, describe,
    SENSITIVE_RE, RISKY_RE, LOOK_ACTIONS, LOOK_REFUSAL, lookOnlyVerdict,
    LIM, cleanText, cleanMultiline, cleanPageText, pageTextCollector,
    gatePage, shareUrl, buildShareBody, dataUrlToJpeg, JPEG_QUALITIES, SHARE_KINDS, SHARE_DESTS, normDest, shareMenuSpec, parseShareMenuId,
    buildAskBody, describeAsk, httpErrorText,
    ACTIVITY, ActivityClock, mergeHostSeconds, buildActivityBody, activityHost,
    parseSettings, sharingState, freshPairState,
    CATEGORIES, MAX_MESSAGES, SHOWN_MAX, normalizeMessages, ledgerHas, ledgerAdd, nextAck, notificationSpec, decisionForButton,
    normalizeBaseUrl, normalizePairCode, chromeMajor, platformOf, browserModel, defaultDeviceName, registerBody,
    compareVersions, MIN_CHROME, featureSupported,
    agoText, describeLastCommand, describeLastShare,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.NoriLib = api;
})(typeof self !== "undefined" ? self : this);
