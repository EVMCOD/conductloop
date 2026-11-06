/* Boardroom: choose the Holding CEO (an AI agent) or leave it to the
 * Boardroom's own reply. Talks to /api/boardroom/holding-ceo; builds the
 * select with textContent only. */
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };

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
    none.textContent = "None — the Boardroom answers";
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
      say(data.holding_ceo ? data.holding_ceo.name + " is the Holding CEO."
                           : "No Holding CEO: the Boardroom answers.", false);
    } catch (e) {
      say("Could not change it: " + e.message, true);
      load();
    } finally { sel.disabled = false; }
  }

  document.addEventListener("DOMContentLoaded", function () {
    $("hceo-select").addEventListener("change", choose);
    load();
  });
})();
