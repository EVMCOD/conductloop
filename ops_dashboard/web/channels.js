/* Channels page (Telegram + Discord): on/off, who answers, pairing code,
 * paired chats, and who may talk in each paired group/server channel.
 * Talks to /api/channels, /api/channels/telegram/, /api/channels/discord/,
 * /members; builds the page with textContent only. */
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var state = null;

  // The language is the person's choice: every word the page writes comes
  // from the dictionary, and the page repaints itself when it changes.
  function t(key, vars) {
    if (window._i18n && window._i18n.t) return window._i18n.t(key, vars);
    return key;
  }

  // The words arrive over the network: wait for them, or the page would
  // paint keys instead of sentences. i18n.js is deferred, so it runs before
  // DOMContentLoaded — that is when the dictionary can be asked for.
  function whenWords(fn) {
    function go() {
      if (window._i18n && window._i18n.ready) window._i18n.ready().then(fn, fn);
      else fn();
    }
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", go);
    else go();
  }

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

  function inboundLabel(ch, kind) {
    if (!ch.enabled) return kind === "discord"
      ? t("channels.off_discord")
      : t("channels.off_telegram");
    var name = ch.bot_username ? t("channels.on_as", { user: ch.bot_username }) : "";
    if (kind === "discord") {
      var inbound = ch.inbound === "gateway" ? t("channels.inbound_live")
        : ch.inbound === "connecting" ? t("channels.inbound_connecting")
        : t("channels.on_short");
      return t("channels.on") + name + ". " + inbound;
    }
    return t("channels.on") + name + ".";
  }

  function fillAgents(sel, currentId) {
    sel.textContent = "";
    var first = document.createElement("option");
    first.value = "";
    first.textContent = t("channels.holding_ceo");
    sel.appendChild(first);
    (state.agents || []).forEach(function (a) {
      var o = document.createElement("option");
      o.value = String(a.id);
      o.textContent = a.name + (a.role ? " — " + a.role : "");
      sel.appendChild(o);
    });
    sel.value = currentId ? String(currentId) : "";
    sel.disabled = false;
  }

  function renderChannel(kind, ch) {
    var hasToken = ch && ch.token !== "nowhere";
    $(kind + "-state").textContent = inboundLabel(ch || {}, kind);
    $(kind + "-toggle").textContent = ch && ch.enabled ? t("channels.turn_off") : t("channels.turn_on");
    $(kind + "-toggle").className = ch && ch.enabled ? "ghost" : "";
    $(kind + "-toggle").disabled = !(ch && (ch.enabled || hasToken));
    $(kind + "-token-hint").hidden = hasToken;
    $(kind + "-last-error").textContent = (ch && ch.last_error) || "";
    $(kind + "-last-error").hidden = !(ch && ch.enabled && ch.last_error);
    $(kind + "-pair").disabled = !(ch && ch.enabled);
    fillAgents($(kind + "-default-agent"), ch && ch.default_agent_id);

    var ul = $(kind + "-peers");
    ul.textContent = "";
    ((ch && ch.peers) || []).forEach(function (p) {
      var li = document.createElement("li");
      li.className = "peer";
      var head = document.createElement("div");
      head.className = "row between";
      var who = document.createElement("span");
      who.textContent = p.label || p.peer_id;
      var pill = document.createElement("span");
      pill.className = "pill";
      pill.textContent = p.chat_type === "private" ? t("channels.private")
        : (kind === "discord" ? t("channels.server") : t("channels.group"));
      who.appendChild(pill);
      head.appendChild(who);
      head.appendChild(button(t("channels.unpair"), "ghost small", function () { unpair(kind, p.id); }));
      li.appendChild(head);
      if (p.chat_type !== "private") li.appendChild(membersOf(kind, p));
      ul.appendChild(li);
    });
    $(kind + "-no-peers").hidden = ((ch && ch.peers) || []).length > 0;
  }

  function render() {
    var channels = (state && state.channels) || {};
    renderChannel("telegram", channels.telegram || { token: "nowhere" });
    renderChannel("discord", channels.discord || { token: "nowhere" });
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

  function membersOf(kind, p) {
    var box = document.createElement("div");
    box.className = "members";
    var members = p.members || [];
    var allowed = members.filter(function (m) { return m.allowed; });
    var asked = members.filter(function (m) { return !m.allowed; });
    var title = document.createElement("p");
    title.className = "hint";
    title.textContent = allowed.length
      ? t("channels.can_talk")
      : t("channels.nobody_can_talk");
    box.appendChild(title);
    allowed.forEach(function (m) {
      var row = document.createElement("div");
      row.className = "row between member";
      var name = document.createElement("span");
      name.textContent = personName(m);
      if (m.reachable === false) {
        var flag = document.createElement("span");
        flag.className = "pill warn";
        flag.textContent = kind === "discord"
          ? t("channels.discord_unreachable")
          : t("channels.unreachable");
        name.appendChild(flag);
      }
      row.appendChild(name);
      row.appendChild(button(t("channels.remove"), "ghost small", function () { removeMember(kind, p.id, m.user_id); }));
      box.appendChild(row);
    });
    if (asked.length) {
      var t2 = document.createElement("p");
      t2.className = "hint";
      t2.textContent = t("channels.wrote_not_list");
      box.appendChild(t2);
      asked.forEach(function (m) {
        var row = document.createElement("div");
        row.className = "row between member";
        var name = document.createElement("span");
        name.textContent = personName(m);
        row.appendChild(name);
        row.appendChild(button(t("channels.allow"), "small", function () { addMember(kind, p.id, m.user_id); }));
        box.appendChild(row);
      });
    }
    var add = document.createElement("div");
    add.className = "row member";
    var input = document.createElement("input");
    input.type = "text";
    input.inputMode = "numeric";
    input.placeholder = kind === "discord" ? t("channels.discord_user_id_ph") : t("channels.user_id_ph");
    input.className = "member-id";
    add.appendChild(input);
    add.appendChild(button(t("channels.add"), "ghost small", function () {
      var v = input.value.trim();
      if (v) addMember(kind, p.id, v);
    }));
    box.appendChild(add);
    return box;
  }

  async function addMember(kind, peerRow, userId) {
    try {
      await call("POST", "/api/channels/" + kind + "/peers/" + encodeURIComponent(peerRow) +
                 "/members", { user_id: String(userId) });
      await load();
    } catch (e) { showError(e.message); }
  }

  async function removeMember(kind, peerRow, userId) {
    try {
      await call("DELETE", "/api/channels/" + kind + "/peers/" + encodeURIComponent(peerRow) +
                 "/members/" + encodeURIComponent(userId));
      await load();
    } catch (e) { showError(e.message); }
  }

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
    } catch (e) { showError(t("channels.load_error", { msg: e.message })); }
  }

  async function toggle(kind) {
    var ch = state.channels[kind];
    var on = ch && ch.enabled;
    $(kind + "-toggle").disabled = true;
    try {
      await call("POST", "/api/channels/" + kind + "/" + (on ? "disable" : "enable"), {});
      $(kind + "-code").hidden = true; $(kind + "-code-hint").hidden = true;
      await load();
    } catch (e) { showError(e.message); $(kind + "-toggle").disabled = false; }
  }

  async function pair(kind) {
    try {
      var r = await call("POST", "/api/channels/" + kind + "/pairing", {});
      $(kind + "-code").textContent = r.code;
      $(kind + "-code").hidden = false;
      $(kind + "-code-hint").textContent = r.instructions;
      $(kind + "-code-hint").hidden = false;
    } catch (e) { showError(e.message); }
  }

  async function setDefault(kind) {
    var v = $(kind + "-default-agent").value;
    try {
      await call("POST", "/api/channels/" + kind + "/default-agent", { agent_id: v ? Number(v) : null });
      await load();
    } catch (e) { showError(e.message); }
  }

  async function unpair(kind, id) {
    try {
      await call("DELETE", "/api/channels/" + kind + "/peers/" + encodeURIComponent(id));
      await load();
    } catch (e) { showError(e.message); }
  }

  document.addEventListener("DOMContentLoaded", function () {
    ["telegram", "discord"].forEach(function (kind) {
      $(kind + "-toggle").addEventListener("click", function () { toggle(kind); });
      $(kind + "-pair").addEventListener("click", function () { pair(kind); });
      $(kind + "-default-agent").addEventListener("change", function () { setDefault(kind); });
    });
    whenWords(function () {
      // When the person switches the language, the words around the page
      // change too.
      if (window._i18n && window._i18n.onLocaleChange) {
        window._i18n.onLocaleChange(function () { if (state) render(); });
      }
      load();
      setInterval(function () { if (!document.hidden && !typing()) load(); }, 15000);
    });
  });
})();
