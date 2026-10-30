// 👍/👎 under Brain Hub answers (learning PR L3, decision D7: always local).
//
// Loaded by index.html with one <script> line; it does not touch askBrain().
// It watches #brain-chat for the ".meta" line that askBrain() appends when an
// answer finishes streaming, and adds the buttons after it. The streaming API
// does not return message ids, so the vote identifies the answer by its exact
// text (POST /api/learning/feedback {answer, rating}); the server resolves it
// to the stored assistant message.
//
// Security: only textContent / createElement here (no innerHTML).
(function () {
  'use strict';

  var ES = /^es\b/i.test((navigator && navigator.language) || '');
  var T = ES ? {
    up: 'Útil', down: 'No útil', why: '¿Qué falló?', comment: 'Comentario (opcional, se queda en este ordenador)',
    send: 'Enviar', thanks: 'Gracias, guardado en este ordenador.', lessons: 'Lecciones usadas: ', err: 'No se pudo guardar: ',
    r_wrong: 'Incorrecta', r_missing_citation: 'Falta cita', r_bad_citation: 'Cita errónea',
    r_outdated: 'Desactualizada', r_style: 'Estilo', r_unsafe: 'Insegura', r_other: 'Otro'
  } : {
    up: 'Helpful', down: 'Not helpful', why: 'What went wrong?', comment: 'Comment (optional, stays on this computer)',
    send: 'Send', thanks: 'Thanks, saved on this computer.', lessons: 'Lessons used: ', err: 'Could not save: ',
    r_wrong: 'Wrong', r_missing_citation: 'Missing citation', r_bad_citation: 'Bad citation',
    r_outdated: 'Outdated', r_style: 'Style', r_unsafe: 'Unsafe', r_other: 'Other'
  };
  var REASONS = ['wrong', 'missing_citation', 'bad_citation', 'outdated', 'style', 'unsafe', 'other'];

  function el(tag, text, css) {
    var e = document.createElement(tag);
    if (text) e.textContent = text;
    if (css) e.style.cssText = css;
    return e;
  }
  var BTN = 'background:transparent;border:1px solid var(--border);border-radius:6px;padding:2px 8px;cursor:pointer;color:var(--ink);font-size:13px';

  async function send(answer, rating, reason, comment, attempt) {
    var r = await fetch('/api/learning/feedback', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ target_kind: 'brain_message', answer: answer, rating: rating,
                             reason_code: reason || null, comment: comment || null })
    });
    // The answer is persisted right after the stream ends; retry once if we were faster.
    if (r.status === 404 && (attempt || 0) < 2) {
      await new Promise(function (res) { setTimeout(res, 600); });
      return send(answer, rating, reason, comment, (attempt || 0) + 1);
    }
    var d = null;
    try { d = await r.json(); } catch (e) { d = null; }
    if (!r.ok || !d || d.ok === false) throw new Error((d && (d.message || d.error)) || ('HTTP ' + r.status));
    return d;
  }

  function attach(metaEl, bubble) {
    if (metaEl.getAttribute('data-fb') === '1') return;
    metaEl.setAttribute('data-fb', '1');
    var bar = el('div', null, 'display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin:4px 0 8px;font-size:12px;color:var(--muted)');
    bar.className = 'cl-feedback';
    var up = el('button', '👍', BTN); up.type = 'button'; up.title = T.up; up.setAttribute('aria-label', T.up);
    var down = el('button', '👎', BTN); down.type = 'button'; down.title = T.down; down.setAttribute('aria-label', T.down);
    var status = el('span');
    bar.appendChild(up); bar.appendChild(down); bar.appendChild(status);
    metaEl.parentNode.insertBefore(bar, metaEl.nextSibling);

    function answer() { return bubble.textContent || ''; }
    function done(sel) {
      up.disabled = down.disabled = true;
      (sel === 1 ? up : down).style.borderColor = 'var(--blue)';
      status.textContent = T.thanks;
    }
    function fail(e) { status.textContent = T.err + e.message; }
    // L4: show which approved lessons shaped this answer (local lookup by answer text).
    function showLessons() {
      fetch('/api/learning/lessons/used-by', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ answer: answer() }) })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) {
          if (!d || !d.lessons || !d.lessons.length) return;
          var info = el('span', T.lessons + d.lessons.map(function (l) { return 'L' + l.id; }).join(', '));
          info.title = d.lessons.map(function (l) { return 'L' + l.id + ': ' + l.text; }).join('\n');
          bar.appendChild(info);
        }).catch(function () {});
    }
    setTimeout(showLessons, 800);

    up.addEventListener('click', function () {
      send(answer(), 1).then(function () { done(1); }).catch(fail);
    });
    down.addEventListener('click', function () {
      if (bar.querySelector('select')) return;
      var sel = el('select', null, 'background:var(--bg);color:var(--ink);border:1px solid var(--border);border-radius:6px;font-size:12px');
      var o0 = el('option', T.why); o0.value = ''; sel.appendChild(o0);
      REASONS.forEach(function (k) { var o = el('option', T['r_' + k]); o.value = k; sel.appendChild(o); });
      var txt = el('input', null, 'flex:1;min-width:160px;background:var(--bg);color:var(--ink);border:1px solid var(--border);border-radius:6px;padding:2px 6px;font-size:12px');
      txt.placeholder = T.comment; txt.maxLength = 2000;
      var ok = el('button', T.send, BTN); ok.type = 'button';
      ok.addEventListener('click', function () {
        send(answer(), -1, sel.value, txt.value.trim()).then(function () {
          sel.disabled = txt.disabled = ok.disabled = true; done(-1);
        }).catch(fail);
      });
      bar.insertBefore(sel, status); bar.insertBefore(txt, status); bar.insertBefore(ok, status);
    });
  }

  function scan(node) {
    if (!node || node.nodeType !== 1) return;
    var metas = node.matches && node.matches('#brain-chat > .meta') ? [node] : node.querySelectorAll('#brain-chat > .meta');
    for (var i = 0; i < metas.length; i++) {
      var m = metas[i];
      var prev = m.previousElementSibling;
      if (!prev || !prev.classList.contains('msg') || !prev.classList.contains('assistant')) continue;
      var bubble = prev.querySelector('.bubble');
      if (bubble && (bubble.textContent || '').trim()) attach(m, bubble);
    }
  }

  function start() {
    var obs = new MutationObserver(function (muts) {
      for (var i = 0; i < muts.length; i++) {
        for (var j = 0; j < muts[i].addedNodes.length; j++) scan(muts[i].addedNodes[j]);
      }
    });
    obs.observe(document.body, { childList: true, subtree: true });
    scan(document.body);
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();
