/* Channels page (Telegram): on/off, who answers, pairing code, paired chats,
 * and who may talk to the agents in each paired group.
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
      li.className = "peer";
      var head = document.createElement("div");
      head.className = "row between";
      var who = document.createElement("span");
      who.textContent = p.label || p.peer_id;
      var kind = document.createElement("span");
      kind.className = "pill";
      kind.textContent = p.chat_type === "private" ? "private" : "group";
      who.appendChild(kind);
      head.appendChild(who);
      head.appendChild(button("Unpair", "ghost small", function () { unpair(p.id); }));
      li.appendChild(head);
      if (p.chat_type !== "private") li.appendChild(membersOf(p));
      ul.appendChild(li);
    });
    $("no-peers").hidden = (tg.peers || []).length > 0;
  }

  function button(label, cls, onClick) {
    var b = document.createElement("button");
    b.type = "button";
    b.className = cls;
    b.textContent = label;
    b.addEventListener("click", onClick);
    return b;
  }

  function personName(m) {
    return (m.username ? "@" + m.username + " " : "") + "(id " + m.user_id + ")";
  }

  // A group's list: only these people can talk to the agents in the group.
  // Results of finished runs are DMed to them; unreachable = bot cannot DM yet.
  function membersOf(p) {
    var box = document.createElement("div");
    box.className = "members";
    var members = p.members || [];
    var allowed = members.filter(function (m) { return m.allowed; });
    var asked = members.filter(function (m) { return !m.allowed; });
    var title = document.createElement("p");
    title.className = "hint";
    title.textContent = allowed.length
      ? "Can talk to the agents here (results come to them privately):"
      : "Nobody can talk to the agents here yet. Add people below.";
    box.appendChild(title);
    allowed.forEach(function (m) {
      var row = document.createElement("div");
      row.className = "row between member";
      var name = document.createElement("span");
      name.textContent = personName(m);
      if (m.reachable === false) {
        var flag = document.createElement("span");
        flag.className = "pill warn";
        flag.textContent = "unreachable — ask them to send /start to the bot";
        name.appendChild(flag);
      }
      row.appendChild(name);
      row.appendChild(button("Remove", "ghost small", function () { removeMember(p.id, m.user_id); }));
      box.appendChild(row);
    });
    if (asked.length) {
      var t2 = document.createElement("p");
      t2.className = "hint";
      t2.textContent = "Wrote to the bot here but are not on the list (told once per day):";
      box.appendChild(t2);
      asked.forEach(function (m) {
        var row = document.createElement("div");
        row.className = "row between member";
        var name = document.createElement("span");
        name.textContent = personName(m);
        row.appendChild(name);
        row.appendChild(button("Allow", "small", function () { addMember(p.id, m.user_id); }));
        box.appendChild(row);
      });
    }
    var add = document.createElement("div");
    add.className = "row member";
    var input = document.createElement("input");
    input.type = "text";
    input.inputMode = "numeric";
    input.placeholder = "Telegram user id";
    input.className = "member-id";
    add.appendChild(input);
    add.appendChild(button("Add", "ghost small", function () {
      var v = input.value.trim();
      if (v) addMember(p.id, v);
    }));
    box.appendChild(add);
    return box;
  }

  async function addMember(peerRow, userId) {
    try {
      state = await call("POST", "/api/channels/telegram/peers/" + encodeURIComponent(peerRow) +
                         "/members", { user_id: String(userId) });
      state = await call("GET", "/api/channels");
      render();
    } catch (e) { showError(e.message); }
  }

  async function removeMember(peerRow, userId) {
    try {
      await call("DELETE", "/api/channels/telegram/peers/" + encodeURIComponent(peerRow) +
                 "/members/" + encodeURIComponent(userId));
      await load();
    } catch (e) { showError(e.message); }
  }

  // Do not redraw over an id being typed.
  function typing() {
    var inputs = document.querySelectorAll("input.member-id");
    for (var i = 0; i < inputs.length; i++) {
      if (inputs[i].value || document.activeElement === inputs[i]) return true;
    }
    return false;
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
    setInterval(function () { if (!document.hidden && !typing()) load(); }, 15000);
  });
})();
