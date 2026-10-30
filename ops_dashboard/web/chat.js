/* Team chat (/chat). Talk to one agent, or to the CEO in team mode: the CEO
 * splits the request, each agent works in its own card, then the CEO answers.
 * Served under a strict CSP: no inline script or style, every server string is
 * escaped before it reaches innerHTML. */
(function () {
  'use strict';

  var $ = function (id) { return document.getElementById(id); };
  var state = { agents: [], lead: null, builtin: false, providers: [], current: null, threadId: null, busy: false };
  var SUGGEST = {
    brain: ['What did I work on this week?', 'What is still open in my TODOs?', 'Summarise my notes on pricing'],
    lead: ['What should the team focus on this week?', 'Review the plan for our next launch', 'Where are we losing time or money?'],
    member: ['What would you fix first, from your side?', 'What risks do you see right now?', 'Give me a plan for next week in your area']
  };

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
        if (queued) return;
        queued = true;
        requestAnimationFrame(function () { queued = false; target.innerHTML = md(text); scrollDown(); });
      },
      set: function (t) { text = t || ''; target.innerHTML = md(text); },
      text: function () { return text; }
    };
  }

  // ---------- theme ----------
  try { if (localStorage.getItem('ops-theme') === 'dark') document.body.classList.add('dark'); } catch (e) { /* storage off */ }
  $('theme').addEventListener('click', function () {
    var dark = document.body.classList.toggle('dark');
    try { localStorage.setItem('ops-theme', dark ? 'dark' : 'light'); } catch (e) { /* storage off */ }
  });

  // ---------- rail ----------
  function agentItem(a) {
    var li = el('<li class="agent-item" data-key="' + esc(a.key) + '">' + avatar(a) +
      '<div class="agent-meta"><div class="agent-name">' + esc(a.name) + '</div><div class="agent-title">' +
      esc(a.title) + '</div></div>' + (a.lead ? '<span class="lead-chip">LEADS</span>' : '') + '</li>');
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
    $('team-hint').textContent = state.builtin ? '· built-in' : '';
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
    (data.threads || []).forEach(function (t) {
      var li = el('<li class="thread-item" data-id="' + esc(t.id) + '"><span class="thread-title">' + esc(t.title) +
        '</span><button class="thread-del" type="button" title="Delete">×</button></li>');
      li.addEventListener('click', function () { openThread(t.id); closeRail(); });
      var del = li.querySelector('.thread-del');
      del.addEventListener('click', async function (ev) {
        ev.stopPropagation();
        if (!del.dataset.armed) {          // two clicks: no confirm() dialogs in the app webview
          del.dataset.armed = '1'; del.textContent = 'delete?';
          setTimeout(function () { delete del.dataset.armed; del.textContent = '×'; }, 3000);
          return;
        }
        try { await api('/api/chat/threads/' + t.id + '/delete', { method: 'POST' }); } catch (e) { return; }
        if (state.threadId === t.id) newChat();
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
      (a.lead ? '<span class="lead-chip">LEADS THE TEAM</span>' : '') + '</div><div class="who-sub">' +
      esc(a.title + (a.description ? ' · ' + a.description : '')) + '</div></div>';
    $('team-toggle-wrap').hidden = !a.lead;
    $('input').placeholder = 'Message ' + a.name + '…';
    updateHint();
  }
  function teamSize() { return state.agents.filter(function (x) { return x.kind !== 'brain' && !x.lead; }).length; }
  function updateHint() {
    var a = state.current, hint;
    if (teamMode()) hint = a.name + ' picks who on the team works on it (' + teamSize() + ' people), then writes the answer. Each agent is one model call.';
    else if (a.kind === 'brain') hint = 'Answers from your notes, repos and memory, with sources.';
    else hint = a.name + ' answers from your notes and repos. Enter to send, Shift+Enter for a new line.';
    $('hint').textContent = hint;
  }
  $('team-toggle').addEventListener('change', updateHint);

  function renderEmpty() {
    var a = state.current, box = $('messages');
    box.textContent = '';
    var team = state.agents.filter(function (x) { return x.kind !== 'brain' && !x.lead; }).slice(0, 6);
    var html = '<div class="hero">' + avatar(a, 'lg') + '<h1>' + esc(a.lead ? 'Ask ' + a.name + ' and the team' : 'Ask ' + a.name) + '</h1>' +
      '<p>' + esc(a.lead
        ? a.name + ' splits your request across the team, you watch each agent work, and you get one answer back.'
        : (a.description || a.title)) + '</p>';
    if (a.lead && team.length) html += '<div class="stack">' + team.map(function (t) { return avatar(t, 'sm'); }).join('') + '</div>';
    html += '<div class="suggestions"></div></div>';
    var hero = el(html);
    var list = SUGGEST[a.kind === 'brain' ? 'brain' : (a.lead ? 'lead' : 'member')];
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
      return avatar({ key: a.agent, avatar: a.avatar }, 'sm');
    }).join('');
    block.teamLabel = panel.querySelector('.team-head .grow');
    block.teamLabel.textContent = 'Working with ' + plan.assignments.length + (plan.assignments.length === 1 ? ' agent' : ' agents') + '…';
    var cards = panel.querySelector('.cards');
    plan.assignments.forEach(function (a) {
      var node = el('<div class="agent-card"><button class="agent-row" type="button">' + avatar({ key: a.agent, avatar: a.avatar }) +
        '<div class="agent-meta"><div class="agent-name">' + esc(a.name) + ' <span class="agent-title">· ' + esc(a.title) +
        '</span></div><div class="task">' + esc(a.task) + '</div></div><span class="status"></span><span class="chev">▶</span></button>' +
        '<div class="agent-out md"></div></div>');
      var card = { node: node, status: node.querySelector('.status'), out: renderer(node.querySelector('.agent-out')), done: false };
      setStatus(card, '', 'Queued');
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
      block.teamLabel.textContent = n + (n === 1 ? ' agent' : ' agents') + ' · ' + ((Date.now() - block.started) / 1000).toFixed(1) + 's';
    }
  }
  function showSources(block, citations) {
    if (!citations || !citations.length) return;
    var box = block.node.querySelector('.sources');
    box.hidden = false;
    box.innerHTML = '<button class="sources-btn" type="button">📎 ' + citations.length + (citations.length === 1 ? ' source' : ' sources') + '</button><ol hidden>' +
      citations.map(function (c) { return '<li>' + esc(c.title || c.path || c.source || 'source') + '</li>'; }).join('') + '</ol>';
    box.querySelector('.sources-btn').addEventListener('click', function () {
      var ol = box.querySelector('ol'); ol.hidden = !ol.hidden;
    });
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
        if (c) { setStatus(c, 'working', 'Thinking'); c.node.classList.add('open', 'streaming'); }
        break;
      case 'agent_delta':
        c = block.cards[evt.agent];
        if (c) { if (!c.writing) { c.writing = true; setStatus(c, 'working', 'Writing'); } c.out.add(evt.content); }
        break;
      case 'agent_done':
        c = block.cards[evt.agent];
        if (c) { c.done = true; setStatus(c, 'done', 'Done · ' + (evt.ms / 1000).toFixed(1) + 's'); c.node.classList.remove('streaming'); }
        break;
      case 'agent_error':
        c = block.cards[evt.agent];
        if (c) { c.done = true; setStatus(c, 'failed', 'Failed'); c.out.add('\n\n_' + evt.message + '_'); }
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
    var t = data.thread;
    if (!state.current || state.current.key !== t.agent) {
      var a = byKey(t.agent);
      if (!a) return;
      state.current = a;
      renderHeader();
    }
    state.threadId = t.id;
    syncUrl();
    var box = $('messages');
    box.textContent = '';
    var block = null;
    t.messages.forEach(function (m) {
      if (m.role === 'user') { userBubble(m.content); block = null; return; }
      if (!block) block = agentBlock(state.current);
      if (m.role === 'plan') {
        try { showPlan(block, JSON.parse(m.content)); } catch (e) { /* old row */ }
      } else if (m.role.indexOf('agent:') === 0) {
        var c = block.cards[m.role.slice(6)];
        if (c) { c.out.set(m.content); c.done = true; setStatus(c, 'done', 'Done'); c.node.classList.add('collapsed'); }
      } else if (m.role === 'assistant') {
        block.answer.set(m.content);
        block.answerEl.classList.remove('pending');
        block.meta.textContent = m.provider || '';
        if (block.teamLabel) { var n = Object.keys(block.cards).length; block.teamLabel.textContent = n + (n === 1 ? ' agent' : ' agents'); }
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
    var sel = $('provider'), saved = '';
    try { saved = localStorage.getItem('chat-provider') || ''; } catch (e) { /* storage off */ }
    sel.appendChild(el('<option value="">Auto model</option>'));
    (data.providers || []).forEach(function (p) {
      if (!p || !p.name || p.name === 'mock') return;
      var o = el('<option value="' + esc(p.name) + '">' + esc(p.name) + (p.available ? '' : ' (not set up)') + '</option>');
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
  boot();
})();
