// BYOK key-storage notice (multi-agent PR 2).
// Shows a one-line banner when provider API keys are still stored in plaintext
// in ~/ops-dashboard/config.json. Moving them to the macOS Keychain happens
// ONLY when the user clicks "Move to Keychain"; "Not now" just hides the
// prompt. Never displays key values (the API never returns them).
(function () {
  var es = /^es\b/i.test(navigator.language || '');
  var T = es ? {
    plain: 'Tus claves de API ({p}) están guardadas en texto plano en config.json.',
    move: 'Mover al llavero',
    later: 'Ahora no',
    noKc: 'El llavero del sistema no está disponible: tus claves de API ({p}) siguen en config.json (solo legible por tu usuario).',
    ok: 'Claves movidas al llavero ({p}).',
    fail: 'No se pudieron mover algunas claves ({p}); siguen en config.json.',
    err: 'No se pudo completar la operación.'
  } : {
    plain: 'Your API keys ({p}) are stored in plaintext in config.json.',
    move: 'Move to Keychain',
    later: 'Not now',
    noKc: 'The system keychain is unavailable: your API keys ({p}) stay in config.json (readable only by your user).',
    ok: 'Keys moved to the Keychain ({p}).',
    fail: 'Some keys could not be moved ({p}); they stay in config.json.',
    err: 'The operation could not be completed.'
  };

  function fmt(s, list) { return s.replace('{p}', (list || []).join(', ')); }

  function bar() {
    var el = document.getElementById('byok-banner');
    if (el) return el;
    el = document.createElement('div');
    el.id = 'byok-banner';
    el.setAttribute('role', 'status');
    el.style.cssText = 'position:fixed;left:50%;bottom:16px;transform:translateX(-50%);z-index:9000;' +
      'max-width:720px;display:flex;gap:10px;align-items:center;padding:10px 14px;border-radius:8px;' +
      'background:var(--bg,#111);color:var(--ink,#eee);border:1px solid var(--amber,#f59e0b);' +
      'font-size:13px;box-shadow:0 4px 18px rgba(0,0,0,.35)';
    document.body.appendChild(el);
    return el;
  }

  function button(label, primary, onClick) {
    var b = document.createElement('button');
    b.type = 'button';
    b.textContent = label;
    b.style.cssText = 'cursor:pointer;border-radius:4px;padding:4px 10px;font-size:12px;border:1px solid var(--border,#444);' +
      (primary ? 'background:var(--accent,#7c3aed);color:#fff;' : 'background:transparent;color:inherit;');
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

  function hide() { var el = document.getElementById('byok-banner'); if (el) el.remove(); }

  function post(path, body) {
    return fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify(body || {})}).then(function (r) { return r.json(); });
  }

  function render(st) {
    if (!st || !st.should_prompt) return;
    var p = st.plaintext_providers;
    if (!st.keychain_available) {
      show(fmt(T.noKc, p), [button('×', false, function () { post('/api/brain/keys/dismiss').then(hide, hide); })]);
      return;
    }
    show(fmt(T.plain, p), [
      button(T.move, true, function () {
        post('/api/brain/keys/migrate', {consent: true}).then(function (r) {
          if (r && r.failed && r.failed.length) show(fmt(T.fail, r.failed), [button('×', false, hide)]);
          else if (r && r.moved) { show(fmt(T.ok, r.moved), []); setTimeout(hide, 4000); }
          else show(T.err, [button('×', false, hide)]);
        }, function () { show(T.err, [button('×', false, hide)]); });
      }),
      button(T.later, false, function () { post('/api/brain/keys/dismiss').then(hide, hide); })
    ]);
  }

  function start() {
    fetch('/api/brain/keys/status').then(function (r) { return r.ok ? r.json() : null; })
      .then(render, function () {});
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();
