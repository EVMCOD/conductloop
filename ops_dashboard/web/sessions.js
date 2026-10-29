// Multi-agent sessions page (PR 8). Talks only to the /api/sessions API (#23).
//
// Security rules for this file (enforced by tests/test_sessions_ui_static.py):
//   * Everything coming from the server (paths, model output, messages, diffs,
//     events, errors) is untrusted: it is only ever written with textContent /
//     createTextNode / setAttribute on inert attributes. No innerHTML,
//     outerHTML, insertAdjacentHTML, document.write, eval or string handlers.
//   * Only key_ref (a keychain item name) is ever shown or sent, never a key.
// Language: same rule as provider-errors.js / the BYOK banner (#15/#16):
// navigator.language "es*" → Spanish, anything else → English.
(function () {
  'use strict';

  var ES = /^es\b/i.test((navigator && navigator.language) || '');
  var L = {
    en: {
      title: 'Agent sessions',
      subtitle: 'Parallel coding sessions on their own worktrees. Nothing is merged or pushed automatically.',
      nav_home: 'Dashboard', nav_ai: 'Connect AI', nav_runs: 'Runs',
      repos: 'Marked repositories', repo_path_ph: '/path/to/repo', mark: 'Mark', unmark: 'Unmark',
      no_repos: 'No repository marked yet.', enabled: 'enabled', disabled: 'disabled',
      prices: 'Prices per model (EUR / 1M tokens)', provider: 'Provider', model: 'Model',
      price_in: 'In', price_out: 'Out', price_in_ph: 'EUR in / 1M', price_out_ph: 'EUR out / 1M',
      save: 'Save', no_prices: 'No prices yet. Sessions without a price pause before spending.',
      new_session: 'New session', role: 'Role', key_ref: 'Key reference (key_ref)',
      key_ref_hint: 'Name of a key saved in the keychain (e.g. byok.openai.work), never the key itself. Required for anthropic, openai, minimax and gateway.',
      repo: 'Repository', base: 'Base branch', budget_eur: 'Budget (EUR)', max_iter: 'Max iterations',
      create: 'Create', inbox: 'Messages to you', load_more: 'Load more', sessions: 'Sessions',
      no_sessions: 'No sessions yet. Mark a repository, set a price and create one.',
      running_count: '{0} running (max {1})',
      start: 'Start', stop: 'Stop', close: 'Close', resume: 'Resume', open: 'Open',
      spent: 'Spent', of: 'of', incl_est: 'includes estimates', real_usage: 'real usage',
      iterations: 'Iterations', heartbeat: 'Heartbeat', never: 'never', ago: '{0} ago',
      resume_title: 'Resume session', new_budget: 'New budget (EUR)',
      confirm_stalled: 'I checked the worktree: resume this stalled session',
      cancel: 'Cancel', tasks: 'Task queue', no_tasks: 'No tasks for this session.',
      task_title: 'Task title', task_prompt: 'What should the session do?',
      task_paths: 'Paths it will touch (comma separated globs, optional)', add_task: 'Add task',
      review: 'Review', review_btn: 'Review',
      accept_no_merge: 'Accepting does NOT merge anything: it only closes the task and frees the session. The branch stays for you to integrate.',
      review_note: 'Note (optional)', accept: 'Accept (no merge)', reject: 'Reject',
      diff: 'Diff vs base', load_diff: 'Load diff', no_changes: 'No changes vs base.',
      truncated: 'Diff truncated (256 KB).', uncommitted: 'Uncommitted in the worktree: {0}',
      claims: 'Path claims', no_claims: 'No active claims.', release: 'Release', release_all: 'Release all',
      messages: 'Messages', no_messages: 'No messages.', message_ph: 'Message to the session', send: 'Send',
      events: 'Events', no_events: 'No events.', you: 'you', session_n: 'session #{0}', task_n: 'task #{0}',
      confirm: 'Confirm', ok: 'OK',
      confirm_close: 'Close session #{0}? The worktree is kept; an unfinished task goes back to the queue.',
      confirm_stalled_dialog: 'Session #{0} stalled (no heartbeat). Resume it only if you checked its worktree. Resume now?',
      confirm_accept: 'Accept task #{0}? This does NOT merge: the branch and its diff stay as they are.',
      confirm_reject: 'Reject task #{0}? It is marked failed and the session is freed.',
      confirm_cancel_task: 'Cancel task #{0}?',
      confirm_release_all: 'Release every claim of session #{0}?',
      accepted: 'Task #{0} accepted. Nothing was merged.', rejected: 'Task #{0} rejected.',
      reason_paused: 'Paused: {0}', reason_stalled: 'Stalled: confirm explicitly to resume.',
      reason_waiting: 'Waiting for you: {0}',
      need_stalled_confirm: 'Tick the confirmation to resume a stalled session.',
      err_network: 'Could not reach the local server.',
      err_bad_response: 'Unexpected response from the server (HTTP {0}).',
      err_validation: 'Invalid value: {0}', err_not_found: 'Not found.',
      err_forbidden_origin: 'Request refused: it did not come from this app.',
      err_limit: 'Limit reached: {0}', err_path_conflict: 'Path conflict: {0}',
      err_invalid_state: 'Not possible right now: {0}',
      err_platform: 'Not available on this system: {0}',
      code_off_platform: 'Code sessions are turned off on this system (Windows): there is no OS sandbox for running commands yet, and ConductLoop never runs them without one.',
      code_off_sandbox: 'Running commands is turned off: the OS sandbox (bubblewrap on Linux) is missing or does not work. Agents can still read and edit files, but cannot run tests.',
      err_price_missing: 'Set a price for this model first (Prices panel), then resume.',
      err_budget_exhausted: 'Budget exhausted: raise the budget to resume.',
      err_iteration_limit: 'Iteration limit reached: raise max iterations to resume.',
      err_confirm_required: 'This session stalled: confirm explicitly to resume it.',
      err_worktree: 'Worktree error: {0}', err_not_a_repo: 'Not a git repository root: {0}',
      err_auth: 'Authentication required.', err_generic: 'Error: {0}',
      st_created: 'created', st_provisioning: 'provisioning', st_idle: 'idle', st_running: 'running',
      st_waiting_human: 'waiting for you', st_paused: 'paused', st_stalled: 'stalled',
      st_closing: 'closing', st_closed: 'closed', st_failed: 'failed',
      ts_queued: 'queued', ts_claimed: 'claimed', ts_running: 'running', ts_needs_review: 'needs review',
      ts_done: 'done', ts_blocked: 'blocked', ts_failed: 'failed', ts_cancelled: 'cancelled'
    },
    es: {
      title: 'Sesiones de agentes',
      subtitle: 'Sesiones de código en paralelo, cada una en su worktree. Nada se fusiona ni se sube automáticamente.',
      nav_home: 'Panel', nav_ai: 'Conectar IA', nav_runs: 'Ejecuciones',
      repos: 'Repositorios marcados', repo_path_ph: '/ruta/al/repo', mark: 'Marcar', unmark: 'Desmarcar',
      no_repos: 'Aún no hay repositorios marcados.', enabled: 'activo', disabled: 'desactivado',
      prices: 'Precios por modelo (EUR / 1M tokens)', provider: 'Proveedor', model: 'Modelo',
      price_in: 'Entrada', price_out: 'Salida', price_in_ph: 'EUR entrada / 1M', price_out_ph: 'EUR salida / 1M',
      save: 'Guardar', no_prices: 'Sin precios. Las sesiones sin precio se pausan antes de gastar.',
      new_session: 'Nueva sesión', role: 'Rol', key_ref: 'Referencia de clave (key_ref)',
      key_ref_hint: 'Nombre de una clave guardada en el llavero (p. ej. byok.openai.trabajo), nunca la clave. Obligatoria para anthropic, openai, minimax y gateway.',
      repo: 'Repositorio', base: 'Rama base', budget_eur: 'Presupuesto (EUR)', max_iter: 'Iteraciones máx.',
      create: 'Crear', inbox: 'Mensajes para ti', load_more: 'Cargar más', sessions: 'Sesiones',
      no_sessions: 'Aún no hay sesiones. Marca un repositorio, pon un precio y crea una.',
      running_count: '{0} en marcha (máx. {1})',
      start: 'Arrancar', stop: 'Parar', close: 'Cerrar', resume: 'Reanudar', open: 'Abrir',
      spent: 'Gastado', of: 'de', incl_est: 'incluye estimaciones', real_usage: 'uso real',
      iterations: 'Iteraciones', heartbeat: 'Latido', never: 'nunca', ago: 'hace {0}',
      resume_title: 'Reanudar sesión', new_budget: 'Nuevo presupuesto (EUR)',
      confirm_stalled: 'He revisado el worktree: reanudar esta sesión atascada',
      cancel: 'Cancelar', tasks: 'Cola de tareas', no_tasks: 'Esta sesión no tiene tareas.',
      task_title: 'Título de la tarea', task_prompt: '¿Qué debe hacer la sesión?',
      task_paths: 'Rutas que tocará (globs separados por comas, opcional)', add_task: 'Añadir tarea',
      review: 'Revisión', review_btn: 'Revisar',
      accept_no_merge: 'Aceptar NO fusiona nada: solo cierra la tarea y libera la sesión. La rama queda para que la integres tú.',
      review_note: 'Nota (opcional)', accept: 'Aceptar (sin fusionar)', reject: 'Rechazar',
      diff: 'Diff contra la base', load_diff: 'Cargar diff', no_changes: 'Sin cambios respecto a la base.',
      truncated: 'Diff truncado (256 KB).', uncommitted: 'Sin commitear en el worktree: {0}',
      claims: 'Reservas de rutas', no_claims: 'Sin reservas activas.', release: 'Liberar', release_all: 'Liberar todas',
      messages: 'Mensajes', no_messages: 'Sin mensajes.', message_ph: 'Mensaje para la sesión', send: 'Enviar',
      events: 'Eventos', no_events: 'Sin eventos.', you: 'tú', session_n: 'sesión #{0}', task_n: 'tarea #{0}',
      confirm: 'Confirmar', ok: 'Aceptar',
      confirm_close: '¿Cerrar la sesión #{0}? El worktree se conserva; una tarea sin terminar vuelve a la cola.',
      confirm_stalled_dialog: 'La sesión #{0} se atascó (sin latido). Reanúdala solo si has revisado su worktree. ¿Reanudar ahora?',
      confirm_accept: '¿Aceptar la tarea #{0}? Esto NO fusiona: la rama y su diff quedan como están.',
      confirm_reject: '¿Rechazar la tarea #{0}? Se marca como fallida y la sesión queda libre.',
      confirm_cancel_task: '¿Cancelar la tarea #{0}?',
      confirm_release_all: '¿Liberar todas las reservas de la sesión #{0}?',
      accepted: 'Tarea #{0} aceptada. No se ha fusionado nada.', rejected: 'Tarea #{0} rechazada.',
      reason_paused: 'En pausa: {0}', reason_stalled: 'Atascada: confirma explícitamente para reanudar.',
      reason_waiting: 'Esperando por ti: {0}',
      need_stalled_confirm: 'Marca la confirmación para reanudar una sesión atascada.',
      err_network: 'No se pudo contactar con el servidor local.',
      err_bad_response: 'Respuesta inesperada del servidor (HTTP {0}).',
      err_validation: 'Valor no válido: {0}', err_not_found: 'No encontrado.',
      err_forbidden_origin: 'Petición rechazada: no viene de esta aplicación.',
      err_limit: 'Límite alcanzado: {0}', err_path_conflict: 'Conflicto de rutas: {0}',
      err_invalid_state: 'No es posible ahora mismo: {0}',
      err_platform: 'No disponible en este sistema: {0}',
      code_off_platform: 'Las sesiones de código están desactivadas en este sistema (Windows): todavía no hay sandbox del sistema para ejecutar comandos, y ConductLoop nunca los ejecuta sin uno.',
      code_off_sandbox: 'La ejecución de comandos está desactivada: falta el sandbox del sistema (bubblewrap en Linux) o no funciona. Los agentes pueden leer y editar archivos, pero no ejecutar tests.',
      err_price_missing: 'Pon primero un precio para este modelo (panel Precios) y reanuda.',
      err_budget_exhausted: 'Presupuesto agotado: súbelo para reanudar.',
      err_iteration_limit: 'Límite de iteraciones alcanzado: súbelo para reanudar.',
      err_confirm_required: 'Esta sesión se atascó: confirma explícitamente para reanudarla.',
      err_worktree: 'Error del worktree: {0}', err_not_a_repo: 'No es la raíz de un repositorio git: {0}',
      err_auth: 'Hace falta iniciar sesión.', err_generic: 'Error: {0}',
      st_created: 'creada', st_provisioning: 'preparando', st_idle: 'inactiva', st_running: 'en marcha',
      st_waiting_human: 'esperando por ti', st_paused: 'en pausa', st_stalled: 'atascada',
      st_closing: 'cerrando', st_closed: 'cerrada', st_failed: 'fallida',
      ts_queued: 'en cola', ts_claimed: 'reclamada', ts_running: 'en curso', ts_needs_review: 'pendiente de revisión',
      ts_done: 'hecha', ts_blocked: 'bloqueada', ts_failed: 'fallida', ts_cancelled: 'cancelada'
    }
  };
  var LANG = ES ? 'es' : 'en';

  function t(key) {
    var s = L[LANG][key];
    if (s == null) s = L.en[key];
    if (s == null) s = key;
    for (var i = 1; i < arguments.length; i++) s = s.split('{' + (i - 1) + '}').join(String(arguments[i]));
    return s;
  }

  var SESSION_STATUSES = ['created', 'provisioning', 'idle', 'running', 'waiting_human', 'paused',
    'stalled', 'closing', 'closed', 'failed'];
  var TASK_STATUSES = ['queued', 'claimed', 'running', 'needs_review', 'done', 'blocked', 'failed', 'cancelled'];
  var PROVIDERS = ['anthropic', 'openai', 'minimax', 'gateway', 'ollama', 'mock'];
  var RESUMABLE = ['paused', 'waiting_human', 'stalled'];
  var PAGE = 50;

  // ── DOM helpers: text only ────────────────────────────────────────────────
  function $(id) { return document.getElementById(id); }
  function h(tag, props) {
    var el = document.createElement(tag);
    if (props) {
      Object.keys(props).forEach(function (k) {
        var v = props[k];
        if (v == null || v === false) return;
        if (k === 'class') el.className = v;
        else if (k === 'text') el.textContent = String(v);
        else if (k === 'on') Object.keys(v).forEach(function (ev) { el.addEventListener(ev, v[ev]); });
        else if (k === 'dataset') Object.keys(v).forEach(function (d) { el.dataset[d] = String(v[d]); });
        else if (k === 'disabled' || k === 'checked' || k === 'hidden') el[k] = !!v;
        else el.setAttribute(k, String(v));
      });
    }
    for (var i = 2; i < arguments.length; i++) add(el, arguments[i]);
    return el;
  }
  function add(el, c) {
    if (c == null || c === false) return;
    if (Array.isArray(c)) { c.forEach(function (x) { add(el, x); }); return; }
    el.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); }
  function show(el, on) { el.hidden = !on; }
  function safeClass(value, allowed, prefix) {
    return allowed.indexOf(value) >= 0 ? prefix + value : prefix + 'unknown';
  }
  function eur(cents) {
    var n = Number(cents) / 100;
    return (isFinite(n) ? n : 0).toLocaleString(ES ? 'es-ES' : 'en-US',
      { style: 'currency', currency: 'EUR', minimumFractionDigits: 2, maximumFractionDigits: n > 0 && n < 0.1 ? 4 : 2 });
  }
  function when(ts) {
    if (!ts) return '';
    return new Date(ts * 1000).toLocaleString(ES ? 'es-ES' : 'en-US');
  }
  function ago(ts) {
    if (!ts) return t('never');
    var s = Math.max(0, Math.round(Date.now() / 1000 - ts));
    var txt = s < 60 ? s + ' s' : s < 3600 ? Math.round(s / 60) + ' min' : Math.round(s / 3600) + ' h';
    return t('ago', txt);
  }
  function statusLabel(st) { return SESSION_STATUSES.indexOf(st) >= 0 ? t('st_' + st) : String(st); }
  function taskLabel(st) { return TASK_STATUSES.indexOf(st) >= 0 ? t('ts_' + st) : String(st); }

  // ── API ──────────────────────────────────────────────────────────────────
  function csrfToken() {
    var m = /(?:^|;\s*)ops_csrf=([^;]+)/.exec(document.cookie || '');
    return m ? decodeURIComponent(m[1]) : '';
  }
  function ApiFailure(status, data) { this.status = status; this.data = data || {}; }
  async function api(method, path, body) {
    var opt = { method: method, credentials: 'same-origin', headers: {} };
    if (body !== undefined) { opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body); }
    if (method !== 'GET') { var c = csrfToken(); if (c) opt.headers['X-CSRF-Token'] = c; }
    var r;
    try { r = await fetch(path, opt); } catch (e) { throw new ApiFailure(0, { error: 'network' }); }
    var data;
    try { data = await r.json(); } catch (e) { throw new ApiFailure(r.status, { error: 'bad_response' }); }
    if (!r.ok || (data && data.ok === false)) throw new ApiFailure(r.status, data);
    return data;
  }

  // ── errors (same look as provider-errors.js) ───────────────────────────────
  function errorText(err) {
    var d = (err && err.data) || {};
    var msg = typeof d.message === 'string' ? d.message : '';
    switch (d.error || d.code) {
      case 'network': return t('err_network');
      case 'bad_response': return t('err_bad_response', err.status);
      case 'validation_error': case 'invalid_json': return t('err_validation', msg || d.field || '');
      case 'not_found': return t('err_not_found');
      case 'forbidden_origin': return t('err_forbidden_origin');
      case 'limit_reached': return t('err_limit', msg);
      case 'path_conflict': return t('err_path_conflict', msg);
      case 'invalid_state': return t('err_invalid_state', msg);
      case 'platform_unsupported': return t('err_platform', msg);
      case 'price_missing': return t('err_price_missing');
      case 'budget_exhausted': return t('err_budget_exhausted');
      case 'iteration_limit': return t('err_iteration_limit');
      case 'confirm_required': return t('err_confirm_required');
      case 'worktree_error': return t('err_worktree', msg);
      case 'not_a_repo': return t('err_not_a_repo', msg);
    }
    if (err && err.status === 401) return t('err_auth');
    var PE = window.ProviderErrors;
    return t('err_generic', (PE ? PE.friendly(d) : msg) || d.error || ('HTTP ' + (err && err.status)));
  }
  function showError(target, err, opts) {
    var PE = window.ProviderErrors;
    target.hidden = false;
    if (PE && PE.providerOf(err && err.data)) { PE.show(target, err.data, opts); return; }
    target.textContent = '⚠ ' + errorText(err);
    target.style.fontStyle = 'normal';
    target.style.color = 'var(--amber,#f59e0b)';
    target.setAttribute('role', 'alert');
    target.dataset.errorCode = String(((err && err.data) || {}).error || '');
  }
  function showInfo(target, text) {
    target.hidden = false;
    target.textContent = text;
    target.style.color = 'var(--ok,#22c55e)';
    target.removeAttribute('role');
    delete target.dataset.errorCode;
  }
  function clearError(target) { target.hidden = true; target.textContent = ''; delete target.dataset.errorCode; }
  function guarded(target, fn, opts) {
    return async function (ev) {
      if (ev && ev.preventDefault) ev.preventDefault();
      clearError(target);
      try { await fn(ev); } catch (e) {
        if (e instanceof ApiFailure) showError(target, e, opts); else { console.error(e); showError(target, new ApiFailure(0, { error: 'bad_response' })); }
      }
    };
  }

  // ── confirmation dialog (no window.confirm, text only) ──────────────────────
  function confirmBox(text) {
    return new Promise(function (resolve) {
      var dlg = h('dialog', { class: 'confirm', id: 'confirm-dialog' },
        h('p', { text: text }),
        h('div', { class: 'row end' },
          h('button', { type: 'button', class: 'ghost', id: 'confirm-no', text: t('cancel'),
            on: { click: function () { done(false); } } }),
          h('button', { type: 'button', id: 'confirm-yes', text: t('confirm'),
            on: { click: function () { done(true); } } })));
      function done(v) { try { dlg.close(); } catch (e) { /* closed */ } dlg.remove(); resolve(v); }
      dlg.addEventListener('cancel', function (e) { e.preventDefault(); done(false); });
      document.body.appendChild(dlg);
      dlg.showModal();
    });
  }

  // ── state ────────────────────────────────────────────────────────────────
  var S = {
    sessions: [], selected: null, detail: null, reviewTask: null, maxRunning: 0,
    events: { items: [], after: 0, full: false, sid: null },
    messages: { items: [], after: 0, full: false, sid: null },
    inbox: { items: [], after: 0, full: false },
    taskPages: 1, tasksFull: false
  };

  // ── repos ────────────────────────────────────────────────────────────────
  var repos = [];
  async function loadRepos() {
    var d = await api('GET', '/api/sessions/repos');
    repos = d.items || [];
    var ul = $('repos'); clear(ul);
    if (!repos.length) ul.appendChild(h('li', { class: 'hint', text: t('no_repos') }));
    repos.forEach(function (r) {
      ul.appendChild(h('li', { class: 'repo' },
        h('code', { class: 'path', text: r.repo_path, title: r.repo_path }),
        h('span', { class: 'pill', text: r.enabled ? t('enabled') : t('disabled') }),
        r.enabled ? h('button', { type: 'button', class: 'ghost small', text: t('unmark'),
          on: { click: guarded($('repo-error'), async function () {
            await api('POST', '/api/sessions/repos/unmark', { path: r.repo_path });
            await loadRepos();
          }) } }) : null));
    });
    var sel = $('s-repo'); var prev = sel.value; clear(sel);
    repos.filter(function (r) { return r.enabled; }).forEach(function (r) {
      sel.appendChild(h('option', { value: r.repo_path, text: r.repo_path }));
    });
    if (prev) sel.value = prev;
  }

  // ── prices ───────────────────────────────────────────────────────────────
  async function loadPrices() {
    var d = await api('GET', '/api/sessions/prices');
    var tb = $('prices'); clear(tb);
    var items = d.items || [];
    if (!items.length) tb.appendChild(h('tr', null, h('td', { colspan: 4, class: 'hint', text: t('no_prices') })));
    items.forEach(function (p) {
      tb.appendChild(h('tr', null, h('td', { text: p.provider }), h('td', null, h('code', { text: p.model })),
        h('td', { text: eur(p.in_cents_per_mtok) }), h('td', { text: eur(p.out_cents_per_mtok) })));
    });
  }

  // ── sessions list ────────────────────────────────────────────────────────
  async function loadSessions() {
    var d = await api('GET', '/api/sessions');
    S.sessions = d.items || [];
    S.maxRunning = d.max_running || 0;
    renderSessions();
  }
  function sessionActions(s) {
    var err = $('action-error');        // not cleared by the polling loop
    var out = [];
    var terminal = s.status === 'closed' || s.status === 'failed';
    out.push(h('button', { type: 'button', class: 'ghost small', text: t('open'), dataset: { act: 'open' },
      on: { click: function () { select(s.id); } } }));
    if (!terminal && !s.running && RESUMABLE.indexOf(s.status) < 0) {
      out.push(h('button', { type: 'button', class: 'small', text: t('start'), dataset: { act: 'start' },
        on: { click: guarded(err, async function () { await api('POST', '/api/sessions/' + s.id + '/start'); await refresh(); }) } }));
    }
    if (s.running) {
      out.push(h('button', { type: 'button', class: 'small ghost', text: t('stop'), dataset: { act: 'stop' },
        on: { click: guarded(err, async function () { await api('POST', '/api/sessions/' + s.id + '/stop'); await refresh(); }) } }));
    }
    if (RESUMABLE.indexOf(s.status) >= 0) {
      out.push(h('button', { type: 'button', class: 'small', text: t('resume'), dataset: { act: 'resume' },
        on: { click: function () { select(s.id, true); } } }));
    }
    if (!terminal) {
      out.push(h('button', { type: 'button', class: 'small danger', text: t('close'), dataset: { act: 'close' },
        on: { click: guarded(err, async function () {
          if (!(await confirmBox(t('confirm_close', s.id)))) return;
          await api('POST', '/api/sessions/' + s.id + '/close'); await refresh();
        }) } }));
    }
    return out;
  }
  function budgetBar(s) {
    var spent = Number(s.cost_cents_est) || 0, total = Number(s.budget_cost_cents) || 1;
    var pct = Math.max(0, Math.min(100, spent / total * 100));
    var fill = h('div', { class: 'fill' + (pct >= 100 ? ' over' : pct >= 80 ? ' warn' : '') });
    fill.style.width = pct.toFixed(1) + '%';
    return h('div', { class: 'budget' },
      h('div', { class: 'budget-text' },
        h('span', { class: 'spent', text: t('spent') + ' ' + eur(spent) + ' ' + t('of') + ' ' + eur(total) }),
        h('span', { class: 'pill ' + (s.usage_includes_estimates ? 'est' : 'real'),
          text: s.usage_includes_estimates ? t('incl_est') : t('real_usage') })),
      h('div', { class: 'bar' }, fill));
  }
  function renderSessions() {
    var box = $('sessions'); clear(box);
    var running = S.sessions.filter(function (s) { return s.running; }).length;
    $('running-count').textContent = t('running_count', running, S.maxRunning);
    if (!S.sessions.length) { box.appendChild(h('p', { class: 'hint', text: t('no_sessions') })); return; }
    S.sessions.slice().reverse().forEach(function (s) {
      box.appendChild(h('article', { class: 'session' + (S.selected === s.id ? ' selected' : ''),
        dataset: { sid: s.id, status: s.status } },
        h('div', { class: 'row between' },
          h('div', null, h('strong', { class: 's-name', text: s.name }), ' ',
            h('span', { class: 'hint', text: '#' + s.id + ' · ' + s.role })),
          h('span', { class: 'badge ' + safeClass(s.status, SESSION_STATUSES, 'st-'), text: statusLabel(s.status) })),
        h('div', { class: 'meta' },
          h('span', { text: s.provider + ' / ' + s.model }),
          s.key_ref ? h('code', { class: 'keyref', text: s.key_ref }) : null,
          h('span', { text: t('iterations') + ': ' + s.iterations + ' / ' + s.max_iterations }),
          h('span', { text: t('heartbeat') + ': ' + ago(s.heartbeat_at), title: when(s.heartbeat_at) })),
        budgetBar(s),
        s.last_error && s.status !== 'closed' ? h('div', { class: 'last-error', text: s.last_error }) : null,
        h('div', { class: 'row actions' }, sessionActions(s))));
    });
  }

  // ── detail ───────────────────────────────────────────────────────────────
  function resetFeeds(sid) {
    S.events = { items: [], after: 0, full: false, sid: sid };
    S.messages = { items: [], after: 0, full: false, sid: sid };
    S.taskPages = 1; S.reviewTask = null;
    clear($('events')); clear($('messages')); clear($('diff')); clear($('diff-stat'));
    show($('diff'), false); show($('diff-stat'), false); $('diff-meta').textContent = '';
    show($('review'), false);
  }
  async function select(sid, openResume) {
    if (S.selected !== sid) { S.selected = sid; resetFeeds(sid); }
    show($('detail'), true);
    clearError($('detail-error'));
    renderSessions();
    await loadDetail();
    if (openResume) openResumeForm();
    $('detail').scrollIntoView({ block: 'start' });
  }
  async function loadDetail() {
    if (S.selected == null) return;
    var sid = S.selected;
    var d = await api('GET', '/api/sessions/' + sid);
    if (S.selected !== sid) return;
    S.detail = d;
    var s = d.session;
    $('detail-title').textContent = '#' + s.id + ' · ' + s.name + ' — ' + statusLabel(s.status);
    $('detail-branch').textContent = (s.branch || '') + (s.base_ref ? '  ←  ' + s.base_ref : '');
    if (RESUMABLE.indexOf(s.status) < 0) show($('resume-form'), false);
    else if (!$('resume-form').hidden) fillResumeReason(s);
    var active = s.status !== 'closed' && s.status !== 'failed';
    show($('task-form'), active); show($('message-form'), active);
    renderClaims(d.claims || []);
    await Promise.all([loadTasks(), loadFeed('events'), loadFeed('messages')]);
  }
  function fillResumeReason(s) {
    var why = s.status === 'stalled' ? t('reason_stalled')
      : s.status === 'paused' ? t('reason_paused', s.last_error || '') : t('reason_waiting', s.last_error || '');
    $('resume-reason').textContent = why;
    show($('r-stalled-wrap'), s.status === 'stalled');
    $('r-stalled').checked = false;
  }
  function openResumeForm() {
    var s = S.detail && S.detail.session;
    if (!s || RESUMABLE.indexOf(s.status) < 0) return;
    fillResumeReason(s);
    $('r-budget').value = (Number(s.budget_cost_cents) / 100).toFixed(2);
    $('r-iters').value = s.max_iterations;
    show($('resume-form'), true);
  }
  async function submitResume() {
    var s = S.detail.session;
    var body = {};
    var b = Math.round(parseFloat($('r-budget').value) * 100);
    if (isFinite(b) && b !== Number(s.budget_cost_cents)) body.budget_cost_cents = b;
    var it = parseInt($('r-iters').value, 10);
    if (isFinite(it) && it !== Number(s.max_iterations)) body.max_iterations = it;
    if (s.status === 'stalled') {
      if (!$('r-stalled').checked) { showError($('detail-error'), new ApiFailure(400, { error: 'confirm_required' })); return; }
      if (!(await confirmBox(t('confirm_stalled_dialog', s.id)))) return;
      body.confirm_stalled = true;
    }
    await api('POST', '/api/sessions/' + s.id + '/resume', body);
    show($('resume-form'), false);
    await refresh();
  }

  // tasks
  async function loadTasks() {
    var sid = S.selected, items = [], after = 0, full = false;
    for (var p = 0; p < S.taskPages; p++) {
      var d = await api('GET', '/api/sessions/tasks?session_id=' + sid + '&limit=' + PAGE + '&after_id=' + after);
      items = items.concat(d.items || []);
      full = (d.items || []).length >= PAGE;
      after = d.next_after_id;
      if (!full) break;
    }
    if (S.selected !== sid) return;
    S.tasksFull = full;
    renderTasks(items);
  }
  function renderTasks(items) {
    var ul = $('tasks'); clear(ul);
    show($('tasks-more'), S.tasksFull);
    if (!items.length) ul.appendChild(h('li', { class: 'hint', text: t('no_tasks') }));
    var err = $('detail-error');
    items.forEach(function (tk) {
      var cancellable = ['done', 'cancelled', 'failed'].indexOf(tk.status) < 0;
      ul.appendChild(h('li', { class: 'task', dataset: { tid: tk.id, status: tk.status } },
        h('div', { class: 'row between' },
          h('div', null, h('strong', { text: '#' + tk.id + ' ' + tk.title })),
          h('span', { class: 'badge ' + safeClass(tk.status, TASK_STATUSES, 'ts-'), text: taskLabel(tk.status) })),
        tk.result_summary ? h('div', { class: 'summary', text: tk.result_summary }) : null,
        (tk.paths_hint || []).length ? h('div', { class: 'hint', text: (tk.paths_hint || []).join(', ') }) : null,
        h('div', { class: 'row actions' },
          tk.status === 'needs_review' ? h('button', { type: 'button', class: 'small', text: t('review_btn'), dataset: { act: 'review' },
            on: { click: guarded(err, async function () { await openReview(tk); }) } }) : null,
          cancellable ? h('button', { type: 'button', class: 'small ghost', text: t('cancel'), dataset: { act: 'cancel' },
            on: { click: guarded(err, async function () {
              if (!(await confirmBox(t('confirm_cancel_task', tk.id)))) return;
              await api('POST', '/api/sessions/tasks/' + tk.id + '/cancel'); await refresh();
            }) } }) : null)));
    });
  }
  async function openReview(tk) {
    S.reviewTask = tk.id;
    $('review-note').value = '';
    show($('review'), true);
    $('review').dataset.tid = tk.id;
    await loadDiff();
    $('review').scrollIntoView({ block: 'nearest' });
  }
  async function review(decision) {
    var tid = S.reviewTask;
    if (!tid) return;
    if (!(await confirmBox(t(decision === 'accept' ? 'confirm_accept' : 'confirm_reject', tid)))) return;
    var note = $('review-note').value.trim();
    var d = await api('POST', '/api/sessions/tasks/' + tid + '/review', note ? { decision: decision, note: note } : { decision: decision });
    S.reviewTask = null;
    show($('review'), false);
    showInfo($('detail-error'), t(decision === 'accept' ? 'accepted' : 'rejected', tid) + (d.merged === false ? ' (merged: false)' : ''));
    await refresh(true);
  }

  // diff (untrusted text, rendered line by line as text)
  async function loadDiff() {
    var sid = S.selected;
    var d = await api('GET', '/api/sessions/' + sid + '/diff');
    if (S.selected !== sid) return;
    var stat = $('diff-stat'), pre = $('diff');
    clear(stat); clear(pre);
    stat.textContent = d.stat || '';
    show(stat, !!d.stat);
    var lines = (d.diff || '').split('\n');
    if (lines.length > 6000) lines = lines.slice(0, 6000);
    lines.forEach(function (ln) {
      var cls = /^(\+\+\+|---|diff |index )/.test(ln) ? 'meta' : ln[0] === '+' ? 'add' : ln[0] === '-' ? 'del' : ln.slice(0, 2) === '@@' ? 'hunk' : 'ctx';
      pre.appendChild(h('span', { class: 'ln ' + cls, text: ln + '\n' }));
    });
    show(pre, !!d.diff);
    var meta = [];
    if (!d.diff) meta.push(t('no_changes'));
    if (d.truncated) meta.push(t('truncated'));
    if ((d.uncommitted || []).length) meta.push(t('uncommitted', d.uncommitted.join(', ')));
    $('diff-meta').textContent = (d.base_ref || '') + '...' + (d.branch || '') + (meta.length ? ' — ' + meta.join(' ') : '');
  }

  // claims
  function renderClaims(claims) {
    var ul = $('claims'); clear(ul);
    show($('claims-release-all'), claims.length > 0);
    if (!claims.length) ul.appendChild(h('li', { class: 'hint', text: t('no_claims') }));
    var err = $('detail-error');
    claims.forEach(function (c) {
      ul.appendChild(h('li', { class: 'claim', dataset: { cid: c.id } },
        h('code', { text: c.pattern }), h('span', { class: 'pill', text: c.mode }),
        c.task_id ? h('span', { class: 'hint', text: t('task_n', c.task_id) }) : null,
        h('button', { type: 'button', class: 'small ghost', text: t('release'),
          on: { click: guarded(err, async function () {
            await api('POST', '/api/sessions/claims/release', { session_id: S.selected, claim_ids: [c.id] });
            await loadDetail();
          }) } })));
    });
  }

  // paginated feeds (messages / events / inbox)
  function feedPath(kind, st) {
    if (kind === 'inbox') return '/api/sessions/inbox?limit=' + PAGE + '&after_id=' + st.after;
    return '/api/sessions/' + st.sid + '/' + kind + '?limit=' + PAGE + '&after_id=' + st.after;
  }
  async function loadFeed(kind, more) {
    var st = kind === 'inbox' ? S.inbox : S[kind];
    if (kind !== 'inbox' && st.sid !== S.selected) return;
    if (st.full && !more) return;            // older page pending: wait for "Load more"
    var d = await api('GET', feedPath(kind, st));
    if (kind !== 'inbox' && st.sid !== S.selected) return;
    var items = d.items || [];
    st.full = items.length >= PAGE;
    st.after = d.next_after_id || st.after;
    var ul = $(kind);
    if (!st.items.length && !items.length && !ul.firstChild) {
      ul.appendChild(h('li', { class: 'hint empty', text: t(kind === 'inbox' ? 'no_messages' : 'no_' + kind) }));
    }
    if (items.length) { var e = ul.querySelector('.empty'); if (e) e.remove(); }
    items.forEach(function (it) {
      st.items.push(it);
      ul.appendChild(kind === 'events' ? eventItem(it) : messageItem(it));
    });
    show($(kind + '-more'), st.full);
  }
  function who(id) { return id == null ? t('you') : t('session_n', id); }
  function messageItem(m) {
    return h('li', { class: 'msg kind-' + (['info', 'request', 'handoff', 'conflict', 'system'].indexOf(m.kind) >= 0 ? m.kind : 'other'),
      dataset: { mid: m.id } },
      h('div', { class: 'meta' }, h('span', { text: who(m.from_session) + ' → ' + who(m.to_session) }),
        h('span', { class: 'pill', text: m.kind }), m.task_id ? h('span', { text: t('task_n', m.task_id) }) : null,
        h('span', { class: 'hint', text: when(m.created_at) })),
      h('div', { class: 'body', text: m.body }));
  }
  function eventItem(ev) {
    var payload = ev.payload == null ? '' : typeof ev.payload === 'string' ? ev.payload : JSON.stringify(ev.payload);
    if (payload.length > 2000) payload = payload.slice(0, 2000) + '…';
    var cost = Number(ev.cost_cents_est) || 0;
    return h('li', { class: 'ev', dataset: { eid: ev.id } },
      h('div', { class: 'meta' }, h('code', { text: ev.kind }),
        ev.task_id ? h('span', { text: t('task_n', ev.task_id) }) : null,
        (ev.tokens_in || ev.tokens_out) ? h('span', { text: ev.tokens_in + '↑ ' + ev.tokens_out + '↓' }) : null,
        cost ? h('span', { text: eur(cost) }) : null,
        h('span', { class: 'hint', text: when(ev.created_at) })),
      payload ? h('div', { class: 'body mono', text: payload }) : null);
  }

  // ── refresh loop ─────────────────────────────────────────────────────────
  var busy = false;
  async function refresh(force) {
    if (busy && !force) return;
    busy = true;
    try {
      await loadSessions();
      await loadFeed('inbox');
      if (S.selected != null) await loadDetail();
      clearError($('global-error'));
    } catch (e) {
      if (e instanceof ApiFailure) showError($('global-error'), e); else console.error(e);
    } finally { busy = false; }
  }

  // ── wiring ───────────────────────────────────────────────────────────────
  function translatePage() {
    document.documentElement.lang = LANG;
    document.title = t('title') + ' — ConductLoop';
    Array.prototype.forEach.call(document.querySelectorAll('[data-i18n]'), function (el) { el.textContent = t(el.dataset.i18n); });
    Array.prototype.forEach.call(document.querySelectorAll('[data-i18n-ph]'), function (el) { el.placeholder = t(el.dataset.i18nPh); });
    ['price-provider', 's-provider'].forEach(function (id) {
      var sel = $(id); clear(sel);
      PROVIDERS.forEach(function (p) { sel.appendChild(h('option', { value: p, text: p })); });
    });
  }

  function wire() {
    $('repo-form').addEventListener('submit', guarded($('repo-error'), async function () {
      await api('POST', '/api/sessions/repos', { path: $('repo-path').value.trim() });
      $('repo-path').value = '';
      await loadRepos();
    }));
    $('price-form').addEventListener('submit', guarded($('price-error'), async function () {
      await api('POST', '/api/sessions/prices', {
        provider: $('price-provider').value, model: $('price-model').value.trim(),
        in_cents_per_mtok: Math.round(parseFloat($('price-in').value) * 10000) / 100,
        out_cents_per_mtok: Math.round(parseFloat($('price-out').value) * 10000) / 100 });
      await loadPrices();
    }));
    $('session-form').addEventListener('submit', guarded($('session-error'), async function () {
      var body = {
        role: $('s-role').value.trim(), provider: $('s-provider').value, model: $('s-model').value.trim(),
        repo_path: $('s-repo').value, base_ref: $('s-base').value.trim(),
        budget_cost_cents: Math.round(parseFloat($('s-budget').value) * 100) };
      var kr = $('s-keyref').value.trim(); if (kr) body.key_ref = kr;
      var it = parseInt($('s-iters').value, 10); if (isFinite(it)) body.max_iterations = it;
      var d = await api('POST', '/api/sessions', body);
      await refresh(true);
      await select(d.session.id);
    }, { selector: 's-provider' }));
    $('resume-form').addEventListener('submit', guarded($('detail-error'), submitResume, { selector: 's-provider' }));
    $('resume-cancel').addEventListener('click', function () { show($('resume-form'), false); });
    $('task-form').addEventListener('submit', guarded($('detail-error'), async function () {
      var paths = $('t-paths').value.split(',').map(function (x) { return x.trim(); }).filter(Boolean);
      var body = { session_id: S.selected, title: $('t-title').value.trim(), prompt: $('t-prompt').value };
      if (paths.length) body.paths_hint = paths;
      await api('POST', '/api/sessions/tasks', body);
      $('t-title').value = ''; $('t-prompt').value = ''; $('t-paths').value = '';
      await refresh(true);
    }));
    $('tasks-more').addEventListener('click', guarded($('detail-error'), async function () { S.taskPages += 1; await loadTasks(); }));
    $('review-accept').addEventListener('click', guarded($('detail-error'), function () { return review('accept'); }));
    $('review-reject').addEventListener('click', guarded($('detail-error'), function () { return review('reject'); }));
    $('diff-load').addEventListener('click', guarded($('detail-error'), loadDiff));
    $('claims-release-all').addEventListener('click', guarded($('detail-error'), async function () {
      if (!(await confirmBox(t('confirm_release_all', S.selected)))) return;
      await api('POST', '/api/sessions/claims/release', { session_id: S.selected });
      await loadDetail();
    }));
    $('message-form').addEventListener('submit', guarded($('detail-error'), async function () {
      await api('POST', '/api/sessions/' + S.selected + '/messages', { body: $('m-body').value });
      $('m-body').value = '';
      await loadFeed('messages');
    }));
    ['events', 'messages', 'inbox'].forEach(function (k) {
      $(k + '-more').addEventListener('click', guarded(k === 'inbox' ? $('global-error') : $('detail-error'),
        function () { return loadFeed(k, true); }));
    });
  }

  // What this OS cannot do (GET /api/platform): say so up front instead of failing later.
  async function loadPlatform() {
    var p = await api('GET', '/api/platform');
    var c = p.code_execution || {};
    var note = $('platform-note');
    if (c.available !== false) { clearError(note); return; }
    note.hidden = false;
    note.textContent = t(c.reason === 'platform_unsupported' ? 'code_off_platform' : 'code_off_sandbox');
    if (c.reason === 'platform_unsupported') {
      var btn = $('session-form').querySelector('button[type="submit"]');
      if (btn) btn.disabled = true;
    }
  }

  async function init() {
    translatePage();
    wire();
    try { await loadPlatform(); } catch (e) { /* unknown platform: no banner */ }
    try { await Promise.all([loadRepos(), loadPrices()]); } catch (e) { if (e instanceof ApiFailure) showError($('global-error'), e); }
    await refresh(true);
    var m = /[?&]session=(\d+)/.exec(location.search);
    if (m) await select(parseInt(m[1], 10)).catch(function () {});
    setInterval(refresh, 3000);
    document.body.dataset.ready = '1';
  }
  window.SessionsUI = { t: t, lang: LANG, refresh: refresh, select: select };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})();
