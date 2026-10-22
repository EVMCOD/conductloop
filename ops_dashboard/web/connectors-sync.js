// "Conectores" page add-on: a "Sincronizar ahora" button on every connected
// native connector that can ingest into the vault (POST /api/connectors-sync/<id>).
// Kept out of connectors.js so the framework file is not edited: it watches
// #connectors-list (re-rendered by connectors.js) and decorates the cards.
// It also offers "use the same app" on connectors that can share one BYOK OAuth
// app (e.g. the three Google ones): POST /api/connectors-client-group/<group>
// copies the client ID/secret only — every connector keeps its own token.
// Same rules: textContent only, same-origin JSON only.
(function () {
  'use strict';
  var ES = /^es\b/i.test((navigator && navigator.language) || '');
  var T = ES ? { sync: 'Sincronizar ahora', syncing: 'Sincronizando…', done: 'Importado: {0}', failed: 'Error al sincronizar: {0}',
                 share: 'Usar la misma app OAuth que {0}', shareHint: 'Copia solo el client ID y el secret; cada conector pide su propio permiso y guarda su propio token.' }
             : { sync: 'Sync now', syncing: 'Syncing…', done: 'Imported: {0}', failed: 'Sync failed: {0}',
                 share: 'Use the same OAuth app as {0}', shareHint: 'Copies only the client ID and secret; each connector asks for its own consent and keeps its own token.' };
  var syncable = {};
  var groupOf = {};
  var sharing = false;

  function fmt(s, v) { return s.replace('{0}', String(v)); }
  function api(path, body) {
    var opts = { credentials: 'same-origin', headers: { 'Accept': 'application/json' } };
    if (body !== undefined) { opts.method = 'POST'; opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
    return fetch(path, opts).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        if (!r.ok || j.ok === false) throw new Error((j && (j.message || j.error)) || ('HTTP ' + r.status));
        return j;
      });
    });
  }
  function summary(res) {
    var parts = [];
    Object.keys(res || {}).forEach(function (k) { if (typeof res[k] === 'number') parts.push(k + '=' + res[k]); });
    return parts.join(', ');
  }
  function offerShare() {
    if (sharing || !Object.keys(groupOf).length) return;
    sharing = true;
    api('/api/connectors').then(function (r) {
      var by = {};
      (r.connectors || []).forEach(function (c) { by[c.id] = c; });
      var cards = document.querySelectorAll('#connectors-list [data-connector]');
      for (var i = 0; i < cards.length; i++) {
        var card = cards[i];
        var id = card.getAttribute('data-connector');
        var g = groupOf[id];
        if (!g || !by[id] || by[id].client_id_set || card.querySelector('[data-testid=share-client]')) continue;
        var src = null;
        for (var k in by) { if (k !== id && groupOf[k] === g && by[k].client_id) { src = by[k]; break; } }
        if (!src) continue;
        (function (card, g, src) {
          var b = document.createElement('button'); b.type = 'button'; b.className = 'ghost small';
          b.textContent = fmt(T.share, src.name); b.setAttribute('data-testid', 'share-client');
          var hint = document.createElement('div'); hint.className = 'hint'; hint.textContent = T.shareHint;
          b.addEventListener('click', function () {
            b.disabled = true;
            api('/api/connectors-client-group/' + encodeURIComponent(g), { from: src.id }).then(function () {
              location.reload();
            }).catch(function (e) { hint.textContent = e.message; b.disabled = false; });
          });
          card.appendChild(b); card.appendChild(hint);
        })(card, g, src);
      }
    }).catch(function () {}).then(function () { sharing = false; });
  }
  function decorate() {
    offerShare();
    var cards = document.querySelectorAll('#connectors-list [data-connector]');
    for (var i = 0; i < cards.length; i++) {
      var card = cards[i];
      var id = card.getAttribute('data-connector');
      var st = card.querySelector('[data-testid=status]');
      var connected = st && st.className.indexOf('connected') !== -1;
      if (!syncable[id] || !connected || card.querySelector('[data-testid=sync]')) continue;
      (function (card, id) {
        var out = document.createElement('div'); out.className = 'hint'; out.setAttribute('data-testid', 'sync-result');
        var b = document.createElement('button'); b.type = 'button'; b.className = 'ghost small'; b.textContent = T.sync;
        b.setAttribute('data-testid', 'sync');
        b.addEventListener('click', function () {
          b.disabled = true; out.textContent = T.syncing;
          api('/api/connectors-sync/' + encodeURIComponent(id), {}).then(function (r) {
            out.textContent = fmt(T.done, summary(r.result));
          }).catch(function (e) { out.textContent = fmt(T.failed, e.message); }).then(function () { b.disabled = false; });
        });
        card.appendChild(b); card.appendChild(out);
      })(card, id);
    }
  }
  document.addEventListener('DOMContentLoaded', function () {
    var list = document.getElementById('connectors-list');
    if (!list) return;
    api('/api/connectors-sync').then(function (r) {
      (r.connectors || []).forEach(function (id) { syncable[id] = true; });
      var groups = r.client_groups || {};
      Object.keys(groups).forEach(function (g) { (groups[g] || []).forEach(function (id) { groupOf[id] = g; }); });
      decorate();
      new MutationObserver(decorate).observe(list, { childList: true, subtree: true });
    }).catch(function () { /* page still works without the add-on */ });
  });
})();
