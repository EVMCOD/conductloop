/* Channels page (Telegram): on/off, who answers, pairing code, paired chats.
 * Talks to /api/channels…; builds the page with textContent only. */
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var state = null;

  function showError(msg) {
    var el = $("error");
    el.textContent = msg || "";
    el.hidden = !msg;
  }

  async function call(method, url, body) {
    var opts = { method: method, headers: { "Content-Type": "application/json" } };
    if (body !== undefined) opts.body = JSON.stringify(body);
    var r = await fetch(url, opts);
    var data = {};
    try { data = await r.json(); } catch (e) { data = {}; }
    if (!r.ok || data.ok === false) throw new Error(data.message || data.error || ("HTTP " + r.status));
    return data;
  }

  function render() {
    var tg = state.channels.telegram;
    var hasToken = tg.token !== "nowhere";
    $("state").textContent = tg.enabled
      ? "On" + (tg.bot_username ? " as @" + tg.bot_username : "") + "."
      : "Off. Nothing is sent to or read from Telegram.";
    $("toggle").textContent = tg.enabled ? "Turn off" : "Turn on";
    $("toggle").className = tg.enabled ? "ghost" : "";
    $("toggle").disabled = !tg.enabled && !hasToken;
    $("token-hint").hidden = hasToken;
    $("last-error").textContent = tg.last_error || "";
    $("last-error").hidden = !(tg.enabled && tg.last_error);
    $("pair").disabled = !tg.enabled;

    var sel = $("default-agent");
    sel.textContent = "";
    var first = document.createElement("option");
    first.value = "";
    first.textContent = "Holding CEO (default)";
    sel.appendChild(first);
    (state.agents || []).forEach(function (a) {
      var o = document.createElement("option");
      o.value = String(a.id);
      o.textContent = a.name + (a.role ? " — " + a.role : "");
      sel.appendChild(o);
    });
    sel.value = tg.default_agent_id ? String(tg.default_agent_id) : "";
    sel.disabled = false;

    var ul = $("peers");
    ul.textContent = "";
    (tg.peers || []).forEach(function (p) {
      var li = document.createElement("li");
      var who = document.createElement("span");
      who.textContent = p.label || p.peer_id;
      var kind = document.createElement("span");
      kind.className = "pill";
      kind.textContent = p.chat_type === "private" ? "private" : "group";
      who.appendChild(kind);
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "ghost small";
      btn.textContent = "Unpair";
      btn.addEventListener("click", function () { unpair(p.id); });
      li.appendChild(who);
      li.appendChild(btn);
      ul.appendChild(li);
    });
    $("no-peers").hidden = (tg.peers || []).length > 0;
  }

  async function load() {
    try {
      state = await call("GET", "/api/channels");
      showError("");
      render();
    } catch (e) { showError("Could not load the channels: " + e.message); }
  }

  async function toggle() {
    var on = state.channels.telegram.enabled;
    $("toggle").disabled = true;
    try {
      await call("POST", "/api/channels/telegram/" + (on ? "disable" : "enable"), {});
      $("code").hidden = true; $("code-hint").hidden = true;
      await load();
    } catch (e) { showError(e.message); $("toggle").disabled = false; }
  }

  async function pair() {
    try {
      var r = await call("POST", "/api/channels/telegram/pairing", {});
      $("code").textContent = r.code;
      $("code").hidden = false;
      $("code-hint").textContent = r.instructions;
      $("code-hint").hidden = false;
    } catch (e) { showError(e.message); }
  }

  async function setDefault() {
    var v = $("default-agent").value;
    try {
      await call("POST", "/api/channels/telegram/default-agent", { agent_id: v ? Number(v) : null });
      await load();
    } catch (e) { showError(e.message); }
  }

  async function unpair(id) {
    try {
      await call("DELETE", "/api/channels/telegram/peers/" + encodeURIComponent(id));
      await load();
    } catch (e) { showError(e.message); }
  }

  document.addEventListener("DOMContentLoaded", function () {
    $("toggle").addEventListener("click", toggle);
    $("pair").addEventListener("click", pair);
    $("default-agent").addEventListener("change", setDefault);
    load();
    setInterval(function () { if (!document.hidden) load(); }, 15000);
  });
})();
