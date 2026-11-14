/* Team chat (/chat). Talk to one agent, or to the CEO in team mode: the CEO
 * splits the request, each agent works in its own card, then the CEO answers.
 * Served under a strict CSP: no inline script or style, every server string is
 * escaped before it reaches innerHTML. */
(function () {
  'use strict';

  var $ = function (id) { return document.getElementById(id); };
  var state = { agents: [], lead: null, builtin: false, providers: [], current: null, threadId: null, busy: false };

  // The language is the person's choice (the selector in the top bar). Every
  // string the page writes goes through t(); the key is the fallback, so a
  // missing translation shows the key instead of nothing.
  function t(key, vars) {
    if (window._i18n && window._i18n.t) return window._i18n.t(key, vars);
    return key;
  }
  function n(v) { return String(v == null ? '' : v); }

  // The three things to ask, by who you are talking to.
  var SUGGEST_KEYS = {
    brain: ['chat.suggest_brain_1', 'chat.suggest_brain_2', 'chat.suggest_brain_3'],
    lead: ['chat.suggest_lead_1', 'chat.suggest_lead_2', 'chat.suggest_lead_3'],
    member: ['chat.suggest_member_1', 'chat.suggest_member_2', 'chat.suggest_member_3']
  };
  function suggestions(kind) {
    return (SUGGEST_KEYS[kind] || SUGGEST_KEYS.brain).map(function (k) { return t(k); });
  }

  // ---------- small helpers ----------
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function tint(key) {
    var h = 0;
    for (var i = 0; i < key.length; i++) h = (h * 31 + key.charCodeAt(i)) >>> 0;
    return 'tint-' + (h % 8);
  }
  function avatar(agent, size) {
    // The twelve roles of the starter pack draw their own mark; the mark
    // moves inside the frame, the way the identity kit moves it. Anybody
    // else keeps the emoji.
    var mark = /^[a-z][a-z0-9_]*$/.test(agent.mark || '') ? agent.mark : '';
    if (mark) {
      return '<span class="avatar ' + (size || '') + ' avatar-mark">' +
             '<img class="mark mark-' + mark + '" src="/assets/agents/' + mark + '.png" alt="">' +
             '</span>';
    }
    return '<span class="avatar ' + (size || '') + ' ' + tint(agent.key) + '" aria-hidden="true">' + esc(agent.avatar || '✦') + '</span>';
  }
  function byKey(key) {
    for (var i = 0; i < state.agents.length; i++) if (state.agents[i].key === key) return state.agents[i];
    return null;
  }
  function el(html) {
    var t = document.createElement('template');
    t.innerHTML = html.trim();
    return t.content.firstChild;
  }
  function scrollDown(force) {
    var s = $('scroll');
    if (force || s.scrollHeight - s.scrollTop - s.clientHeight < 160) s.scrollTop = s.scrollHeight;
  }
  async function api(path, opts) {
    var r = await fetch(path, opts);
    var data = null;
    try { data = await r.json(); } catch (e) { /* not JSON */ }
    if (!r.ok) throw new Error((data && (data.message || data.error)) || ('HTTP ' + r.status));
    return data;
  }

  // ---------- feedback buttons ----------
  // The 👍/👎 bars live in feedback-buttons.js, which index.html loads for the
  // Brain Hub. The team chat is a page of its own and chat.html is not ours to
  // edit, so the same script is pulled in from here.
  (function loadFeedbackButtons() {
    if (window.__clFeedbackButtons) return;
    var s = document.createElement('script');
    s.src = '/feedback-buttons.js';
    s.onerror = function () { /* no script, no votes — the chat keeps working */ };
    document.head.appendChild(s);
  })();

  // ---------- markdown (escape first, then format) ----------
  function inline(s) {
    s = esc(s);
    s = s.replace(/`([^`]+)`/g, '<code>$1</code>');
    s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    s = s.replace(/(^|[^*\w])\*([^*\n]+)\*(?!\w)/g, '$1<em>$2</em>');
    s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, function (_m, text, url) {
      return '<a href="' + url + '" target="_blank" rel="noopener noreferrer">' + text + '</a>';
    });
    s = s.replace(/\[(\d{1,2})\]/g, '<span class="cite">$1</span>');
    // Some models cite by source ("[obsidian:Pricing.md]") instead of [N]: show the name as a chip.
    s = s.replace(/\[(obsidian|repo|memory|web|voice|mcp):([^\]\n]{1,120})\]/g, function (_m, kind, name) {
      var label = name.split('/').pop().replace(/\.md$/i, '');
      return '<span class="cite" title="' + kind + ':' + name + '">' + label + '</span>';
    });
    return s;
  }
  var LIST = /^(\s*)([-*•]|\d+[.)])\s+(.*)$/;
  // One list starting at lines[i]. Deeper-indented items nest inside the item
  // above, and an ordered list keeps its first number, so "1. … - note … 2. …"
  // doesn't restart at 1.
  function list(lines, i, indent) {
    var first = LIST.exec(lines[i]), ordered = /\d/.test(first[2]), items = [];
    while (i < lines.length) {
      if (!lines[i].trim() && i + 1 < lines.length) {
        var next = LIST.exec(lines[i + 1]);
        if (next && next[1].length >= indent) { i++; continue; }
        break;
      }
      var m = LIST.exec(lines[i]);
      if (!m || m[1].length < indent) break;
      if (m[1].length > indent && items.length) {
        var sub = list(lines, i, m[1].length);
        items[items.length - 1] += sub.html;
        i = sub.i;
        continue;
      }
      if (/\d/.test(m[2]) !== ordered) break;
      items.push('<li>' + inline(m[3]));
      i++;
    }
    var tag = ordered ? 'ol' : 'ul', n = ordered ? parseInt(first[2], 10) : 1;
    return {
      html: '<' + tag + (n > 1 ? ' start="' + n + '"' : '') + '>' + items.join('</li>') + '</li></' + tag + '>',
      i: i
    };
  }
  function md(src) {
    var out = [], lines = String(src || '').replace(/\r/g, '').split('\n'), i = 0, para = [];
    function flush() { if (para.length) { out.push('<p>' + para.map(inline).join('<br>') + '</p>'); para = []; } }
    while (i < lines.length) {
      var line = lines[i];
      if (/^```/.test(line)) {
        flush();
        var code = [];
        i++;
        while (i < lines.length && !/^```/.test(lines[i])) code.push(lines[i++]);
        i++;
        out.push('<pre><code>' + esc(code.join('\n')) + '</code></pre>');
        continue;
      }
      var h = /^(#{1,3})\s+(.*)$/.exec(line);
      if (h) { flush(); out.push('<h' + h[1].length + '>' + inline(h[2]) + '</h' + h[1].length + '>'); i++; continue; }
      var item = LIST.exec(line);
      if (item) {
        flush();
        var r = list(lines, i, item[1].length);
        out.push(r.html);
        i = r.i;
        continue;
      }
      if (/^>\s?/.test(line)) { flush(); out.push('<blockquote>' + inline(line.replace(/^>\s?/, '')) + '</blockquote>'); i++; continue; }
      if (!line.trim()) { flush(); i++; continue; }
      para.push(line);
      i++;
    }
    flush();
    return out.join('');
  }
  // Re-render at most once per frame while text streams in.
  function renderer(target) {
    var text = '', queued = false;
    return {
      add: function (chunk) {
        text += chunk;
        // The server stores the answer as it was written, and a vote is matched
        // against that text; the rendered markdown would not match it.
        target.rawAnswer = text;
        if (queued) return;
        queued = true;
        requestAnimationFrame(function () { queued = false; target.innerHTML = md(text); scrollDown(); });
      },
      set: function (t) { text = t || ''; target.rawAnswer = text; target.innerHTML = md(text); },
      text: function () { return text; }
    };
  }

  // ---------- theme ----------
  // appearance.js applies the stored choice (or follows the system).
  $('theme').addEventListener('click', function () {
    if (window.clAppearance) { window.clAppearance.toggle(); return; }
    var dark = document.body.classList.toggle('dark');
    try { localStorage.setItem('ops-theme', dark ? 'dark' : 'light'); } catch (e) { /* storage off */ }
  });

  // ---------- rail ----------
  function agentItem(a) {
    var li = el('<li class="agent-item" data-key="' + esc(a.key) + '">' + avatar(a) +
      '<div class="agent-meta"><div class="agent-name">' + esc(a.name) + '</div><div class="agent-title">' +
      esc(a.title) + '</div></div>' + (a.lead ? '<span class="lead-chip">' + esc(t('chat.leads')) + '</span>' : '') + '</li>');
    li.addEventListener('click', function () { selectAgent(a.key); closeRail(); });
    return li;
  }
  function renderRail() {
    var main = $('agents-main'), team = $('agents-team');
    main.textContent = ''; team.textContent = '';
    state.agents.forEach(function (a) {
      if (a.kind === 'brain' || a.lead) main.appendChild(agentItem(a));
      else team.appendChild(agentItem(a));
    });
    $('team-section').hidden = !team.children.length;
    $('team-hint').textContent = state.builtin ? t('chat.built_in') : '';
    markActive();
  }
  function markActive() {
    document.querySelectorAll('.agent-item').forEach(function (n) {
      n.classList.toggle('active', !!state.current && n.dataset.key === state.current.key);
    });
    document.querySelectorAll('.thread-item').forEach(function (n) {
      n.classList.toggle('active', String(state.threadId) === n.dataset.id);
    });
  }
  async function loadThreads() {
    var list = $('threads');
    list.textContent = '';
    if (!state.current) return;
    var data;
    try { data = await api('/api/chat/threads?agent=' + encodeURIComponent(state.current.key)); } catch (e) { return; }
    // `thread`, not `t`: `t` is the translation function this list calls.
    (data.threads || []).forEach(function (thread) {
      var li = el('<li class="thread-item" data-id="' + esc(thread.id) + '"><span class="thread-title">' + esc(thread.title) +
        '</span><button class="thread-del" type="button" title="' + esc(t('chat.delete_thread')) + '">×</button></li>');
      li.addEventListener('click', function () { openThread(thread.id); closeRail(); });
      var del = li.querySelector('.thread-del');
      del.addEventListener('click', async function (ev) {
        ev.stopPropagation();
        if (!del.dataset.armed) {          // two clicks: no confirm() dialogs in the app webview
          del.dataset.armed = '1'; del.textContent = t('chat.delete_confirm');
          setTimeout(function () { delete del.dataset.armed; del.textContent = '×'; }, 3000);
          return;
        }
        try { await api('/api/chat/threads/' + thread.id + '/delete', { method: 'POST' }); } catch (e) { return; }
        if (state.threadId === thread.id) newChat();
        loadThreads();
      });
      list.appendChild(li);
    });
    $('threads-empty').hidden = list.children.length > 0;
    markActive();
  }
  function closeRail() { $('rail').classList.remove('open'); }
  $('rail-toggle').addEventListener('click', function () { $('rail').classList.toggle('open'); });

  // ---------- header + empty state ----------
  function teamMode() { return !!(state.current && state.current.lead && $('team-toggle').checked); }
  function renderHeader() {
    var a = state.current;
    $('who').innerHTML = avatar(a) + '<div class="agent-meta"><div class="who-name">' + esc(a.name) +
      (a.lead ? '<span class="lead-chip">' + esc(t('chat.leads_the_team')) + '</span>' : '') + '</div><div class="who-sub">' +
      esc(a.title + (a.description ? ' · ' + a.description : '')) + '</div></div>';
    $('team-toggle-wrap').hidden = !a.lead;
    $('input').placeholder = t('chat.placeholder_to', { name: a.name });
    updateHint();
  }
  function teamSize() { return state.agents.filter(function (x) { return x.kind !== 'brain' && !x.lead; }).length; }
  function updateHint() {
    var a = state.current, hint;
    if (teamMode()) hint = t('chat.hint_team', { name: a.name, n: teamSize() });
    else if (a.kind === 'brain') hint = t('chat.hint_brain');
    else hint = t('chat.hint_member', { name: a.name });
    $('hint').textContent = hint;
  }
  $('team-toggle').addEventListener('change', updateHint);

  function renderEmpty() {
    var a = state.current, box = $('messages');
    box.textContent = '';
    var team = state.agents.filter(function (x) { return x.kind !== 'brain' && !x.lead; }).slice(0, 6);
    var html = '<div class="hero">' + avatar(a, 'lg') + '<h1>' + esc(a.lead ? t('chat.ask_and_team', { name: a.name }) : t('chat.ask', { name: a.name })) + '</h1>' +
      '<p>' + esc(a.lead
        ? t('chat.lead_splits', { name: a.name })
        : (a.description || a.title)) + '</p>';
    if (a.lead && team.length) html += '<div class="stack">' + team.map(function (t) { return avatar(t, 'sm'); }).join('') + '</div>';
    html += '<div class="suggestions"></div></div>';
    var hero = el(html);
    var list = suggestions(a.kind === 'brain' ? 'brain' : (a.lead ? 'lead' : 'member'));
    list.forEach(function (s) {
      var b = el('<button class="suggestion" type="button">' + esc(s) + '</button>');
      b.addEventListener('click', function () { send(s); });
      hero.querySelector('.suggestions').appendChild(b);
    });
    box.appendChild(hero);
  }

  // ---------- conversation blocks ----------
  function userBubble(text) {
    var hero = document.querySelector('.hero');
    if (hero) hero.remove();
    $('messages').appendChild(el('<div class="msg-user">' + esc(text) + '</div>'));
  }
  function agentBlock(agent) {
    var node = el('<div class="msg-agent">' + avatar(agent) + '<div class="msg-body"><div class="msg-head"><span class="msg-name">' +
      esc(agent.name) + '</span><span class="msg-meta"></span></div><p class="plan-note" hidden></p>' +
      '<div class="team-panel" hidden><div class="team-head"><span class="stack"></span><span class="grow"></span></div><div class="cards"></div></div>' +
      '<div class="answer md pending"></div><div class="sources" hidden></div></div></div>');
    $('messages').appendChild(node);
    return {
      node: node,
      meta: node.querySelector('.msg-meta'),
      answer: renderer(node.querySelector('.answer')),
      answerEl: node.querySelector('.answer'),
      cards: {},
      started: Date.now()
    };
  }
  function setStatus(card, cls, label) {
    card.status.className = 'status ' + cls;
    card.status.innerHTML = '<span class="dot"></span>' + esc(label);
  }
  function showPlan(block, plan) {
    var note = block.node.querySelector('.plan-note');
    if (plan.note) { note.textContent = plan.note; note.hidden = false; }
    if (!plan.assignments || !plan.assignments.length) return;
    var panel = block.node.querySelector('.team-panel');
    panel.hidden = false;
    panel.querySelector('.stack').innerHTML = plan.assignments.map(function (a) {
      return avatar({ key: a.agent, avatar: a.avatar, mark: a.mark }, 'sm');
    }).join('');
    block.teamLabel = panel.querySelector('.team-head .grow');
    block.teamLabel.textContent = plan.assignments.length === 1
      ? t('chat.working_with_one')
      : t('chat.working_with_many', { n: plan.assignments.length });
    var cards = panel.querySelector('.cards');
    plan.assignments.forEach(function (a) {
      var node = el('<div class="agent-card"><button class="agent-row" type="button">' + avatar({ key: a.agent, avatar: a.avatar, mark: a.mark }) +
        '<div class="agent-meta"><div class="agent-name">' + esc(a.name) + ' <span class="agent-title">· ' + esc(a.title) +
        '</span></div><div class="task">' + esc(a.task) + '</div></div><span class="status"></span><span class="chev">▶</span></button>' +
        '<div class="agent-out md"></div></div>');
      var card = { node: node, status: node.querySelector('.status'), out: renderer(node.querySelector('.agent-out')), done: false };
      setStatus(card, '', t('chat.queued'));
      node.querySelector('.agent-row').addEventListener('click', function () {
        if (node.classList.contains('collapsed')) { node.classList.remove('collapsed'); node.classList.add('open'); }
        else if (node.classList.contains('open')) { node.classList.remove('open'); if (card.done) node.classList.add('collapsed'); }
        else node.classList.add('open');
      });
      block.cards[a.agent] = card;
      cards.appendChild(node);
    });
  }
  function collapseCards(block) {
    Object.keys(block.cards).forEach(function (k) {
      var c = block.cards[k];
      c.node.classList.remove('open', 'streaming');
      if (c.out.text()) c.node.classList.add('collapsed');
    });
    if (block.teamLabel) {
      var n = Object.keys(block.cards).length;
      block.teamLabel.textContent = (n === 1 ? t('chat.agent_one', { n: n }) : t('chat.agent_many', { n: n }))
        + ' · ' + ((Date.now() - block.started) / 1000).toFixed(1) + 's';
    }
  }
  function showSources(block, citations) {
    if (!citations || !citations.length) return;
    var box = block.node.querySelector('.sources');
    box.hidden = false;
    box.textContent = '';
    var btn = el('<button class="sources-btn" type="button">📎 ' + (citations.length === 1
      ? t('chat.source_one', { n: citations.length })
      : t('chat.sources_many', { n: citations.length })) + '</button>');
    var ol = el('<ol hidden></ol>');
    citations.forEach(function (c) {
      // doc_id is newer than the citations already stored, so it can be
      // missing: the link still works, it just opens /note.
      var id = c && c.doc_id;
      var href = '/note';
      if (typeof id === 'number' && isFinite(id)) href = '/note/' + encodeURIComponent(id);
      else if (typeof id === 'string' && /^\d+$/.test(id)) href = '/note/' + encodeURIComponent(id);
      var li = el('<li><a href="' + href + '" target="_blank" rel="noopener noreferrer">' +
        esc((c && (c.title || c.path || c.source)) || t('chat.source')) + '</a></li>');
      li.querySelector('a').addEventListener('click', function () {
        // The raw answer is what the server stored, and the event is matched
        // against it; the rendered markdown would not match.
        recordCitationOpen(c, block.answer.text() || '');
      });
      ol.appendChild(li);
    });
    btn.addEventListener('click', function () {
      ol.hidden = !ol.hidden;
    });
    box.appendChild(btn);
    box.appendChild(ol);
  }
  // The note opens in a new tab, but the server still wants to know which
  // source was used, so the signal goes out before the click is over. It must
  // never block the link, and it must never throw into the click.
  function recordCitationOpen(c, answer) {
    try {
      fetch('/api/brain/citation-open', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ answer: answer || '', path: (c && c.path) || '',
                               source: (c && c.source) || '', conversation_id: null }),
        keepalive: true
      }).catch(function () {});
    } catch (e) { /* an older server without the endpoint must not break the link */ }
  }

  function handle(block, evt) {
    var c;
    switch (evt.type) {
      case 'start':
        if (state.threadId !== evt.thread_id) { state.threadId = evt.thread_id; syncUrl(); loadThreads(); }
        block.meta.textContent = evt.provider + (evt.model ? ' · ' + evt.model : '');
        break;
      case 'context': block.citations = evt.citations; break;
      case 'plan': showPlan(block, evt); break;
      case 'agent_start':
        c = block.cards[evt.agent];
        if (c) { setStatus(c, 'working', t('chat.thinking')); c.node.classList.add('open', 'streaming'); }
        break;
      case 'agent_delta':
        c = block.cards[evt.agent];
        if (c) { if (!c.writing) { c.writing = true; setStatus(c, 'working', 'Writing'); } c.out.add(evt.content); }
        break;
      case 'agent_done':
        c = block.cards[evt.agent];
        if (c) { c.done = true; setStatus(c, 'done', t('chat.done_in', { s: (evt.ms / 1000).toFixed(1) })); c.node.classList.remove('streaming'); }
        break;
      case 'agent_error':
        c = block.cards[evt.agent];
        if (c) { c.done = true; setStatus(c, 'failed', t('chat.failed')); c.out.add('\n\n_' + evt.message + '_'); }
        break;
      case 'synthesis': collapseCards(block); break;
      case 'delta': block.answer.add(evt.content); break;
      case 'error':
        block.answerEl.classList.remove('pending');
        block.node.querySelector('.msg-body').appendChild(el('<div class="error-line">⚠ ' +
          esc(window.ProviderErrors ? window.ProviderErrors.friendly(evt) : evt.message) + '</div>'));
        break;
      case 'done':
        block.answerEl.classList.remove('pending');
        showSources(block, block.citations);
        break;
    }
    scrollDown();
  }

  // ---------- sending ----------
  async function send(text) {
    text = (text || '').trim();
    if (!text || state.busy || !state.current) return;
    state.busy = true; $('send').disabled = true;
    $('input').value = ''; autosize();
    userBubble(text);
    var block = agentBlock(state.current);
    scrollDown(true);
    var body = { agent: state.current.key, message: text, team: teamMode() };
    if (state.threadId) body.thread_id = state.threadId;
    if ($('provider').value) body.provider = $('provider').value;
    try {
      var r = await fetch('/api/chat/send', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      var pu = window.ProviderErrors ? await window.ProviderErrors.fromResponse(r) : null;
      if (pu) { handle(block, { type: 'error', message: window.ProviderErrors.friendly(pu) }); return; }
      if (!r.ok || !r.body) {
        var msg = 'HTTP ' + r.status;
        try { var d = await r.json(); msg = d.message || d.error || msg; } catch (e) { /* not JSON */ }
        handle(block, { type: 'error', message: msg });
        return;
      }
      var reader = r.body.getReader(), dec = new TextDecoder(), buf = '';
      for (;;) {
        var chunk = await reader.read();
        if (chunk.done) break;
        buf += dec.decode(chunk.value, { stream: true });
        var frames = buf.split('\n\n');
        buf = frames.pop() || '';
        frames.forEach(function (f) {
          f = f.trim();
          if (f.indexOf('data: ') !== 0) return;
          try { handle(block, JSON.parse(f.slice(6))); } catch (e) { /* partial frame */ }
        });
      }
    } catch (e) {
      handle(block, { type: 'error', message: e.message });
    } finally {
      block.answerEl.classList.remove('pending');
      state.busy = false; $('send').disabled = false;
      $('input').focus();
    }
  }

  // ---------- threads ----------
  function newChat() {
    state.threadId = null;
    syncUrl();
    renderEmpty();
    markActive();
    $('input').focus();
  }
  async function openThread(id) {
    var data;
    try { data = await api('/api/chat/threads/' + id); } catch (e) { return; }
    // `thread`, not `t`: `t` is the translation function used below.
    var thread = data.thread;
    if (!state.current || state.current.key !== thread.agent) {
      var a = byKey(thread.agent);
      if (!a) return;
      state.current = a;
      renderHeader();
    }
    state.threadId = thread.id;
    syncUrl();
    var box = $('messages');
    box.textContent = '';
    var block = null;
    thread.messages.forEach(function (m) {
      if (m.role === 'user') { userBubble(m.content); block = null; return; }
      if (!block) block = agentBlock(state.current);
      if (m.role === 'plan') {
        try { showPlan(block, JSON.parse(m.content)); } catch (e) { /* old row */ }
      } else if (m.role.indexOf('agent:') === 0) {
        var c = block.cards[m.role.slice(6)];
        if (c) { c.out.set(m.content); c.done = true; setStatus(c, 'done', t('chat.done')); c.node.classList.add('collapsed'); }
      } else if (m.role === 'assistant') {
        block.answer.set(m.content);
        block.answerEl.classList.remove('pending');
        block.meta.textContent = m.provider || '';
        if (block.teamLabel) {
          var n = Object.keys(block.cards).length;
          block.teamLabel.textContent = n === 1 ? t('chat.agent_one', { n: n }) : t('chat.agent_many', { n: n });
        }
        block = null;
      }
    });
    document.querySelectorAll('.answer.pending').forEach(function (n) { n.classList.remove('pending'); });
    markActive();
    loadThreads();
    scrollDown(true);
  }
  function syncUrl() {
    var q = '?agent=' + encodeURIComponent(state.current ? state.current.key : '') + (state.threadId ? '&thread=' + state.threadId : '');
    try { history.replaceState(null, '', '/chat' + q); } catch (e) { /* file:// */ }
    try { localStorage.setItem('chat-agent', state.current ? state.current.key : ''); } catch (e) { /* storage off */ }
  }
  function selectAgent(key) {
    var a = byKey(key);
    if (!a || state.busy) return;
    state.current = a;
    renderHeader();
    newChat();
    loadThreads();
  }

  // ---------- composer ----------
  function autosize() {
    var t = $('input');
    t.style.height = 'auto';
    t.style.height = Math.min(t.scrollHeight, 220) + 'px';
  }
  $('input').addEventListener('input', autosize);
  $('input').addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); send($('input').value); }
  });
  $('composer').addEventListener('submit', function (e) { e.preventDefault(); send($('input').value); });
  $('new-chat').addEventListener('click', function () { if (!state.busy) newChat(); });
  $('provider').addEventListener('change', function () {
    try { localStorage.setItem('chat-provider', $('provider').value); } catch (e) { /* storage off */ }
  });

  // ---------- boot ----------
  async function boot() {
    var data;
    try { data = await api('/api/chat/agents'); } catch (e) {
      $('messages').appendChild(el('<div class="error-line">⚠ ' + esc(e.message) + '</div>'));
      return;
    }
    state.agents = data.agents || [];
    state.lead = data.lead;
    state.builtin = !!data.builtin_team;
    state.providers = data.providers || [];
    var sel = $('provider'), saved = '';
    try { saved = localStorage.getItem('chat-provider') || ''; } catch (e) { /* storage off */ }
    sel.appendChild(el('<option value="">' + esc(t('chat.auto_model')) + '</option>'));
    (data.providers || []).forEach(function (p) {
      if (!p || !p.name || p.name === 'mock') return;
      var o = el('<option value="' + esc(p.name) + '">' + esc(p.name) + (p.available ? '' : esc(t('chat.provider_not_set_up'))) + '</option>');
      sel.appendChild(o);
    });
    sel.value = saved;
    if (sel.value !== saved) sel.value = '';
    renderRail();
    var params = new URLSearchParams(location.search);
    var stored = '';
    try { stored = localStorage.getItem('chat-agent') || ''; } catch (e) { /* storage off */ }
    var key = params.get('agent') || stored || state.lead;
    state.current = byKey(key) || byKey(state.lead) || state.agents[0];
    renderHeader();
    var thread = parseInt(params.get('thread') || '', 10);
    if (thread) openThread(thread); else newChat();
    // "Ask" on the dashboard's notes home: the question is put in the box,
    // not sent, so a link alone never spends a call on the user's key.
    var ask = params.get('q');
    if (ask && !thread) { $('input').value = ask.slice(0, 4000); autosize(); }
    loadThreads();
  }

  // The language is the person's choice: when it changes, the page says the
  // same things in the new language. A conversation in progress is left
  // alone; only the words around it are redrawn.
  window.addEventListener('localechange', function () {
    if (window._i18n && window._i18n.apply) window._i18n.apply();
    var sel = $('provider');
    if (sel) {
      var keep = sel.value;
      sel.textContent = '';
      sel.appendChild(el('<option value="">' + esc(t('chat.auto_model')) + '</option>'));
      (state.providers || []).forEach(function (p) {
        if (!p || !p.name || p.name === 'mock') return;
        sel.appendChild(el('<option value="' + esc(p.name) + '">' + esc(p.name) +
          (p.available ? '' : esc(t('chat.provider_not_set_up'))) + '</option>'));
      });
      sel.value = keep;
    }
    renderRail();
    if (state.current) { renderHeader(); updateHint(); }
    if (!$('messages').children.length) renderEmpty();
    loadThreads();
  });

  // The words arrive over the network: wait for them, or the page would
  // show keys instead of sentences.
  if (window._i18n && window._i18n.ready) window._i18n.ready().then(boot, boot);
  else boot();
})();
