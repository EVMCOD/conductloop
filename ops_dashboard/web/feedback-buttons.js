// 👍/👎 under Brain Hub answers (learning PR L3, decision D7: always local).
//
// Loaded by index.html with one <script> line; it does not touch askBrain().
// It watches #brain-chat for the ".meta" line that askBrain() appends when an
// answer finishes streaming, and adds the buttons after it. The streaming API
// does not return message ids, so the vote identifies the answer by its exact
// text (POST /api/learning/feedback {answer, rating}); the server resolves it
// to the stored assistant message.
//
// The team chat (chat.js) is the same thing on its own page: every reply is a
// .msg-agent block whose answer lands in .answer, and the vote goes out with
// target_kind "team_message" so the server resolves it against the stored
// team-chat message instead of failing.
//
// Security: only textContent / createElement here (no innerHTML).
(function () {
  'use strict';

  // chat.js pulls this file into the team chat page; the flag keeps it from
  // loading the same script twice.
  window.__clFeedbackButtons = true;

  // The language is the app's, not the browser's: the person chose it.
  var ES = (window._i18n && window._i18n.getLocale)
    ? window._i18n.getLocale() === 'es'
    : /^es\b/i.test((navigator && navigator.language) || '');
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
  var obs = null;   // shared: the page tree and every watched .answer element

  function el(tag, text, css) {
    var e = document.createElement(tag);
    if (text) e.textContent = text;
    if (css) e.style.cssText = css;
    return e;
  }
  var BTN = 'background:transparent;border:1px solid var(--border);border-radius:6px;padding:2px 8px;cursor:pointer;color:var(--ink);font-size:13px';

  async function send(kind, answer, rating, reason, comment, attempt) {
    var r = await fetch('/api/learning/feedback', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ target_kind: kind, answer: answer, rating: rating,
                             reason_code: reason || null, comment: comment || null })
    });
    // The answer is persisted right after the stream ends; retry once if we were faster.
    if (r.status === 404 && (attempt || 0) < 2) {
      await new Promise(function (res) { setTimeout(res, 600); });
      return send(kind, answer, rating, reason, comment, (attempt || 0) + 1);
    }
    var d = null;
    try { d = await r.json(); } catch (e) { d = null; }
    if (!r.ok || !d || d.ok === false) throw new Error((d && (d.message || d.error)) || ('HTTP ' + r.status));
    return d;
  }

  // anchor is the node the bar goes after (the ".meta" line in the Brain Hub,
  // the answer in the team chat); bubble is the node holding the answer text.
  function attach(anchor, bubble, kind) {
    if (anchor.getAttribute('data-fb') === '1') return;
    anchor.setAttribute('data-fb', '1');
    var bar = el('div', null, 'display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin:4px 0 8px;font-size:12px;color:var(--muted)');
    bar.className = 'cl-feedback';
    var up = el('button', '👍', BTN); up.type = 'button'; up.title = T.up; up.setAttribute('aria-label', T.up);
    var down = el('button', '👎', BTN); down.type = 'button'; down.title = T.down; down.setAttribute('aria-label', T.down);
    var status = el('span');
    bar.appendChild(up); bar.appendChild(down); bar.appendChild(status);
    anchor.parentNode.insertBefore(bar, anchor.nextSibling);

    // The team chat renders markdown, so the text in the DOM is not what was
    // stored; the renderer keeps the raw answer on the element and the server
    // matches the vote against that.
    function answer() { return bubble.rawAnswer || bubble.textContent || ''; }
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
      send(kind, answer(), 1).then(function () { done(1); }).catch(fail);
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
        send(kind, answer(), -1, sel.value, txt.value.trim()).then(function () {
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
      if (bubble && (bubble.textContent || '').trim()) attach(m, bubble, 'brain_message');
    }
    // team chat: one .msg-agent block per reply
    var blocks = node.matches && node.matches('.msg-agent') ? [node] : node.querySelectorAll('.msg-agent');
    for (var j = 0; j < blocks.length; j++) watchAgent(blocks[j]);
  }

  // The team chat streams into .answer and drops the "pending" class when the
  // reply is over, so that class change is when the buttons can appear.
  function watchAgent(block) {
    var answer = block.querySelector('.answer');
    if (!answer || !obs || answer.getAttribute('data-fb-watch') === '1') return;
    answer.setAttribute('data-fb-watch', '1');
    obs.observe(answer, { attributes: true, attributeFilter: ['class'] });
    attachAgent(block);
  }

  function attachAgent(block) {
    var answer = block.querySelector('.answer');
    if (!answer || answer.classList.contains('pending')) return;
    if (!(answer.textContent || '').trim()) return;
    if (block.querySelector('.error-line')) return;   // the reply failed, there is nothing to vote on
    attach(answer, answer, 'team_message');
  }

  function start() {
    obs = new MutationObserver(function (muts) {
      for (var i = 0; i < muts.length; i++) {
        if (muts[i].type === 'attributes') {
          // the team chat drops "pending" when the reply is finished
          var host = muts[i].target && muts[i].target.closest ? muts[i].target.closest('.msg-agent') : null;
          if (host) attachAgent(host);
          continue;
        }
        for (var j = 0; j < muts[i].addedNodes.length; j++) scan(muts[i].addedNodes[j]);
      }
    });
    obs.observe(document.body, { childList: true, subtree: true });
    scan(document.body);
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();
