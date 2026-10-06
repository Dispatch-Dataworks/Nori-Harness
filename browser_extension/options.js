const $ = (id) => document.getElementById(id);
chrome.storage.local.get({ baseUrl: "", token: "" }).then((c) => {
  $("url").value = c.baseUrl;
  $("token").value = c.token;
});
$("save").addEventListener("click", async () => {
  const baseUrl = $("url").value.trim().replace(/\/+$/, "");
  const token = $("token").value.trim();
  if (!/^https?:\/\//.test(baseUrl)) { $("msg").textContent = "The address must start with http:// or https://"; return; }
  if (!token.startsWith("nbr_")) { $("msg").textContent = "That doesn't look like a Nori bridge token (it starts with nbr_)."; return; }
  await chrome.storage.local.set({ baseUrl, token });
  $("msg").textContent = "Saved. The popup shows whether it connected.";
});
