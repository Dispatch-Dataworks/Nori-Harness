const $ = (id) => document.getElementById(id);
async function render() {
  const c = await chrome.storage.local.get({ status: null, paused: false, baseUrl: "" });
  const s = c.status || {};
  $("state").textContent = c.paused ? "Paused (Nori can't do anything)" : (s.state || "starting…");
  const bits = [];
  if (c.baseUrl) bits.push(c.baseUrl);
  if (s.session_until && s.session_until * 1000 > Date.now() && !c.paused) {
    bits.push("session ends " + new Date(s.session_until * 1000).toLocaleTimeString());
  }
  if (s.error) bits.push(s.error);
  $("detail").textContent = bits.join(" · ");
  $("pause").textContent = c.paused ? "Resume" : "Pause";
}
$("pause").addEventListener("click", async () => {
  const c = await chrome.storage.local.get({ paused: false });
  await chrome.storage.local.set({ paused: !c.paused });
  render();
});
$("options").addEventListener("click", () => chrome.runtime.openOptionsPage());
chrome.storage.onChanged.addListener(render);
render();
