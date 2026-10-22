// "Conectores" page (connectors framework). Talks only to /api/connectors/*.
//
// Security rules (enforced by tests/test_connectors_ui_static.py):
//   * Server data (connector names, scopes, payloads, audit details) is
//     untrusted: only written with textContent / createTextNode. No innerHTML,
//     outerHTML, insertAdjacentHTML, document.write, eval or string handlers.
//   * The only external link is the provider consent URL, and only if https.
// Language: navigator.language "es*" → Spanish, anything else → English.
(function () {
  'use strict';

  var ES = /^es\b/i.test((navigator && navigator.language) || '');
  var L = {
    es: {
      title: 'Conectores',
      subtitle: 'Conecta tus servicios. Los tokens se guardan solo en el llavero de este Mac.',
      nav_home: 'Panel',
      keychain_off: 'No hay llavero del sistema disponible: no se puede conectar nada (los tokens nunca se guardan en un fichero).',
      privacy: 'Privacidad y permisos',
      p_keychain: 'Tokens solo en el llavero del sistema; nunca en la base de datos ni en ficheros.',
      p_scopes: 'Se piden los permisos mínimos: solo lectura salvo que actives la escritura.',
      p_confirm: 'Cualquier escritura (enviar un correo, publicar un mensaje…) espera a que la confirmes aquí.',
      p_telemetry: 'Sin telemetría: el registro de auditoría se queda en este Mac.',
      legacy: 'Tienes {0} conexión(es) con token en la vista «Conexiones».',
      pending: 'Pendiente de confirmar', no_pending: 'No hay escrituras pendientes.',
      connectors: 'Conectores',
      no_connectors: 'Aún no hay conectores instalados. El marco está listo; los conectores (Google, Microsoft, Slack, Notion…) llegarán en próximas versiones.',
      audit: 'Registro de auditoría (local)', refresh: 'Actualizar',
      when: 'Cuándo', connector: 'Conector', event: 'Evento', detail: 'Detalle',
      s_connected: 'conectado', s_disconnected: 'desconectado', s_error: 'error',
      perm_read: 'solo lectura', perm_write: 'lectura y escritura',
      read_scopes: 'Permisos de lectura', write_scopes: 'Permisos de escritura',
      client_id: 'Client ID de tu app OAuth (BYOK)', client_secret: 'Client secret (este proveedor lo exige)',
      save: 'Guardar', clear: 'Quitar',
      connect_read: 'Conectar (solo lectura)', connect_write: 'Conectar con escritura',
      disconnect: 'Desconectar', make_read: 'Pasar a solo lectura',
      open_consent: 'Abrir la página de autorización', waiting: 'Esperando la autorización en el navegador…',
      connected_ok: 'Conectado.', flow_failed: 'No se pudo conectar: {0}',
      confirm: 'Confirmar y ejecutar', reject: 'Rechazar', expires: 'caduca {0}',
      confirm_disconnect: '¿Desconectar {0}? Se borrará el token del llavero.',
      account: 'Cuenta: {0}', error: 'Error: {0}', byok_needed: 'Necesitas tu propio client ID para conectar.',
      mcp_title: 'Servidores MCP',
      mcp_hint: 'Las herramientas son de solo lectura por defecto. Las que escriben o tienen efectos esperan tu confirmación. Lo que devuelven se trata como contenido no confiable.',
      mcp_add: 'Añadir o editar un servidor', mcp_name: 'Nombre', mcp_transport: 'Transporte',
      mcp_command: 'Comando (programa y argumentos, sin shell)', mcp_url: 'URL (https)',
      mcp_env: 'Variables no secretas (CLAVE=valor, una por línea)',
      mcp_secret_name: 'Variable secreta (al llavero)', mcp_secret_value: 'Valor', mcp_token: 'Token Bearer (al llavero)',
      mcp_none: 'No hay servidores MCP.', mcp_refresh: 'Probar conexión', mcp_remove: 'Eliminar',
      mcp_confirm_remove: '¿Eliminar el servidor MCP {0}? Se borrarán sus secretos del llavero.',
      mcp_allow_write: 'Permitir escrituras (con confirmación)', mcp_read_only: 'Volver a solo lectura',
      mcp_tools: 'Herramientas', mcp_changed: 'cambió: revísala', mcp_legacy: 'Variables guardadas por la vista antigua: vuelve a introducirlas aquí para moverlas al llavero.',
      mcp_secret_set: 'en el llavero', mcp_secret_missing: 'falta', mcp_token_set: 'token en el llavero',
      k_read: 'lectura', k_write: 'escritura', m_auto: 'automático', m_read: 'lectura', m_write: 'escritura', m_disabled: 'desactivada'
    },
    en: {
      title: 'Connectors',
      subtitle: 'Connect your services. Tokens are stored only in this Mac\u2019s keychain.',
      nav_home: 'Dashboard',
      keychain_off: 'No system keychain available: nothing can be connected (tokens are never stored in a file).',
      privacy: 'Privacy & permissions',
      p_keychain: 'Tokens only in the system keychain; never in the database or in files.',
      p_scopes: 'Minimal permissions: read-only unless you enable write.',
      p_confirm: 'Every write (sending an email, posting a message…) waits for your confirmation here.',
      p_telemetry: 'No telemetry: the audit log stays on this Mac.',
      legacy: 'You have {0} token connection(s) in the \u201cConnections\u201d view.',
      pending: 'Waiting for confirmation', no_pending: 'No pending writes.',
      connectors: 'Connectors',
      no_connectors: 'No connectors installed yet. The framework is ready; connectors (Google, Microsoft, Slack, Notion…) ship in upcoming releases.',
      audit: 'Audit log (local)', refresh: 'Refresh',
      when: 'When', connector: 'Connector', event: 'Event', detail: 'Detail',
      s_connected: 'connected', s_disconnected: 'disconnected', s_error: 'error',
      perm_read: 'read-only', perm_write: 'read & write',
      read_scopes: 'Read permissions', write_scopes: 'Write permissions',
      client_id: 'Client ID of your OAuth app (BYOK)', client_secret: 'Client secret (required by this provider)',
      save: 'Save', clear: 'Remove',
      connect_read: 'Connect (read-only)', connect_write: 'Connect with write',
      disconnect: 'Disconnect', make_read: 'Make read-only',
      open_consent: 'Open the authorization page', waiting: 'Waiting for authorization in the browser…',
      connected_ok: 'Connected.', flow_failed: 'Could not connect: {0}',
      confirm: 'Confirm and run', reject: 'Reject', expires: 'expires {0}',
      confirm_disconnect: 'Disconnect {0}? The token will be removed from the keychain.',
      account: 'Account: {0}', error: 'Error: {0}', byok_needed: 'You need your own client ID to connect.',
      mcp_title: 'MCP servers',
      mcp_hint: 'Tools are read-only by default. Tools that write or have side effects wait for your confirmation. What they return is treated as untrusted content.',
      mcp_add: 'Add or edit a server', mcp_name: 'Name', mcp_transport: 'Transport',
      mcp_command: 'Command (program and arguments, no shell)', mcp_url: 'URL (https)',
      mcp_env: 'Non-secret variables (KEY=value, one per line)',
      mcp_secret_name: 'Secret variable (to the keychain)', mcp_secret_value: 'Value', mcp_token: 'Bearer token (to the keychain)',
      mcp_none: 'No MCP servers.', mcp_refresh: 'Test connection', mcp_remove: 'Remove',
      mcp_confirm_remove: 'Remove MCP server {0}? Its secrets will be deleted from the keychain.',
      mcp_allow_write: 'Allow writes (with confirmation)', mcp_read_only: 'Back to read-only',
      mcp_tools: 'Tools', mcp_changed: 'changed: review it', mcp_legacy: 'Variables saved by the old view: re-enter them here to move them to the keychain.',
      mcp_secret_set: 'in keychain', mcp_secret_missing: 'missing', mcp_token_set: 'token in keychain',
      k_read: 'read', k_write: 'write', m_auto: 'automatic', m_read: 'read', m_write: 'write', m_disabled: 'disabled'
    }
  };
  var T = ES ? L.es : L.en;
  function t(key) {
    var s = Object.prototype.hasOwnProperty.call(T, key) ? T[key] : key;
    for (var i = 1; i < arguments.length; i++) s = s.replace('{' + (i - 1) + '}', String(arguments[i]));
    return s;
  }
  function $(id) { return document.getElementById(id); }
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  }
  function clear(n) { while (n.firstChild) n.removeChild(n.firstChild); }
  function fmtTime(ts) { try { return new Date(ts * 1000).toLocaleString(ES ? 'es' : 'en'); } catch (e) { return String(ts); } }

  function showError(msg) { var g = $('global-error'); g.textContent = msg ? t('error', msg) : ''; g.hidden = !msg; }

  function api(path, body) {
    var opts = { credentials: 'same-origin', headers: { 'Accept': 'application/json' } };
    if (body !== undefined) {
      opts.method = 'POST';
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    return fetch(path, opts).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        if (!r.ok || j.ok === false) throw new Error((j && (j.message || j.error)) || ('HTTP ' + r.status));
        return j;
      });
    });
  }

  function applyStatic() {
    document.documentElement.lang = ES ? 'es' : 'en';
    document.title = t('title') + ' — ConductLoop';
    var nodes = document.querySelectorAll('[data-i18n]');
    for (var i = 0; i < nodes.length; i++) nodes[i].textContent = t(nodes[i].getAttribute('data-i18n'));
  }

  function scopeList(label, scopes) {
    var wrap = el('div');
    wrap.appendChild(el('div', 'hint', label));
    var box = el('div', 'scopes');
    (scopes || []).forEach(function (s) { box.appendChild(el('span', 'badge mono', s)); });
    wrap.appendChild(box);
    return wrap;
  }

  function button(label, cls, onClick) {
    var b = el('button', cls, label);
    b.type = 'button';
    b.addEventListener('click', onClick);
    return b;
  }

  function pollFlow(state, statusNode, done) {
    var tries = 0;
    function tick() {
      api('/api/connectors/flows/' + encodeURIComponent(state)).then(function (f) {
        if (f.status === 'connected') { statusNode.textContent = t('connected_ok'); done(); return; }
        if (f.status === 'error' || f.status === 'expired') { statusNode.textContent = t('flow_failed', f.error || f.status); done(); return; }
        if (++tries < 300) setTimeout(tick, 1500);
      }).catch(function (e) { statusNode.textContent = t('flow_failed', e.message); });
    }
    tick();
  }

  function startConnect(c, permission, statusNode) {
    showError('');
    api('/api/connectors/' + encodeURIComponent(c.id) + '/connect', { permission: permission }).then(function (r) {
      clear(statusNode);
      var url = String(r.authorize_url || '');
      if (!/^https:\/\//i.test(url)) { showError('bad authorize_url'); return; }
      var a = el('a', 'link', t('open_consent'));
      a.href = url; a.target = '_blank'; a.rel = 'noopener noreferrer';
      a.setAttribute('data-testid', 'consent-link');
      statusNode.appendChild(a);
      var w = el('div', 'hint', t('waiting'));
      statusNode.appendChild(w);
      pollFlow(r.state, w, load);
    }).catch(function (e) { showError(e.message); });
  }

  function renderConnector(c) {
    if (c.auth === 'token' && window.CLToken) return window.CLToken.render(c, { el: el, button: button, api: api, t: t, load: load, showError: showError, clear: clear, scopeList: scopeList, ES: ES });
    var card = el('div', 'conn');
    card.setAttribute('data-connector', c.id);
    var head = el('div', 'row between');
    var title = el('div', 'row');
    title.appendChild(el('h3', null, c.name));
    var st = el('span', 'badge ' + (c.status === 'connected' ? 'connected' : (c.status === 'error' ? 'error' : '')), t('s_' + c.status));
    st.setAttribute('data-testid', 'status');
    title.appendChild(st);
    if (c.status === 'connected') {
      var p = el('span', 'badge' + (c.permission === 'write' ? ' write' : ''), t('perm_' + c.permission));
      p.setAttribute('data-testid', 'permission');
      title.appendChild(p);
    }
    head.appendChild(title);
    card.appendChild(head);
    if (c.account_label) card.appendChild(el('div', 'hint', t('account', c.account_label)));
    if (c.last_error) card.appendChild(el('div', 'hint', t('error', c.last_error)));
    card.appendChild(scopeList(t('read_scopes'), c.read_scopes));
    if (c.write_scopes && c.write_scopes.length) card.appendChild(scopeList(t('write_scopes'), c.write_scopes));

    if (c.auth === 'oauth_pkce') {
      var form = el('div', 'row');
      var lab = el('label', 'hint', t('client_id'));
      var inp = el('input'); inp.type = 'text'; inp.value = c.client_id || ''; inp.autocomplete = 'off';
      inp.setAttribute('aria-label', t('client_id'));
      lab.appendChild(document.createElement('br')); lab.appendChild(inp);
      form.appendChild(lab);
      var sec = null;
      if (c.client_secret_required) {
        var lab2 = el('label', 'hint', t('client_secret'));
        sec = el('input'); sec.type = 'password'; sec.autocomplete = 'off';
        sec.setAttribute('aria-label', t('client_secret'));
        lab2.appendChild(document.createElement('br')); lab2.appendChild(sec);
        form.appendChild(lab2);
      }
      form.appendChild(button(t('save'), 'ghost small', function () {
        var body = { client_id: inp.value.trim() };
        if (sec && sec.value) body.client_secret = sec.value;
        api('/api/connectors/' + encodeURIComponent(c.id) + '/client', body).then(load).catch(function (e) { showError(e.message); });
      }));
      card.appendChild(form);
      if (!c.client_id_set) card.appendChild(el('div', 'hint', t('byok_needed')));
    }

    var actions = el('div', 'row');
    var statusNode = el('div', 'hint');
    statusNode.setAttribute('data-testid', 'flow');
    if (c.status !== 'connected') {
      var b1 = button(t('connect_read'), 'small', function () { startConnect(c, 'read', statusNode); });
      b1.disabled = !c.client_id_set;
      actions.appendChild(b1);
      if (c.write_scopes && c.write_scopes.length) {
        var b2 = button(t('connect_write'), 'ghost small', function () { startConnect(c, 'write', statusNode); });
        b2.disabled = !c.client_id_set;
        actions.appendChild(b2);
      }
    } else {
      if (c.permission === 'write') {
        actions.appendChild(button(t('make_read'), 'ghost small', function () {
          api('/api/connectors/' + encodeURIComponent(c.id) + '/permission', { permission: 'read' }).then(load).catch(function (e) { showError(e.message); });
        }));
      } else if (c.write_scopes && c.write_scopes.length) {
        actions.appendChild(button(t('connect_write'), 'ghost small', function () { startConnect(c, 'write', statusNode); }));
      }
      actions.appendChild(button(t('disconnect'), 'danger small', function () {
        if (!window.confirm(t('confirm_disconnect', c.name))) return;
        api('/api/connectors/' + encodeURIComponent(c.id) + '/disconnect', {}).then(load).catch(function (e) { showError(e.message); });
      }));
    }
    card.appendChild(actions);
    card.appendChild(statusNode);
    return card;
  }

  function renderPending(list) {
    var ul = $('pending-list'); clear(ul);
    $('pending-count').textContent = String(list.length);
    $('pending-empty').hidden = list.length > 0;
    list.forEach(function (a) {
      var li = el('li');
      li.setAttribute('data-action-id', String(a.id));
      li.appendChild(el('div', null, a.summary));
      li.appendChild(el('div', 'hint mono', a.connector_id + ' · ' + a.action + ' · ' + t('expires', fmtTime(a.expires_at))));
      li.appendChild(el('pre', 'payload mono', JSON.stringify(a.payload, null, 2)));
      li.appendChild(el('div', 'hint mono', 'sha256 ' + String(a.payload_hash).slice(0, 16) + '…'));
      var row = el('div', 'row');
      row.appendChild(button(t('confirm'), 'ok small', function () {
        api('/api/connectors/actions/' + a.id + '/confirm', { payload_hash: a.payload_hash }).then(load).catch(function (e) { showError(e.message); load(); });
      }));
      row.appendChild(button(t('reject'), 'ghost small', function () {
        api('/api/connectors/actions/' + a.id + '/reject', {}).then(load).catch(function (e) { showError(e.message); });
      }));
      li.appendChild(row);
      ul.appendChild(li);
    });
  }

  function renderAudit(events) {
    var tb = $('audit-body'); clear(tb);
    events.forEach(function (ev) {
      var tr = el('tr');
      tr.appendChild(el('td', null, fmtTime(ev.ts)));
      tr.appendChild(el('td', 'mono', ev.connector_id));
      tr.appendChild(el('td', 'mono', ev.event));
      tr.appendChild(el('td', 'mono', JSON.stringify(ev.detail)));
      tb.appendChild(tr);
    });
  }


  // ── MCP servers ────────────────────────────────────────────────────────
  function mcpUrl(name, tail) { return '/api/connectors/mcp/servers/' + encodeURIComponent(name) + (tail || ''); }

  function renderMcpServer(sv) {
    var card = el('div', 'conn');
    card.setAttribute('data-mcp', sv.name);
    var head = el('div', 'row');
    head.appendChild(el('h3', null, sv.name));
    var st = el('span', 'badge ' + (sv.status === 'connected' ? 'connected' : (sv.status === 'error' ? 'error' : '')), t('s_' + (sv.status || 'error')));
    st.setAttribute('data-testid', 'mcp-status');
    head.appendChild(st);
    var perm = el('span', 'badge' + (sv.permission === 'write' ? ' write' : ''), t('perm_' + (sv.permission || 'read')));
    perm.setAttribute('data-testid', 'mcp-permission');
    head.appendChild(perm);
    card.appendChild(head);
    card.appendChild(el('div', 'hint mono', sv.transport === 'http' ? sv.url : (sv.command || []).join(' ')));
    if (sv.server_label) card.appendChild(el('div', 'hint', sv.server_label));
    if (sv.last_error) card.appendChild(el('div', 'hint', t('error', sv.last_error)));
    if (sv.legacy_env) card.appendChild(el('div', 'hint', t('mcp_legacy')));
    var secrets = el('div', 'scopes');
    (sv.secret_vars || []).forEach(function (v) {
      secrets.appendChild(el('span', 'badge mono' + (v.set ? '' : ' error'), v.name + ' · ' + t(v.set ? 'mcp_secret_set' : 'mcp_secret_missing')));
    });
    if (sv.token_set) secrets.appendChild(el('span', 'badge mono', t('mcp_token_set')));
    card.appendChild(secrets);

    if (sv.tools && sv.tools.length) {
      var tbl = el('table', 'table');
      var tb = el('tbody');
      sv.tools.forEach(function (tool) {
        var tr = el('tr');
        tr.setAttribute('data-tool', tool.name);
        var c1 = el('td', 'mono', tool.name);
        if (tool.changed) { c1.appendChild(document.createTextNode(' ')); c1.appendChild(el('span', 'badge warn', t('mcp_changed'))); }
        tr.appendChild(c1);
        tr.appendChild(el('td', null, tool.description));
        var kb = el('td'); kb.appendChild(el('span', 'badge' + (tool.kind === 'write' ? ' write' : ''), tool.kind ? t('k_' + tool.kind) : t('m_disabled')));
        tr.appendChild(kb);
        var sel = el('select');
        sel.setAttribute('aria-label', tool.name);
        ['auto', 'read', 'write', 'disabled'].forEach(function (m) {
          var o = el('option', null, t('m_' + m)); o.value = m; if (m === tool.mode) o.selected = true; sel.appendChild(o);
        });
        sel.addEventListener('change', function () {
          api(mcpUrl(sv.name, '/tools/' + encodeURIComponent(tool.name) + '/mode'), { mode: sel.value }).then(load).catch(function (e) { showError(e.message); });
        });
        var c4 = el('td'); c4.appendChild(sel); tr.appendChild(c4);
        tb.appendChild(tr);
      });
      tbl.appendChild(tb);
      card.appendChild(el('div', 'hint', t('mcp_tools')));
      card.appendChild(tbl);
    }
    var row = el('div', 'row');
    row.appendChild(button(t('mcp_refresh'), 'small', function () {
      api(mcpUrl(sv.name, '/refresh'), {}).then(load).catch(function (e) { showError(e.message); load(); });
    }));
    row.appendChild(button(sv.permission === 'write' ? t('mcp_read_only') : t('mcp_allow_write'), 'ghost small', function () {
      api(mcpUrl(sv.name, '/permission'), { permission: sv.permission === 'write' ? 'read' : 'write' }).then(load).catch(function (e) { showError(e.message); });
    }));
    row.appendChild(button(t('mcp_remove'), 'danger small', function () {
      if (!window.confirm(t('mcp_confirm_remove', sv.name))) return;
      api(mcpUrl(sv.name, '/remove'), {}).then(load).catch(function (e) { showError(e.message); });
    }));
    card.appendChild(row);
    return card;
  }

  function renderMcp(data) {
    var list = $('mcp-list'); clear(list);
    if (!data.servers.length) list.appendChild(el('p', 'hint', t('mcp_none')));
    data.servers.forEach(function (sv) { list.appendChild(renderMcpServer(sv)); });
  }

  function parseEnv(text) {
    var out = {};
    String(text || '').split('\n').forEach(function (line) {
      var i = line.indexOf('=');
      if (i > 0) out[line.slice(0, i).trim()] = line.slice(i + 1);
    });
    return out;
  }

  function syncTransport() {
    var http = $('mcp-transport').value === 'http';
    $('mcp-command-row').hidden = http; $('mcp-env-row').hidden = http; $('mcp-secret-row').hidden = http;
    $('mcp-url-row').hidden = !http; $('mcp-token-row').hidden = !http;
  }

  function saveMcp() {
    showError('');
    var body = { name: $('mcp-name').value.trim(), transport: $('mcp-transport').value };
    if (body.transport === 'http') {
      body.url = $('mcp-url').value.trim();
      if ($('mcp-token').value) body.token = $('mcp-token').value;
    } else {
      body.command = $('mcp-command').value;
      body.env = parseEnv($('mcp-env').value);
      var sn = $('mcp-secret-name').value.trim();
      if (sn && $('mcp-secret-value').value) { body.secret_env = {}; body.secret_env[sn] = $('mcp-secret-value').value; }
    }
    api('/api/connectors/mcp/servers', body).then(function () {
      $('mcp-secret-value').value = ''; $('mcp-token').value = '';
      load();
    }).catch(function (e) { showError(e.message); });
  }

  function load() {
    return Promise.all([
      api('/api/connectors'), api('/api/connectors/actions?status=pending'), api('/api/connectors/audit?limit=50'),
      api('/api/connectors/mcp')
    ]).then(function (res) {
      renderMcp(res[3]);
      var data = res[0];
      $('keychain-warning').hidden = !!data.keychain;
      var lh = $('legacy-hint');
      lh.hidden = !data.legacy_token_connections;
      lh.textContent = t('legacy', data.legacy_token_connections || 0);
      var list = $('connectors-list'); clear(list);
      $('connectors-empty').hidden = data.connectors.length > 0;
      data.connectors.forEach(function (c) { list.appendChild(renderConnector(c)); });
      renderPending(res[1].actions || []);
      renderAudit(res[2].events || []);
    }).catch(function (e) { showError(e.message); });
  }

  document.addEventListener('DOMContentLoaded', function () {
    applyStatic();
    $('audit-refresh').addEventListener('click', load);
    $('mcp-transport').addEventListener('change', syncTransport);
    $('mcp-save').addEventListener('click', saveMcp);
    syncTransport();
    load();
  });
})();
