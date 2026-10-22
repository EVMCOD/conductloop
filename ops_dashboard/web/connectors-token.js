// Token / no-auth connectors (GitHub, Linear, Todoist, RSS, Readwise…)
// on the "Conectores" page. connectors.js calls window.CLToken.render(c, H)
// for every connector with auth === 'token' and passes its helpers (H).
//
// Same rules as connectors.js (enforced by tests/test_token_kit.py):
// server data is untrusted and only written with textContent; no innerHTML,
// outerHTML, insertAdjacentHTML, document.write, eval or string handlers.
(function () {
  'use strict';

  var L = {
    es: {
      n_token: 'Token personal', n_connect: 'Conectar (solo lectura)', n_enable: 'Activar (solo lectura)',
      n_use_conn: 'Usar esta conexión', n_existing: 'Conexión existente (vista «Conexiones»)',
      n_src_keychain: 'Token en el llavero del sistema.', n_src_conn: 'Usa la conexión «{0}» de la vista «Conexiones».',
      n_src_none: 'Sin autenticación: solo datos públicos.', n_src_missing: 'Falta el token: vuelve a conectar.',
      n_conn_note: 'Si pegas un token nuevo se guarda como conexión «conectores» en la vista «Conexiones» (cifrado; la clave está en el llavero).',
      n_save_cfg: 'Guardar configuración', n_sync: 'Sincronizar ahora', n_forget: 'Olvidar datos importados',
      n_confirm_forget: '¿Borrar del vault todo lo que importó {0}?',
      n_last_sync: 'Última sincronización: {0} · {1} elementos ({2} nuevos o cambiados)', n_never: 'Aún no se ha sincronizado.',
      n_docs: '{0} documentos en el vault', n_grant: 'Permitir escrituras (con confirmación)',
      n_sync_ok: 'Sincronizado: {0} elementos.', n_read_only_note: 'Lectura e ingesta en el vault. Las escrituras, si las hay, esperan tu confirmación.'
    },
    en: {
      n_token: 'Personal token', n_connect: 'Connect (read-only)', n_enable: 'Enable (read-only)',
      n_use_conn: 'Use this connection', n_existing: 'Existing connection (\u201cConnections\u201d view)',
      n_src_keychain: 'Token in the system keychain.', n_src_conn: 'Uses the \u201c{0}\u201d connection from the \u201cConnections\u201d view.',
      n_src_none: 'No authentication: public data only.', n_src_missing: 'Token missing: connect again.',
      n_conn_note: 'A new token is saved as the \u201cconectores\u201d connection in the \u201cConnections\u201d view (encrypted; the key is in the keychain).',
      n_save_cfg: 'Save settings', n_sync: 'Sync now', n_forget: 'Forget imported data',
      n_confirm_forget: 'Delete everything {0} imported into the vault?',
      n_last_sync: 'Last sync: {0} · {1} items ({2} new or changed)', n_never: 'Not synced yet.',
      n_docs: '{0} documents in the vault', n_grant: 'Allow writes (with confirmation)',
      n_sync_ok: 'Synced: {0} items.', n_read_only_note: 'Read and ingest into the vault. Writes, if any, wait for your confirmation.'
    }
  };

  var cache = null;
  function details(H) {
    if (!cache) {
      cache = H.api('/api/connectors/token');
      cache.then(function () { setTimeout(function () { cache = null; }, 0); }, function () { cache = null; });
    }
    return cache;
  }

  function tr(H, key) {
    var D = H.ES ? L.es : L.en;
    var s = Object.prototype.hasOwnProperty.call(D, key) ? D[key] : key;
    for (var i = 2; i < arguments.length; i++) s = s.replace('{' + (i - 2) + '}', String(arguments[i]));
    return s;
  }

  function fmt(H, ts) { try { return new Date(ts * 1000).toLocaleString(H.ES ? 'es' : 'en'); } catch (e) { return String(ts); } }

  function post(H, c, tail, body, after) {
    H.showError('');
    return H.api('/api/connectors/token/' + encodeURIComponent(c.id) + tail, body || {})
      .then(function (r) { if (after) after(r); H.load(); })
      .catch(function (e) { H.showError(e.message); H.load(); });
  }

  function fill(H, c, box, d) {
    H.clear(box);
    var help = d.token_help ? (H.ES ? d.token_help.es : d.token_help.en) : '';
    if (help) box.appendChild(H.el('div', 'hint', help));
    var src = d.token_mode === 'none' ? tr(H, 'n_src_none')
      : (!d.credential_stored && c.status !== 'disconnected') ? tr(H, 'n_src_missing')
      : d.token_mode === 'keychain' ? (d.credential_stored ? tr(H, 'n_src_keychain') : '')
      : (d.linked_connection ? tr(H, 'n_src_conn', d.linked_connection) : '');
    if (src) { var s = H.el('div', 'hint', src); s.setAttribute('data-testid', 'token-source'); box.appendChild(s); }

    if (c.status !== 'connected') {
      if (d.token_mode === 'connections' && d.connections && d.connections.length) {
        var row1 = H.el('div', 'row');
        var lab = H.el('label', 'hint', tr(H, 'n_existing'));
        var sel = H.el('select'); sel.setAttribute('aria-label', tr(H, 'n_existing'));
        d.connections.forEach(function (cn) { var o = H.el('option', null, cn.name); o.value = String(cn.id); sel.appendChild(o); });
        lab.appendChild(document.createElement('br')); lab.appendChild(sel); row1.appendChild(lab);
        row1.appendChild(H.button(tr(H, 'n_use_conn'), 'small', function () {
          post(H, c, '/token', { connection_id: parseInt(sel.value, 10) });
        }));
        box.appendChild(row1);
      }
      var row2 = H.el('div', 'row');
      if (d.token_mode === 'none') {
        row2.appendChild(H.button(tr(H, 'n_enable'), 'small', function () { post(H, c, '/token', {}); }));
      } else {
        var lab2 = H.el('label', 'hint', tr(H, 'n_token'));
        var inp = H.el('input'); inp.type = 'password'; inp.autocomplete = 'off'; inp.setAttribute('aria-label', tr(H, 'n_token'));
        lab2.appendChild(document.createElement('br')); lab2.appendChild(inp); row2.appendChild(lab2);
        row2.appendChild(H.button(tr(H, 'n_connect'), 'small', function () {
          var v = inp.value; inp.value = '';
          post(H, c, '/token', { token: v });
        }));
      }
      box.appendChild(row2);
      if (d.token_mode === 'connections') box.appendChild(H.el('div', 'hint', tr(H, 'n_conn_note')));
    }

    if (d.config_fields && d.config_fields.length) {
      var form = H.el('div', 'row');
      var inputs = {};
      d.config_fields.forEach(function (f) {
        var l = H.el('label', 'hint', H.ES ? f.label_es : f.label_en);
        var v = d.config ? d.config[f.key] : '';
        var w;
        if (f.kind === 'list') { w = H.el('textarea'); w.rows = 3; w.value = (v || []).join('\n'); }
        else { w = H.el('input'); w.type = 'text'; w.value = v || ''; }
        w.setAttribute('aria-label', H.ES ? f.label_es : f.label_en);
        w.setAttribute('data-field', f.key);
        l.appendChild(document.createElement('br')); l.appendChild(w); form.appendChild(l);
        inputs[f.key] = w;
      });
      form.appendChild(H.button(tr(H, 'n_save_cfg'), 'ghost small', function () {
        var cfg = {};
        Object.keys(inputs).forEach(function (k) { cfg[k] = inputs[k].value; });
        post(H, c, '/config', { config: cfg });
      }));
      box.appendChild(form);
    }

    if (c.status === 'connected') {
      box.appendChild(H.el('div', 'hint', tr(H, 'n_read_only_note')));
      var ls = d.last_sync;
      var st = H.el('div', 'hint', ls ? tr(H, 'n_last_sync', fmt(H, ls.at), ls.fetched, ls.inserted_or_updated) : tr(H, 'n_never'));
      st.setAttribute('data-testid', 'token-last-sync');
      box.appendChild(st);
      box.appendChild(H.el('div', 'hint', tr(H, 'n_docs', d.documents || 0)));
      var row3 = H.el('div', 'row');
      var flow = H.el('div', 'hint');
      row3.appendChild(H.button(tr(H, 'n_sync'), 'small', function () {
        post(H, c, '/sync', {}, function (r) { flow.textContent = tr(H, 'n_sync_ok', (r.sync && r.sync.fetched) || 0); });
      }));
      if (d.documents) {
        row3.appendChild(H.button(tr(H, 'n_forget'), 'ghost small', function () {
          if (!window.confirm(tr(H, 'n_confirm_forget', c.name))) return;
          post(H, c, '/forget', {});
        }));
      }
      if (d.has_writes) {
        if (c.permission === 'write') {
          row3.appendChild(H.button(H.t('make_read'), 'ghost small', function () { post(H, c, '/permission', { permission: 'read' }); }));
        } else {
          row3.appendChild(H.button(tr(H, 'n_grant'), 'ghost small', function () { post(H, c, '/permission', { permission: 'write' }); }));
        }
      }
      row3.appendChild(H.button(H.t('disconnect'), 'danger small', function () {
        if (!window.confirm(H.t('confirm_disconnect', c.name))) return;
        H.api('/api/connectors/' + encodeURIComponent(c.id) + '/disconnect', {}).then(H.load).catch(function (e) { H.showError(e.message); });
      }));
      box.appendChild(row3);
      box.appendChild(flow);
    }
  }

  function render(c, H) {
    var card = H.el('div', 'conn');
    card.setAttribute('data-connector', c.id);
    card.setAttribute('data-token-connector', '1');
    var title = H.el('div', 'row');
    title.appendChild(H.el('h3', null, c.name));
    var st = H.el('span', 'badge ' + (c.status === 'connected' ? 'connected' : (c.status === 'error' ? 'error' : '')), H.t('s_' + c.status));
    st.setAttribute('data-testid', 'status');
    title.appendChild(st);
    if (c.status === 'connected') {
      var p = H.el('span', 'badge' + (c.permission === 'write' ? ' write' : ''), H.t('perm_' + c.permission));
      p.setAttribute('data-testid', 'permission');
      title.appendChild(p);
    }
    card.appendChild(title);
    if (c.account_label) card.appendChild(H.el('div', 'hint', H.t('account', c.account_label)));
    if (c.last_error) card.appendChild(H.el('div', 'hint', H.t('error', c.last_error)));
    // connectors-sync.js (OAuth PRs) decorates every syncable card with its own
    // "Sincronizar ahora" button unless one is marked; these cards bring theirs.
    var mark = H.el('span'); mark.hidden = true; mark.setAttribute('data-testid', 'sync'); card.appendChild(mark);
    var box = H.el('div');
    box.setAttribute('data-testid', 'token-details');
    card.appendChild(box);
    details(H).then(function (r) {
      var d = r.connectors && r.connectors[c.id];
      if (d) fill(H, c, box, d);
    }).catch(function (e) { H.showError(e.message); });
    return card;
  }

  window.CLToken = { render: render, L: L };
})();
