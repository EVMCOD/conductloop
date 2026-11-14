// Sample-data notice.
//
// The first-run guide can load a fictional workspace so that someone who has
// just installed the app can see the product before they have a vault or an
// API key. This banner says so, every time it is on screen, and offers the
// way to see what the app actually does: ask the Brain a question and watch
// it answer out of the sample notes.
//
// It never shows anything about the user's own notes, only the count of the
// sample ones.
(function () {
  'use strict';
  var ES = {
    text: 'Estás viendo datos de ejemplo: {n} notas ficticias. Hazle una pregunta al Brain y verás cómo responde citando esas notas.',
    ask: 'Preguntarle algo',
    setup: 'Usar mis notas',
    remove: 'Quitar el ejemplo',
    done: 'Datos de ejemplo quitados.',
    fail: 'No se pudieron quitar los datos de ejemplo.',
    question: '¿Qué es el método Zettelkasten?'
  };
  var EN = {
    text: 'You are looking at sample data: {n} fictional notes. Ask the Brain something and watch it answer out of those notes.',
    ask: 'Ask it a question',
    setup: 'Use my notes',
    remove: 'Remove the sample',
    done: 'Sample data removed.',
    fail: 'The sample data could not be removed.',
    question: 'What is the Zettelkasten method?'
  };
  var T = ES;

  // The banner speaks the language of the app, which is the language the
  // person chose.
  function pick() {
    var l = null;
    if (window._i18n && window._i18n.getLocale) l = window._i18n.getLocale();
    if (l !== 'es' && l !== 'en') l = /^es\b/i.test(navigator.language || '') ? 'es' : 'en';
    T = l === 'es' ? ES : EN;
  }

  function fmt(s, n) { return s.replace('{n}', String(n)); }

  function csrfToken() {
    var m = /(?:^|;\s*)ops_csrf=([^;]+)/.exec(document.cookie || '');
    if (!m) return '';
    try { return decodeURIComponent(m[1]); } catch (e) { return m[1]; }
  }

  function bar() {
    var el = document.getElementById('sample-banner');
    if (el) return el;
    el = document.createElement('div');
    el.id = 'sample-banner';
    el.setAttribute('role', 'status');
    el.style.cssText = 'position:fixed;left:50%;bottom:64px;transform:translateX(-50%);z-index:9000;' +
      'max-width:720px;display:flex;gap:10px;align-items:center;padding:12px 16px;border-radius:14px;' +
      'background:var(--cl-overlay,rgba(255,255,255,.86));color:var(--cl-ink,#1d1d1f);border:0;' +
      '-webkit-backdrop-filter:saturate(180%) blur(20px);backdrop-filter:saturate(180%) blur(20px);' +
      'font-size:13px;line-height:1.4;box-shadow:var(--cl-shadow-3,0 12px 40px rgba(0,0,0,.18))';
    document.body.appendChild(el);
    return el;
  }

  function button(label, primary, onClick) {
    var b = document.createElement('button');
    b.type = 'button';
    b.textContent = label;
    b.style.cssText = 'cursor:pointer;border-radius:999px;height:26px;padding:0 12px;font:inherit;font-size:12px;font-weight:500;border:0;white-space:nowrap;' +
      (primary ? 'background:var(--cl-accent,#7c3aed);color:#fff;' : 'background:var(--cl-bg-2,#ececf0);color:inherit;');
    b.addEventListener('click', onClick);
    return b;
  }

  function show(text, actions) {
    var el = bar();
    el.textContent = '';
    var span = document.createElement('span');
    span.textContent = text;
    span.style.flex = '1';
    el.appendChild(span);
    (actions || []).forEach(function (a) { el.appendChild(a); });
  }

  function post(path) {
    var headers = { 'Content-Type': 'application/json' };
    var c = csrfToken();
    if (c) headers['X-CSRF-Token'] = c;
    return fetch(path, { method: 'POST', headers: headers, body: '{}' })
      .then(function (r) { return r.json().catch(function () { return {}; }); });
  }

  function removeSample() {
    post('/api/onboarding/demo/clear').then(function (r) {
      if (r && r.ok) { show(T.done, []); setTimeout(function () { window.location.reload(); }, 1200); }
      else show(T.fail, []);
    }, function () { show(T.fail, []); });
  }

  function askBrain() {
    // The Brain, not the team lead: this is about reading the notes. The chat
    // page puts the question in the composer without sending it, so a link
    // alone never spends a call on the user's key.
    window.location.replace('/chat?agent=brain&q=' + encodeURIComponent(T.question));
  }

  function render(st) {
    if (!st || !st.demo) return;
    pick();
    var n = st.demo_documents || 0;
    show(fmt(T.text, n), [
      button(T.ask, true, askBrain),
      button(T.setup, false, function () { window.location.replace('/welcome'); }),
      button(T.remove, false, removeSample)
    ]);
  }

  function start() {
    fetch('/api/onboarding/state', { headers: { 'Accept': 'application/json' } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(render, function () {});
    // The person can change the language from the app; the notice follows.
    window.addEventListener('localechange', function () {
      fetch('/api/onboarding/state', { headers: { 'Accept': 'application/json' } })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(render, function () {});
    });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();
