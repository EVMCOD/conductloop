/* Boardroom: choose the Holding CEO (an AI agent) or leave it to the
 * Boardroom's own reply. Talks to /api/boardroom/holding-ceo; builds the
 * select with textContent only. */
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };

  // The words of the app: the dictionary when it has arrived, the English
  // text written here when it has not.
  function t(key, fallback, vars) {
    return (window.BoardroomI18n ? window.BoardroomI18n.t(key, fallback, vars)
      : String(fallback == null ? key : fallback).replace(/\{(\w+)\}/g, function (m, k) {
          return (vars && vars[k] !== undefined) ? vars[k] : m; }));
  }

  function say(text, isError) {
    var m = $("hceo-msg");
    m.textContent = text || "";
    m.className = "meta" + (isError ? " err" : "");
    m.hidden = !text;
  }

  async function call(method, body) {
    var opts = { method: method, headers: { "Content-Type": "application/json" } };
    if (body !== undefined) opts.body = JSON.stringify(body);
    var r = await fetch("/api/boardroom/holding-ceo", opts);
    var data = {};
    try { data = await r.json(); } catch (e) { data = {}; }
    if (!r.ok || data.ok === false) throw new Error(data.message || data.error || ("HTTP " + r.status));
    return data;
  }

  function render(data) {
    var sel = $("hceo-select");
    sel.textContent = "";
    var none = document.createElement("option");
    none.value = "";
    none.textContent = t("boardroom.holding_ceo_none_option", "None — the Boardroom answers");
    sel.appendChild(none);
    (data.candidates || []).forEach(function (a) {
      var o = document.createElement("option");
      o.value = String(a.id);
      o.textContent = a.name + (a.role ? " — " + a.role : "") + (a.project ? " (" + a.project + ")" : "");
      sel.appendChild(o);
    });
    sel.value = data.holding_ceo ? String(data.holding_ceo.id) : "";
    $("hceo").hidden = false;
  }

  async function load() {
    try { render(await call("GET")); } catch (e) { /* not the chairman: keep it hidden */ }
  }

  async function choose() {
    var sel = $("hceo-select");
    var v = sel.value;
    sel.disabled = true;
    try {
      var data = await call("POST", { member_id: v ? Number(v) : null });
      render(data);
      say(data.holding_ceo
            ? t("boardroom.holding_ceo_set", "{name} is the Holding CEO.", { name: data.holding_ceo.name })
            : t("boardroom.holding_ceo_none", "No Holding CEO: the Boardroom answers."), false);
    } catch (e) {
      say(t("boardroom.holding_ceo_error", "Could not change it: {msg}", { msg: e.message }), true);
      load();
    } finally { sel.disabled = false; }
  }

  // The words arrive over the network: paint when they are here, and paint
  // again if the person changes the language.
  function start() {
    if (window.BoardroomI18n) {
      BoardroomI18n.ready(function () { load(); });
      BoardroomI18n.onChange(function () { load(); });
    } else {
      load();
    }
  }

  document.addEventListener("DOMContentLoaded", function () {
    $("hceo-select").addEventListener("change", choose);
    start();
  });
})();
