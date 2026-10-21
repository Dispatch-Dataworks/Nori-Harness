// The toolbar popup. Everything shown here is read from the same stored state the worker acts on
// (chrome.storage.local), through the same pure functions in lib.js: nothing is a hard-coded claim.
const $ = (id) => document.getElementById(id);
const L = self.NoriLib;
const KEYS = {
  baseUrl: "", token: "", name: "", paused: false, includePrivate: false, notify: true, notifBlocked: false,
  serverSettings: null, settingsTs: 0, authFailedToken: "", status: null, lastCommand: null, lastShare: null,
  lastActivity: null, shareDest: "chat",
};

function line(id, label, on, value) {
  const el = $(id);
  el.textContent = label + ": " + (value || (on ? "on" : "off"));
  el.className = "line " + (on ? "on" : "off");
}

async function activeTab() {
  try { return (await chrome.tabs.query({ active: true, currentWindow: true }))[0] || null; } catch (e) { return null; }
}

async function render() {
  const c = await chrome.storage.local.get(KEYS);
  const t = Date.now();
  const st = L.sharingState(c, t);
  const s = c.status || {};
  $("state").textContent = !st.configured ? "Not paired with Nori yet" : c.paused ? "Paused (Nori can't do anything)" : (s.state || "starting…");
  const bits = [];
  if (c.name && st.configured) bits.push(c.name);
  if (c.baseUrl && st.configured) bits.push(c.baseUrl);
  if (s.session_until && s.session_until * 1000 > t && !c.paused) bits.push("session ends " + new Date(s.session_until * 1000).toLocaleTimeString());
  if (s.error && !c.paused) bits.push(s.error);
  $("detail").textContent = bits.join(" · ");
  $("pause").textContent = c.paused ? "Resume" : "Pause";
  $("pause").disabled = !st.configured;

  line("sh-notif", "desktop notifications", st.notifications);
  line("sh-activity", "browsing time sharing", st.activity);
  line("sh-look", "standing look", st.standingLook);
  line("sh-private", "private windows", st.configured && !c.paused && st.includePrivate, st.includePrivate ? "included" : "not included");
  const la = c.lastActivity;
  $("last-activity").hidden = !(st.activity && la && la.ok && la.hosts && la.hosts.length);
  if (!$("last-activity").hidden) {
    $("last-activity").textContent = "Last report " + L.agoText(t - la.ts) + " (host names and seconds only): " + la.hosts.slice(0, 6).map((h) => h.host + " " + h.seconds + " s").join(", ");
  }

  $("last-command").textContent = L.describeLastCommand(c.lastCommand, t);

  $("dest").value = L.normDest(c.shareDest);
  const withText = $("withtext").checked;
  $("send-what").textContent = withText
    ? "Sends this tab's title, address and up to 8,000 characters of its visible text."
    : "Sends only this tab's title and address.";
  $("last-share").textContent = L.describeLastShare(c.lastShare, t);
  const tab = await activeTab();
  const gate = L.gatePage(tab && tab.url, tab && tab.incognito, { configured: st.configured, paused: !!c.paused, includePrivate: !!c.includePrivate, hidden: st.hidden });
  $("send").disabled = !gate.ok;
  const sm = $("send-msg");
  if (!gate.ok) {
    if (!sm.dataset.busy) { sm.className = "msg small"; sm.textContent = gate.error; sm.dataset.gate = "1"; }
  } else if (sm.dataset.gate) { sm.textContent = ""; delete sm.dataset.gate; }
  $("panel").hidden = !(chrome.sidePanel && chrome.sidePanel.open);
}

$("pause").addEventListener("click", async () => {
  const c = await chrome.storage.local.get({ paused: false });
  await chrome.storage.local.set({ paused: !c.paused });
  render();
});
$("options").addEventListener("click", () => chrome.runtime.openOptionsPage());
$("dest").addEventListener("change", async () => { await chrome.storage.local.set({ shareDest: L.normDest($("dest").value) }); });
$("withtext").addEventListener("change", render);

$("send").addEventListener("click", async () => {
  const msg = $("send-msg");
  $("send").disabled = true;
  msg.dataset.busy = "1";
  msg.className = "msg small";
  msg.textContent = "Sending…";
  let r;
  try {
    r = await chrome.runtime.sendMessage({ type: "share", kind: "page", dest: L.normDest($("dest").value), withText: $("withtext").checked, note: $("note").value });
  } catch (e) { r = { ok: false, error: "couldn't reach the extension's background worker" }; }
  delete msg.dataset.busy;
  msg.className = "msg small " + (r && r.ok ? "ok" : "err");
  msg.textContent = r && r.ok ? "Sent." : "Not sent: " + ((r && r.error) || "no answer");
  if (r && r.ok) $("note").value = "";
  render();
});

$("panel").addEventListener("click", async () => {
  try {
    const tab = await activeTab();
    await chrome.sidePanel.open({ windowId: tab ? tab.windowId : undefined });
    window.close();
  } catch (e) {
    $("send-msg").className = "msg small err";
    $("send-msg").textContent = "Couldn't open the side panel: " + String(e && e.message || e).slice(0, 100);
  }
});

chrome.storage.onChanged.addListener(render);
render();
