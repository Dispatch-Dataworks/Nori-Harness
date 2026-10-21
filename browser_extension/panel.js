// Side panel: "Ask about this page". NOTHING is read automatically, ever: the page's text is read only inside
// the submit handler below, i.e. when he presses Ask. What is sent (title, host, how many characters) is shown
// before and while it goes. The reply is shown as plain text (never as HTML).
const $ = (id) => document.getElementById(id);
const L = self.NoriLib;
const KEYS = { baseUrl: "", token: "", paused: false, includePrivate: false, serverSettings: null, authFailedToken: "" };
const ASK_TIMEOUT_MS = 120000;
let busy = false;

function addLine(role, text, extraClass) {
  const d = document.createElement("div");
  d.className = "bubble " + role + (extraClass ? " " + extraClass : "");
  d.textContent = text;                                    // textContent: a reply can never inject markup
  $("log").appendChild(d);
  if ($("log").scrollTo) { try { $("log").scrollTo(0, $("log").scrollHeight); } catch (e) { /* cosmetic */ } }
  return d;
}

async function currentTab() {
  try { return (await chrome.tabs.query({ active: true, currentWindow: true }))[0] || null; } catch (e) { return null; }
}

// Shows which tab a question would be about. Tab metadata only (title and host): no page content is touched.
async function showPage() {
  const tab = await currentTab();
  const c = await chrome.storage.local.get(KEYS);
  const hidden = c.serverSettings && c.serverSettings.hidden_hosts || [];
  const host = tab ? L.hostOf(tab.url || "") : null;
  const el = $("page");
  if (!tab) { el.textContent = "No tab in front."; return; }
  const gate = L.gatePage(tab.url, tab.incognito, { configured: !!(c.baseUrl && c.token), paused: !!c.paused, includePrivate: !!c.includePrivate, hidden });
  el.textContent = gate.ok
    ? "This tab: “" + L.cleanText(tab.title, 80) + "” (" + host + ")"
    : "This tab can't be asked about: " + gate.error + ".";
}

async function onAsk(ev) {
  if (ev && ev.preventDefault) ev.preventDefault();
  if (busy) return;
  const question = L.cleanMultiline($("q").value, L.LIM.QUESTION);
  if (!question) { $("sent").textContent = "Type a question first."; return; }
  const c = await chrome.storage.local.get(KEYS);
  const hidden = c.serverSettings && c.serverSettings.hidden_hosts || [];
  const tab = await currentTab();
  if (!tab) { addLine("system", "There is no tab in front to ask about.", "err"); return; }
  const gate = L.gatePage(tab.url, tab.incognito, { configured: !!(c.baseUrl && c.token), paused: !!c.paused, includePrivate: !!c.includePrivate, hidden });
  if (!gate.ok) { addLine("system", "Nothing was read or sent: " + gate.error + ".", "err"); return; }
  const shared = L.shareUrl(tab.url, hidden);
  if (!shared.ok) { addLine("system", "Nothing was read or sent: " + shared.error + ".", "err"); return; }

  busy = true;
  $("ask").disabled = true;
  $("q").value = "";
  addLine("user", question);
  $("sent").textContent = "Reading the visible text of “" + L.cleanText(tab.title, 80) + "” (" + gate.host + ")…";
  try {
    let page;
    try {
      const [r] = await chrome.scripting.executeScript({ target: { tabId: tab.id }, func: L.pageTextCollector, args: [L.LIM.PAGE_TEXT_ASK] });
      page = r && r.result;
    } catch (e) {
      addLine("system", "Nothing was sent: this page can't be read (Chrome blocks extensions on some pages, such as its own settings and the Web Store).", "err");
      return;
    }
    if (!page) { addLine("system", "Nothing was sent: the page gave back no text.", "err"); return; }
    let after = null;
    try { after = await chrome.tabs.get(tab.id); } catch (e) { /* closed */ }
    if (!after || String(after.url || "").split("#")[0] !== String(tab.url || "").split("#")[0]) {
      addLine("system", "Nothing was sent: the tab changed while I was reading it.", "err");
      return;
    }
    const built = L.buildAskBody({ question, url: shared.url, title: tab.title, text: page.text });
    if (!built.ok) { addLine("system", built.error, "err"); return; }
    $("sent").textContent = L.describeAsk(tab.title, gate.host, built.chars) + " …";
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), ASK_TIMEOUT_MS);
    let resp, data;
    try {
      resp = await fetch(c.baseUrl.replace(/\/+$/, "") + "/bridge/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: "Bearer " + c.token },
        body: JSON.stringify(built.body),
        signal: ctl.signal,
      });
      data = await resp.json().catch(() => ({}));
    } catch (e) {
      addLine("system", String(e).includes("AbortError") ? "Nori didn't answer in time. The page text was sent; try again in a moment." : "Couldn't reach Nori, so no answer. (The page text was not delivered.)", "err");
      return;
    } finally { clearTimeout(timer); }
    if (resp.status === 401) { try { await chrome.storage.local.set({ authFailedToken: c.token }); } catch (e) { /* ignore */ } }
    if (resp.status >= 200 && resp.status < 300 && data && data.ok === true && typeof data.reply === "string") {
      addLine("nori", data.reply);
      $("sent").textContent = L.describeAsk(tab.title, gate.host, built.chars).replace(/^Sending/, "Sent") + ".";
    } else {
      addLine("system", L.httpErrorText(resp.status, data && data.error, "asking about pages") + ".", "err");
      $("sent").textContent = L.describeAsk(tab.title, gate.host, built.chars).replace(/^Sending/, "Sent") + ", but it was not answered.";
    }
  } finally {
    busy = false;
    $("ask").disabled = false;
  }
}

$("form").addEventListener("submit", onAsk);
$("q").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) onAsk(e); });
try {
  chrome.tabs.onActivated.addListener(() => { showPage(); });
  chrome.tabs.onUpdated.addListener((id, info) => { if (info.title || info.url) showPage(); });
  chrome.storage.onChanged.addListener(() => { showPage(); });
} catch (e) { /* the tab line is a convenience */ }
showPage();
