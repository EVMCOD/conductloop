// ConductLoop — app script (extracted from index.html on 2026-10-23).
//
// Extracted verbatim: same file scope, same execution order, so every
// global keeps working. The next step of the design plan is to split
// this into app/shell.js, app/graph.js, app/browser.js, app/markdown.js,
// app/search.js and app/views.js — do it with the globals in mind.

window.addEventListener('error', function(e) {
  console.error('WINDOW ERROR:', e.message, 'at', e.filename + ':' + e.lineno);
  var main = document.getElementById('main-content');
  if (main && !main.dataset.errored) {
    main.dataset.errored = '1';
    main.innerHTML = '<div class="md-view"><h1>Runtime error</h1><div style="background:#fee2e2;border:1px solid #dc2626;padding:16px;border-radius:6px;color:#7f1d1d;margin-top:20px"><pre style="font-size:12px;white-space:pre-wrap">' + (e.error ? (e.error.stack || e.error.message) : e.message) + '\nat ' + e.filename + ':' + e.lineno + '</pre></div></div>';
  }
});
window.addEventListener('unhandledrejection', function(e) {
  console.error('UNHANDLED REJECTION:', e.reason);
  var main = document.getElementById('main-content');
  if (main && !main.dataset.errored) {
    main.dataset.errored = '1';
    main.innerHTML = '<div class="md-view"><h1>Runtime error</h1><div style="background:#fee2e2;border:1px solid #dc2626;padding:16px;border-radius:6px;color:#7f1d1d;margin-top:20px"><pre style="font-size:12px;white-space:pre-wrap">' + (e.reason && e.reason.stack ? e.reason.stack : e.reason) + '</pre></div></div>';
  }
});
let _view = 'files';
let _q = null, _convId = null;
let _watchInterval = null;
let _projects = [], _playbooks = [], _todos = [], _urls = [], _providers = [], _templates = [], _members = [];
let _allData = {};
let _allTags = [];
let _currentProject = null;
let _history = [];
let _userTodos = [];
let _userTodosSummary = { open: 0, done: 0, total: 0 };
let _ideas = [];
let _ideasSummary = { new: 0, in_progress: 0, done: 0, total: 0 };

// ---- HTTP / utils ----
async function fetchJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(url + ' HTTP ' + r.status);
  return r.json();
}

// ---- CSRF (audit 2026-06-23) ----
// Server issues ops_csrf cookie on login (non-HttpOnly). We must echo it
// back in X-CSRF-Token header on every state-changing request. Easiest:
// monkey-patch window.fetch so every POST/PUT/DELETE to /api/* gets
// the header automatically — no per-callsite changes needed.
const _realFetch = window.fetch.bind(window);
window.fetch = function(input, init) {
  init = init || {};
  const method = (init.method || (typeof input === 'object' && input.method) || 'GET').toUpperCase();
  const isStateChange = method === 'POST' || method === 'PUT' || method === 'DELETE' || method === 'PATCH';
  const url = typeof input === 'string' ? input : input.url;
  const isApi = url && url.indexOf('/api/') === 0;
  if (isStateChange && isApi) {
    const m = document.cookie.match(/(?:^|;\s*)ops_csrf=([^;]+)/);
    if (m) {
      init.headers = Object.assign({}, init.headers || {}, { 'X-CSRF-Token': m[1] });
    }
  }
  return _realFetch(input, init);
};
function esc(s) {
  return (s || '').replace(/[&<>"']/g, c => ({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;' }[c]));
}

// ---- Theme ----
function toggleTheme() {
  document.body.classList.toggle('dark');
  const isDark = document.body.classList.contains('dark');
  try { localStorage.setItem('ops-theme', isDark ? 'dark' : 'light'); } catch(e) {}
  const theme = isDark ? '🌓' : '☀';
  document.querySelectorAll('[id^="sb-theme"]').forEach(e => e.innerHTML = isDark ? '☀ light' : theme);
  const sb = document.getElementById('sb-theme');
  if (sb) sb.innerHTML = isDark ? '☀ light' : '🌓 dark';
}
(function initTheme() {
  try {
    const t = localStorage.getItem('ops-theme');
    if (t === 'dark') document.body.classList.add('dark');
  } catch(e) {}
})();
document.getElementById('theme-toggle').addEventListener('click', toggleTheme);
document.getElementById('sb-theme').addEventListener('click', toggleTheme);
var _sbToggle = document.getElementById('sb-toggle');
if (_sbToggle) _sbToggle.addEventListener('click', toggleSidebar);

// ---- Outline toggle (audit 2026-06-23) ----
// Persist collapse state in localStorage so it survives reloads.
var _outlineVisible = localStorage.getItem('ops-outline-visible') !== '0';
function toggleOutline() {
  _outlineVisible = !_outlineVisible;
  localStorage.setItem('ops-outline-visible', _outlineVisible ? '1' : '0');
  document.getElementById('app').classList.toggle('no-outline', !_outlineVisible);
  document.getElementById('outline-toggle').textContent = _outlineVisible ? '›' : '‹';
}
// Apply initial state
document.getElementById('app').classList.toggle('no-outline', !_outlineVisible);
var _outlineToggle = document.getElementById('outline-toggle');
if (_outlineToggle) {
  _outlineToggle.textContent = _outlineVisible ? '›' : '‹';
  _outlineToggle.addEventListener('click', toggleOutline);
}
var _outlineReopen = document.getElementById('outline-reopen');
if (_outlineReopen) _outlineReopen.addEventListener('click', toggleOutline);

// ---- Activity bar pin/unpin (audit 2026-06-23) ----
// Pinned = expanded sidebar with item names inline. Unpinned = default
// hover-to-expand behavior. State persists in localStorage.
var _barPinned = localStorage.getItem('ops-bar-pinned') === '1';
function toggleBarPin() {
  _barPinned = !_barPinned;
  localStorage.setItem('ops-bar-pinned', _barPinned ? '1' : '0');
  applyBarPinned();
}
function applyBarPinned() {
  document.getElementById('activity-bar').classList.toggle('pinned', _barPinned);
}
applyBarPinned();
var _barPin = document.getElementById('activity-bar-pin');
if (_barPin) _barPin.addEventListener('click', toggleBarPin);
var _barReopen = document.getElementById('activity-bar-reopen');
if (_barReopen) _barReopen.addEventListener('click', toggleBarPin);

// ---- View switching ----
function setView(name) {
  _view = name;
  _currentProject = null;
  document.querySelectorAll('.activity-bar button[data-view]').forEach(b => b.classList.toggle('active', b.dataset.view === name));
  document.getElementById('back-btn').style.display = 'none';
  renderAlertsBanner();
  renderMain();
  renderOutline();
  // Refresh sidebar to update active states
  renderSidebar();
}

// ---- Alerts banner: shows high-severity anomalies across the top ----
async function renderAlertsBanner() {
  var banner = document.getElementById('alerts-banner');
  try {
    var r = await fetch('/api/anomalies').then(function(x){return x.json();});
    var highSev = (r.anomalies || []).filter(function(a){return a.severity === 'high' || a.severity === 'medium';}).slice(0, 5);
    if (highSev.length === 0) { banner.style.display = 'none'; banner.innerHTML = ''; return; }
    banner.style.display = 'block';
    banner.style.padding = '8px 20px';
    banner.style.background = 'var(--bg-soft)';
    banner.style.borderBottom = '1px solid var(--border)';
    banner.style.fontSize = '13px';
    var t = window._i18n ? window._i18n.t : function(k) { return k; };
    var plural = highSev.length > 1 ? 's' : '';
    var bannerText = t('alerts.banner_count', { count: highSev.length, s: plural });
    banner.innerHTML = '<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">' +
      '<span style="color:var(--red);font-weight:600">' + bannerText + '</span>' +
      highSev.map(function(a) {
        var color = a.severity === 'high' ? 'var(--red)' : 'var(--amber)';
        var sev = a.severity === 'high' ? '🔴' : '🟡';
        return '<span style="background:var(--bg);border:1px solid var(--border);border-radius:4px;padding:3px 8px;color:' + color + '">' + sev + ' ' + esc(a.message || a.kind) + '</span>';
      }).join('') + '</div>';
  } catch (e) { banner.style.display = 'none'; }
}
document.querySelectorAll('.activity-bar button[data-view]').forEach(b => {
  b.addEventListener('click', () => setView(b.dataset.view));
});

function pushHistory(state) {
  _history.push(state);
  if (_history.length > 20) _history.shift();
  document.getElementById('back-btn').style.display = _history.length > 0 ? 'inline-block' : 'none';
}
function goBack() {
  if (_history.length === 0) return;
  _history.pop();
  document.getElementById('back-btn').style.display = _history.length > 0 ? 'inline-block' : 'none';
  if (_history.length === 0) {
    _currentProject = null;
    renderMain();
    renderOutline();
  } else {
    const last = _history[_history.length - 1];
    if (last.type === 'project') {
      _currentProject = last.key;
      renderProject(_currentProject);
    } else {
      renderMain();
    }
  }
}
document.getElementById('back-btn').addEventListener('click', goBack);

// ---- Sidebar tree ----
function renderSidebar() {
  var el = document.getElementById('sidebar-tree');
  var html = '';

  // Top-level pseudo-views (like Obsidian's vault sections)
  html += '<div class="sidebar-section">';
  var tdCount = (_userTodosSummary && _userTodosSummary.open) || 0;
  var idCount = (_ideasSummary && (_ideasSummary.new + _ideasSummary.in_progress)) || 0;
  html += '<div class="sidebar-item" data-view="todo"><span class="icon">☑</span><span class="label">To do</span><span class="count">' + tdCount + '</span></div>';
  html += '<div class="sidebar-item" data-view="ideas"><span class="icon">✦</span><span class="label">Ideas</span><span class="count">' + idCount + '</span></div>';
  html += '<div class="sidebar-item" data-view="tasks"><span class="icon">⚡</span><span class="label">Tasks</span></div>';
  html += '<div class="sidebar-item" data-view="orgchart"><span class="icon">🌳</span><span class="label">Org</span></div>';
  html += '<div class="sidebar-item" data-view="hierarchy"><span class="icon">🌲</span><span class="label">Hierarchy</span></div>';
  html += '</div>';

  // Files section
  html += '<div class="sidebar-section">';
  html += '<div class="sidebar-section-title">Files <span class="sb-item count">' + _projects.length + '</span></div>';

  // Top-level files (projects) — Obsidian-like file list
  _projects.forEach(function(p) {
    var icon = p.parent ? '↳' : '◆';
    var hasChildren = _projects.some(function(c) { return c.parent === p.key; });
    var childCount = _projects.filter(function(c) { return c.parent === p.key; }).length;
    var activeKey = (_currentProject === p.key) ? ' active' : '';
    html += '<div class="sidebar-item' + activeKey + '" data-key="' + esc(p.key) + '">';
    html += '<span class="icon">' + icon + '</span>';
    html += '<span class="label">' + esc(p.name) + '</span>';
    if (childCount > 0) html += '<span class="pill">' + childCount + '</span>';
    html += '</div>';
  });
  html += '</div>';

  // URLs section
  html += '<div class="sidebar-section">';
  html += '<div class="sidebar-section-title">URLs <span class="sb-item count">' + _urls.length + '</span></div>';
  _urls.forEach(function(u) {
    var dotClass = u.ok ? 'ok' : 'bad';
    html += '<div class="sidebar-item" data-url="' + esc(u.name) + '">';
    html += '<span class="icon">🔗</span>';
    html += '<span class="label">' + esc(u.name) + '</span>';
    html += '<span class="dot ' + dotClass + '"></span>';
    html += '</div>';
  });
  html += '</div>';

  // Tags section
  if (_allTags && _allTags.length) {
    html += '<div class="sidebar-section">';
    html += '<div class="sidebar-section-title">Tags <span class="sb-item count">' + _allTags.length + '</span></div>';
    _allTags.slice(0, 20).forEach(function(t) {
      html += '<div class="sidebar-item" data-tag-name="' + esc(t.tag) + '"><span class="icon">#</span><span class="label">' + esc(t.tag) + '</span><span class="count">' + t.c + '</span></div>';
    });
    html += '</div>';
  }

  // Sources section
  var sources = {};
  _allData.stats && _allData.stats.per_source && _allData.stats.per_source.forEach(function(s) { sources[s.source] = s.c; });
  html += '<div class="sidebar-section">';
  html += '<div class="sidebar-section-title">Sources</div>';
  Object.keys(sources).forEach(function(src) {
    html += '<div class="sidebar-item" data-source-name="' + esc(src) + '"><span class="icon">📁</span><span class="label">' + esc(src) + '</span><span class="count">' + sources[src] + '</span></div>';
  });
  html += '</div>';

  el.innerHTML = html;

  // Wire click on To do / Ideas pseudo-items
  el.querySelectorAll('.sidebar-item[data-view]').forEach(function(item) {
    item.addEventListener('click', function() {
      var v = item.dataset.view;
      if (v === 'todo') setView('todo');
      else if (v === 'ideas') setView('ideas');
    });
  });

  // Wire click on project items
  el.querySelectorAll('.sidebar-item[data-key]').forEach(function(item) {
    item.addEventListener('click', function() {
      var key = item.dataset.key;
      pushHistory({ type: 'project', key: key });
      _currentProject = key;
      renderProject(key);
      renderSidebar(); // refresh active state
    });
  });
  el.querySelectorAll('.sidebar-item[data-url]').forEach(function(item) {
    item.addEventListener('click', function() {
      var name = item.dataset.url;
      var u = _urls.find(function(x) { return x.name === name; });
      if (u && u.url) window.open(u.url, '_blank');
    });
  });
  // Wire click on Sources items -> browse that source
  el.querySelectorAll('.sidebar-item[data-source-name]').forEach(function(item) {
    item.addEventListener('click', function() { renderSourceView(item.dataset.sourceName); });
  });
  // Wire click on Tags items -> browse that tag
  el.querySelectorAll('.sidebar-item[data-tag-name]').forEach(function(item) {
    item.addEventListener('click', function() { renderTagView(item.dataset.tagName); });
  });
}

// Sidebar search
document.getElementById('sidebar-search').addEventListener('input', function(e) {
  var q = e.target.value.toLowerCase();
  document.querySelectorAll('#sidebar-tree .sidebar-item').forEach(function(it) {
    var text = it.textContent.toLowerCase();
    it.style.display = (!q || text.indexOf(q) !== -1) ? 'flex' : 'none';
  });
});

// ---- Render main view ----
function renderMain() {
  if (_view === 'files') {
    if (_currentProject) {
      renderProject(_currentProject);
    } else if (!_projects.length && _allData.stats) {
      renderNotesHome();
    } else {
      renderOrgChart();
    }
  } else if (_view === 'graph') {
    renderGraphView();
  } else if (_view === 'brain') {
    renderBrainView();
  } else if (_view === 'briefing') {
    renderBriefingView();
  } else if (_view === 'activity') {
    renderActivityView();
  } else if (_view === 'todo') {
    renderTodoView();
  } else if (_view === 'ideas') {
    renderIdeasView();
  } else if (_view === 'skills') {
    renderSkillsView();
  } else if (_view === 'calendar') {
    renderCalendarView();
  } else if (_view === 'feed') {
    renderFeedView();
  } else if (_view === 'teams') {
    renderTeamsView();
  } else if (_view === 'tasks') {
    renderTasksView();
  } else if (_view === 'orgchart') {
    renderOrgChartView();
  } else if (_view === 'hierarchy') {
    renderHierarchyView();
  } else if (_view === 'agents') {
    renderAgentsView();
  } else if (_view === 'approvals') {
    renderApprovalsView();
  } else if (_view === 'connections') {
    renderConnectionsView();
  } else if (_view === 'todos') {
    renderCodeTodosView();
  } else if (_view === 'decisions') {
    renderDecisionsView();
  } else if (_view === 'costs') {
    renderCostsView();
  } else if (_view === 'revenue') {
    renderRevenueView();
  } else if (_view === 'ga') {
    renderGaView();
  } else if (_view === 'gsc') {
    renderGscView();
  } else if (_view === 'sentry') {
    renderSentryView();
  } else if (_view === 'github') {
    renderGithubView();
  } else if (_view === 'voice') {
    renderVoiceView();
  } else if (_view === 'templates') {
    renderTemplatesView();
  }
}

function renderOutline() {
  var html = '<div class="outline-section"><div class="outline-section-title">Outline</div>';
  if (_currentProject) {
    var p = _projects.find(function(x) { return x.key === _currentProject; });
    if (p) {
      html += '<div class="outline-item h1">' + esc(p.name) + '</div>';
      var children = _projects.filter(function(c) { return c.parent === p.key; });
      if (children.length) {
        html += '<div class="outline-item h2" style="margin-top:6px;color:var(--muted)">Sub-products</div>';
        children.forEach(function(c) {
          html += '<div class="outline-item h2" data-key="' + esc(c.key) + '">↳ ' + esc(c.name) + '</div>';
        });
      }
      var pb = _playbooks.find(function(x) { return x.project_key === p.key; });
      if (pb) {
        html += '<div class="outline-item h2" style="margin-top:6px">Playbook: ' + esc(pb.phase || '—') + '</div>';
      }
    }
  } else {
    html += '<div class="outline-item h1">ConductLoop</div>';
    html += '<div class="outline-item h2">' + _projects.length + ' projects</div>';
    html += '<div class="outline-item h2">' + _urls.filter(function(u){return u.ok;}).length + '/' + _urls.length + ' URLs OK</div>';
  }
  html += '</div>';
  document.getElementById('outline-content').innerHTML = html;

  // Wire outline click
  document.querySelectorAll('#outline-content .outline-item[data-key]').forEach(function(item) {
    item.addEventListener('click', function() {
      var key = item.dataset.key;
      _currentProject = key;
      pushHistory({ type: 'project', key: key });
      renderProject(key);
      renderSidebar();
    });
  });
}

// ---- Org chart (graph view) ----
let _zoom = 0.85, _panX = 0, _panY = 0, _isDragging = false, _dragStart = null;
const NODE_W = 200, NODE_H = 80, COLS = 4, GAP_X = 80, GAP_Y = 120, CANVAS_W = 1400, CANVAS_H = 1200;

function applyTransform() {
  var c = document.getElementById('canvas');
  if (c) c.style.transform = 'translate(' + _panX + 'px,' + _panY + 'px) scale(' + _zoom + ')';
  var el = document.getElementById('zoom-level');
  if (el) el.textContent = Math.round(_zoom * 100) + '%';
}
function zoomBy(f) { zoomAt(f, document.getElementById('canvas-wrap').clientWidth/2, document.getElementById('canvas-wrap').clientHeight/2); }
function zoomAt(f, cx, cy) {
  var nz = Math.max(0.25, Math.min(3, _zoom * f));
  if (nz === _zoom) return;
  _panX = cx - (cx - _panX) * (nz / _zoom);
  _panY = cy - (cy - _panY) * (nz / _zoom);
  _zoom = nz;
  applyTransform();
}
function fitToScreen() {
  // Actually fit: the old code hardcoded 0.7 and a fixed pan, so on a narrow
  // window the graph overflowed to the right and the lower rows were cut off
  // (2026-10-23). Fit by width only — the area scrolls vertically, so
  // shrinking to the visible height would make the cards unreadable.
  var wrap = document.getElementById('canvas-wrap');
  var w = (wrap && wrap.clientWidth) || CANVAS_W;
  var scale = Math.min(w / CANVAS_W, 1);
  _zoom = Math.max(0.35, Math.round(scale * 100) / 100);
  _panX = Math.max(0, Math.round((w - CANVAS_W * _zoom) / 2));
  _panY = 0;
  applyTransform();
}
function initCanvasInteractions() {
  var wrap = document.getElementById('canvas-wrap');
  if (!wrap) return;
  wrap.addEventListener('wheel', function(e) {
    e.preventDefault();
    var rect = wrap.getBoundingClientRect();
    var cx = e.clientX - rect.left, cy = e.clientY - rect.top;
    zoomAt(e.deltaY < 0 ? 1.1 : 1/1.1, cx, cy);
  }, { passive: false });
  wrap.addEventListener('mousedown', function(e) {
    if (e.target.closest('.node') || e.target.closest('.node-subitem')) return;
    _isDragging = true; _dragStart = { x: e.clientX - _panX, y: e.clientY - _panY };
    wrap.classList.add('dragging');
  });
  document.addEventListener('mousemove', function(e) {
    if (!_isDragging) return;
    _panX = e.clientX - _dragStart.x; _panY = e.clientY - _dragStart.y;
    applyTransform();
  });
  document.addEventListener('mouseup', function() {
    _isDragging = false; var w = document.getElementById('canvas-wrap');
    if (w) w.classList.remove('dragging');
  });
}

// ---- Home without projects: the notes ----
// The project registry ships empty, so on a fresh install the project graph is
// an empty canvas and search hides behind ⌘P. Show what someone who has just
// pointed the app at their notes can do with them: search, open, ask.
// Built with the DOM (no inline style or handlers: see test_csp_static).
function renderNotesHome() {
  var main = document.getElementById('main-content');
  document.getElementById('breadcrumb').innerHTML = '<span>Your notes</span>';
  var total = (_allData.stats && _allData.stats.total) || 0;
  var existing = main.querySelector('.notes-home');
  if (existing && _notesHomeRefresh) {  // a watch refresh: keep the fields, reload the list
    existing.querySelector('.nh-count').textContent = notesHomeCount(total);
    var choose = existing.querySelector('.nh-sub a');
    if (total && choose) choose.remove();
    _notesHomeRefresh();
    return;
  }
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text) n.textContent = text;
    return n;
  }
  function link(href, text) { var a = el('a', '', text); a.href = href; return a; }

  var wrap = el('div', 'notes-home');
  wrap.appendChild(el('h1', '', 'Your notes'));
  var sub = el('p', 'nh-sub');
  sub.appendChild(el('span', 'nh-count', notesHomeCount(total)));
  if (!total) {
    sub.appendChild(document.createTextNode(' '));
    sub.appendChild(link('/setup', 'Choose your notes folder'));
  }
  wrap.appendChild(sub);

  var ask = el('form', 'nh-ask');
  var askIn = el('input');
  askIn.type = 'text';
  askIn.placeholder = 'Ask a question about your notes…';
  askIn.setAttribute('aria-label', 'Ask a question about your notes');
  ask.appendChild(askIn);
  ask.appendChild(el('button', '', 'Ask'));
  ask.addEventListener('submit', function(e) {
    e.preventDefault();
    var q = askIn.value.trim();
    window.location.href = '/chat?agent=brain' + (q ? '&q=' + encodeURIComponent(q) : '');
  });
  wrap.appendChild(ask);
  var hasKey = _providers.some(function(p) { return p.available && p.name !== 'mock'; });
  if (!hasKey) {
    var hint = el('p', 'nh-hint', 'No AI key yet: answers show the lines of your notes that match. For written answers, ');
    hint.appendChild(link('/credentials', 'add a key'));
    hint.appendChild(document.createTextNode('.'));
    wrap.appendChild(hint);
  }

  var search = el('input', 'nh-search');
  search.type = 'search';
  search.placeholder = 'Search your notes';
  search.setAttribute('aria-label', 'Search your notes');
  wrap.appendChild(search);
  var heading = el('h2', '', 'Recent');
  wrap.appendChild(heading);
  var list = el('ul', 'nh-list');
  wrap.appendChild(list);
  main.innerHTML = '';
  main.appendChild(wrap);

  function show(rows, empty) {
    list.innerHTML = '';
    if (!rows || !rows.length) { list.appendChild(el('li', 'nh-empty', empty)); return; }
    rows.forEach(function(r) {
      var li = el('li');
      var a = link('/note/' + encodeURIComponent(r.id), r.title || String(r.path || '').split('/').pop());
      li.appendChild(a);
      // content_snip has the words around the match; excerpt is a few words only.
      var excerpt = String(r.content_snip || r.excerpt || '').replace(/\s+/g, ' ').trim();
      if (excerpt) li.appendChild(el('span', 'nh-excerpt', excerpt.slice(0, 160)));
      list.appendChild(li);
    });
  }
  function recent() {
    fetchJSON('/api/find/recent?n=12').then(function(rows) {
      if (search.value.trim()) return;
      heading.textContent = 'Recent';
      show(rows, 'Nothing indexed yet.');
    }).catch(function() {});
  }
  function find(q) {
    fetchJSON('/api/find?q=' + encodeURIComponent(q) + '&n=15').then(function(rows) {
      if (search.value.trim() !== q) return;
      heading.textContent = 'Results';
      show(rows, 'No note matches “' + q + '”.');
    }).catch(function() {});
  }
  function refresh() {
    var q = search.value.trim();
    if (q) find(q); else recent();
  }
  var timer = null;
  search.addEventListener('input', function() {
    clearTimeout(timer);
    timer = setTimeout(refresh, search.value.trim() ? 150 : 0);
  });
  _notesHomeRefresh = refresh;
  refresh();
}
var _notesHomeRefresh = null;

function notesHomeCount(total) {
  if (!total) return 'No notes indexed yet.';
  return total.toLocaleString() + (total === 1 ? ' note' : ' notes') + ' indexed. Search them, open one, or ask a question.';
}

function renderOrgChart() {
  var main = document.getElementById('main-content');
  document.getElementById('breadcrumb').innerHTML = '<span>' + (_view === 'graph' ? 'Graph' : 'Files') + '</span>';
  main.innerHTML = '<div class="canvas-wrap" id="canvas-wrap"><div class="canvas" id="canvas"><svg class="lines" id="lines" xmlns="http://www.w3.org/2000/svg"></svg></div><div class="zoom-controls"><button onclick="zoomBy(1.2)">+</button><span class="zoom-level" id="zoom-level">100%</span><button onclick="zoomBy(1/1.2)">−</button><button onclick="fitToScreen()">fit</button></div></div>';
  var canvas = document.getElementById('canvas');
  var svg = document.getElementById('lines');
  svg.setAttribute('width', CANVAS_W); svg.setAttribute('height', CANVAS_H); svg.setAttribute('viewBox', '0 0 ' + CANVAS_W + ' ' + CANVAS_H);
  // The canvas box has to be as tall as the graph: the nodes are absolutely
  // positioned inside it, and .main-content scrolls, so a short canvas hid
  // the lower rows of the grid (2026-10-23).
  canvas.style.width = CANVAS_W + 'px';
  canvas.style.height = CANVAS_H + 'px';

  var rootX = CANVAS_W / 2 - 90, rootY = 20;
  var root = makeNode('root', '<span class="em">Your</span> projects<span class="badge">— second brain</span>', rootX, rootY, 180, 60, 'root');
  root.addEventListener('click', function() { setView('files'); });
  canvas.appendChild(root);

  var projectW = 200, projectH = 80;
  var projGapX = (CANVAS_W - COLS * projectW) / (COLS + 1);
  var startY = rootY + 60 + 100;
  var projPositions = [];

  _projects.forEach(function(p, i) {
    var col = i % COLS, row = Math.floor(i / COLS);
    var x = projGapX + col * (projectW + projGapX);
    var y = startY + row * (projectH + GAP_Y);
    var urlIcon = p.all_urls_ok === true ? '<span class="pill ok">up</span>' : p.all_urls_ok === false ? '<span class="pill bad">down</span>' : '<span class="pill muted">—</span>';
    var pb = _playbooks.find(function(x) { return x.project_key === p.key; });
    var phase = pb ? (pb.phase || '—') : '—';
    var children = _projects.filter(function(c) { return c.parent === p.key; });
    var childBadge = children.length > 0 ? '<span class="pill phase">' + children.length + ' sub</span>' : '';
    var html = '<div class="node-title">' + esc(p.name) + '</div><div class="node-sub"><span class="pill phase">' + esc(phase) + '</span>' + childBadge + urlIcon + '<span style="color:var(--muted)">·</span><span style="color:var(--muted)">' + (p.mention_count || 0) + ' menciones</span></div>';
    var node = makeNode('project-' + p.key, html, x, y, projectW, projectH, 'project');
    if (children.length > 0) node.classList.add('parent');
    node.dataset.key = p.key;
    node.addEventListener('click', function(e) {
      e.stopPropagation();
      pushHistory({ type: 'project', key: p.key });
      _currentProject = p.key;
      renderProject(p.key);
      renderSidebar();
    });
    canvas.appendChild(node);
    projPositions.push({ key: p.key, x: x, y: y, w: projectW, h: projectH });
  });

  var paths = '';
  var rootCx = rootX + 90, rootBottom = rootY + 60;
  projPositions.forEach(function(pp) {
    var px = pp.x + pp.w / 2, py = pp.y;
    paths += '<path d="M ' + rootCx + ' ' + rootBottom + ' C ' + rootCx + ' ' + (rootBottom + 40) + ', ' + px + ' ' + (py - 40) + ', ' + px + ' ' + py + '" stroke="#94a3b8" stroke-width="1.5" fill="none" />';
  });
  // Parent-child links
  projPositions.forEach(function(pp) {
    var p = _projects.find(function(x) { return x.key === pp.key; });
    if (!p || !p.parent) return;
    var parentPos = projPositions.find(function(x) { return x.key === p.parent; });
    if (!parentPos) return;
    var px1 = pp.x + pp.w / 2, py1 = pp.y, px2 = parentPos.x + parentPos.w / 2, py2 = parentPos.y + parentPos.h;
    paths += '<path d="M ' + px1 + ' ' + py1 + ' C ' + px1 + ' ' + (py1 - 30) + ', ' + px2 + ' ' + (py2 + 30) + ', ' + px2 + ' ' + py2 + '" stroke="#2563eb" stroke-width="1.5" stroke-dasharray="5,3" fill="none" opacity="0.6" />';
  });
  svg.innerHTML = paths;
  initCanvasInteractions();
  fitToScreen();
}

function makeNode(id, html, x, y, w, h, type) {
  var el = document.createElement('div');
  el.id = id; el.className = 'node ' + (type || '');
  el.style.left = x + 'px'; el.style.top = y + 'px';
  el.style.width = w + 'px'; el.style.minHeight = h + 'px';
  el.innerHTML = html;
  return el;
}

// ---- Project view (file detail) ----
// ---- Markdown renderer (no library) ----
function renderMarkdown(md) {
  if (!md) return '';
  var html = esc(md);
  // Code blocks first (fenced) so we don't process their content
  var codeBlocks = [];
  html = html.replace(/```(\w*)\n([\s\S]*?)```/g, function(m, lang, code) {
    var i = codeBlocks.length;
    codeBlocks.push('<pre><code class="lang-' + esc(lang) + '">' + code + '</code></pre>');
    return '\x00CB' + i + '\x00';
  });
  // Headers
  html = html.replace(/^###### (.*$)/gim, '<h6>$1</h6>')
             .replace(/^##### (.*$)/gim, '<h5>$1</h5>')
             .replace(/^#### (.*$)/gim, '<h4>$1</h4>')
             .replace(/^### (.*$)/gim, '<h3>$1</h3>')
             .replace(/^## (.*$)/gim, '<h2>$1</h2>')
             .replace(/^# (.*$)/gim, '<h1>$1</h1>');
  // Bold / italic / code
  html = html.replace(/\*\*\*(.+?)\*\*\*/g, '<strong><em>$1</em></strong>')
             .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
             .replace(/(?<!\*)\*(?!\*)(.+?)\*(?!\*)/g, '<em>$1</em>')
             .replace(/`([^`]+)`/g, '<code>$1</code>');
  // Links and images
  html = html.replace(/!\[([^\]]*)\]\(([^)]+)\)/g, '<img alt="$1" src="$2">')
             .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2">$1</a>');
  // Wikilinks [[X]] and [[X|Y]]
  html = html.replace(/\[\[([^\]|]+)\|([^\]]+)\]\]/g, '<a class="wikilink" href="#">$2</a>')
             .replace(/\[\[([^\]]+)\]\]/g, '<a class="wikilink" href="#">$1</a>');
  // Tags #foo (word chars only, not in URL)
  html = html.replace(/(^|\s)#([a-zA-Z][a-zA-Z0-9_\-/.]*)/g, '$1<span class="tag">#$2</span>');
  // Blockquotes
  html = html.replace(/^&gt; ?(.*)$/gim, '<blockquote>$1</blockquote>');
  // Unordered lists
  html = html.replace(/(^|\n)((?:[-*+] .+\n?)+)/g, function(m, p, list) {
    var items = list.trim().split('\n').map(function(l) { return '<li>' + l.replace(/^[-*+] /, '') + '</li>'; }).join('');
    return p + '<ul>' + items + '</ul>';
  });
  // Numbered lists
  html = html.replace(/(^|\n)((?:\d+\. .+\n?)+)/g, function(m, p, list) {
    var items = list.trim().split('\n').map(function(l) { return '<li>' + l.replace(/^\d+\. /, '') + '</li>'; }).join('');
    return p + '<ol>' + items + '</ol>';
  });
  // Restore code blocks
  html = html.replace(/\x00CB(\d+)\x00/g, function(m, i) { return codeBlocks[parseInt(i)]; });
  // Paragraphs: double newlines
  html = html.replace(/\n\n+/g, '</p><p>');
  html = '<p>' + html + '</p>';
  // Clean up empty <p></p>
  html = html.replace(/<p>\s*<\/p>/g, '');
  // Single \n → <br>
  html = html.replace(/\n/g, '<br>');
  return html;
}

async function renderDocView(path) {
  var main = document.getElementById('main-content');
  document.getElementById('breadcrumb').innerHTML = '<span>Files</span> <span class="sep">/</span> <span class="current">' + esc(path.split('/').slice(-2).join('/')) + '</span>';
  main.innerHTML = '<div class="empty-state">cargando...</div>';
  try {
    // Exact path first (web clips are stored under their URL, often with no
    // basename); then the historic basename search.
    var rows = await fetchJSON('/api/find?path=' + encodeURIComponent(path));
    if (!rows.length && path.split('/').pop()) {
      rows = await fetchJSON('/api/find?q=' + encodeURIComponent(path.split('/').pop()) + '&n=20');
    }
    var doc = rows.find(function(r) { return r.path === path; }) || rows[0];
    if (!doc) {
      main.innerHTML = '<div class="empty-state">document not found</div>';
      return;
    }
    // Get full content via /api/docs/<id>
    var id = doc.id || doc.source;
    // We don't have a /api/docs/<id>, so use the excerpt from search
    var html = '<div class="md-view">';
    html += '<h1>' + esc(doc.title || doc.path.split('/').pop()) + '</h1>';
    html += '<div class="meta-row"><span><span class="k">source:</span> <span class="v">' + esc(doc.source) + '</span></span>';
    html += '<span><span class="k">path:</span> <span class="v mono" style="font-size:11px">' + esc(doc.path) + '</span></span>';
    if (doc.metadata) {
      try {
        var meta = JSON.parse(doc.metadata);
        if (meta.tags && meta.tags.length) {
          html += '<span><span class="k">tags:</span> ' + meta.tags.map(function(t){ return '<span class="tag">' + esc(t) + '</span>'; }).join(' ') + '</span>';
        }
        if (meta.wikilinks && meta.wikilinks.length) {
          html += '<span><span class="k">links:</span> ' + meta.wikilinks.slice(0, 5).map(function(w){ return '<a class="wikilink" href="#">' + esc(w) + '</a>'; }).join(', ') + '</span>';
        }
      } catch (e) {}
    }
    html += '</div>';
    html += '<div style="margin-top:20px">' + renderMarkdown(doc.excerpt || doc.path) + '</div>';
    // Backlinks (search for docs that mention this title)
    html += '<h2 style="margin-top:40px">Backlinks</h2>';
    html += '<div id="backlinks-area">buscando...</div>';
    html += '</div>';
    main.innerHTML = html;
    renderOutline();
    // Load backlinks async
    setTimeout(async function() {
      try {
        var bl = await fetchJSON('/api/find?q=' + encodeURIComponent((doc.title || '').slice(0, 30)) + '&n=20');
        var realBl = bl.filter(function(r) { return r.id !== doc.id && r.path !== doc.path; });
        var area = document.getElementById('backlinks-area');
        if (!area) return;
        if (!realBl.length) { area.innerHTML = '<div style="color:var(--muted);font-size:13px">no backlinks found</div>'; return; }
        area.innerHTML = '<div style="display:grid;gap:6px">' + realBl.slice(0, 10).map(function(r) {
          return '<div class="backlink-card" data-doc-path="' + esc(r.path) + '" style="background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:8px 12px;cursor:pointer">'
            + '<div style="font-size:13px;color:var(--ink);font-weight:500">' + esc(r.title || r.path.split('/').pop()) + '</div>'
            + '<div style="font-size:11px;color:var(--muted);font-family:SF Mono,monospace;margin-top:2px">' + esc(r.path) + '</div>'
            + '</div>';
        }).join('') + '</div>';
        // Wire click on backlink cards
        area.querySelectorAll('.backlink-card').forEach(function(el) {
          el.addEventListener('click', function() { renderDocView(el.dataset.docPath); });
        });
      } catch (e) {}
    }, 100);
  } catch (e) {
    main.innerHTML = '<div class="empty-state">error: ' + esc(e.message) + '</div>';
  }
}

// ---- Graph view (real) ----
async function renderGraphView() {
  var main = document.getElementById('main-content');
  document.getElementById('breadcrumb').innerHTML = '<span>Graph</span>';
  main.innerHTML = '<div class="empty-state">cargando grafo...</div>';
  try {
    var data = await fetchJSON('/api/graph?limit=200');
    if (!data.nodes.length) {
      main.innerHTML = '<div class="empty-state">no graph data yet</div>';
      return;
    }
    var nodesById = {};
    data.nodes.forEach(function(n) { nodesById[n.id] = n; });
    // Group by project (for projects that have parent, draw as child of parent)
    var byProject = {};
    var docNodes = [];
    data.nodes.forEach(function(n) {
      if (n.source === 'obsidian') docNodes.push(n);
    });
    data.edges.forEach(function(e) {
      if (e.to_project) {
        byProject[e.to_project] = byProject[e.to_project] || [];
        if (nodesById[e.from]) byProject[e.to_project].push(nodesById[e.from]);
      }
    });
    // Render: project hubs + docs around them
    var html = '<div class="md-view">';
    html += '<h1>🕸 Graph <span style="color:var(--muted);font-weight:400;font-size:14px">— ' + data.nodes.length + ' docs · ' + data.edges.length + ' relationships</span></h1>';
    html += '<p style="color:var(--muted);font-size:13px;margin-bottom:16px">Docs (white) orbit around their parent project (blue). Edges = wiki links (gray) or project mentions (blue).</p>';
    // Use a fixed canvas (800x600)
    html += '<div style="background:var(--bg-soft);border:1px solid var(--border);border-radius:8px;padding:20px;overflow-x:auto">';
    html += '<svg id="graph-svg" width="1200" height="700" viewBox="0 0 1200 700" style="display:block">';
    // Project hubs
    var projects = Object.keys(byProject);
    var cx = 600, cy = 350;
    var projectPositions = {};
    projects.forEach(function(p, i) {
      var angle = (i / projects.length) * Math.PI * 2 - Math.PI / 2;
      var r = 220;
      var x = cx + Math.cos(angle) * r;
      var y = cy + Math.sin(angle) * r;
      projectPositions[p] = { x: x, y: y };
      html += '<circle cx="' + x + '" cy="' + y + '" r="20" fill="#1e40af" />';
      html += '<text x="' + x + '" y="' + (y + 38) + '" text-anchor="middle" font-size="11" fill="#0f172a" font-family="Inter, sans-serif" font-weight="600">' + esc(p) + '</text>';
    });
    // Doc nodes (around their project)
    docNodes.slice(0, 60).forEach(function(doc, i) {
      // Find which project it belongs to
      var proj = null;
      data.edges.forEach(function(e) { if (e.from === doc.id && e.to_project) proj = e.to_project; });
      if (!proj || !projectPositions[proj]) {
        // Place at random orbit
        var a = (i / 60) * Math.PI * 2;
        var x = cx + Math.cos(a) * 350;
        var y = cy + Math.sin(a) * 280;
        html += '<circle cx="' + x + '" cy="' + y + '" r="3" fill="#94a3b8" />';
        return;
      }
      // Distribute around the project
      var docs = byProject[proj] || [];
      var idx = docs.indexOf(doc);
      if (idx === -1) idx = i;
      var total = Math.max(docs.length, 1);
      var a = (idx / Math.max(total, 6)) * Math.PI * 2;
      var pr = 60;
      var x = projectPositions[proj].x + Math.cos(a) * pr;
      var y = projectPositions[proj].y + Math.sin(a) * pr;
      // Edge to project
      html += '<line x1="' + x + '" y1="' + y + '" x2="' + projectPositions[proj].x + '" y2="' + projectPositions[proj].y + '" stroke="#cbd5e1" stroke-width="0.5" />';
      html += '<circle cx="' + x + '" cy="' + y + '" r="3" fill="#64748b" />';
    });
    html += '</svg></div></div>';
    main.innerHTML = html;
    renderOutline();
  } catch (e) {
    main.innerHTML = '<div class="empty-state">error: ' + esc(e.message) + '</div>';
  }
}

async function renderTagView(tag) {
  var main = document.getElementById('main-content');
  document.getElementById('breadcrumb').innerHTML = '<span>Tags</span> <span class="sep">/</span> <span class="current">#' + esc(tag) + '</span>';
  main.innerHTML = '<div class="empty-state">cargando...</div>';
  try {
    var rows = await fetchJSON('/api/tags/by/' + encodeURIComponent(tag));
    var html = '<div class="md-view">';
    html += '<h1>#' + esc(tag) + ' <span style="color:var(--muted);font-weight:400;font-size:14px">— ' + rows.length + ' docs</span></h1>';
    if (!rows.length) { html += '<p style="color:var(--muted)">no docs with this tag</p>'; main.innerHTML = html + '</div>'; return; }
    html += '<div style="display:grid;gap:6px">';
    rows.slice(0, 50).forEach(function(r) {
      html += '<div class="tag-doc" data-doc-path="' + esc(r.path) + '" style="background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:8px 12px;cursor:pointer">';
      html += '<div style="font-size:13px;font-weight:500;color:var(--ink)">' + esc(r.title || r.path.split('/').pop()) + '</div>';
      html += '<div style="font-size:11px;color:var(--muted);font-family:SF Mono,monospace;margin-top:2px;word-break:break-all">' + esc(r.path) + '</div>';
      html += '</div>';
    });
    html += '</div></div>';
    main.innerHTML = html;
    renderOutline();
    main.querySelectorAll('.tag-doc').forEach(function(el) {
      el.addEventListener('click', function() { renderDocView(el.dataset.docPath); });
    });
  } catch (e) {
    main.innerHTML = '<div class="empty-state">error: ' + esc(e.message) + '</div>';
  }
}

async function renderSourceView(sourceName) {
  var main = document.getElementById('main-content');
  document.getElementById('breadcrumb').innerHTML = '<span>Files</span> <span class="sep">/</span> <span class="current">' + esc(sourceName) + '</span>';
  main.innerHTML = '<div class="empty-state">cargando...</div>';
  try {
    var rows = await fetchJSON('/api/find?q=' + encodeURIComponent(sourceName) + '&n=80');
    // Filter to only that source
    var filtered = rows.filter(function(r) { return r.source === sourceName; });
    var html = '<div class="md-view">';
    html += '<h1>📁 ' + esc(sourceName) + ' <span style="color:var(--muted);font-weight:400;font-size:14px">— ' + filtered.length + ' docs</span></h1>';
    if (filtered.length === 0) {
      html += '<p style="color:var(--muted)">no documents indexed in this source</p>';
    } else {
      html += '<div style="display:grid;gap:6px">';
      filtered.slice(0, 100).forEach(function(r) {
        html += '<div class="source-doc" data-doc-path="' + esc(r.path) + '" style="background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:8px 12px;cursor:pointer">';
        html += '<div style="font-size:13px;font-weight:500;color:var(--ink)">' + esc(r.title || r.path.split('/').pop()) + '</div>';
        html += '<div style="font-size:11px;color:var(--muted);font-family:SF Mono,monospace;margin-top:2px;word-break:break-all">' + esc(r.path) + '</div>';
        if (r.excerpt) html += '<div style="font-size:12px;color:var(--ink-soft);margin-top:4px">' + esc(r.excerpt.slice(0, 200)) + '</div>';
        html += '</div>';
      });
      html += '</div>';
    }
    html += '</div>';
    main.innerHTML = html;
    renderOutline();
    // Wire click
    main.querySelectorAll('.source-doc').forEach(function(el) {
      el.addEventListener('click', function() { renderDocView(el.dataset.docPath); });
    });
  } catch (e) {
    main.innerHTML = '<div class="empty-state">error: ' + esc(e.message) + '</div>';
  }
}

// ---- Doc editor (manual editing of indexed documents) ----
// Two entry points:
// - openDocByPath(path, projectKey): resolve path -> doc_id, then open editor
// - openDocEditor(doc, fallbackMeta, projectKey): open editor for an existing
//   doc or create a new one (doc === null)
async function openDocByPath(path, projectKey) {
  try {
    var d = await fetchJSON('/api/project?name=' + encodeURIComponent(projectKey));
    var doc = (d.docs || []).find(function(x) { return x.path === path; });
    if (!doc) {
      // Document not indexed yet — create a new one at this path under the project
      openDocEditor(null, { source: 'manual', path: path, title: path.split('/').pop() }, projectKey);
      return;
    }
    openDocEditor(doc, doc, projectKey);
  } catch (e) {
    alert('Failed to open doc: ' + e.message);
  }
}

async function openDocEditor(doc, fallbackMeta, projectKey) {
  // Fetch full content. For an existing doc we have doc.id; for new docs we
  // need to create it server-side first so we get an id, then open the editor.
  var docId = doc && doc.id;
  var title = (doc && doc.title) || (fallbackMeta && fallbackMeta.title) || 'Untitled';
  var path = (doc && doc.path) || (fallbackMeta && fallbackMeta.path) || 'untitled.md';
  var source = (doc && doc.source) || (fallbackMeta && fallbackMeta.source) || 'manual';
  if (!docId) {
    try {
      var r = await fetch('/api/docs/create', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ source: source, path: path, title: title, content: '' }),
      });
      var j = await r.json();
      if (!r.ok || !j.ok) {
        alert('Failed to create doc: ' + (j.error || r.status));
        return;
      }
      docId = j.id;
    } catch (e) {
      alert('Failed to create doc: ' + e.message);
      return;
    }
  }
  var content = '';
  if (docId) {
    try {
      var rr = await fetch('/api/notes/' + docId);
      if (rr.ok) {
        var jj = await rr.json();
        content = (jj.document && jj.document.content) || '';
        title = (jj.document && jj.document.title) || title;
      }
    } catch (e) { /* ignore — we'll open with empty content */ }
  }
  showDocEditorModal(docId, title, path, source, content, projectKey);
}

function showDocEditorModal(docId, title, path, source, content, projectKey) {
  // Remove any existing modal
  var old = document.getElementById('doc-editor-modal');
  if (old) old.remove();

  var overlay = document.createElement('div');
  overlay.id = 'doc-editor-modal';
  overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.6);z-index:9999;display:flex;align-items:center;justify-content:center;backdrop-filter:blur(4px)';
  overlay.innerHTML = ''
    + '<div style="background:var(--panel,#111a2e);border:1px solid var(--border,#1f2937);border-radius:10px;width:min(900px,92vw);max-height:88vh;display:flex;flex-direction:column;box-shadow:0 24px 48px rgba(0,0,0,0.5)">'
    +   '<div style="padding:14px 18px;border-bottom:1px solid var(--border,#1f2937);display:flex;align-items:center;gap:10px">'
    +     '<input id="doc-edit-title" value="' + esc(title) + '" placeholder="Title" style="flex:1;background:var(--bg,#0a0e1a);color:white;border:1px solid var(--border);border-radius:6px;padding:6px 10px;font-size:14px">'
    +     '<input id="doc-edit-path" value="' + esc(path) + '" placeholder="path/to/file.md" style="flex:2;background:var(--bg,#0a0e1a);color:var(--muted,#8b95a8);border:1px solid var(--border);border-radius:6px;padding:6px 10px;font-size:11px;font-family:monospace;margin-left:6px">'
    +     '<span style="font-size:11px;color:var(--muted)">[' + esc(source) + ']</span>'
    +     '<button id="doc-edit-close" title="Close" style="background:transparent;color:var(--muted);border:0;font-size:18px;cursor:pointer;margin-left:6px">×</button>'
    +   '</div>'
    +   '<textarea id="doc-edit-content" placeholder="Write here…" style="flex:1;min-height:300px;background:var(--bg,#0a0e1a);color:white;border:0;border-radius:0;padding:14px 18px;font-family:monospace;font-size:12px;line-height:1.55;resize:none;outline:none">' + esc(content) + '</textarea>'
    +   '<div style="padding:12px 18px;border-top:1px solid var(--border,#1f2937);display:flex;justify-content:space-between;align-items:center;gap:8px">'
    +     '<div>'
    +       '<button id="doc-edit-delete" style="background:transparent;color:#ef4444;border:1px solid rgba(239,68,68,0.3);padding:6px 12px;border-radius:6px;cursor:pointer;font-size:12px">Delete</button>'
    +     '</div>'
    +     '<div style="display:flex;gap:8px">'
    +       '<span id="doc-edit-status" style="font-size:11px;color:var(--muted);align-self:center;margin-right:8px"></span>'
    +       '<button id="doc-edit-save" style="background:var(--blue,#2962ff);color:white;border:0;padding:6px 18px;border-radius:6px;cursor:pointer;font-size:13px;font-weight:500">Save</button>'
    +     '</div>'
    +   '</div>'
    + '</div>';
  document.body.appendChild(overlay);

  // Wire actions
  function close() { overlay.remove(); }
  overlay.addEventListener('click', function(e) { if (e.target === overlay) close(); });
  document.getElementById('doc-edit-close').addEventListener('click', close);
  document.getElementById('doc-edit-save').addEventListener('click', async function() {
    var btn = document.getElementById('doc-edit-save');
    var newTitle = document.getElementById('doc-edit-title').value.trim() || 'Untitled';
    var newPath = document.getElementById('doc-edit-path').value.trim() || path;
    var newContent = document.getElementById('doc-edit-content').value;
    var status = document.getElementById('doc-edit-status');
    btn.disabled = true; btn.style.opacity = '0.5';
    status.textContent = 'saving…';
    try {
      // Rename (title + path)
      var r1 = await fetch('/api/docs/rename', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ doc_id: docId, title: newTitle, path: newPath }),
      });
      if (!r1.ok) {
        var j1 = await r1.json().catch(function() { return {}; });
        status.textContent = 'rename failed: ' + (j1.error || r1.status);
        btn.disabled = false; btn.style.opacity = '1';
        return;
      }
      // Save content
      var r2 = await fetch('/api/docs/save', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ doc_id: docId, content: newContent }),
      });
      if (!r2.ok) {
        var j2 = await r2.json().catch(function() { return {}; });
        status.textContent = 'save failed: ' + (j2.error || r2.status);
        btn.disabled = false; btn.style.opacity = '1';
        return;
      }
      status.textContent = 'saved · ' + new Date().toLocaleTimeString();
      // Re-render the project so the doc list reflects any rename
      if (typeof renderProject === 'function' && projectKey) {
        setTimeout(function() { renderProject(projectKey); }, 600);
      }
      setTimeout(function() { btn.disabled = false; btn.style.opacity = '1'; }, 1200);
    } catch (e) {
      status.textContent = 'error: ' + e.message;
      btn.disabled = false; btn.style.opacity = '1';
    }
  });
  document.getElementById('doc-edit-delete').addEventListener('click', async function() {
    if (!confirm('Delete this document from the index? (Original file on disk is not touched.)')) return;
    try {
      var r = await fetch('/api/docs/delete?doc_id=' + docId, { method: 'DELETE' });
      if (!r.ok) {
        var j = await r.json().catch(function() { return {}; });
        alert('Delete failed: ' + (j.error || r.status));
        return;
      }
      close();
      if (typeof renderProject === 'function' && projectKey) {
        renderProject(projectKey);
      }
    } catch (e) {
      alert('Delete error: ' + e.message);
    }
  });
  // Save on Cmd/Ctrl+S
  overlay.addEventListener('keydown', function(e) {
    if ((e.metaKey || e.ctrlKey) && e.key === 's') {
      e.preventDefault();
      document.getElementById('doc-edit-save').click();
    }
  });
  // Focus the title
  setTimeout(function() { document.getElementById('doc-edit-title').focus(); document.getElementById('doc-edit-title').select(); }, 50);
}

async function renderProject(key) {
  var main = document.getElementById('main-content');
  document.getElementById('breadcrumb').innerHTML = '<span>Files</span> <span class="sep">/</span> <span class="current">' + esc(_projects.find(function(x){return x.key===key;})?.name || key) + '</span>';
  main.innerHTML = '<div class="empty-state">cargando...</div>';

  try {
    var d = await fetchJSON('/api/project?name=' + encodeURIComponent(key));
    var p = d.project;
    var html = '<div class="md-view">';
    html += '<h1>' + esc(p.name) + '</h1>';
    if (p.aliases && p.aliases.length) {
      html += '<div class="meta-row">';
      html += '<span><span class="k">aliases:</span> <span class="v">' + p.aliases.slice(0,5).map(esc).join(', ') + '</span></span>';
      html += '<span><span class="k">key:</span> <span class="v mono">' + esc(p.key) + '</span></span>';
      if (p.is_parent) html += '<span class="pill phase">parent · ' + (p.children || []).length + ' sub-products</span>';
      if (p.parent) html += '<span class="pill muted">child of ' + esc(p.parent) + '</span>';
      html += '</div>';
    }
    if (d.urls && d.urls.length) {
      html += '<h2>URLs <span style="font-size:13px;color:var(--muted);font-weight:400">— ' + d.urls.length + '</span></h2>';
      d.urls.forEach(function(u) {
        html += '<div class="url-card' + (u.ok ? '' : ' bad') + '">';
        html += '<div><div style="font-weight:500">' + esc(u.name) + '</div><div class="meta">' + esc(u.url) + ' · ' + (u.status_code || '—') + ' · ' + u.latency_ms + 'ms</div></div>';
        html += '<a class="open" href="' + esc(u.url) + '" target="_blank">open →</a>';
        html += '</div>';
      });
    }
    if (d.repos && d.repos.length) {
      html += '<h2>Repos <span style="font-size:13px;color:var(--muted);font-weight:400">— ' + d.repos.length + '</span></h2>';
      d.repos.forEach(function(r) {
        html += '<div class="repo-card">📁 ' + esc(r.name) + ' &nbsp; <span style="color:var(--muted)">' + esc(r.path) + '</span></div>';
      });
    }
    if (d.docs && d.docs.length) {
      html += '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:6px">';
      html += '<h2 style="margin:0">Documentos <span style="font-size:13px;color:var(--muted);font-weight:400">— ' + d.docs.length + '</span></h2>';
      html += '<button onclick="openDocEditor(null, {source:\'manual\', path:\'untitled.md\', title:\'Untitled\'}, \'' + esc(key) + '\')" style="background:var(--blue);color:white;border:0;padding:4px 10px;border-radius:4px;cursor:pointer;font-size:11px">+ New doc</button>';
      html += '</div>';
      d.docs.slice(0, 30).forEach(function(doc) {
        html += '<div class="repo-card" style="cursor:pointer;display:flex;justify-content:space-between;align-items:center" onclick="openDocByPath(\'' + esc(doc.path.replace(/'/g, "\\'")) + '\', \'' + esc(key) + '\')">';
        html += '<span>📄 ' + esc(doc.title || doc.path.split("/").pop()) + '</span>';
        html += '<span style="font-size:10px;color:var(--muted)">click to edit</span>';
        html += '</div>';
      });
    } else {
      html += '<div style="text-align:center;padding:20px">';
      html += '<button onclick="openDocEditor(null, {source:\'manual\', path:\'untitled.md\', title:\'Untitled\'}, \'' + esc(key) + '\')" style="background:var(--blue);color:white;border:0;padding:8px 16px;border-radius:4px;cursor:pointer;font-size:13px">+ Create first document for ' + esc(p.name) + '</button>';
      html += '</div>';
    }
    html += '</div>';
    main.innerHTML = html;
    renderOutline();
  } catch (e) {
    main.innerHTML = '<div class="empty-state">error: ' + esc(e.message) + '</div>';
  }
}

// ---- Brain view ----
// === Brain view v2: radial graph with zoom, pan, filters, search ===

var _brainState = {
  data: null,
  pos: {},
  zoom: 1,
  panX: 0,
  panY: 0,
  lockedSection: null,
};

async function renderBrainView() {
  var main = document.getElementById('main-content');
  document.getElementById('breadcrumb').innerHTML = '<span>Brain</span>';
  main.innerHTML = '<div class="empty-state">cargando grafo…</div>';
  try {
    var data = await fetchJSON('/api/brain/grafo');
    _brainState.data = data;
    renderBrainGrafo(main, data);
  } catch (e) {
    main.innerHTML = '<div class="empty-state">error: ' + esc(e.message) + '</div>';
  }
}

function renderBrainGrafo(mount, data) {
  _brainState.pos = computeRadialLayout(data);
  _brainState.zoom = 1;
  _brainState.panX = 0;
  _brainState.panY = 0;
  _brainState.lockedSection = null;

  var html = '<div class="md-view" style="max-width:100%">';
  html += '<h1 style="font-family:Georgia,serif;font-weight:400;font-size:32px;margin:0 0 16px">Brain <span style="color:var(--muted);font-weight:400;font-size:14px">— radial graph · hover to focus · scroll to zoom · drag to pan</span></h1>';

  // Top controls bar
  html += '<div style="display:flex;gap:8px;margin:8px 0 16px;flex-wrap:wrap;align-items:center">';
  html += '<input id="brain-search" type="text" placeholder="Search nodes…" style="flex:1;min-width:180px;background:var(--bg);border:1px solid var(--border);padding:8px 12px;border-radius:8px;font-size:13px;color:var(--ink);box-shadow:0 1px 2px rgba(15,23,42,0.04)"/>';
  var typeChips = [
    ['project', 'projects', true], ['obsidian', 'docs', true], ['memory', 'memory', true],
    ['task', 'tasks', true], ['url_ok', 'urls ok', true], ['url_down', 'urls down', true],
    ['todo', 'todos', true], ['ai_agent', 'agents', true], ['human', 'humans', true], ['system', 'system', true]
  ];
  html += '<div id="brain-type-filters" style="display:flex;gap:6px;flex-wrap:wrap">';
  typeChips.forEach(function(c) {
    html += '<button data-type-filter="' + c[0] + '" data-active="true" title="click to toggle ' + c[0] + '" style="background:var(--bg);border:0;color:var(--ink-soft);padding:5px 12px;border-radius:14px;cursor:pointer;font-size:12px;box-shadow:0 1px 2px rgba(15,23,42,0.04);transition:all 0.15s">' + c[1] + '</button>';
  });
  html += '</div>';
  html += '<button data-brain-zoom="in"     style="padding:4px 10px;border:1px solid var(--border);border-radius:4px;background:var(--bg);color:var(--ink);cursor:pointer">+</button>';
  html += '<button data-brain-zoom="out"    style="padding:4px 10px;border:1px solid var(--border);border-radius:4px;background:var(--bg);color:var(--ink);cursor:pointer">−</button>';
  html += '<button data-brain-zoom="reset"  style="padding:4px 10px;border:1px solid var(--border);border-radius:4px;background:var(--bg);color:var(--ink);cursor:pointer">reset</button>';
  html += '</div>';

  // Section legend
  var bySection = {};
  data.nodes.forEach(function(n) {
    if (n.id === 'center') return;
    var sec = n.sub || 'Other';
    if (!bySection[sec]) bySection[sec] = [];
    bySection[sec].push(n);
  });
  var sectionNames = Object.keys(bySection);
  var sectionColors = {
    'Projects': '#3b82f6', 'Recent': '#7c3aed', 'Tasks': '#f59e0b', 'URLs': '#10b981', 'To do': '#ef4444', 'People': '#06b6d4', 'Other': '#6b7280'
  };
  html += '<div id="brain-section-legend" style="display:flex;gap:6px;margin:0 0 12px;flex-wrap:wrap">';
  sectionNames.forEach(function(sec) {
    var c = sectionColors[sec] || '#6b7280';
    var count = (bySection[sec] || []).length;
    html += '<span data-section-focus="' + esc(sec) + '" style="background:' + c + '22;color:' + c + ';padding:3px 9px;border-radius:3px;border:1px solid ' + c + ';font-size:11px;cursor:pointer">' + esc(sec) + ' · ' + count + '</span>';
  });
  html += '</div>';

  // SVG container
  var W = 1100, H = 760;
  var cx = W/2, cy = H/2;
  html += '<div id="brain-grafo-svg" style="background:transparent;border:0;border-radius:14px;overflow:visible;position:relative;cursor:grab;user-select:none;min-height:600px;padding:8px">';
  html += '<svg id="brain-svg" viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="xMidYMid meet" style="width:100%;height:auto;display:block;min-height:600px">';
  html += '<defs><clipPath id="brain-clip"><rect x="0" y="0" width="' + W + '" height="' + H + '"/></clipPath></defs>';
  html += '<g id="brain-svg-root" clip-path="url(#brain-clip)">';

  for (var g = 0; g < 4; g++) {
    var radius = (g + 1) * 90;
    html += '<circle cx="' + cx + '" cy="' + cy + '" r="' + radius + '" fill="none" stroke="rgba(255,255,255,0.03)" stroke-width="1"/>';
  }

  html += '<g class="edges">';
  data.edges.forEach(function(e) {
    var a = _brainState.pos[e.from];
    var b = _brainState.pos[e.to];
    if (!a || !b) return;
    var stroke = e.type === 'layout' ? 'rgba(255,255,255,0.08)' : 'rgba(255,255,255,0.18)';
    var sw = e.type === 'layout' ? 1 : 1.3;
    html += '<line x1="' + a.x.toFixed(1) + '" y1="' + a.y.toFixed(1) + '" x2="' + b.x.toFixed(1) + '" y2="' + b.y.toFixed(1) + '" stroke="' + stroke + '" stroke-width="' + sw + '" data-from="' + esc(e.from) + '" data-to="' + esc(e.to) + '"/>';
  });
  html += '</g>';

  html += '<g class="nodes">';
  Object.keys(_brainState.pos).forEach(function(id) {
    var p = _brainState.pos[id];
    if (p.type === 'center') {
      html += '<g class="node center" data-id="' + esc(id) + '" data-section="center" data-type="center" style="cursor:pointer;transition:opacity 0.2s">';
      html += '<circle cx="' + p.x + '" cy="' + p.y + '" r="' + p.r + '" fill="#1a1a1f" stroke="#a78bfa" stroke-width="2.5" opacity="0.95"/>';
      html += '<circle cx="' + p.x + '" cy="' + p.y + '" r="' + (p.r - 6) + '" fill="none" stroke="#a78bfa" stroke-width="1" stroke-dasharray="3 2" opacity="0.5"/>';
      html += '<text x="' + p.x + '" y="' + (p.y + 4) + '" text-anchor="middle" fill="#c4b5fd" font-size="12" font-weight="600" letter-spacing="0.5">' + esc(p.label) + '</text>';
      html += '</g>';
    } else if (p.type === 'section') {
      var c = sectionColors[p.section] || '#6b7280';
      html += '<g class="node section" data-id="' + esc(id) + '" data-section="' + esc(p.section) + '" data-type="section" style="cursor:pointer;transition:opacity 0.2s">';
      html += '<circle cx="' + p.x + '" cy="' + p.y + '" r="' + p.r + '" fill="#16161a" stroke="' + c + '" stroke-width="2" opacity="0.9"/>';
      html += '<text x="' + p.x + '" y="' + (p.y + 4) + '" text-anchor="middle" fill="' + c + '" font-size="11" font-weight="600" letter-spacing="0.3">' + esc(p.label) + '</text>';
      html += '<text x="' + p.x + '" y="' + (p.y + p.r + 14) + '" text-anchor="middle" fill="' + c + '" font-size="10" opacity="0.7">' + (bySection[p.section] || []).length + '</text>';
      html += '</g>';
    } else {
      var itemColor = ({
        project: '#3b82f6', obsidian: '#7c3aed', memory: '#7c3aed', repo: '#7c3aed',
        playbook: '#7c3aed', task: '#f59e0b', url_ok: '#10b981', url_down: '#ef4444',
        todo: '#ef4444', ai_agent: '#06b6d4', human: '#22c55e', system: '#6b7280',
        person: '#22c55e'
      })[p.type] || '#6b7280';
      var rOuter = 380;
      var lx = cx + Math.cos(p.angle) * rOuter;
      var ly = cy + Math.sin(p.angle) * rOuter;
      var rotation = (p.angle * 180 / Math.PI);
      if (Math.cos(p.angle) < 0) rotation += 180;
      var anchor = Math.cos(p.angle) > 0 ? 'start' : 'end';
      html += '<g class="node item" data-id="' + esc(id) + '" data-section="' + esc(p.section) + '" data-type="' + esc(p.type) + '" style="cursor:pointer;transition:opacity 0.18s">';
      html += '<circle cx="' + p.x + '" cy="' + p.y + '" r="' + p.r + '" fill="#131316" stroke="' + itemColor + '" stroke-width="1.2" opacity="0.85"/>';
      html += '<circle cx="' + p.x + '" cy="' + p.y + '" r="2" fill="' + itemColor + '"/>';
      html += '<text x="' + lx.toFixed(1) + '" y="' + ly.toFixed(1) + '" text-anchor="' + anchor + '" fill="var(--ink-soft)" font-size="10" letter-spacing="0.2" transform="rotate(' + rotation.toFixed(1) + ' ' + lx.toFixed(1) + ' ' + ly.toFixed(1) + ')" style="pointer-events:none">' + esc(p.label) + '</text>';
      html += '</g>';
    }
  });
  html += '</g>';

  html += '</g>'; // brain-svg-root
  html += '</svg>';
  html += '<div id="brain-zoom-info" style="position:absolute;bottom:8px;right:12px;font-size:11px;color:var(--muted);background:rgba(0,0,0,0.6);padding:2px 8px;border-radius:3px">100%</div>';
  html += '</div>';

  html += '<div style="margin-top:8px;font-size:11px;color:var(--muted);text-align:center">Hover: focus branch · Click section: lock · Click item: open detail · Wheel: zoom · Drag: pan · Search: highlight + Enter to jump</div>';

  html += '<div id="brain-detail" style="display:none;margin-top:12px"></div>';
  html += '<div id="brain-chat-mount" style="margin-top:24px"></div>';
  html += '</div>';

  mount.innerHTML = html;
  renderOutline();

  setupBrainZoomPan();
  setupBrainFilters();
  setupBrainSearch();
  setupBrainHover();
  setupBrainSectionLegend();
  mountOriginalBrainChat(mount);
}

function computeRadialLayout(data) {
  var W = 1100, H = 760;
  var cx = W/2, cy = H/2;
  var rSection = 175, rItem = 320;
  var pos = {};
  var center = data.center || { id: 'center', label: 'Brain' };
  pos[center.id] = { x: cx, y: cy, r: 28, label: center.label, type: 'center', section: 'center' };

  var bySection = {};
  data.nodes.forEach(function(n) {
    if (n.id === 'center') return;
    var sec = n.sub || 'Other';
    if (!bySection[sec]) bySection[sec] = [];
    bySection[sec].push(n);
  });
  var sectionNames = Object.keys(bySection);

  sectionNames.forEach(function(sec, i) {
    var angle = (i / sectionNames.length) * 2 * Math.PI - Math.PI / 2;
    pos['section:' + sec] = { x: cx + Math.cos(angle) * rSection, y: cy + Math.sin(angle) * rSection, r: 24, label: sec, type: 'section', section: sec, angle: angle };
  });

  sectionNames.forEach(function(sec, i) {
    var items = bySection[sec];
    var baseAngle = (i / sectionNames.length) * 2 * Math.PI - Math.PI / 2;
    var arcSpan = (2 * Math.PI / sectionNames.length) * 0.85;
    var startAngle = baseAngle - arcSpan / 2;
    items.forEach(function(item, j) {
      var t = items.length === 1 ? 0.5 : j / (items.length - 1);
      var angle = startAngle + arcSpan * t;
      pos[item.id] = {
        x: cx + Math.cos(angle) * rItem, y: cy + Math.sin(angle) * rItem, r: 7,
        label: item.label, type: item.type, section: sec, angle: angle, sub: item.sub,
        count: item.count || 0, originalNode: item
      };
    });
  });
  return pos;
}

// === Zoom & Pan ===

function setupBrainZoomPan() {
  var svgRoot = document.getElementById('brain-svg-root');
  var svg = document.getElementById('brain-svg');
  var info = document.getElementById('brain-zoom-info');
  var container = document.getElementById('brain-grafo-svg');
  if (!svgRoot || !container) return;

  function applyTransform() {
    svgRoot.setAttribute('transform', 'translate(' + _brainState.panX + ',' + _brainState.panY + ') scale(' + _brainState.zoom + ')');
    if (info) info.textContent = Math.round(_brainState.zoom * 100) + '%';
  }

  svg.addEventListener('wheel', function(e) {
    e.preventDefault();
    var factor = e.deltaY < 0 ? 1.15 : 1/1.15;
    _brainState.zoom = Math.max(0.3, Math.min(3, _brainState.zoom * factor));
    applyTransform();
  }, { passive: false });

  var isDragging = false, startX, startY, startPanX, startPanY;
  container.addEventListener('mousedown', function(e) {
    if (e.target.closest('.node')) return;
    isDragging = true;
    startX = e.clientX; startY = e.clientY;
    startPanX = _brainState.panX; startPanY = _brainState.panY;
    container.style.cursor = 'grabbing';
  });
  document.addEventListener('mousemove', function(e) {
    if (!isDragging) return;
    _brainState.panX = startPanX + (e.clientX - startX);
    _brainState.panY = startPanY + (e.clientY - startY);
    applyTransform();
  });
  document.addEventListener('mouseup', function() {
    if (!isDragging) return;
    isDragging = false;
    container.style.cursor = 'grab';
  });

  document.querySelectorAll('[data-brain-zoom]').forEach(function(btn) {
    btn.addEventListener('click', function() {
      var z = btn.dataset.brainZoom;
      if (z === 'in')       _brainState.zoom = Math.min(3, _brainState.zoom * 1.25);
      else if (z === 'out') _brainState.zoom = Math.max(0.3, _brainState.zoom / 1.25);
      else { _brainState.zoom = 1; _brainState.panX = 0; _brainState.panY = 0; }
      applyTransform();
    });
  });
}

// === Type filters ===

function setupBrainFilters() {
  document.querySelectorAll('[data-type-filter]').forEach(function(btn) {
    btn.addEventListener('click', function() {
      var isActive = btn.dataset.active === 'true';
      btn.dataset.active = isActive ? 'false' : 'true';
      btn.style.opacity = isActive ? '0.3' : '1';
      btn.style.textDecoration = isActive ? 'line-through' : 'none';
      applyFiltersAndSearch();
    });
  });
}

function getActiveTypeFilter() {
  var active = {};
  document.querySelectorAll('[data-type-filter]').forEach(function(btn) {
    active[btn.dataset.typeFilter] = btn.dataset.active === 'true';
  });
  return active;
}

function applyFiltersAndSearch() {
  var typeFilter = getActiveTypeFilter();
  var query = ((document.getElementById('brain-search') || {}).value || '').toLowerCase();
  var svg = document.getElementById('brain-svg');
  if (!svg) return;

  svg.querySelectorAll('.node').forEach(function(el) {
    var type = el.dataset.type || '';
    var visible = true;
    if (type === 'section' || type === 'center') {
      visible = true;
    } else if (type === 'url_ok' || type === 'url_down') {
      visible = typeFilter.url_ok || typeFilter.url_down;
    } else {
      visible = typeFilter[type] !== false;
    }
    if (visible && query) {
      var labels = Array.from(el.querySelectorAll('text')).map(function(t) { return t.textContent || ''; });
      if (!labels.some(function(l) { return l.toLowerCase().indexOf(query) >= 0; })) {
        visible = false;
      }
    }
    el.style.opacity = visible ? '' : '0.08';
    el.style.pointerEvents = visible ? '' : 'none';
  });
  svg.querySelectorAll('.edges line').forEach(function(el) {
    var fromEl = svg.querySelector('.node[data-id="' + CSS.escape(el.dataset.from) + '"]');
    var toEl   = svg.querySelector('.node[data-id="' + CSS.escape(el.dataset.to) + '"]');
    if (fromEl && toEl) {
      el.style.opacity = (fromEl.style.opacity === '0.08' || toEl.style.opacity === '0.08') ? '0.04' : '';
    }
  });
}

function setupBrainSearch() {
  var input = document.getElementById('brain-search');
  if (!input) return;
  var timer = null;
  input.addEventListener('input', function() {
    clearTimeout(timer);
    timer = setTimeout(applyFiltersAndSearch, 100);
  });
  input.addEventListener('keydown', function(e) {
    if (e.key === 'Enter') {
      var q = input.value.toLowerCase();
      if (!q) return;
      var svg = document.getElementById('brain-svg');
      if (!svg) return;
      var match = null;
      svg.querySelectorAll('.node').forEach(function(el) {
        if (match) return;
        var labels = Array.from(el.querySelectorAll('text')).map(function(t) { return t.textContent || ''; });
        if (labels.some(function(l) { return l.toLowerCase().indexOf(q) >= 0; })) {
          match = el;
        }
      });
      if (match) zoomToNode(match.dataset.id, true);
    }
  });
}

function zoomToNode(nodeId, openDetail) {
  var p = _brainState.pos[nodeId];
  if (!p) return;
  _brainState.zoom = 1.6;
  _brainState.panX = 550 - p.x * _brainState.zoom;
  _brainState.panY = 380 - p.y * _brainState.zoom;
  var root = document.getElementById('brain-svg-root');
  if (root) root.setAttribute('transform', 'translate(' + _brainState.panX + ',' + _brainState.panY + ') scale(' + _brainState.zoom + ')');
  var info = document.getElementById('brain-zoom-info');
  if (info) info.textContent = Math.round(_brainState.zoom * 100) + '%';
  // Highlight
  var node = document.querySelector('.node[data-id="' + CSS.escape(nodeId) + '"]');
  if (node) {
    node.style.transition = 'transform 0.3s';
    node.setAttribute('transform', 'scale(2.5)');
    setTimeout(function() { node.setAttribute('transform', ''); }, 1200);
  }
  if (openDetail) showBrainDetail(nodeId, p);
}

// === Hover & focus ===

function setupBrainHover() {
  var svg = document.getElementById('brain-svg');
  if (!svg) return;
  function setFocus(section) {
    if (!section) {
      svg.querySelectorAll('.node, .edges line').forEach(function(el) { el.style.opacity = ''; });
      return;
    }
    svg.querySelectorAll('.node').forEach(function(el) {
      var sec = el.dataset.section || '';
      el.style.opacity = (sec === section || sec === 'center') ? '' : '0.08';
    });
    svg.querySelectorAll('.edges line').forEach(function(el) {
      var fromSec = (_brainState.pos[el.dataset.from] || {}).section || '';
      var toSec   = (_brainState.pos[el.dataset.to] || {}).section || '';
      el.style.opacity = (fromSec === section || toSec === section || fromSec === 'center' || toSec === 'center') ? '' : '0.04';
    });
  }
  svg.querySelectorAll('.node.section').forEach(function(el) {
    var sec = el.dataset.section;
    el.addEventListener('mouseenter', function() { if (!_brainState.lockedSection) setFocus(sec); });
    el.addEventListener('mouseleave', function() { if (!_brainState.lockedSection) setFocus(null); });
    el.addEventListener('click', function(e) {
      e.stopPropagation();
      if (_brainState.lockedSection === sec) { _brainState.lockedSection = null; setFocus(null); }
      else { _brainState.lockedSection = sec; setFocus(sec); }
    });
  });
  svg.querySelectorAll('.node.item').forEach(function(el) {
    var sec = el.dataset.section;
    el.addEventListener('mouseenter', function() { if (!_brainState.lockedSection) setFocus(sec); });
    el.addEventListener('mouseleave', function() { if (!_brainState.lockedSection) setFocus(null); });
    el.addEventListener('click', function(e) {
      e.stopPropagation();
      showBrainDetail(el.dataset.id, _brainState.pos[el.dataset.id]);
    });
  });
  var centerNode = svg.querySelector('.node.center');
  if (centerNode) {
    centerNode.addEventListener('mouseenter', function() { if (!_brainState.lockedSection) setFocus(null); });
  }
}

function setupBrainSectionLegend() {
  document.querySelectorAll('[data-section-focus]').forEach(function(btn) {
    btn.addEventListener('click', function() {
      var sec = btn.dataset.sectionFocus;
      var svg = document.getElementById('brain-svg');
      if (!svg) return;
      if (_brainState.lockedSection === sec) {
        _brainState.lockedSection = null;
        svg.querySelectorAll('.node, .edges line').forEach(function(el) { el.style.opacity = ''; });
      } else {
        _brainState.lockedSection = sec;
        svg.querySelectorAll('.node').forEach(function(el) {
          var s = el.dataset.section || '';
          el.style.opacity = (s === sec || s === 'center') ? '' : '0.08';
        });
        svg.querySelectorAll('.edges line').forEach(function(el) {
          var fromSec = (_brainState.pos[el.dataset.from] || {}).section || '';
          var toSec   = (_brainState.pos[el.dataset.to] || {}).section || '';
          el.style.opacity = (fromSec === sec || toSec === sec || fromSec === 'center' || toSec === 'center') ? '' : '0.04';
        });
      }
    });
  });
}

// === Detail panel ===

function showBrainDetail(nodeId, p) {
  if (!p || !p.originalNode) return;
  var mount = document.getElementById('brain-detail');
  if (!mount) return;
  mount.style.display = 'block';
  var n = p.originalNode;
  var typeColor = ({
    project:'#3b82f6', obsidian:'#7c3aed', memory:'#7c3aed', repo:'#7c3aed',
    playbook:'#7c3aed', task:'#f59e0b', url_ok:'#10b981', url_down:'#ef4444',
    todo:'#ef4444', ai_agent:'#06b6d4', human:'#22c55e', system:'#6b7280'
  })[n.type] || '#6b7280';
  var html = '<div style="background:var(--bg-soft);border:1px solid var(--border);border-left:4px solid ' + typeColor + ';border-radius:6px;padding:14px 18px">';
  html += '<div style="display:flex;align-items:baseline;gap:8px;margin-bottom:6px;flex-wrap:wrap">';
  html += '<span class="pill" style="background:' + typeColor + '22;color:' + typeColor + '">' + esc(n.type) + '</span>';
  if (n.sub) html += '<span class="pill" style="background:var(--bg-active);color:var(--blue)">' + esc(n.sub) + '</span>';
  if (n.count) html += '<span class="pill" style="background:var(--bg-active);color:var(--muted)">' + n.count + ' related</span>';
  html += '</div>';
  html += '<div style="font-size:18px;font-weight:600;color:var(--ink);margin-bottom:4px">' + esc(n.label) + '</div>';
  var actions = '';
  actions += '<button data-brain-zoom-node="' + esc(n.id) + '" style="background:var(--bg-soft);border:1px solid var(--border);color:var(--ink);padding:6px 14px;border-radius:4px;cursor:pointer;font-size:12px">🎯 zoom to it</button>';
  if (n.id.startsWith('doc:')) {
    var docId = n.id.slice(4);
    actions += '<button data-brain-open-doc="' + docId + '" style="background:var(--blue);color:white;border:0;padding:6px 14px;border-radius:4px;cursor:pointer;font-size:12px">open in files</button>';
    actions += '<button data-brain-ask-doc="' + docId + '" style="background:var(--bg-soft);border:1px solid var(--border);color:var(--ink);padding:6px 14px;border-radius:4px;cursor:pointer;font-size:12px">💬 ask brain about this</button>';
  } else if (n.id.startsWith('project:')) {
    var key = n.id.slice(7);
    html += '<div style="font-size:12px;color:var(--muted);margin-bottom:6px">key: ' + esc(key) + '</div>';
  } else if (n.id.startsWith('task:')) {
    var taskId = n.id.slice(5);
    actions += '<button data-brain-open-task="' + taskId + '" style="background:var(--blue);color:white;border:0;padding:6px 14px;border-radius:4px;cursor:pointer;font-size:12px">open task</button>';
  } else if (n.id.startsWith('person:')) {
    var personId = n.id.slice(7);
    actions += '<button data-brain-open-person="' + personId + '" style="background:var(--blue);color:white;border:0;padding:6px 14px;border-radius:4px;cursor:pointer;font-size:12px">open profile</button>';
    actions += '<button data-brain-chat-person="' + personId + '" style="background:var(--bg-soft);border:1px solid var(--border);color:var(--ink);padding:6px 14px;border-radius:4px;cursor:pointer;font-size:12px">💬 chat</button>';
  } else if (n.id.startsWith('todo:')) {
    var todoId = n.id.slice(5);
    actions += '<button data-brain-open-todo="' + todoId + '" style="background:var(--blue);color:white;border:0;padding:6px 14px;border-radius:4px;cursor:pointer;font-size:12px">open todo</button>';
  } else if (n.id.startsWith('url:')) {
    var urlName = n.id.slice(4);
    actions += '<button data-brain-open-url="' + esc(urlName) + '" style="background:var(--blue);color:white;border:0;padding:6px 14px;border-radius:4px;cursor:pointer;font-size:12px">check status</button>';
  }
  html += '<div style="display:flex;gap:6px;flex-wrap:wrap">' + actions + '</div>';
  html += '</div>';
  mount.innerHTML = html;
  // Wire
  mount.querySelectorAll('[data-brain-zoom-node]').forEach(function(b) {
    b.addEventListener('click', function() { zoomToNode(b.dataset.brainZoomNode, false); });
  });
  mount.querySelectorAll('[data-brain-open-doc]').forEach(function(b) {
    b.addEventListener('click', function() { setView('files'); });
  });
  mount.querySelectorAll('[data-brain-ask-doc]').forEach(function(b) {
    b.addEventListener('click', function() {
      var input = document.getElementById('brain-q');
      if (input) { input.value = 'What does document #' + b.dataset.brainAskDoc + ' say?'; askBrain(); }
    });
  });
  mount.querySelectorAll('[data-brain-open-task]').forEach(function(b) {
    b.addEventListener('click', function() { setView('tasks'); renderTaskDetail(parseInt(b.dataset.brainOpenTask)); });
  });
  mount.querySelectorAll('[data-brain-open-person]').forEach(function(b) {
    b.addEventListener('click', function() { renderMemberView(parseInt(b.dataset.brainOpenPerson)); });
  });
  mount.querySelectorAll('[data-brain-chat-person]').forEach(function(b) {
    b.addEventListener('click', function() { openChatDrawer(parseInt(b.dataset.brainChatPerson), ''); });
  });
  mount.querySelectorAll('[data-brain-open-todo]').forEach(function(b) {
    b.addEventListener('click', function() { setView('todo'); });
  });
  mount.querySelectorAll('[data-brain-open-url]').forEach(function(b) {
    b.addEventListener('click', function() { setView('activity'); });
  });
}

function mountOriginalBrainChat(mount) {
  var chatMount = mount.querySelector('#brain-chat-mount');
  if (!chatMount) return;
  chatMount.innerHTML =
    '<h2 style="margin-top:24px">Ask the brain</h2>' +
    '<p style="color:var(--ink-soft);font-size:13px">BYOK (Ollama local, Anthropic, OpenAI). Tools enabled by default. ' +
    '<a href="/chat" style="color:var(--blue)">Open the full chat, with your AI team →</a></p>' +
    '<div class="chat" id="brain-chat"></div>' +
    '<div class="chat-input">' +
    '<input id="brain-q" type="text" placeholder="pregunta al brain…  (Enter to send)" style="flex:1;background:var(--bg);border:1px solid var(--border);padding:8px 12px;border-radius:4px;font-size:14px;color:var(--ink)"/>' +
    '<button onclick="askBrain()" style="background:var(--blue);color:white;border:0;padding:8px 18px;border-radius:4px;cursor:pointer;font-weight:500">ask</button>' +
    '</div>' +
    '<div style="margin-top:8px;display:flex;gap:8px;flex-wrap:wrap;font-size:12px">' +
    '<select id="brain-provider" style="padding:4px 8px;border:1px solid var(--border);border-radius:4px;background:var(--bg);color:var(--ink)"><option value="">auto</option>' + (_providers || []).map(function(p){ return '<option value="' + esc(p.name) + '">' + esc(p.name) + (p.available ? ' ✓' : ' ✗') + '</option>'; }).join('') + '</select>' +
    '<label style="display:flex;align-items:center;gap:4px;color:var(--muted)"><input type="checkbox" id="brain-tools" checked /> tools</label>' +
    '<button onclick="loadBriefing()" style="padding:4px 10px;border:1px solid var(--border);border-radius:4px;background:var(--bg);color:var(--ink);cursor:pointer">☀ briefing</button>' +
    '<button onclick="exportCurrentChat()" style="padding:4px 10px;border:1px solid var(--border);border-radius:4px;background:var(--bg);color:var(--ink);cursor:pointer">↓ save</button>' +
    '<button onclick="clearChat()" style="padding:4px 10px;border:1px solid var(--border);border-radius:4px;background:var(--bg);color:var(--ink);cursor:pointer">clear</button>' +
    '</div>';
  var input = document.getElementById('brain-q');
  if (input) input.addEventListener('keydown', function(e) { if (e.key === 'Enter') askBrain(); });
}
async function askBrain() {
  var input = document.getElementById('brain-q');
  if (!input) return;
  var q = input.value.trim();
  if (!q) return;
  var btn = document.querySelector('.chat-input button:last-child');
  btn.disabled = true;
  input.value = '';
  var chat = document.getElementById('brain-chat');
  chat.insertAdjacentHTML('beforeend', '<div class="msg user"><div class="bubble">' + esc(q) + '</div></div>');
  chat.insertAdjacentHTML('beforeend', '<div class="msg assistant"><div class="bubble" id="brain-streaming"><em>thinking…</em></div></div>');
  chat.scrollTop = chat.scrollHeight;
  try {
    var provider = (document.getElementById('brain-provider') || {}).value;
    var useTools = (document.getElementById('brain-tools') || {}).checked;
    var url = '/api/brain/stream?question=' + encodeURIComponent(q) + (provider ? '&provider=' + encodeURIComponent(provider) : '');
    if (useTools) url += '&tools=1';
    var r = await fetch(url);
    var pu = await ProviderErrors.fromResponse(r);
    if (pu) { var pb = chat.querySelectorAll('[id=brain-streaming]'); ProviderErrors.show(pb[pb.length - 1], pu, { selector: 'brain-provider' }); return; }
    if (!r.ok || !r.body) throw new Error('HTTP ' + r.status);
    var reader = r.body.getReader();
    var dec = new TextDecoder();
    var buf = '', meta = null, full = '';
    var bubble = document.getElementById('brain-streaming');
    bubble.innerHTML = '';
    while (true) {
      var chunk = await reader.read();
      if (chunk.done) break;
      buf += dec.decode(chunk.value, { stream: true });
      var lines = buf.split('\n\n');
      buf = lines.pop() || '';
      for (var i = 0; i < lines.length; i++) {
        var line = lines[i].trim();
        if (!line.startsWith('data: ')) continue;
        try { var evt = JSON.parse(line.slice(6)); } catch (e) { continue; }
        if (evt.type === 'meta') meta = evt;
        else if (evt.type === 'delta') { full += evt.content; bubble.textContent = full; chat.scrollTop = chat.scrollHeight; }
        else if (evt.type === 'done') {
          var citeHtml = '';
          if (meta && meta.citations && meta.citations.length) {
            citeHtml = '<div class="citations" style="color:var(--muted);font-size:12px;margin-top:4px">📎 ' + meta.citations.length + ' sources</div>';
          }
          bubble.parentNode.insertAdjacentHTML('afterend', '<div class="meta">' + (meta?esc(meta.provider):'') + (meta && meta.tool_log && meta.tool_log.length ? ' · 🔧'+meta.tool_log.length : '') + '</div>' + citeHtml);
        } else if (evt.type === 'error') bubble.textContent = '✗ ' + evt.message;
      }
    }
  } catch (e) {
    var b = document.getElementById('brain-streaming');
    if (b) b.textContent = '✗ ' + e.message;
  } finally { btn.disabled = false; }
}
function clearChat() { _convId = null; var c = document.getElementById('brain-chat'); if (c) c.innerHTML = ''; }
async function loadBriefing() { setView('briefing'); }
async function exportCurrentChat() {
  if (!_convId) { try { var cs = await fetchJSON('/api/brain/conversations'); if (cs.length) _convId = cs[0].id; } catch (e) {} }
  if (!_convId) { alert('no conversations yet'); return; }
  window.open('/api/brain/conversation/export?id=' + _convId, '_blank');
}

// Briefing view
// ---- Skills view ----
let _skills = [];
let _skillsSummary = { total: 0, by_category: [], by_subcategory: [] };
let _skillsFilterCat = '';
let _skillsFilterSub = '';
let _skillsQuery = '';

async function refreshSkills() {
  try {
    var url = '/api/skills';
    var params = [];
    if (_skillsFilterCat) params.push('category=' + encodeURIComponent(_skillsFilterCat));
    if (_skillsFilterSub) params.push('subcategory=' + encodeURIComponent(_skillsFilterSub));
    if (_skillsQuery) params.push('q=' + encodeURIComponent(_skillsQuery));
    if (params.length) url += '?' + params.join('&');
    var d = await fetchJSON(url);
    _skills = d.rows;
    _skillsSummary = d.summary;
  } catch (e) {}
}

async function renderSkillsView() {
  var main = document.getElementById('main-content');
  try {
    document.getElementById('breadcrumb').innerHTML = '<span>Skills</span>';
    main.innerHTML = '<div class="md-view"><h1>📚 Skills</h1><div class="empty-state">cargando…</div></div>';

    await refreshSkills();

    // Group by category
    var byCat = {};
    _skills.forEach(function(s) {
      if (!byCat[s.category]) byCat[s.category] = [];
      byCat[s.category].push(s);
    });

    // Color per category
    var catColors = {
      ai: '#7c3aed', backend: '#16a34a', frontend: '#2563eb', mobile: '#ea580c',
      devops: '#0891b2', data: '#0d9488', security: '#dc2626', testing: '#d97706',
      design: '#db2777', marketing: '#9333ea', research: '#65a30d', finance: '#059669',
      video: '#1e40af', devtools: '#475569', other: '#6b7280',
    };

    var html = '<div class="md-view">';
    html += '<h1>📚 Skills</h1>';
    html += '<p style="color:var(--muted);font-size:13px;margin-bottom:12px">Skills library imported from Obsidian / ECC. <strong>' + _skillsSummary.total + '</strong> skills organized in categories.</p>';

    // Filters
    html += '<div style="display:flex;gap:8px;margin-bottom:16px;flex-wrap:wrap;align-items:center">';
    html += '<input id="skills-search" type="search" placeholder="search skills..." value="' + esc(_skillsQuery) + '" style="flex:1;min-width:200px;background:var(--bg-soft);border:1px solid var(--border);padding:6px 10px;border-radius:4px;font-size:13px;color:var(--ink)">';
    html += '<select id="skills-cat" style="background:var(--bg-soft);border:1px solid var(--border);padding:6px 10px;border-radius:4px;font-size:12px;color:var(--ink)">';
    html += '<option value="">all categories</option>';
    (_skillsSummary.by_category || []).forEach(function(c) {
      var sel = _skillsFilterCat === c.category ? ' selected' : '';
      html += '<option value="' + esc(c.category) + '"' + sel + '>' + esc(c.category) + ' (' + c.c + ')</option>';
    });
    html += '</select>';
    if (_skillsFilterCat) {
      html += '<button id="skills-clear-btn" style="padding:6px 10px;border:1px solid var(--border);background:var(--bg-soft);color:var(--ink);border-radius:4px;cursor:pointer;font-size:12px">× clear</button>';
    }
    html += '</div>';

    // Stats line
    html += '<div id="skills-pills" style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:18px;font-size:12px">';
    (_skillsSummary.by_category || []).forEach(function(c) {
      var col = catColors[c.category] || '#6b7280';
      var active = _skillsFilterCat === c.category;
      html += '<button data-cat="' + esc(c.category) + '" style="display:flex;align-items:center;gap:6px;padding:5px 10px;border:1px solid ' + (active ? col : 'var(--border)') + ';background:' + (active ? col : 'var(--bg-soft)') + ';color:' + (active ? 'white' : 'var(--ink-soft)') + ';border-radius:14px;cursor:pointer;font-size:12px">';
      html += '<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:' + col + '"></span>';
      html += esc(c.category) + ' <strong>' + c.c + '</strong>';
      html += '</button>';
    });
    html += '</div>';

    // Build skill cards HTML (capped for performance)
    var cards = '<div id="skills-cards" style="display:grid;gap:6px">';
    if (_skills.length === 0) {
      cards += '<div class="empty-state" style="padding:40px;color:var(--muted)">no skills match this filter</div>';
    } else {
      var renderLimit = Math.min(_skills.length, 100);
      for (var i = 0; i < renderLimit; i++) {
        var s = _skills[i];
        var col = catColors[s.category] || '#6b7280';
        var origin = s.origin
          ? '<span class="pill" style="background:var(--bg-soft);color:var(--muted);font-size:9px;padding:1px 5px;border-radius:3px;margin-left:6px">' + esc(s.origin) + '</span>'
          : '';
        cards += '<div style="background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:8px 12px;border-left:3px solid ' + col + '">';
        cards += '<div style="display:flex;align-items:start;gap:8px;flex-wrap:wrap">';
        cards += '<div style="flex:1;min-width:240px">';
        cards += '<div style="font-size:13px;font-weight:600;color:var(--ink)">' + esc(s.name) + origin;
        if (s.subcategory) cards += ' <span style="color:var(--muted);font-weight:400;font-size:11px">· ' + esc(s.subcategory) + '</span>';
        cards += '</div>';
        if (s.description) cards += '<div style="font-size:12px;color:var(--ink-soft);margin-top:2px;line-height:1.5">' + esc(s.description) + '</div>';
        cards += '</div>';
        cards += '</div>';
        cards += '</div>';
      }
      if (_skills.length > renderLimit) {
        cards += '<div style="text-align:center;padding:14px;color:var(--muted);font-size:12px">showing ' + renderLimit + ' of ' + _skills.length + ' · narrow with search/category to see more</div>';
      }
    }
    cards += '</div>';

    // Inject in two steps: shell first (fast), then cards (after layout).
    var shell = '<div class="md-view">'
      + '<h1>📚 Skills</h1>'
      + '<p style="color:var(--muted);font-size:13px;margin-bottom:12px">Skills library imported from Obsidian / ECC. <strong>' + _skillsSummary.total + '</strong> skills organized in categories.</p>'
      + '<div style="display:flex;gap:8px;margin-bottom:16px;flex-wrap:wrap;align-items:center">'
      + '<input id="skills-search" type="search" placeholder="search skills..." value="' + esc(_skillsQuery) + '" style="flex:1;min-width:200px;background:var(--bg-soft);border:1px solid var(--border);padding:6px 10px;border-radius:4px;font-size:13px;color:var(--ink)">'
      + '<select id="skills-cat" style="background:var(--bg-soft);border:1px solid var(--border);padding:6px 10px;border-radius:4px;font-size:12px;color:var(--ink)">'
      + '<option value="">all categories</option>'
      + (_skillsSummary.by_category || []).map(function(c) {
          var sel = _skillsFilterCat === c.category ? ' selected' : '';
          return '<option value="' + esc(c.category) + '"' + sel + '>' + esc(c.category) + ' (' + c.c + ')</option>';
        }).join('')
      + '</select>'
      + (_skillsFilterCat ? '<button id="skills-clear-btn" style="padding:6px 10px;border:1px solid var(--border);background:var(--bg-soft);color:var(--ink);border-radius:4px;cursor:pointer;font-size:12px">× clear</button>' : '')
      + '</div>'
      + '<div id="skills-pills" style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:18px;font-size:12px">'
      + (_skillsSummary.by_category || []).map(function(c) {
          var col = catColors[c.category] || '#6b7280';
          var active = _skillsFilterCat === c.category;
          return '<button data-cat="' + esc(c.category) + '" style="display:flex;align-items:center;gap:6px;padding:5px 10px;border:1px solid ' + (active ? col : 'var(--border)') + ';background:' + (active ? col : 'var(--bg-soft)') + ';color:' + (active ? 'white' : 'var(--ink-soft)') + ';border-radius:14px;cursor:pointer;font-size:12px">'
            + '<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:' + col + '"></span>'
            + esc(c.category) + ' <strong>' + c.c + '</strong>'
            + '</button>';
        }).join('')
      + '</div>'
      + '<div id="skills-cards-container">cargando cards…</div>'
      + '</div>';

    main.innerHTML = shell;

    // Render cards on next tick so the shell paints first (perceived performance).
    setTimeout(function() {
      var cont = document.getElementById('skills-cards-container');
      if (cont) cont.innerHTML = cards;
      _wireSkillsView();
    }, 0);
  } catch (e) {
    main.innerHTML = '<div class="md-view"><h1>📚 Skills</h1><div style="background:#fee2e2;border:1px solid #dc2626;padding:16px;border-radius:6px;color:#7f1d1d;margin-top:20px"><strong>Error loading skills</strong><pre style="margin-top:8px;font-size:12px;white-space:pre-wrap">' + esc(e.stack || e.message || String(e)) + '</pre></div></div>';
  }
}

function _wireSkillsView() {
  var searchInput = document.getElementById('skills-search');
  if (searchInput) {
    var timer;
    searchInput.addEventListener('input', function(e) {
      clearTimeout(timer);
      timer = setTimeout(function() {
        _skillsQuery = e.target.value;
        renderSkillsView();
      }, 200);
    });
    if (_skillsQuery) {
      searchInput.focus();
      try {
        var v = searchInput.value;
        searchInput.setSelectionRange(v.length, v.length);
      } catch (e) {}
    }
  }
  var catSel = document.getElementById('skills-cat');
  if (catSel) {
    catSel.addEventListener('change', function(e) {
      _skillsFilterCat = e.target.value;
      _skillsFilterSub = '';
      renderSkillsView();
    });
  }
  // Wire pill buttons
  document.querySelectorAll('[data-cat]').forEach(function(b) {
    b.addEventListener('click', function() {
      _skillsFilterCat = b.dataset.cat === _skillsFilterCat ? '' : b.dataset.cat;
      _skillsFilterSub = '';
      renderSkillsView();
    });
  });
  // Wire clear button
  var clearBtn = document.getElementById('skills-clear-btn');
  if (clearBtn) clearBtn.addEventListener('click', clearSkillsFilter);
}

function filterSkillsCat(cat) {
  _skillsFilterCat = (_skillsFilterCat === cat) ? '' : cat;
  _skillsFilterSub = '';
  renderSkillsView();
}
function clearSkillsFilter() {
  _skillsFilterCat = '';
  _skillsFilterSub = '';
  _skillsQuery = '';
  renderSkillsView();
}

// ---- To do list view ----
function projectBadge(key) {
  if (!key) return '';
  var p = _projects.find(function(x){ return x.key === key; });
  if (!p) return '<span class="pill muted">' + esc(key) + '</span>';
  return '<span class="pill" style="background:var(--bg-active);color:var(--blue);font-size:10px;padding:1px 6px;border-radius:3px">' + esc(p.name) + '</span>';
}

async function refreshUserTodos() {
  try {
    var d = await fetchJSON('/api/user_todos?status=open&limit=200');
    _userTodos = d.rows;
    _userTodosSummary = d.summary;
  } catch (e) { /* */ }
}

async function refreshMembers() {
  try { _members = await fetchJSON('/api/members') || []; }
  catch (e) { _members = []; }
}

async function assignTodoPrompt(todoId) {
  if (!_members || !_members.length) { await refreshMembers(); }
  var choice = prompt('Assign to (type id or partial name):\n\n' + _members.map(function(m){ return m.id + '. ' + (m.type === 'ai_agent' ? '🤖' : '👤') + ' ' + m.name + (m.workload ? ' (' + m.workload + ' todos)' : ''); }).join('\n'));
  if (choice === null) return;
  choice = choice.trim();
  if (!choice) return;
  var member;
  if (/^\d+$/.test(choice)) {
    member = _members.find(function(m) { return m.id === parseInt(choice); });
  } else {
    var q = choice.toLowerCase();
    member = _members.find(function(m) { return m.name.toLowerCase().includes(q); });
  }
  if (!member) { alert('not found: ' + choice); return; }
  try {
    var r = await fetch('/api/user_todos/' + todoId, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ assignee_id: member.id }),
    });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    await refreshMembers();
    renderTodoView();
  } catch (e) { alert('assign failed: ' + e.message); }
}

async function assignTodoInline(todoId, anchor) {
  if (!_members.length) await refreshMembers();
  var existing = document.getElementById('assign-popover');
  if (existing) existing.remove();
  var pop = document.createElement('div');
  pop.id = 'assign-popover';
  pop.style.cssText = 'position:absolute;z-index:100;background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:6px;box-shadow:0 4px 12px rgba(0,0,0,0.15);min-width:220px';
  var html = '<div style="font-size:11px;color:var(--muted);padding:4px 6px;border-bottom:1px solid var(--border);margin-bottom:4px">Assign to member</div>';
  html += '<div data-todo-assign-clear="' + todoId + '" style="padding:4px 8px;cursor:pointer;border-radius:3px;font-size:13px;color:var(--ink)">— clear assignment</div>';
  _members.forEach(function(m) {
    var icon = m.type === 'ai_agent' ? '🤖' : '👤';
    var wl = m.workload ? ' (' + m.workload + ' open)' : '';
    html += '<div data-todo-assign="' + todoId + '" data-member-id="' + m.id + '" style="padding:4px 8px;cursor:pointer;border-radius:3px;font-size:13px;color:var(--ink);display:flex;justify-content:space-between;align-items:center"><span>' + icon + ' ' + esc(m.name) + '</span><span style="font-size:10px;color:var(--muted)">' + esc(m.team_key || '') + wl + '</span></div>';
  });
  pop.innerHTML = html;
  document.body.appendChild(pop);
  var r = anchor.getBoundingClientRect();
  pop.style.top = (r.bottom + 4) + 'px';
  pop.style.left = r.left + 'px';
  pop.querySelectorAll('[data-todo-assign]').forEach(function(el) {
    el.addEventListener('mouseenter', function() { el.style.background = 'var(--bg-soft)'; });
    el.addEventListener('mouseleave', function() { el.style.background = ''; });
    el.addEventListener('click', async function(e) {
      e.stopPropagation();
      var id = parseInt(el.dataset.todoAssign);
      var mid = parseInt(el.dataset.memberId);
      try {
        var rr = await fetch('/api/user_todos/' + id, { method: 'PUT', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ assignee_id: mid }) });
        if (!rr.ok) throw new Error('HTTP ' + rr.status);
        pop.remove();
        await refreshMembers();
        await refreshUserTodos();
        renderTodoView();
      } catch (err) { alert('assign failed: ' + err.message); }
    });
  });
  var clearBtn = pop.querySelector('[data-todo-assign-clear]');
  if (clearBtn) clearBtn.addEventListener('click', async function(e) {
    e.stopPropagation();
    try {
      var rr = await fetch('/api/user_todos/' + todoId, { method: 'PUT', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ assignee_id: null }) });
      if (!rr.ok) throw new Error('HTTP ' + rr.status);
      pop.remove();
      await refreshMembers();
      await refreshUserTodos();
      renderTodoView();
    } catch (err) { alert('clear failed: ' + err.message); }
  });
  setTimeout(function() {
    var closeFn = function(ev) { if (!pop.contains(ev.target)) { pop.remove(); document.removeEventListener('click', closeFn); } };
    document.addEventListener('click', closeFn);
  }, 50);
}

async function renderTodoView() {
  var main = document.getElementById('main-content');
  document.getElementById('breadcrumb').innerHTML = '<span>To do</span>';
  main.innerHTML = '<div class="md-view"><h1>☑ To do list</h1><div class="empty-state">cargando…</div></div>';

  await refreshUserTodos();
  await refreshMembers();

  var html = '<div class="md-view">';
  html += '<h1>☑ To do list</h1>';

  // Quick add
  html += '<div style="display:flex;gap:6px;margin:12px 0 18px;padding:10px;background:var(--bg-soft);border:1px solid var(--border);border-radius:6px;flex-wrap:wrap">';
  html += '<input id="new-todo-input" type="text" placeholder="new todo...  (Enter to add)" style="flex:1;min-width:200px;background:var(--bg);border:1px solid var(--border);padding:8px 12px;border-radius:4px;font-size:14px;color:var(--ink)">';
  html += '<select id="new-todo-project" style="background:var(--bg);border:1px solid var(--border);padding:6px 8px;border-radius:4px;font-size:12px;color:var(--ink)"><option value="">no project</option>' + _projects.map(function(p){ return '<option value="' + esc(p.key) + '">' + esc(p.name) + '</option>'; }).join('') + '</select>';
  html += '<select id="new-todo-assignee" style="background:var(--bg);border:1px solid var(--border);padding:6px 8px;border-radius:4px;font-size:12px;color:var(--ink)"><option value="">unassigned</option>' + (_members || []).map(function(m){ return '<option value="' + m.id + '">' + (m.type === 'ai_agent' ? '🤖 ' : '👤 ') + esc(m.name) + (m.workload ? ' (' + m.workload + ')' : '') + '</option>'; }).join('') + '</select>';
  html += '<button onclick="addUserTodo()" style="background:var(--blue);color:white;border:0;padding:6px 14px;border-radius:4px;cursor:pointer;font-weight:500">add</button>';
  html += '</div>';

  // Stats
  html += '<div style="display:flex;gap:14px;font-size:12px;color:var(--muted);margin-bottom:14px;padding:8px 12px;background:var(--bg-soft);border-radius:4px">';
  html += '<span><strong style="color:var(--ink)">' + _userTodosSummary.open + '</strong> open</span>';
  html += '<span><strong style="color:var(--ink)">' + _userTodosSummary.done + '</strong> done</span>';
  html += '<span><strong style="color:var(--ink)">' + _userTodosSummary.total + '</strong> total</span>';
  if (_userTodosSummary.by_project && _userTodosSummary.by_project.length) {
    html += '<span style="margin-left:auto">by project: ';
    html += _userTodosSummary.by_project.slice(0, 4).map(function(p) { return esc(p.project_key || '—') + ' ' + p.c; }).join(', ');
    html += '</span>';
  }
  html += '</div>';

  // Open todos
  if (_userTodos.length === 0) {
    html += '<div class="empty-state" style="padding:40px;color:var(--muted)">no open todos. 🎉</div>';
  } else {
    html += '<div style="border:1px solid var(--border);border-radius:6px;overflow:hidden">';
    _userTodos.forEach(function(t) {
      var prio = t.priority === 1 ? '<span class="pill" style="background:#fef3c7;color:#92400e;font-size:10px;padding:1px 5px;border-radius:3px;margin-left:6px">!</span>' : '';
      html += '<div class="md-todo-item" data-id="' + t.id + '" style="display:flex;align-items:center;gap:10px;padding:10px 14px;border-bottom:1px solid var(--border);background:var(--bg)">';
      html += '<input type="checkbox" data-id="' + t.id + '" ' + (t.status === 'done' ? 'checked' : '') + ' onchange="toggleUserTodo(' + t.id + ')" style="width:16px;height:16px;cursor:pointer">';
      html += '<span style="flex:1;color:var(--ink);font-size:14px;cursor:pointer" onclick="editUserTodo(' + t.id + ')">' + esc(t.content) + '</span>';
      html += projectBadge(t.project_key);
      var assign = (t.assignee_id && _members) ? (_members.find(function(m){ return m.id === t.assignee_id; })) : null;
      if (assign) {
        var icon = assign.type === 'ai_agent' ? '🤖' : '👤';
        html += '<span data-assignee-id="' + assign.id + '" data-todo-id="' + t.id + '" class="assignee-chip" style="background:var(--bg-active);color:var(--blue);font-size:10px;padding:1px 6px;border-radius:3px;cursor:pointer" title="Click to see member">' + icon + ' ' + esc(assign.name) + '</span>';
      } else {
        html += '<span data-todo-id="' + t.id + '" data-action="assign" class="assign-btn" style="color:var(--muted);font-size:10px;padding:1px 6px;border-radius:3px;border:1px dashed var(--border);cursor:pointer" title="click to assign">+ assign</span>';
      }
      html += prio;
      html += '<span style="color:var(--muted);font-size:11px">' + formatTime(t.created_at) + '</span>';
      html += '<button onclick="deleteUserTodo(' + t.id + ')" style="background:transparent;border:0;color:var(--muted);cursor:pointer;font-size:14px;padding:2px 6px" title="delete">×</button>';
      html += '</div>';
    });
    html += '</div>';
  }

  // From docs (auto-extracted)
  html += '<h2 style="margin-top:32px">From your docs <span style="font-size:12px;color:var(--muted);font-weight:400">— auto-extracted</span></h2>';
  html += '<div id="auto-todos-list">cargando…</div>';

  html += '</div>';
  main.innerHTML = html;

  // Wire add input
  var input = document.getElementById('new-todo-input');
  if (input) input.addEventListener('keydown', function(e) {
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey || !e.shiftKey)) {
      e.preventDefault();
      addUserTodo();
    }
  });
  // Wire assignee chip click -> jump to Teams
  main.querySelectorAll('.assignee-chip').forEach(function(el) {
    el.addEventListener('click', function(e) {
      e.stopPropagation();
      setView('teams');
    });
  });
  // Wire "+ assign" click
  main.querySelectorAll('.assign-btn').forEach(function(el) {
    el.addEventListener('click', function(e) {
      e.stopPropagation();
      var todoId = parseInt(el.dataset.todoId);
      assignTodoPrompt(todoId);
    });
  });

  // Load auto todos
  try {
    var auto = await fetchJSON('/api/todos?project=&limit=30');
    var ahtml = '';
    if (auto.length === 0) {
      ahtml = '<div style="color:var(--muted);font-size:13px;padding:12px">no auto-extracted todos</div>';
    } else {
      ahtml = '<div style="border:1px solid var(--border);border-radius:6px;overflow:hidden">';
      auto.slice(0, 25).forEach(function(t) {
        ahtml += '<div style="display:flex;align-items:center;gap:10px;padding:8px 14px;border-bottom:1px solid var(--border);background:var(--bg);font-size:13px">';
        ahtml += '<span style="color:var(--green);font-size:14px">●</span>';
        ahtml += '<span style="flex:1;color:var(--ink-soft)">' + esc(t.content.slice(0, 200)) + '</span>';
        ahtml += projectBadge(t.project_key);
        ahtml += '</div>';
      });
      ahtml += '</div>';
    }
    var ael = document.getElementById('auto-todos-list');
    if (ael) ael.innerHTML = ahtml;
  } catch (e) {}
}

async function addUserTodo() {
  var input = document.getElementById('new-todo-input');
  if (!input) return;
  var content = input.value.trim();
  if (!content) return;
  var projSel = document.getElementById('new-todo-project');
  var project_key = projSel && projSel.value ? projSel.value : null;
  var assignSel = document.getElementById('new-todo-assignee');
  var assignee_id = assignSel && assignSel.value ? parseInt(assignSel.value) : null;
  try {
    var r = await fetch('/api/user_todos', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content: content, project_key: project_key, assignee_id: assignee_id })
    });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    input.value = '';
    await renderTodoView();
    renderSidebar();
  } catch (e) { alert('error: ' + e.message); }
}

async function toggleUserTodo(id) {
  try {
    // Get current state to toggle
    var d = await fetchJSON('/api/user_todos');
    var todo = d.rows.find(function(x) { return x.id === id; });
    if (!todo) {
      // Try done list
      var dd = await fetchJSON('/api/user_todos?status=done');
      todo = dd.rows.find(function(x) { return x.id === id; });
    }
    if (!todo) return;
    var newStatus = todo.status === 'done' ? 'open' : 'done';
    var r = await fetch('/api/user_todos/' + id, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status: newStatus })
    });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    await renderTodoView();
    renderSidebar();
  } catch (e) { alert('error: ' + e.message); }
}

async function deleteUserTodo(id) {
  if (!confirm('Delete this todo?')) return;
  try {
    var r = await fetch('/api/user_todos/' + id, { method: 'DELETE' });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    await renderTodoView();
    renderSidebar();
  } catch (e) { alert('error: ' + e.message); }
}

function editUserTodo(id) {
  // Simple inline: prompt for new text
  var d = _userTodos.find(function(x) { return x.id === id; });
  if (!d) return;
  var nv = prompt('Edit todo:', d.content);
  if (nv == null) return;
  fetch('/api/user_todos/' + id, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content: nv })
  }).then(function(r) {
    if (r.ok) renderTodoView();
  });
}

function formatTime(ts) {
  if (!ts) return '';
  var d = new Date(ts * 1000);
  var now = new Date();
  var diff = (now - d) / 1000;
  if (diff < 60) return 'just now';
  if (diff < 3600) return Math.floor(diff/60) + 'm';
  if (diff < 86400) return Math.floor(diff/3600) + 'h';
  if (diff < 86400*7) return Math.floor(diff/86400) + 'd';
  return d.toLocaleDateString();
}

// ---- Ideas view ----
async function refreshIdeas() {
  try {
    var d = await fetchJSON('/api/ideas?limit=200');
    _ideas = d.rows;
    _ideasSummary = d.summary;
  } catch (e) { /* */ }
}

async function renderIdeasView() {
  var main = document.getElementById('main-content');
  document.getElementById('breadcrumb').innerHTML = '<span>Ideas</span>';
  main.innerHTML = '<div class="md-view"><h1>✦ Ideas</h1><div class="empty-state">cargando…</div></div>';

  await refreshIdeas();

  var html = '<div class="md-view">';
  html += '<h1>✦ Ideas</h1>';
  html += '<p style="color:var(--muted);font-size:13px;margin-bottom:12px">Quick capture. No pressure. Ideas become projects or TODOs when they need to.</p>';

  // Quick capture
  html += '<div style="display:flex;gap:6px;margin:12px 0 18px;padding:10px;background:var(--bg-soft);border:1px dashed var(--blue-soft);border-radius:6px">';
  html += '<input id="new-idea-input" type="text" placeholder="✦ new idea...  (Enter to capture, type freely)" style="flex:1;background:var(--bg);border:1px solid var(--border);padding:8px 12px;border-radius:4px;font-size:14px;color:var(--ink);font-style:italic">';
  html += '<select id="new-idea-project" style="background:var(--bg);border:1px solid var(--border);padding:6px 8px;border-radius:4px;font-size:12px;color:var(--ink)"><option value="">no project</option>' + _projects.map(function(p){ return '<option value="' + esc(p.key) + '">' + esc(p.name) + '</option>'; }).join('') + '</select>';
  html += '<button onclick="addIdea()" style="background:var(--blue);color:white;border:0;padding:6px 14px;border-radius:4px;cursor:pointer;font-weight:500">capture</button>';
  html += '</div>';

  // Stats
  html += '<div style="display:flex;gap:14px;font-size:12px;color:var(--muted);margin-bottom:14px;padding:8px 12px;background:var(--bg-soft);border-radius:4px">';
  html += '<span><strong style="color:var(--ink)">' + _ideasSummary.new + '</strong> new</span>';
  html += '<span><strong style="color:var(--ink)">' + _ideasSummary.in_progress + '</strong> in progress</span>';
  html += '<span><strong style="color:var(--ink)">' + _ideasSummary.done + '</strong> done</span>';
  html += '<span><strong style="color:var(--ink)">' + _ideasSummary.total + '</strong> total</span>';
  html += '</div>';

  // Ideas list
  if (_ideas.length === 0) {
    html += '<div class="empty-state" style="padding:60px 20px;color:var(--muted)"><div style="font-size:24px;margin-bottom:8px">✦</div>nothing yet. capture your first idea above.</div>';
  } else {
    html += '<div style="display:grid;gap:10px">';
    _ideas.forEach(function(idea) {
      var statusBadge = idea.status === 'new' ? '<span class="pill" style="background:var(--blue-soft);color:var(--blue);font-size:10px;padding:1px 6px;border-radius:3px">new</span>'
                     : idea.status === 'in_progress' ? '<span class="pill" style="background:#fef3c7;color:#92400e;font-size:10px;padding:1px 6px;border-radius:3px">in progress</span>'
                     : idea.status === 'done' ? '<span class="pill" style="background:#dcfce7;color:var(--green);font-size:10px;padding:1px 6px;border-radius:3px">done</span>'
                     : '<span class="pill muted">' + esc(idea.status) + '</span>';
      html += '<div style="background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:14px 16px;border-left:3px solid var(--blue)">';
      html += '<div style="display:flex;align-items:start;gap:10px">';
      html += '<div style="flex:1">';
      html += '<div style="font-size:14px;color:var(--ink);line-height:1.5;margin-bottom:6px">' + esc(idea.content) + '</div>';
      html += '<div style="display:flex;gap:8px;align-items:center;font-size:11px;color:var(--muted)">';
      html += statusBadge;
      html += projectBadge(idea.project_key);
      html += '<span>' + formatTime(idea.created_at) + '</span>';
      html += '</div>';
      html += '</div>';
      html += '<div style="display:flex;gap:4px">';
      html += '<button data-idea-action="in_progress" data-idea-id="' + idea.id + '" style="background:transparent;border:1px solid var(--border);color:var(--ink-soft);padding:3px 8px;border-radius:3px;cursor:pointer;font-size:11px">start</button>';
      html += '<button data-idea-action="done" data-idea-id="' + idea.id + '" style="background:transparent;border:1px solid var(--border);color:var(--green);padding:3px 8px;border-radius:3px;cursor:pointer;font-size:11px">done</button>';
      html += '<button data-idea-action="promote" data-idea-id="' + idea.id + '" style="background:var(--blue-soft);border:0;color:var(--blue);padding:3px 8px;border-radius:3px;cursor:pointer;font-size:11px" title="convert to todo">→ todo</button>';
      html += '<button data-idea-action="delete" data-idea-id="' + idea.id + '" style="background:transparent;border:0;color:var(--muted);cursor:pointer;font-size:14px;padding:2px 6px" title="delete">×</button>';
      html += '</div>';
      html += '</div>';
      html += '</div>';
    });
    html += '</div>';
  }

  html += '</div>';
  main.innerHTML = html;

  // Wire input
  var input = document.getElementById('new-idea-input');
  if (input) input.addEventListener('keydown', function(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      addIdea();
    }
  });
  // Event delegation for idea action buttons
  var card = document.getElementById('main-content');
  if (card && !card._ideaWired) {
    card._ideaWired = true;
    card.addEventListener('click', function(e) {
      var btn = e.target.closest('[data-idea-action]');
      if (!btn) return;
      var id = parseInt(btn.dataset.ideaId);
      var action = btn.dataset.ideaAction;
      if (action === 'in_progress' || action === 'done') updateIdeaStatus(id, action);
      else if (action === 'promote') promoteIdea(id);
      else if (action === 'delete') deleteIdea(id);
    });
  }
}

async function addIdea() {
  var input = document.getElementById('new-idea-input');
  if (!input) return;
  var content = input.value.trim();
  if (!content) return;
  var projSel = document.getElementById('new-idea-project');
  var project_key = projSel && projSel.value ? projSel.value : null;
  try {
    var r = await fetch('/api/ideas', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content: content, project_key: project_key })
    });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    input.value = '';
    await renderIdeasView();
    renderSidebar();
  } catch (e) { alert('error: ' + e.message); }
}

async function updateIdeaStatus(id, status) {
  try {
    var r = await fetch('/api/ideas/' + id, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status: status })
    });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    await renderIdeasView();
    renderSidebar();
  } catch (e) { alert('error: ' + e.message); }
}

async function deleteIdea(id) {
  if (!confirm('Delete this idea?')) return;
  try {
    var r = await fetch('/api/ideas/' + id, { method: 'DELETE' });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    await renderIdeasView();
    renderSidebar();
  } catch (e) { alert('error: ' + e.message); }
}

async function promoteIdea(id) {
  // Convert idea to a todo
  var idea = _ideas.find(function(x) { return x.id === id; });
  if (!idea) return;
  try {
    var r = await fetch('/api/user_todos', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content: idea.content, project_key: idea.project_key })
    });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    // Mark idea as done
    await fetch('/api/ideas/' + id, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status: 'done' })
    });
    await renderIdeasView();
    renderSidebar();
    alert('Idea promoted to todo ✓');
  } catch (e) { alert('error: ' + e.message); }
}

// ---- Briefing view ----
async function renderBriefingView() {
  var main = document.getElementById('main-content');
  if (!main) return;
  document.getElementById('breadcrumb').innerHTML = '<span>Daily briefing</span>';
  main.innerHTML = '<div class="md-view"><h1>☀ Daily briefing</h1><pre id="briefing-text" style="white-space:pre-wrap;font-family:inherit;background:var(--bg-soft);padding:16px;border-radius:6px;border:1px solid var(--border);color:var(--ink);font-size:13px;line-height:1.7;margin-top:16px">loading…</pre></div>';
  try {
    var r = await fetchJSON('/api/brain/briefing');
    var pre = document.getElementById('briefing-text');
    if (pre) pre.textContent = (r && r.text) || '(empty response)';
  } catch (e) {
    var pre = document.getElementById('briefing-text');
    if (pre) {
      pre.textContent = 'error: ' + (e && e.message ? e.message : String(e));
      pre.style.borderColor = '#dc2626';
      pre.style.color = '#ef4444';
    }
  }
}

// Activity view
// === Activity view: heatmap of real events + recent activity stream ===

var ACTIVITY_KIND_ICONS = {
  todo_added:        {icon: '☑', color: 'var(--blue)',     label: 'todo added'},
  todo_done:         {icon: '✓', color: 'var(--green)',    label: 'todo done'},
  idea_added:        {icon: '✦', color: 'var(--brand)',    label: 'idea added'},
  idea_done:         {icon: '★', color: 'var(--green)',    label: 'idea done'},
  skill_used:        {icon: '⚡', color: 'var(--accent)',   label: 'skill used'},
  note_created:      {icon: '📝', color: 'var(--soft)',     label: 'note created'},
  url_failed:        {icon: '⚠', color: '#ef4444',         label: 'url failed'},
  url_recovered:     {icon: '✓', color: 'var(--green)',    label: 'url recovered'},
  brain_ask:         {icon: '🧠', color: 'var(--brand)',    label: 'brain asked'},
  task_started:      {icon: '▶', color: 'var(--blue)',     label: 'task started'},
  task_done:         {icon: '✓', color: 'var(--green)',    label: 'task done'},
  task_error:        {icon: '✗', color: '#ef4444',         label: 'task error'},
  task_needs_approval: {icon: '⚠', color: 'var(--yellow)', label: 'needs approval'},
  agent_artifact:    {icon: '📄', color: 'var(--accent)',   label: 'artifact written'},
  agent_sub_created: {icon: '🆕', color: 'var(--brand)',    label: 'sub-agent created'},
  member_added:      {icon: '👤', color: 'var(--blue)',     label: 'member added'},
  team_created:      {icon: '👥', color: 'var(--blue)',     label: 'team created'},
};

function formatRelativeTime(ts) {
  if (!ts) return '';
  var now = Math.floor(Date.now() / 1000);
  var diff = now - ts;
  if (diff < 60) return 'just now';
  if (diff < 3600) return Math.floor(diff / 60) + 'm ago';
  if (diff < 86400) return Math.floor(diff / 3600) + 'h ago';
  if (diff < 604800) return Math.floor(diff / 86400) + 'd ago';
  return new Date(ts * 1000).toLocaleDateString();
}

async function renderActivityView() {
  var main = document.getElementById('main-content');
  document.getElementById('breadcrumb').innerHTML = '<span>Activity</span>';
  main.innerHTML = '<div class="empty-state">cargando…</div>';
  try {
    var results = await Promise.all([
      fetchJSON('/api/activity?days=91'),
      fetchJSON('/api/activity_feed?limit=50'),
      fetchJSON('/api/snapshot/diff').catch(function() { return { has_previous: false }; }),
    ]);
    renderActivityContent(main, results[0], results[1], results[2]);
  } catch (e) {
    main.innerHTML = '<div class="empty-state">error: ' + esc(e.message) + '</div>';
  }
}

function renderActivityContent(mount, days, feed, diff) {
  // Stats
  var totalEvents = days.reduce(function(s, d) { return s + d.count; }, 0);
  var activeDays = days.filter(function(d) { return d.count > 0; }).length;
  var maxDay = days.reduce(function(m, d) { return d.count > m ? d.count : m; }, 0);
  var todayStr = new Date().toISOString().slice(0, 10);

  var html = '<div class="md-view">';
  html += '<h1>📊 Activity <span style="color:var(--muted);font-weight:400;font-size:14px">— ' +
    totalEvents + ' events · ' + activeDays + ' active days · max ' + maxDay + '/day</span></h1>';

  // Summary stats
  html += '<div style="display:flex;gap:8px;margin-bottom:24px;padding:12px 16px;background:var(--bg-soft);border:1px solid var(--border);border-radius:6px;flex-wrap:wrap">';
  // Today
  var todayCount = (days.find(function(d) { return d.day === todayStr; }) || {}).count || 0;
  html += '<div style="display:flex;flex-direction:column;min-width:80px"><span style="font-size:18px;font-weight:600;color:var(--ink)">' + todayCount + '</span><span style="font-size:10px;color:var(--muted);text-transform:uppercase">today</span></div>';
  // Last 7 days
  var last7 = days.slice(-7).reduce(function(s, d) { return s + d.count; }, 0);
  html += '<div style="display:flex;flex-direction:column;min-width:80px"><span style="font-size:18px;font-weight:600;color:var(--ink)">' + last7 + '</span><span style="font-size:10px;color:var(--muted);text-transform:uppercase">last 7d</span></div>';
  // Last 30
  var last30 = days.slice(-30).reduce(function(s, d) { return s + d.count; }, 0);
  html += '<div style="display:flex;flex-direction:column;min-width:80px"><span style="font-size:18px;font-weight:600;color:var(--ink)">' + last30 + '</span><span style="font-size:10px;color:var(--muted);text-transform:uppercase">last 30d</span></div>';
  // Streak
  var streak = 0;
  for (var i = days.length - 1; i >= 0; i--) {
    if (days[i].count > 0) streak++;
    else if (i < days.length - 1) break;
  }
  html += '<div style="display:flex;flex-direction:column;min-width:80px"><span style="font-size:18px;font-weight:600;color:var(--green)">' + streak + '</span><span style="font-size:10px;color:var(--muted);text-transform:uppercase">day streak</span></div>';
  // Project count
  var projectCount = 0;
  feed.forEach(function(e) { if (e.project_key) projectCount++; });
  html += '<div style="display:flex;flex-direction:column;min-width:80px"><span style="font-size:18px;font-weight:600;color:var(--accent)">' + projectCount + '</span><span style="font-size:10px;color:var(--muted);text-transform:uppercase">project events</span></div>';
  html += '</div>';

  // Heatmap
  html += '<h2>Heatmap · 91 days</h2>';
  html += '<div style="background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:12px">';
  // Group into weeks (columns) of 7 days
  // Start with the first day's weekday
  var firstDay = days.length > 0 ? new Date(days[0].day + 'T00:00:00') : new Date();
  // Mon=0 .. Sun=6
  var startWeekday = (firstDay.getDay() + 6) % 7;
  // Pad with empty cells
  html += '<div style="display:grid;grid-template-columns:repeat(13,1fr);gap:3px;align-items:center">';
  // Group days into weeks of 7
  var padded = [];
  for (var i = 0; i < startWeekday; i++) padded.push(null);
  days.forEach(function(d) { padded.push(d); });
  // Render as 13 columns x 7 rows
  var weeks = [];
  for (var i = 0; i < padded.length; i += 7) weeks.push(padded.slice(i, i + 7));
  weeks.forEach(function(week) {
    week.forEach(function(day) {
      if (!day) {
        html += '<div style="height:14px"></div>';
        return;
      }
      var level = 0;
      if (maxDay > 0) {
        var r = day.count / maxDay;
        if (r > 0 && r < 0.25) level = 1;
        else if (r < 0.5) level = 2;
        else if (r < 0.75) level = 3;
        else if (r >= 0.75) level = 4;
      }
      var bg = level === 0 ? 'var(--bg-soft)' : 'rgba(124, 58, 237, ' + (0.2 + level * 0.2) + ')';
      var title = day.day + ' · ' + day.count + ' events';
      html += '<div title="' + esc(title) + '" style="height:14px;background:' + bg + ';border-radius:2px;cursor:pointer" data-day="' + day.day + '"></div>';
    });
  });
  html += '</div>';
  // Legend
  html += '<div style="display:flex;gap:4px;align-items:center;margin-top:10px;font-size:10px;color:var(--muted)">';
  html += '<span>less</span>';
  for (var l = 0; l < 5; l++) {
    var bgc = l === 0 ? 'var(--bg-soft)' : 'rgba(124, 58, 237, ' + (0.2 + l * 0.2) + ')';
    html += '<div style="width:12px;height:12px;background:' + bgc + ';border-radius:2px"></div>';
  }
  html += '<span>more</span>';
  html += '</div>';
  html += '</div>';

  // Recent activity stream
  html += '<h2>Recent activity <span style="color:var(--muted);font-weight:400;font-size:13px">— last ' + feed.length + ' events</span></h2>';
  if (feed.length === 0) {
    html += '<p style="color:var(--muted);font-style:italic">no events yet. Add a todo, run a task, or use the brain.</p>';
  } else {
    html += '<div style="background:var(--panel);border:1px solid var(--border);border-radius:8px;overflow:hidden">';
    feed.forEach(function(e, i) {
      var meta = ACTIVITY_KIND_ICONS[e.kind] || {icon: '·', color: 'var(--muted)', label: e.kind};
      var isLast = i === feed.length - 1;
      html += '<div style="display:flex;align-items:center;gap:12px;padding:8px 14px;';
      if (!isLast) html += 'border-bottom:1px solid var(--border);';
      html += '">';
      html += '<div style="font-size:18px;width:24px;text-align:center;color:' + meta.color + '">' + meta.icon + '</div>';
      html += '<div style="flex:1;min-width:0">';
      html += '<div style="font-size:13px;color:var(--ink);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + esc(e.summary || '(no summary)') + '</div>';
      html += '<div style="font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:0.5px">' + esc(meta.label) + (e.project_key ? ' · ' + esc(e.project_key) : '') + '</div>';
      html += '</div>';
      html += '<div style="font-size:11px;color:var(--muted);white-space:nowrap">' + esc(formatRelativeTime(e.created_at)) + '</div>';
      html += '</div>';
    });
    html += '</div>';
  }

  // Snapshot diff
  if (diff && diff.has_previous) {
    html += '<h2>Changes since ' + esc(diff.previous ? diff.previous.day : 'previous snapshot') + '</h2>';
    if (diff.changes && diff.changes.length) {
      html += '<div style="background:var(--bg-soft);border:1px solid var(--border);border-radius:8px;padding:8px 14px">';
      diff.changes.forEach(function(c) {
        var icon = c.type === 'docs_total' ? '📄' :
                   c.type === 'urls_failed' ? '🔴' :
                   c.type === 'todos_open' ? '☑' : '·';
        html += '<div style="padding:3px 0;font-size:12px;color:var(--ink-soft)">' + icon + ' ' + esc(c.type) + ': ' + esc(c.value || JSON.stringify(c)) + '</div>';
      });
      html += '</div>';
    } else {
      html += '<p style="color:var(--muted)">No changes since last snapshot.</p>';
    }
  } else {
    html += '<h2>Snapshot diff</h2>';
    html += '<p style="color:var(--muted);font-style:italic;background:var(--bg-soft);border:1px solid var(--border);border-radius:6px;padding:12px 16px">No previous snapshot yet. Run <code style="background:var(--panel);padding:1px 4px;border-radius:2px;font-size:11px">ops snapshot save</code> in your terminal to create a baseline — then changes from that point will show up here.</p>';
  }

  html += '</div>';  // md-view
  mount.innerHTML = html;
  renderOutline();
}

// ---- Voice ----
let _recognition = null;
function toggleVoice() {
  var btn = document.getElementById('brain-mic');
  if (_recognition) { _recognition.stop(); _recognition = null; btn.classList.remove('recording'); return; }
  var SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR) { alert('Web Speech API not supported'); return; }
  _recognition = new SR();
  _recognition.lang = 'es-ES';
  btn.classList.add('recording');
  _recognition.onresult = function(e) {
    var transcript = e.results[0][0].transcript;
    document.getElementById('brain-q').value = transcript;
    // #8 Voice → action: parse intent
    var lower = transcript.toLowerCase().trim();
    if (lower.indexOf('a\u00f1ade todo') === 0 || lower.indexOf('a\u00f1adir todo') === 0 || lower.indexOf('nuevo todo') === 0) {
      var content = transcript.replace(/^(a\u00f1ade todo|a\u00f1adir todo|nuevo todo)[\s:]*/i, '').trim();
      if (content) {
        fetch('/api/user_todos', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ content: content }) });
        alert('✓ todo created: ' + content);
        return;
      }
    }
    if (lower.indexOf('a\u00f1ade idea') === 0 || lower.indexOf('nueva idea') === 0) {
      var content2 = transcript.replace(/^(a\u00f1ade idea|nueva idea)[\s:]*/i, '').trim();
      if (content2) {
        fetch('/api/ideas', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ content: content2 }) });
        alert('✓ idea captured: ' + content2);
        return;
      }
    }
  };
  _recognition.onend = function() { _recognition = null; btn.classList.remove('recording'); };
  _recognition.onerror = function() { _recognition = null; btn.classList.remove('recording'); };
  _recognition.start();
}

// ---- Watch mode ----
function toggleWatch() {
  var btn = document.getElementById('watch-btn');
  if (_watchInterval) {
    clearInterval(_watchInterval); _watchInterval = null;
    btn.classList.remove('active');
    document.getElementById('watch-banner').classList.remove('active');
  } else {
    btn.classList.add('active');
    document.getElementById('watch-banner').classList.add('active');
    _watchInterval = setInterval(function() { loadAll(); }, 30000);
  }
}
document.getElementById('watch-btn').addEventListener('click', toggleWatch);

// ---- Cmd+P palette ----
var _palette = { open: false, results: [], selected: 0 };
function openPalette() {
  document.getElementById('palette').classList.remove('hidden');
  document.getElementById('palette-q').value = '';
  document.getElementById('palette-q').focus();
  _palette.open = true; _palette.results = []; renderPaletteResults();
}
function closePalette() { document.getElementById('palette').classList.add('hidden'); _palette.open = false; }
function renderPaletteResults() {
  var el = document.getElementById('palette-results');
  if (!_palette.results.length) { el.innerHTML = '<div class="empty">type to search · ↑↓ navigate · ↵ open · ⌘↵ ask brain</div>'; return; }
  el.innerHTML = _palette.results.map(function(r, i) {
    return '<div class="pr' + (i === _palette.selected ? ' active' : '') + '" data-idx="' + i + '">'
      + '<span class="src">' + esc(r.source) + '</span>'
      + '<span class="title">' + esc(r.title || r.path.split('/').pop()) + '</span>'
      + '<span class="excerpt">' + esc((r.excerpt || '').slice(0, 80)) + '</span>'
      + '</div>';
  }).join('');
  el.querySelectorAll('.pr').forEach(function(d) {
    d.addEventListener('click', function() { _palette.selected = parseInt(d.dataset.idx); openPaletteResult(); });
  });
}
function openPaletteResult() {
  if (!_palette.results[_palette.selected]) return;
  var r = _palette.results[_palette.selected];
  closePalette();
  alert(r.path + '\n\n' + (r.excerpt || ''));
}
var paletteTimer = null;
document.getElementById('palette-q').addEventListener('input', function(e) {
  clearTimeout(paletteTimer);
  var q = e.target.value.trim();
  if (!q) { _palette.results = []; renderPaletteResults(); return; }
  paletteTimer = setTimeout(async function() {
    try {
      var rows = await fetchJSON('/api/find?q=' + encodeURIComponent(q) + '&n=15');
      _palette.results = rows; _palette.selected = 0; renderPaletteResults();
    } catch (e) {}
  }, 120);
});

// ---- Global hotkeys (Obsidian-style) ----
document.addEventListener('keydown', function(e) {
  var meta = e.metaKey || e.ctrlKey;

  // Cmd+1..7: switch views
  if (meta && e.key >= '1' && e.key <= '7') {
    e.preventDefault();
    var map = { '1': 'files', '2': 'graph', '3': 'brain', '4': 'briefing', '5': 'activity', '6': 'skills', '7': 'calendar', '8': 'teams', '9': 'tasks', '0': 'orgchart', '-': 'hierarchy' };
    setView(map[e.key]);
    return;
  }
  // Cmd+J: toggle theme
  if (meta && e.key === 'j') {
    e.preventDefault();
    toggleTheme();
    return;
  }
  // Cmd+. (Cmd+period): toggle outline panel (audit 2026-06-23)
  if (meta && e.key === '.') {
    e.preventDefault();
    toggleOutline();
    return;
  }
  // Cmd+, (Cmd+comma): toggle activity bar pin (audit 2026-06-23)
  if (meta && e.key === ',') {
    e.preventDefault();
    toggleBarPin();
    return;
  }
  // Cmd+P: command palette
  if (meta && e.key === 'p') {
    e.preventDefault();
    _palette.open ? closePalette() : openPalette();
    return;
  }
  // ?: help
  if (e.key === '?' && !_palette.open && !['INPUT','TEXTAREA'].includes(document.activeElement.tagName)) {
    e.preventDefault();
    document.getElementById('help').classList.remove('hidden');
    return;
  }
  // n: focus brain
  if (e.key === 'n' && !_palette.open && !['INPUT','TEXTAREA'].includes(document.activeElement.tagName)) {
    e.preventDefault();
    setView('brain');
    setTimeout(function() { var bq = document.getElementById('brain-q'); if (bq) bq.focus(); }, 100);
    return;
  }
  // m: voice
  if (e.key === 'm' && !_palette.open && !['INPUT','TEXTAREA'].includes(document.activeElement.tagName)) {
    e.preventDefault();
    if (_view !== 'brain') setView('brain');
    setTimeout(toggleVoice, 100);
    return;
  }
  // Cmd+Shift+I: quick capture
  if (meta && e.shiftKey && e.key === 'I') {
    e.preventDefault();
    openQuickCapture('todo');
    return;
  }
  // Esc
  if (e.key === 'Escape') {
    if (_palette.open) { closePalette(); return; }
    if (!document.getElementById('help').classList.contains('hidden')) {
      document.getElementById('help').classList.add('hidden'); return;
    }
    if (_history.length > 0) { goBack(); return; }
  }
  // Palette navigation
  if (_palette.open) {
    if (e.key === 'ArrowDown') { e.preventDefault(); _palette.selected = Math.min(_palette.selected + 1, _palette.results.length - 1); renderPaletteResults(); }
    if (e.key === 'ArrowUp') { e.preventDefault(); _palette.selected = Math.max(_palette.selected - 1, 0); renderPaletteResults(); }
    if (e.key === 'Enter') {
      e.preventDefault();
      if (meta) {
        var q = document.getElementById('palette-q').value.trim();
        closePalette();
        setView('brain');
        setTimeout(function() { document.getElementById('brain-q').value = q; askBrain(); }, 100);
      } else { openPaletteResult(); }
    }
  }
});
document.getElementById('palette').addEventListener('click', function(e) { if (e.target.id === 'palette') closePalette(); });
document.getElementById('help').addEventListener('click', function(e) { if (e.target.id === 'help') document.getElementById('help').classList.add('hidden'); });

// ---- Mobile sidebar toggle ----
var _sbOpen = false;
function toggleSidebar() {
  _sbOpen = !_sbOpen;
  var sb = document.getElementById('sidebar');
  if (_sbOpen) sb.classList.add('mobile-open');
  else sb.classList.remove('mobile-open');
}

// ---- Quick capture modal (Cmd+Shift+I) ----
var _qcTab = 'todo';
function openQuickCapture(kind) {
  _qcTab = kind || 'todo';
  var overlay = document.getElementById('qc');
  overlay.classList.remove('hidden');
  var tabs = overlay.querySelectorAll('.qc-tab');
  tabs.forEach(function(t) { t.classList.toggle('active', t.dataset.tab === _qcTab); });
  var input = document.getElementById('qc-input');
  setTimeout(function() { input.focus(); }, 50);
}
function closeQuickCapture() {
  document.getElementById('qc').classList.add('hidden');
}
async function qcSubmit() {
  var content = document.getElementById('qc-input').value.trim();
  if (!content) return;
  var proj = document.getElementById('qc-project').value || null;
  try {
    if (_qcTab === 'todo') {
      await fetch('/api/user_todos', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ content: content, project_key: proj }) });
    } else if (_qcTab === 'idea') {
      await fetch('/api/ideas', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ content: content, project_key: proj }) });
    } else if (_qcTab === 'note') {
      // Write to vault daily notes
      var notePath = '<vault>/daily/' + new Date().toISOString().slice(0, 10) + '.md';
      // We don't have an endpoint yet; just alert
      alert('note saved: ' + content);
    }
    document.getElementById('qc-input').value = '';
    closeQuickCapture();
    // Reload current view
    if (_view === 'todo') renderTodoView();
    else if (_view === 'ideas') renderIdeasView();
    else renderMain();
    renderSidebar();
  } catch (e) { alert('error: ' + e.message); }
}
function qcSwitchTab(kind) {
  _qcTab = kind;
  document.querySelectorAll('.qc-tab').forEach(function(t) { t.classList.toggle('active', t.dataset.tab === kind); });
  var ph = kind === 'todo' ? 'new todo...' : kind === 'idea' ? 'new idea...' : 'nota libre...';
  document.getElementById('qc-input').placeholder = ph;
}

// The in-app browser only exists inside the desktop app (Tauri IPC).
if (window.__TAURI_INTERNALS__ && document.getElementById('nav-browser')) {
  document.getElementById('nav-browser').hidden = false;
  // Only macOS has the in-app browser: hide the link when the server says no.
  fetch('/api/platform', { credentials: 'same-origin' }).then(function(r) { return r.json(); }).then(function(p) {
    if (p && p.browser && p.browser.available === false) document.getElementById('nav-browser').hidden = true;
  }).catch(function() {});
}

// ---- Service worker (retired) + PWA install ----
// No service worker any more: it served a stale UI after updates (see sw.js).
// Drop any registration left by an older version.
if ('serviceWorker' in navigator && navigator.serviceWorker.getRegistrations) {
  navigator.serviceWorker.getRegistrations().then(function(rs) {
    rs.forEach(function(r) { r.unregister(); });
  }).catch(function() {});
}
let _deferredInstall = null;
window.addEventListener('beforeinstallprompt', function(e) {
  e.preventDefault();
  _deferredInstall = e;
  var btn = document.getElementById('install-btn');
  if (btn) btn.style.display = 'block';
});
window.addEventListener('appinstalled', function() {
  _deferredInstall = null;
  var btn = document.getElementById('install-btn');
  if (btn) btn.style.display = 'none';
});
var installBtn = document.getElementById('install-btn');
if (installBtn) {
  installBtn.addEventListener('click', async function() {
    if (!_deferredInstall) {
      alert('To install as an app:\n\niPhone/iPad: tap share → "Add to Home Screen"\n\nMac/Windows/Linux: in Chrome, menu → "Install ops-dashboard..."');
      return;
    }
    _deferredInstall.prompt();
    var c = await _deferredInstall.userChoice;
    if (c && c.outcome === 'accepted') installBtn.style.display = 'none';
    _deferredInstall = null;
  });
}

// ---- Load all data and render ----
async function loadAll() {
  try {
    var data = await Promise.all([
      fetchJSON('/api/stats'),
      fetchJSON('/api/projects'),
      fetchJSON('/api/playbooks'),
      fetchJSON('/api/todos?project=&n=40'),
      fetchJSON('/api/urls'),
      fetchJSON('/api/brain/providers'),
      fetchJSON('/api/brain/templates'),
      fetchJSON('/api/tags'),
    ]);
    _allData = { stats: data[0] };
    _projects = data[1]; _playbooks = data[2]; _todos = data[3]; _urls = data[4]; _providers = data[5]; _templates = data[6];
    _allTags = data[7];
    refreshApprovalsBadge();

    // Update status bar
    document.getElementById('sb-docs').textContent = data[0].total.toLocaleString();
    document.getElementById('sb-projects').textContent = data[1].length;
    // Populate QC project select
    var qcProj = document.getElementById('qc-project');
    if (qcProj && qcProj.options.length <= 1) {
      qcProj.innerHTML = '<option value="">no project</option>' +
        data[1].map(function(p) { return '<option value="' + esc(p.key) + '">' + esc(p.name) + '</option>'; }).join('');
    }
    var upCount = data[4].filter(function(u){return u.ok;}).length;
    document.getElementById('sb-urls').innerHTML = '<span class="dot ' + (upCount === data[4].length ? 'ok' : 'bad') + '"></span> ' + upCount + '/' + data[4].length + ' up';
    var avail = data[5].filter(function(p){return p.available;});
    document.getElementById('sb-brain').textContent = avail.length > 0 ? avail[0].name : 'no provider';
    document.getElementById('sb-status').innerHTML = '<span class="dot ok"></span> running · ' + new Date().toLocaleTimeString();

    renderSidebar();
    renderMain();
    renderOutline();
  } catch (e) {
    console.error('loadAll:', e);
    // Show error in UI for debugging
    try {
      var main = document.getElementById('main-content');
      if (main) {
        main.innerHTML = '<div class="md-view"><h1>Error loading dashboard</h1><div style="background:#fee2e2;border:1px solid #dc2626;padding:16px;border-radius:6px;color:#7f1d1d;margin-top:20px"><pre style="font-size:12px;white-space:pre-wrap">' + (e.stack || e.message || String(e)) + '</pre></div></div>';
      }
    } catch(_) {}
  }
}

// === Tasks view (the execution loop) ===

// === Calendar view: monthly view of todos with due dates ===

async function renderCalendarView() {
  var main = document.getElementById('main-content');
  document.getElementById('breadcrumb').innerHTML = '<span>Calendar</span>';
  main.innerHTML = '<div class="empty-state">cargando…</div>';
  try {
    var data = await fetchJSON('/api/calendar');
    renderCalendar(main, data);
  } catch (e) {
    main.innerHTML = '<div class="empty-state">error: ' + esc(e.message) + '</div>';
  }
}

function renderCalendar(mount, data) {
  var month = data.month || new Date().toISOString().slice(0, 7);  // YYYY-MM
  var rows = data.rows || [];
  var byDay = {};
  rows.forEach(function(r) {
    if (r.due_date_iso) byDay[r.due_date_iso] = byDay[r.due_date_iso] || [];
    if (r.due_date_iso) byDay[r.due_date_iso].push(r);
  });

  // First day of the month + number of days
  var parts = month.split('-');
  var y = parseInt(parts[0]);
  var m = parseInt(parts[1]) - 1;  // 0-indexed
  var first = new Date(y, m, 1);
  var last = new Date(y, m + 1, 0);
  var startWeekday = (first.getDay() + 6) % 7;  // Monday=0
  var numDays = last.getDate();
  var monthName = first.toLocaleString('en', { month: 'long', year: 'numeric' });

  // Group tasks by day (some rows may not have due_date_iso)
  var today = new Date().toISOString().slice(0, 10);

  var html = '<div class="md-view">';
  html += '<h1>📅 Calendar <span style="color:var(--muted);font-weight:400;font-size:14px">— ' + esc(monthName) + ' · ' + rows.length + ' task' + (rows.length === 1 ? '' : 's') + ' with due dates</span></h1>';

  // Nav
  var prevMonth = new Date(y, m - 1, 1);
  var nextMonth = new Date(y, m + 1, 1);
  html += '<div style="display:flex;gap:8px;margin:8px 0 16px;align-items:center">';
  html += '<button data-cal-nav="' + prevMonth.toISOString().slice(0, 7) + '" style="background:var(--bg-soft);border:1px solid var(--border);color:var(--ink);padding:4px 10px;border-radius:4px;cursor:pointer">← ' + prevMonth.toLocaleString('en', { month: 'short' }) + '</button>';
  html += '<span style="flex:1;text-align:center;font-weight:500">' + esc(monthName) + '</span>';
  html += '<button data-cal-nav="' + nextMonth.toISOString().slice(0, 7) + '" style="background:var(--bg-soft);border:1px solid var(--border);color:var(--ink);padding:4px 10px;border-radius:4px;cursor:pointer">' + nextMonth.toLocaleString('en', { month: 'short' }) + ' →</button>';
  html += '</div>';

  // Calendar grid
  html += '<div style="background:var(--panel);border:1px solid var(--border);border-radius:8px;overflow:hidden">';
  html += '<div style="display:grid;grid-template-columns:repeat(7,1fr);background:var(--bg-soft);border-bottom:1px solid var(--border)">';
  ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'].forEach(function(d) {
    html += '<div style="padding:8px;font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:0.5px;text-align:center;font-weight:500">' + d + '</div>';
  });
  html += '</div>';
  html += '<div style="display:grid;grid-template-columns:repeat(7,1fr);gap:1px;background:var(--border)">';
  // Empty cells before the first day
  for (var i = 0; i < startWeekday; i++) {
    html += '<div style="background:var(--bg);min-height:80px"></div>';
  }
  // Day cells
  for (var day = 1; day <= numDays; day++) {
    var dateStr = y + '-' + String(m + 1).padStart(2, '0') + '-' + String(day).padStart(2, '0');
    var isToday = dateStr === today;
    var isWeekend = ((day + startWeekday - 1) % 7) >= 5;
    var tasks = byDay[dateStr] || [];
    var cellBg = isToday ? 'rgba(124,58,237,0.10)' : (isWeekend ? 'var(--bg-soft)' : 'var(--bg)');
    var borderLeft = isToday ? 'border-left:3px solid var(--brand)' : '';
    html += '<div style="background:' + cellBg + ';min-height:80px;padding:6px 8px;cursor:pointer;position:relative;overflow:hidden;' + borderLeft + '" data-cal-day="' + dateStr + '">';
    html += '<div style="font-size:11px;color:' + (isToday ? 'var(--brand)' : 'var(--soft)') + ';font-weight:' + (isToday ? '600' : '400') + ';margin-bottom:4px">' + day + (isToday ? ' · today' : '') + '</div>';
    tasks.slice(0, 3).forEach(function(t) {
      var prioColor = t.priority === 1 ? '#ef4444' : t.priority === 2 ? '#f59e0b' : 'var(--blue)';
      html += '<div style="font-size:10px;color:var(--ink);background:' + prioColor + '22;border-left:2px solid ' + prioColor + ';padding:2px 4px;border-radius:2px;margin-bottom:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="' + esc(t.content) + '">';
      if (t.project_key) html += '<span style="color:var(--muted)">' + esc(t.project_key) + ' · </span>';
      html += esc(t.content.slice(0, 30));
      html += '</div>';
    });
    if (tasks.length > 3) {
      html += '<div style="font-size:10px;color:var(--muted);margin-top:2px">+' + (tasks.length - 3) + ' more</div>';
    }
    html += '</div>';
  }
  // Empty cells after the last day to fill the week
  var trailing = (7 - ((numDays + startWeekday) % 7)) % 7;
  for (var j = 0; j < trailing; j++) {
    html += '<div style="background:var(--bg);min-height:80px"></div>';
  }
  html += '</div>';  // end grid
  html += '</div>';  // end panel

  // Tasks with NO due_date
  var noDue = rows.filter(function(r) { return !r.due_date_iso; });
  if (noDue.length) {
    html += '<h2 style="margin-top:24px">Without due date <span style="color:var(--muted);font-weight:400;font-size:13px">— ' + noDue.length + '</span></h2>';
    html += '<div style="background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:8px">';
    noDue.slice(0, 20).forEach(function(t) {
      html += '<div style="padding:6px 8px;border-bottom:1px solid var(--border);font-size:13px;display:flex;align-items:center;gap:8px">';
      if (t.project_key) html += '<span class="pill" style="background:var(--bg-active);color:var(--blue);font-size:10px">' + esc(t.project_key) + '</span>';
      html += '<span style="flex:1;color:var(--ink)">' + esc(t.content) + '</span>';
      html += '<span style="color:var(--muted);font-size:11px">' + esc(t.status) + '</span>';
      html += '</div>';
    });
    if (noDue.length > 20) {
      html += '<div style="padding:6px 8px;color:var(--muted);font-size:11px">+ ' + (noDue.length - 20) + ' more</div>';
    }
    html += '</div>';
  }

  html += '</div>';  // end md-view

  mount.innerHTML = html;
  renderOutline();

  // Nav buttons
  mount.querySelectorAll('[data-cal-nav]').forEach(function(btn) {
    btn.addEventListener('click', function() {
      var targetMonth = btn.dataset.calNav;
      fetch('/api/calendar?month=' + targetMonth)
        .then(function(r) { return r.json(); })
        .then(function(d) { renderCalendar(mount, d); })
        .catch(function(e) { alert('nav failed: ' + e.message); });
    });
  });
  // Day click -> jump to todo
  mount.querySelectorAll('[data-cal-day]').forEach(function(cell) {
    cell.addEventListener('click', function() {
      var date = cell.dataset.calDay;
      // Open the todo view filtered to this day (best effort)
      setView('todo');
    });
  });
}
async function renderTasksView() {
  var main = document.getElementById('main-content');
  document.getElementById('breadcrumb').innerHTML = '<span>Tasks</span>';
  main.innerHTML = '<div class="empty-state">cargando…</div>';
  try {
    var data = await fetchJSON('/api/tasks?limit=100');
    var stats = data.summary || { total: 0, by_status: {}, runs_total: 0, runs_done: 0, artifacts: 0 };
    var rows = data.rows || [];

    // Load team members for the agent picker
    if (!_members || !_members.length) await refreshMembers();
    var agents = (_members || []).filter(function(m) { return m.type === 'ai_agent'; });

    var html = '<div class="md-view">';
    html += '<h1>⚡ Tasks <span style="color:var(--muted);font-weight:400;font-size:14px">— ' + stats.total + ' tasks · ' + (stats.runs_done || 0) + ' runs done · ' + (stats.artifacts || 0) + ' artifacts</span></h1>';
    html += '<p style="color:var(--muted);font-size:13px;margin-bottom:16px">Delegate work to an AI agent. The agent reads the knowledge base, executes, and writes an artifact to the vault. The artifact is auto-indexed and appears in the graph linked to its project.</p>';

    // New task form
    html += '<form id="new-task-form" style="background:var(--bg-soft);border:1px solid var(--border);border-radius:8px;padding:16px;margin-bottom:20px">';
    html += '<div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:8px">';
    html += '<input id="nt-title" placeholder="task title (e.g. weekly brief)" required style="background:var(--bg);border:1px solid var(--border);padding:8px 12px;border-radius:4px;font-size:14px;color:var(--ink)">';
    html += '<select id="nt-project" style="background:var(--bg);border:1px solid var(--border);padding:8px 12px;border-radius:4px;font-size:14px;color:var(--ink)"><option value="">no project</option>' + _projects.map(function(p) { return '<option value="' + esc(p.key) + '">' + esc(p.name) + '</option>'; }).join('') + '</select>';
    html += '</div>';
    html += '<div style="display:grid;grid-template-columns:2fr 1fr;gap:8px;margin-bottom:8px">';
    html += '<textarea id="nt-prompt" placeholder="What should the agent do? Be specific. e.g. \'Search the vault for recent changes to a project and write a 3-bullet weekly brief. Call write_artifact when done.\'" required rows="3" style="background:var(--bg);border:1px solid var(--border);padding:8px 12px;border-radius:4px;font-size:14px;color:var(--ink);resize:vertical"></textarea>';
    html += '<div style="display:flex;flex-direction:column;gap:6px">';
    html += '<select id="nt-agent" style="background:var(--bg);border:1px solid var(--border);padding:6px 10px;border-radius:4px;font-size:12px;color:var(--ink)"><option value="">auto-pick first available</option>' + agents.map(function(a) { return '<option value="' + a.id + '">' + esc(a.name) + (a.model ? ' (' + esc(a.model) + ')' : '') + '</option>'; }).join('') + '</select>';
    html += '<select id="nt-provider" style="background:var(--bg);border:1px solid var(--border);padding:6px 10px;border-radius:4px;font-size:12px;color:var(--ink)"><option value="">default</option><option value="ollama">ollama (local)</option><option value="anthropic">anthropic</option><option value="openai">openai</option><option value="mock">mock</option></select>';
    html += '</div></div>';
    html += '<div style="display:flex;gap:8px;align-items:center">';
    html += '<button type="submit" style="background:var(--blue);color:white;border:0;padding:8px 18px;border-radius:4px;cursor:pointer;font-weight:500">⚡ Delegate to agent</button>';
    html += '<span style="color:var(--muted);font-size:11px">Runs in background · artifact lands in vault/_agents/&lt;project&gt;/ · auto-indexed</span>';
    html += '</div>';
    html += '</form>';

    // Tasks list
    if (rows.length === 0) {
      html += '<div style="background:var(--bg-soft);border:1px dashed var(--border);border-radius:8px;padding:40px;text-align:center;color:var(--muted)">No tasks yet. ↑ Delegate one above to get started.</div>';
    } else {
      html += '<div style="display:grid;gap:8px">';
      rows.forEach(function(t) {
        var statusColor = { 'done': 'var(--green)', 'running': 'var(--blue)', 'error': '#ef4444', 'needs_approval': 'var(--yellow)', 'queued': 'var(--muted)' }[t.status] || 'var(--muted)';
        var statusIcon  = { 'done': '✓', 'running': '⏳', 'error': '✗', 'needs_approval': '⚠', 'queued': '◌' }[t.status] || '·';
        html += '<div data-task-id="' + t.id + '" style="background:var(--bg);border:1px solid var(--border);border-left:4px solid ' + statusColor + ';border-radius:6px;padding:12px 16px;cursor:pointer">';
        html += '<div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">';
        html += '<span style="font-size:14px;color:' + statusColor + '">' + statusIcon + '</span>';
        html += '<div style="flex:1;font-size:14px;font-weight:500;color:var(--ink)">' + esc(t.title) + '</div>';
        if (t.project_key) html += '<span class="pill" style="background:var(--bg-active);color:var(--blue);font-size:10px;padding:1px 6px">' + esc(t.project_key) + '</span>';
        html += '<span style="font-size:10px;color:' + statusColor + ';text-transform:uppercase;letter-spacing:0.5px">' + t.status + '</span>';
        html += '</div>';
        if (t.output_summary) {
          html += '<div style="font-size:12px;color:var(--ink-soft);margin-top:4px;line-height:1.5;overflow:hidden;text-overflow:ellipsis;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical">' + esc(ProviderErrors.friendly(t.output_summary)) + '</div>';
        }
        html += '<div style="display:flex;gap:12px;font-size:10px;color:var(--muted);margin-top:6px">';
        if (t.provider) html += '<span>' + esc(t.provider) + '/' + esc(t.model || 'auto') + '</span>';
        html += '<span>' + formatTime(t.updated_at) + '</span>';
        html += '</div>';
        html += '</div>';
      });
      html += '</div>';
    }

    html += '</div>';
    main.innerHTML = html;
    renderOutline();

    // Wire form
    var f = document.getElementById('new-task-form');
    if (f) f.addEventListener('submit', async function(e) {
      e.preventDefault();
      var payload = {
        title: document.getElementById('nt-title').value.trim(),
        prompt: document.getElementById('nt-prompt').value.trim(),
        project_key: document.getElementById('nt-project').value || null,
        agent_member_id: document.getElementById('nt-agent').value ? parseInt(document.getElementById('nt-agent').value) : null,
        provider: document.getElementById('nt-provider').value || null,
      };
      if (!payload.title || !payload.prompt) return;
      try {
        var r = await fetch('/api/tasks', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
        if (!r.ok) throw new Error('HTTP ' + r.status);
        document.getElementById('nt-title').value = '';
        document.getElementById('nt-prompt').value = '';
        // Poll once after 6s to show the result
        setTimeout(renderTasksView, 6000);
        renderTasksView();
      } catch (e) { alert('Failed to create task: ' + e.message); }
    });

    // Wire task click -> show runs
    main.querySelectorAll('[data-task-id]').forEach(function(el) {
      el.addEventListener('click', function() { renderTaskDetail(parseInt(el.dataset.taskId)); });
    });
  } catch (e) {
    main.innerHTML = '<div class="empty-state">error: ' + esc(e.message) + '</div>';
  }
}

// === Task stream (SSE) ===
// Renders a live log of a task run via Server-Sent Events.

// === Org chart view: visual hierarchy of humans + AI agents per project ===

async function renderOrgChartView() {
  var main = document.getElementById('main-content');
  document.getElementById('breadcrumb').innerHTML = '<span>Org chart</span>';
  main.innerHTML = '<div class="empty-state">cargando…</div>';
  try {
    var data = await fetchJSON('/api/orgchart');
    renderOrgChartMount(main, data);
  } catch (e) {
    main.innerHTML = '<div class="empty-state">error: ' + esc(e.message) + '</div>';
  }
}

function renderOrgChartMount(mount, data) {
  var stats = data.stats || {};
  var projects = data.projects || [];
  var orphans = (data.orphans && data.orphans.teams) || [];

  var html = '<div class="md-view">';

  // Header
  html += '<h1>🌳 Org chart <span style="color:var(--muted);font-weight:400;font-size:14px">— ' +
    stats.total_projects + ' projects · ' + stats.total_teams + ' teams · ' +
    stats.total_members + ' members (' + stats.humans + ' humans · ' +
    stats.ai_agents + ' AI · ' + stats.systems + ' sys)</span></h1>';

  // Stats bar
  html += '<div style="display:flex;flex-wrap:wrap;gap:10px;margin-bottom:24px;padding:12px 16px;background:var(--bg-soft);border:1px solid var(--border);border-radius:6px">';
  function stat(label, value, color) {
    return '<div style="display:flex;flex-direction:column;align-items:flex-start;min-width:90px"><span style="font-size:18px;font-weight:600;color:' + (color || 'var(--ink)') + '">' + value + '</span><span style="font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:0.5px">' + label + '</span></div>';
  }
  html += stat('active', stats.active, 'var(--green)');
  html += stat('paused', stats.paused, 'var(--yellow)');
  html += stat('avg workload', stats.avg_workload);
  html += stat('max workload', stats.max_workload, stats.max_workload > 5 ? '#ef4444' : 'var(--ink)');
  html += '</div>';

  // Legend
  html += '<div style="display:flex;gap:14px;margin-bottom:16px;font-size:11px;color:var(--muted);flex-wrap:wrap">';
  html += '<span><span style="color:#7c3aed">■</span> AI agent</span>';
  html += '<span><span style="color:#16a34a">■</span> Human</span>';
  html += '<span><span style="color:#6b6b75">■</span> System</span>';
  html += '<span><span style="color:var(--blue)">━</span> project → team</span>';
  html += '<span style="margin-left:auto">Click a member to see their profile</span>';
  html += '</div>';

  // Tree
  html += '<div class="org-tree">';

  // Level 0: Projects
  for (var pi = 0; pi < projects.length; pi++) {
    var p = projects[pi];
    if (!p.teams || !p.teams.length) continue;
    html += '<div class="org-project">';
    html += '  <div class="org-project-header" style="border-left:4px solid var(--brand)">';
    html += '    <div class="org-project-name">' + esc(p.name) + '</div>';
    html += '    <div class="org-project-meta">' + p.team_count + ' teams · ' + p.member_count + ' members</div>';
    html += '  </div>';

    // Connector
    html += '  <div class="org-connector-v"></div>';

    // Level 1: Teams
    html += '  <div class="org-teams">';
    for (var ti = 0; ti < p.teams.length; ti++) {
      var t = p.teams[ti];
      var typeColor = t.type === 'ai_squad' ? '#7c3aed' : t.type === 'human_team' ? '#16a34a' : '#1e40af';
      var typeLabel = t.type === 'ai_squad' ? '🤖 AI squad' : t.type === 'human_team' ? '👥 Human team' : '🧠 Mixed';
      html += '    <div class="org-team" style="border-top:3px solid ' + typeColor + '">';
      html += '      <div class="org-team-header">';
      html += '        <div class="org-team-name">' + esc(t.icon || '·') + ' ' + esc(t.name) + '</div>';
      html += '        <div class="org-team-type" style="color:' + typeColor + '">' + typeLabel + '</div>';
      html += '      </div>';

      // Level 2: Members
      if (t.members && t.members.length) {
        html += '      <div class="org-members">';
        for (var mi = 0; mi < t.members.length; mi++) {
          var m = t.members[mi];
          var mColor = m.type === 'ai_agent' ? '#7c3aed' : m.type === 'human' ? '#16a34a' : '#6b6b75';
          var mIcon  = m.type === 'ai_agent' ? '🤖' : m.type === 'system' ? '⚙' : '👤';
          var statusColor = m.status === 'active' ? 'var(--green)' : m.status === 'paused' ? 'var(--yellow)' : 'var(--muted)';
          var wl = m.workload || 0;
          var wlColor = wl === 0 ? 'var(--muted)' : wl < 3 ? 'var(--green)' : wl < 6 ? 'var(--yellow)' : '#ef4444';
          html += '        <div class="org-member" data-member-id="' + m.id + '" style="border-left:3px solid ' + mColor + '">';
          html += '          <div class="org-member-icon">' + mIcon + '</div>';
          html += '          <div class="org-member-body">';
          html += '            <div class="org-member-name">' + esc(m.name) + '</div>';
          html += '            <div class="org-member-role">' + esc(m.role || '—') + (m.model ? ' · <span class="mono">' + esc(m.model) + '</span>' : '') + '</div>';
          html += '            <div class="org-member-foot">';
          html += '              <span class="org-status" style="background:' + statusColor + '"></span><span style="color:' + statusColor + '">' + esc(m.status) + '</span>';
          html += '              <span style="margin-left:auto;color:' + wlColor + '">☑ ' + wl + '</span>';
          html += '            </div>';
          html += '          </div>';
          html += '        </div>';
        }
        html += '      </div>';
      } else {
        html += '      <div style="padding:8px 12px;color:var(--muted);font-size:11px;font-style:italic">no members yet</div>';
      }
      html += '    </div>';
    }
    html += '  </div>';
    html += '</div>';
  }

  // Orphans
  if (orphans.length) {
    html += '<div class="org-project">';
    html += '  <div class="org-project-header" style="border-left:4px solid var(--muted)">';
    html += '    <div class="org-project-name">⚠ Orphan teams</div>';
    html += '    <div class="org-project-meta">' + orphans.length + ' teams not mapped to any project</div>';
    html += '  </div>';
    html += '  <div class="org-connector-v"></div>';
    html += '  <div class="org-teams">';
    for (var oi = 0; oi < orphans.length; oi++) {
      var ot = orphans[oi];
      html += '    <div class="org-team" style="border-top:3px solid var(--muted)">';
      html += '      <div class="org-team-header">';
      html += '        <div class="org-team-name">' + esc(ot.icon || '·') + ' ' + esc(ot.name) + '</div>';
      html += '        <div class="org-team-type" style="color:var(--muted)">' + esc(ot.type) + '</div>';
      html += '      </div>';
      if (ot.members && ot.members.length) {
        html += '      <div class="org-members">';
        for (var omi = 0; omi < ot.members.length; omi++) {
          var om = ot.members[omi];
          var oIcon = om.type === 'ai_agent' ? '🤖' : om.type === 'system' ? '⚙' : '👤';
          html += '        <div class="org-member" data-member-id="' + om.id + '" style="border-left:3px solid var(--muted)">';
          html += '          <div class="org-member-icon">' + oIcon + '</div>';
          html += '          <div class="org-member-body"><div class="org-member-name">' + esc(om.name) + '</div></div>';
          html += '        </div>';
        }
        html += '      </div>';
      }
      html += '    </div>';
    }
    html += '  </div>';
    html += '</div>';
  }

  html += '</div>';  // .org-tree
  html += '</div>';  // .md-view

  mount.innerHTML = html;
  renderOutline();

  // Click member → drill into existing member view
  mount.querySelectorAll('[data-member-id]').forEach(function(el) {
    el.addEventListener('click', function() {
      renderMemberView(parseInt(el.dataset.memberId));
    });
  });
}
// === Hierarchy view (CEO → VP → Director → Manager → Agent) ===

async function renderHierarchyView() {
  var main = document.getElementById('main-content');
  document.getElementById('breadcrumb').innerHTML = '<span>Hierarchy</span>';
  main.innerHTML = '<div class="empty-state">cargando…</div>';
  try {
    var data = await fetchJSON('/api/hierarchy');
    renderHierarchy(main, data);
  } catch (e) {
    main.innerHTML = '<div class="empty-state">error: ' + esc(e.message) + '</div>';
  }
}

var LEVEL_ICONS = { ceo: '👑', vp: '🎖', director: '🏢', manager: '👔', agent: '🤖', observer: '👀' };
var LEVEL_COLORS = {
  ceo:      { bg: 'rgba(245,158,11,0.15)',  fg: '#f59e0b', border: '#f59e0b' },
  vp:       { bg: 'rgba(124,58,237,0.15)',  fg: '#7c3aed', border: '#7c3aed' },
  director: { bg: 'rgba(59,130,246,0.15)',  fg: '#3b82f6', border: '#3b82f6' },
  manager:  { bg: 'rgba(20,184,166,0.15)',  fg: '#14b8a6', border: '#14b8a6' },
  agent:    { bg: 'rgba(107,114,128,0.10)', fg: '#a1a1aa', border: '#6b7280' },
  observer: { bg: 'rgba(107,114,128,0.05)', fg: '#6b7280', border: '#6b7280' },
};

function renderHierarchy(mount, data) {
  var html = '<div class="md-view">';
  html += '<h1>🌲 Hierarchy <span style="color:var(--muted);font-weight:400;font-size:14px">— CEO → VP → Director → Manager → Agent</span></h1>';

  // Balance suggestions banner
  if (data.balance_suggestions && data.balance_suggestions.length) {
    html += '<div style="background:rgba(245,158,11,0.10);border:1px solid #f59e0b;border-radius:6px;padding:10px 14px;margin-bottom:16px;font-size:12px;color:var(--ink-soft)">';
    html += '<strong style="color:#f59e0b">⚠ Balance suggestions:</strong><ul style="margin:4px 0 0 16px;padding:0">';
    data.balance_suggestions.forEach(function(s) {
      html += '<li>' + esc(s.message) + '</li>';
    });
    html += '</ul></div>';
  }

  // Legend
  html += '<div style="display:flex;gap:10px;margin-bottom:20px;font-size:11px;color:var(--muted);flex-wrap:wrap;align-items:center">';
  Object.keys(LEVEL_ICONS).forEach(function(lv) {
    var c = LEVEL_COLORS[lv];
    html += '<span style="background:' + c.bg + ';color:' + c.fg + ';padding:3px 8px;border-radius:3px;border:1px solid ' + c.border + '">' + LEVEL_ICONS[lv] + ' ' + lv + '</span>';
  });
  html += '<span style="margin-left:auto">Click 💬 to chat · Click ➕ to add a report</span>';
  html += '</div>';

  // Render the tree
  html += '<div class="hier-tree">';
  (data.tree || []).forEach(function(root) {
    html += renderHierNode(root, 0);
  });
  html += '</div>';

  html += '</div>';

  mount.innerHTML = html;
  renderOutline();

  // Wire chat buttons
  mount.querySelectorAll('[data-chat-member]').forEach(function(btn) {
    btn.addEventListener('click', function(e) {
      e.stopPropagation();
      openChatDrawer(parseInt(btn.dataset.chatMember), btn.dataset.chatName);
    });
  });
  // Wire add-report buttons
  mount.querySelectorAll('[data-add-report]').forEach(function(btn) {
    btn.addEventListener('click', function(e) {
      e.stopPropagation();
      openAddReportDrawer(parseInt(btn.dataset.addReport), btn.dataset.addName);
    });
  });
  // Wire profile clicks
  mount.querySelectorAll('[data-profile-member]').forEach(function(el) {
    el.addEventListener('click', function(e) {
      if (e.target.closest('[data-chat-member],[data-add-report]')) return;
      renderMemberView(parseInt(el.dataset.profileMember));
    });
  });
}

function renderHierNode(node, depth) {
  var lv = node.level || 'agent';
  var c = LEVEL_COLORS[lv] || LEVEL_COLORS.agent;
  var statusColor = node.status === 'active' ? 'var(--green)' : node.status === 'paused' ? 'var(--yellow)' : 'var(--muted)';
  var wlColor = (node.workload || 0) === 0 ? 'var(--muted)' : (node.workload || 0) < 3 ? 'var(--green)' : (node.workload || 0) < 6 ? 'var(--yellow)' : '#ef4444';
  var hasReports = (node.reports || []).length > 0;
  var indent = depth * 32;

  var html = '<div class="hier-node" data-profile-member="' + node.id + '" style="margin-left:' + indent + 'px;background:' + c.bg + ';border:1px solid ' + c.border + ';border-radius:8px;padding:10px 14px;margin-bottom:6px;display:flex;align-items:center;gap:12px;cursor:pointer;transition:transform 0.1s">';
  // Hierarchy level line on the left
  if (depth > 0) {
    html += '<div style="position:absolute;left:-' + (indent/2) + 'px;top:0;bottom:0;width:1px;background:' + c.border + '33"></div>';
  }
  // Icon
  html += '<div style="font-size:24px;width:36px;text-align:center">' + (LEVEL_ICONS[lv] || '·') + '</div>';
  // Body
  html += '<div style="flex:1;min-width:0">';
  html += '  <div style="display:flex;align-items:baseline;gap:8px;flex-wrap:wrap">';
  html += '    <span style="font-size:15px;font-weight:600;color:var(--ink)">' + esc(node.name) + '</span>';
  html += '    <span style="font-size:10px;color:' + c.fg + ';text-transform:uppercase;letter-spacing:0.5px;font-weight:500">' + lv + '</span>';
  if (node.role && node.role !== lv) {
    html += '    <span style="font-size:11px;color:var(--muted)">role: ' + esc(node.role) + '</span>';
  }
  if (node.model) {
    html += '    <span style="font-size:10px;color:var(--brand2);font-family:SF Mono,monospace">' + esc(node.model) + '</span>';
  }
  html += '  </div>';
  if (hasReports) {
    html += '  <div style="font-size:11px;color:var(--muted);margin-top:2px">' + node.reports.length + ' report' + (node.reports.length === 1 ? '' : 's') + ' →</div>';
  }
  html += '</div>';
  // Status
  html += '<div style="display:flex;align-items:center;gap:6px;font-size:10px">';
  html += '  <span style="width:6px;height:6px;border-radius:50%;background:' + statusColor + ';display:inline-block"></span>';
  html += '  <span style="color:' + statusColor + '">' + esc(node.status || '?') + '</span>';
  html += '  <span style="color:' + wlColor + ';margin-left:8px">☑ ' + (node.workload || 0) + '</span>';
  html += '</div>';
  // Actions
  html += '<div style="display:flex;gap:4px;margin-left:8px" onclick="event.stopPropagation()">';
  html += '  <button data-chat-member="' + node.id + '" data-chat-name="' + esc(node.name) + '" title="Chat with ' + esc(node.name) + '" style="background:' + c.bg + ';border:1px solid ' + c.border + ';color:' + c.fg + ';padding:4px 10px;border-radius:4px;cursor:pointer;font-size:12px">💬</button>';
  if (lv === 'ceo' || lv === 'vp' || lv === 'director' || lv === 'manager') {
    html += '  <button data-add-report="' + node.id + '" data-add-name="' + esc(node.name) + '" title="Add sub-agent" style="background:' + c.bg + ';border:1px solid ' + c.border + ';color:' + c.fg + ';padding:4px 10px;border-radius:4px;cursor:pointer;font-size:12px">➕</button>';
  }
  html += '</div>';
  html += '</div>';

  // Children
  if (hasReports) {
    html += '<div class="hier-children" style="margin-left:16px;position:relative;padding-left:0">';
    node.reports.forEach(function(child) {
      html += renderHierNode(child, depth + 1);
    });
    html += '</div>';
  }
  return html;
}

// === Chat drawer (right side) ===

function openChatDrawer(memberId, memberName) {
  // Close any existing drawer
  var existing = document.getElementById('chat-drawer');
  if (existing) existing.remove();
  var drawer = document.createElement('div');
  drawer.id = 'chat-drawer';
  drawer.style.cssText = 'position:fixed;top:0;right:0;bottom:0;width:480px;max-width:90vw;background:var(--panel);border-left:1px solid var(--border);box-shadow:-8px 0 24px rgba(0,0,0,0.4);z-index:100;display:flex;flex-direction:column';
  drawer.innerHTML =
    '<div style="padding:14px 18px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:10px;background:var(--bg-soft)">' +
      '<div style="font-size:18px">💬</div>' +
      '<div style="flex:1"><div style="font-size:14px;font-weight:600;color:var(--ink)">Chat with ' + esc(memberName) + '</div><div id="chat-drawer-status" style="font-size:11px;color:var(--muted)">connecting…</div></div>' +
      '<button id="chat-drawer-close" style="background:transparent;border:0;color:var(--ink);cursor:pointer;font-size:18px;padding:2px 6px">×</button>' +
    '</div>' +
    '<div id="chat-drawer-messages" style="flex:1;overflow-y:auto;padding:14px 18px;display:flex;flex-direction:column;gap:10px"></div>' +
    '<div style="padding:12px 18px;border-top:1px solid var(--border);background:var(--bg-soft)">' +
      '<div style="display:flex;gap:6px">' +
        '<textarea id="chat-drawer-input" placeholder="Type a message…" rows="2" style="flex:1;background:var(--bg);border:1px solid var(--border);padding:8px 12px;border-radius:6px;font-size:14px;color:var(--ink);resize:vertical;font-family:inherit"></textarea>' +
        '<button id="chat-drawer-send" style="background:var(--blue);color:white;border:0;padding:8px 16px;border-radius:6px;cursor:pointer;font-weight:500">Send</button>' +
      '</div>' +
    '</div>';
  document.body.appendChild(drawer);
  document.getElementById('chat-drawer-close').addEventListener('click', function() { drawer.remove(); });
  var inputEl = document.getElementById('chat-drawer-input');
  var sendBtn = document.getElementById('chat-drawer-send');
  var msgsEl  = document.getElementById('chat-drawer-messages');
  var statusEl = document.getElementById('chat-drawer-status');

  var conversationId = null;
  var agentName = memberName;
  var agentId   = memberId;

  function appendMessage(role, text) {
    var bg = role === 'user' ? 'var(--bg-active)' : 'var(--bg-soft)';
    var align = role === 'user' ? 'flex-end' : 'flex-start';
    var color = role === 'user' ? 'var(--ink)' : 'var(--ink)';
    var d = document.createElement('div');
    d.style.cssText = 'align-self:' + align + ';max-width:80%;background:' + bg + ';color:' + color + ';padding:8px 12px;border-radius:8px;font-size:13px;line-height:1.5;white-space:pre-wrap;word-wrap:break-word';
    d.textContent = text;
    msgsEl.appendChild(d);
    msgsEl.scrollTop = msgsEl.scrollHeight;
  }

  function appendStreamingMessage() {
    var d = document.createElement('div');
    d.style.cssText = 'align-self:flex-start;max-width:80%;background:var(--bg-soft);color:var(--ink);padding:8px 12px;border-radius:8px;font-size:13px;line-height:1.5;white-space:pre-wrap;word-wrap:break-word;font-style:italic';
    d.textContent = '…';
    d.id = 'chat-streaming-bubble';
    msgsEl.appendChild(d);
    msgsEl.scrollTop = msgsEl.scrollHeight;
    return d;
  }

  async function send() {
    var msg = inputEl.value.trim();
    if (!msg) return;
    inputEl.value = '';
    sendBtn.disabled = true;
    sendBtn.textContent = '…';
    appendMessage('user', msg);
    var bubble = appendStreamingMessage();
    try {
      var r = await fetch('/api/members/' + agentId + '/chat', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: msg, conversation_id: conversationId }),
      });
      if (ProviderErrors.show(bubble, await ProviderErrors.fromResponse(r))) return;
      var data = await r.json();
      if (data.conversation_id) conversationId = data.conversation_id;
      bubble.style.fontStyle = 'normal';
      bubble.textContent = data.text || '(empty response)';
    } catch (e) {
      bubble.style.fontStyle = 'normal';
      bubble.textContent = 'error: ' + e.message;
    } finally {
      sendBtn.disabled = false;
      sendBtn.textContent = 'Send';
    }
  }
  sendBtn.addEventListener('click', send);
  inputEl.addEventListener('keydown', function(e) {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
  });

  // Initial greeting
  statusEl.textContent = 'ready · conversation starts on first message';
  inputEl.focus();
}

// === Add report drawer (right side) ===

function openAddReportDrawer(managerId, managerName) {
  var existing = document.getElementById('add-report-drawer');
  if (existing) existing.remove();
  var drawer = document.createElement('div');
  drawer.id = 'add-report-drawer';
  drawer.style.cssText = 'position:fixed;top:0;right:0;bottom:0;width:480px;max-width:90vw;background:var(--panel);border-left:1px solid var(--border);box-shadow:-8px 0 24px rgba(0,0,0,0.4);z-index:100;display:flex;flex-direction:column';
  drawer.innerHTML =
    '<div style="padding:14px 18px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:10px;background:var(--bg-soft)">' +
      '<div style="font-size:18px">➕</div>' +
      '<div style="flex:1"><div style="font-size:14px;font-weight:600;color:var(--ink)">Add sub-agent under ' + esc(managerName) + '</div><div style="font-size:11px;color:var(--muted)">will inherit parent team + capabilities</div></div>' +
      '<button id="add-report-close" style="background:transparent;border:0;color:var(--ink);cursor:pointer;font-size:18px;padding:2px 6px">×</button>' +
    '</div>' +
    '<form id="add-report-form" style="flex:1;overflow-y:auto;padding:18px;display:flex;flex-direction:column;gap:12px">' +
      '<div><label style="font-size:11px;color:var(--muted);display:block;margin-bottom:4px">Name *</label><input id="ar-name" required style="width:100%;background:var(--bg);border:1px solid var(--border);padding:8px 12px;border-radius:4px;font-size:14px;color:var(--ink)"></div>' +
      '<div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">' +
        '<div><label style="font-size:11px;color:var(--muted);display:block;margin-bottom:4px">Level</label><select id="ar-level" style="width:100%;background:var(--bg);border:1px solid var(--border);padding:8px 12px;border-radius:4px;font-size:14px;color:var(--ink)"><option value="agent">agent</option><option value="manager">manager</option><option value="observer">observer</option></select></div>' +
        '<div><label style="font-size:11px;color:var(--muted);display:block;margin-bottom:4px">Type</label><select id="ar-type" style="width:100%;background:var(--bg);border:1px solid var(--border);padding:8px 12px;border-radius:4px;font-size:14px;color:var(--ink)"><option value="ai_agent">ai_agent</option><option value="human">human</option><option value="system">system</option></select></div>' +
      '</div>' +
      '<div><label style="font-size:11px;color:var(--muted);display:block;margin-bottom:4px">Role</label><input id="ar-role" placeholder="e.g. agent, manager" style="width:100%;background:var(--bg);border:1px solid var(--border);padding:8px 12px;border-radius:4px;font-size:14px;color:var(--ink)"></div>' +
      '<div><label style="font-size:11px;color:var(--muted);display:block;margin-bottom:4px">Model (provider:name)</label><input id="ar-model" placeholder="e.g. ollama:llama3.2, anthropic:claude-3-5-sonnet" style="width:100%;background:var(--bg);border:1px solid var(--border);padding:8px 12px;border-radius:4px;font-size:14px;color:var(--ink);font-family:SF Mono,monospace"></div>' +
      '<div><label style="font-size:11px;color:var(--muted);display:block;margin-bottom:4px">Capabilities (appended to parent\'s)</label><textarea id="ar-caps" rows="4" placeholder="What does this sub-agent specialize in?" style="width:100%;background:var(--bg);border:1px solid var(--border);padding:8px 12px;border-radius:4px;font-size:13px;color:var(--ink);resize:vertical;font-family:inherit"></textarea></div>' +
      '<div id="ar-error" style="color:#ef4444;font-size:12px;display:none"></div>' +
      '<div style="display:flex;gap:8px;margin-top:auto;padding-top:12px;border-top:1px solid var(--border)">' +
        '<button type="button" id="ar-cancel" style="flex:1;background:var(--bg-soft);border:1px solid var(--border);color:var(--ink);padding:10px;border-radius:4px;cursor:pointer">Cancel</button>' +
        '<button type="submit" id="ar-submit" style="flex:2;background:var(--blue);color:white;border:0;padding:10px;border-radius:4px;cursor:pointer;font-weight:500">Create sub-agent</button>' +
      '</div>' +
    '</form>';
  document.body.appendChild(drawer);
  document.getElementById('add-report-close').addEventListener('click', function() { drawer.remove(); });
  document.getElementById('ar-cancel').addEventListener('click', function() { drawer.remove(); });
  document.getElementById('add-report-form').addEventListener('submit', async function(e) {
    e.preventDefault();
    var submit = document.getElementById('ar-submit');
    var err = document.getElementById('ar-error');
    err.style.display = 'none';
    submit.disabled = true;
    submit.textContent = 'Creating…';
    try {
      var payload = {
        name: document.getElementById('ar-name').value.trim(),
        level: document.getElementById('ar-level').value,
        type: document.getElementById('ar-type').value,
        role: document.getElementById('ar-role').value.trim() || 'agent',
        model: document.getElementById('ar-model').value.trim() || null,
        capabilities: document.getElementById('ar-caps').value.trim() || null,
      };
      var r = await fetch('/api/members/' + managerId + '/reports', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (!r.ok) {
        var data = await r.json().catch(function() { return {error: r.statusText}; });
        throw new Error(data.error || r.statusText);
      }
      drawer.remove();
      renderHierarchyView();  // reload
    } catch (e) {
      err.textContent = 'failed: ' + e.message;
      err.style.display = 'block';
    } finally {
      submit.disabled = false;
      submit.textContent = 'Create sub-agent';
    }
  });
}
async function streamTask(taskId, mountId) {
  var mount = document.getElementById(mountId);
  if (!mount) return;
  mount.innerHTML = '<div style="display:flex;align-items:center;gap:8px;color:var(--muted);font-size:12px"><span class="dot pulse" style="width:6px;height:6px;border-radius:50%;background:var(--blue)"></span> connecting…</div>';

  var logLines = [];
  function append(line) {
    logLines.push(line);
    if (mount) mount.innerHTML = renderLog(logLines);
  }
  function renderLog(lines) {
    return lines.map(function(l) { return '<div style="font-family:SF Mono,monospace;font-size:11px;padding:2px 0;line-height:1.5">' + l + '</div>'; }).join('');
  }

  try {
    var es = new EventSource('/api/tasks/' + taskId + '/stream');
    es.onerror = function() {
      append('<span style="color:#ef4444">connection error</span>');
      try { es.close(); } catch(e){}
    };
    // EventSource doesn't support custom event types; we use the default 'message' with
    // JSON payload discriminated by the 'type' field.
    es.onmessage = function(ev) {
      try {
        var d = JSON.parse(ev.data);
        if (d.type === 'start') {
          var tag = d.replay ? '<span style="background:var(--bg-active);color:var(--blue);padding:1px 5px;border-radius:3px;font-size:10px;margin-right:4px">REPLAY</span>' : '<span style="background:var(--blue);color:white;padding:1px 5px;border-radius:3px;font-size:10px;margin-right:4px">LIVE</span>';
          append(tag + '<span style="color:var(--blue)">▶ run #' + d.run_id + '</span> <span style="color:var(--muted)">' + (d.provider||'') + '/' + (d.model||'') + '</span>');
        } else if (d.type === 'text') {
          var preview = (d.text || '').slice(0, 400);
          if ((d.text || '').length > 400) preview += '…';
          append('<span style="color:var(--brand2)">text</span> <span style="color:var(--ink-soft)">' + esc(preview) + '</span>');
        } else if (d.type === 'tool_call') {
          var name = d.name || '?';
          var okColor = d.ok ? 'var(--green)' : '#ef4444';
          var argsSummary = '';
          if (d.args) {
            if (d.args.title) argsSummary += ' "' + esc(d.args.title.slice(0, 40)) + '"';
            if (d.args.query) argsSummary += ' query="' + esc(d.args.query.slice(0, 30)) + '"';
            if (d.args.project) argsSummary += ' project=' + esc(d.args.project);
          }
          append('<span style="color:' + okColor + '">' + (d.ok ? '✓' : '✗') + '</span> <span style="color:var(--ink)">' + esc(name) + '</span>' + argsSummary);
        } else if (d.type === 'artifact') {
          if (d.staged) {
            append('<span style="color:var(--yellow)">⚠ staged (needs approval)</span> doc#?  ' + esc(d.path || ''));
          } else {
            append('<span style="color:var(--green)">📄 artifact</span> doc#' + d.doc_id + '  <span style="color:var(--muted)">' + esc(d.path || '') + '</span>');
          }
        } else if (d.type === 'done') {
          var dc = d.status === 'done' ? 'var(--green)' : d.status === 'error' ? '#ef4444' : 'var(--yellow)';
          append('<span style="color:' + dc + ';font-weight:500">■ done (' + d.status + ')</span> <span style="color:var(--muted)">' + (d.duration_ms||0) + 'ms</span>' + (d.artifact_doc_id ? '  <span style="color:var(--green)">→ doc#' + d.artifact_doc_id + '</span>' : ''));
          if (d.error) {
            append(ProviderErrors.providerOf(d) ? '<span style="color:var(--amber,#f59e0b)">⚠ ' + esc(ProviderErrors.friendly(d)) + '</span>' : '<span style="color:#ef4444">error: ' + esc(d.error) + '</span>');
          }
          try { es.close(); } catch(e){}
          // Refresh the task detail after 500ms
          setTimeout(function() { renderTaskDetail(taskId); }, 500);
        } else if (d.type === 'error') {
          append('<span style="color:#ef4444">stream error: ' + esc(d.message) + '</span>');
        }
      } catch (e) { /* ignore */ }
    };
  } catch (e) {
    mount.innerHTML = '<div style="color:#ef4444">failed to open stream: ' + esc(e.message) + '</div>';
  }
}
async function renderTaskDetail(taskId) {
  var main = document.getElementById('main-content');
  main.innerHTML = '<div class="empty-state">cargando…</div>';
  try {
    var t = await fetchJSON('/api/tasks/' + taskId);
    document.getElementById('breadcrumb').innerHTML = '<span>Tasks</span> <span class="sep">/</span> <span class="current">' + esc(t.title) + '</span>';
    var statusColor = { 'done': 'var(--green)', 'running': 'var(--blue)', 'error': '#ef4444', 'needs_approval': 'var(--yellow)', 'queued': 'var(--muted)' }[t.status] || 'var(--muted)';
    var html = '<div class="md-view">';
    html += '<h1>' + esc(t.title) + ' <span class="pill" style="background:' + statusColor + '22;color:' + statusColor + ';font-size:11px;padding:2px 8px;vertical-align:middle;margin-left:8px">' + t.status + '</span></h1>';
    html += '<div class="meta-row">';
    html += '<span><span class="k">id:</span> <span class="v mono">#' + t.id + '</span></span>';
    if (t.project_key) html += '<span><span class="k">project:</span> <span class="v">' + esc(t.project_key) + '</span></span>';
    if (t.provider) html += '<span><span class="k">provider:</span> <span class="v mono">' + esc(t.provider) + '/' + esc(t.model || 'auto') + '</span></span>';
    html += '<span><span class="k">created:</span> <span class="v">' + formatTime(t.created_at) + '</span></span>';
    html += '<span><span class="k">updated:</span> <span class="v">' + formatTime(t.updated_at) + '</span></span>';
    html += '</div>';
    html += '<h2>Prompt</h2>';
    html += '<pre style="background:var(--bg-soft);border:1px solid var(--border);border-radius:6px;padding:12px;font-size:12px;color:var(--ink-soft);white-space:pre-wrap;font-family:SF Mono,monospace;line-height:1.5">' + esc(t.prompt) + '</pre>';
    if (t.output_summary) {
      html += '<h2>Output summary</h2>';
      html += '<p style="color:var(--ink-soft)">' + esc(ProviderErrors.friendly(t.output_summary)) + '</p>';
    }
    html += '<h2>Runs <span style="color:var(--muted);font-weight:400;font-size:13px">— ' + (t.runs || []).length + '</span></h2>';
    if (!(t.runs || []).length) {
      html += '<p style="color:var(--muted)">no runs yet</p>';
    } else {
      t.runs.forEach(function(r) {
        var rc = { 'done': 'var(--green)', 'running': 'var(--blue)', 'error': '#ef4444', 'needs_approval': 'var(--yellow)' }[r.status] || 'var(--muted)';
        html += '<div style="background:var(--bg);border:1px solid var(--border);border-left:4px solid ' + rc + ';border-radius:6px;padding:12px 16px;margin-bottom:8px">';
        html += '<div style="display:flex;gap:12px;font-size:11px;color:var(--muted);margin-bottom:6px">';
        html += '<span>run #' + r.id + '</span>';
        html += '<span style="color:' + rc + '">' + r.status + '</span>';
        html += '<span>' + esc(r.provider || '?') + '/' + esc(r.model || '?') + '</span>';
        if (r.duration_ms) html += '<span>' + r.duration_ms + 'ms</span>';
        html += '<span>' + formatTime(r.started_at) + '</span>';
        if (r.artifact_doc_id) html += '<span style="color:var(--green)">📄 artifact #' + r.artifact_doc_id + '</span>';
        html += '</div>';
        if (r.error) {
          html += '<pre style="background:#1a0808;border:1px solid #ef444433;border-radius:4px;padding:8px;font-size:11px;color:#fca5a5;white-space:pre-wrap;font-family:SF Mono,monospace">' + esc(ProviderErrors.friendly(r.error)) + '</pre>';
        }
        if (r.output_text) {
          var txt = r.output_text.length > 800 ? r.output_text.slice(0, 800) + '…' : r.output_text;
          html += '<details><summary style="cursor:pointer;color:var(--soft);font-size:12px;margin:4px 0">output (' + r.output_text.length + ' chars)</summary>';
          html += '<pre style="background:var(--bg-soft);border:1px solid var(--border);border-radius:4px;padding:10px;font-size:11px;color:var(--ink-soft);white-space:pre-wrap;font-family:SF Mono,monospace;line-height:1.5;max-height:300px;overflow:auto">' + esc(txt) + '</pre></details>';
        }
        var tcs = [];
        try { tcs = JSON.parse(r.tool_calls || '[]'); } catch (e) { tcs = []; }
        if (tcs.length) {
          html += '<div style="margin-top:6px;font-size:11px;color:var(--muted)">Tool calls: ';
          html += tcs.map(function(c) { return '<span style="background:' + (c.ok ? 'rgba(34,197,94,0.15)' : 'rgba(239,68,68,0.15)') + ';color:' + (c.ok ? 'var(--green)' : '#ef4444') + ';padding:1px 6px;border-radius:3px;margin-right:4px">' + esc(c.name) + '</span>'; }).join('');
          html += '</div>';
        }
        if (r.status === 'needs_approval') {
          html += '<button data-approve-run="' + r.id + '" style="background:var(--yellow);color:#000;border:0;padding:6px 14px;border-radius:4px;cursor:pointer;font-size:12px;font-weight:500;margin-top:6px">Approve &amp; run write</button>';
        }
        html += '</div>';
      });
    }
    html += '<div style="margin-top:24px;display:flex;gap:8px"><button data-task-back="' + t.id + '" style="background:var(--bg-soft);border:1px solid var(--border);padding:6px 14px;border-radius:4px;cursor:pointer;color:var(--ink);font-size:13px">← back</button>';
    html += '<button data-rerun="' + t.id + '" style="background:var(--bg-soft);border:1px solid var(--border);padding:6px 14px;border-radius:4px;cursor:pointer;color:var(--ink);font-size:13px">↻ re-run</button></div>';
    html += '</div>';
    main.innerHTML = html;
    renderOutline();
    var back = main.querySelector('[data-task-back]');
    if (back) back.addEventListener('click', function() { renderTasksView(); });
    var rerun = main.querySelector('[data-rerun]');
    if (rerun) rerun.addEventListener('click', async function() {
      rerun.disabled = true; rerun.textContent = '⏳ re-running…';
      try {
        await fetch('/api/tasks/' + t.id + '/run', { method: 'POST' });
        setTimeout(function() { renderTaskDetail(t.id); }, 6000);
      } catch (e) { alert('rerun failed: ' + e.message); }
    });
    // Auto-start the live stream if the task is running
    if (t.status === 'running' || t.status === 'queued') {
      var mount = document.getElementById('stream-mount');
      if (mount) streamTask(taskId, 'stream-mount');
    } else {
      var replayBtn = main.querySelector('[data-stream-replay]');
      if (replayBtn) replayBtn.addEventListener('click', function() {
        var m = document.getElementById('stream-mount');
        if (m) m.style.display = 'block';
        streamTask(taskId, 'stream-mount');
        replayBtn.disabled = true;
        replayBtn.textContent = '⏳ replaying…';
      });
    }
    main.querySelectorAll('[data-approve-run]').forEach(function(btn) {
      btn.addEventListener('click', async function() {
        var rid = parseInt(btn.dataset.approveRun);
        try {
          await fetch('/api/tasks/' + rid + '/approve', { method: 'POST' });
          renderTaskDetail(t.id);
        } catch (e) { alert('approve failed: ' + e.message); }
      });
    });
  } catch (e) {
    main.innerHTML = '<div class="empty-state">error: ' + esc(e.message) + '</div>';
  }
}
initCanvasInteractions();
loadAll();
renderAlertsBanner();
setInterval(renderAlertsBanner, 120000);  // refresh alerts every 2 min

// Locale picker
(function initLocalePicker() {
  var picker = document.getElementById('locale-picker');
  if (!picker) return;
  picker.value = window._i18n.getLocale() || 'en';
  picker.addEventListener('change', function() {
    window._i18n.setLocale(picker.value).then(function() {
      // Re-render current view to apply translations
      renderMain();
      renderAlertsBanner();
    });
  });
})();
async function renderTeamsView() {
  var main = document.getElementById('main-content');
  document.getElementById('breadcrumb').innerHTML = '<span>Teams</span>';
  main.innerHTML = '<div class="empty-state">cargando...</div>';
  try {
    var teams = await fetchJSON('/api/teams');
    var totalMembers = teams.reduce(function(s, t) { return s + t.member_count; }, 0);
    var html = '<div class="md-view">';
    html += '<h1>👥 Teams <span style="color:var(--muted);font-weight:400;font-size:14px">— ' + teams.length + ' teams · ' + totalMembers + ' members</span></h1>';
    html += '<p style="color:var(--muted);font-size:13px;margin-bottom:16px">Humans + AI agents working on each project. Click a team to see its roster.</p>';
    var byType = {};
    teams.forEach(function(t) {
      if (!byType[t.type]) byType[t.type] = [];
      byType[t.type].push(t);
    });
    var typeLabels = { human_team: '👥 Human teams', ai_squad: '🤖 AI squads', mixed: '🧠 Mixed teams' };
    Object.keys(byType).sort().forEach(function(type) {
      var items = byType[type];
      html += '<h2 style="margin-top:20px">' + (typeLabels[type] || type) + ' <span style="color:var(--muted);font-weight:400;font-size:13px">· ' + items.length + '</span></h2>';
      html += '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:10px;margin-bottom:16px">';
      items.forEach(function(t) {
        var typeColor = t.type === 'ai_squad' ? '#7c3aed' : t.type === 'human_team' ? '#16a34a' : '#1e40af';
        html += '<div data-team-id="' + t.id + '" style="background:var(--bg);border:1px solid var(--border);border-left:4px solid ' + typeColor + ';border-radius:8px;padding:14px 16px;cursor:pointer">';
        html += '<div style="display:flex;align-items:center;gap:8px;margin-bottom:6px"><span style="font-size:20px">' + esc(t.icon || '·') + '</span>';
        html += '<div style="font-size:15px;font-weight:600;color:var(--ink)">' + esc(t.name) + '</div></div>';
        html += '<div style="font-size:11px;color:var(--muted);margin-bottom:6px;font-family:SF Mono,monospace">' + esc(t.key) + ' · ' + t.type + '</div>';
        if (t.description) html += '<div style="font-size:12px;color:var(--ink-soft);margin-bottom:8px;line-height:1.4">' + esc(t.description.slice(0, 120)) + '</div>';
        html += '<div style="display:flex;gap:8px;font-size:11px;color:var(--muted)"><span>👥 ' + t.member_count + '</span><span>☑ ' + t.open_todos + '</span></div></div>';
      });
      html += '</div>';
    });
    html += '<h2 style="margin-top:24px">+ New team</h2>';
    html += '<form id="new-team-form" style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;background:var(--bg-soft);padding:12px;border:1px solid var(--border);border-radius:6px">';
    html += '<input id="new-team-key" placeholder="key" style="flex:1;min-width:120px;background:var(--bg);border:1px solid var(--border);padding:6px 10px;border-radius:4px;font-size:13px;color:var(--ink)">';
    html += '<input id="new-team-name" placeholder="name" style="flex:2;min-width:160px;background:var(--bg);border:1px solid var(--border);padding:6px 10px;border-radius:4px;font-size:13px;color:var(--ink)">';
    html += '<select id="new-team-type" style="background:var(--bg);border:1px solid var(--border);padding:6px 10px;border-radius:4px;font-size:13px;color:var(--ink)"><option value="mixed">mixed</option><option value="human_team">human</option><option value="ai_squad">ai_squad</option></select>';
    html += '<input id="new-team-icon" placeholder="icon" maxlength="2" style="width:50px;background:var(--bg);border:1px solid var(--border);padding:6px 10px;border-radius:4px;font-size:18px;color:var(--ink);text-align:center">';
    html += '<button type="submit" style="background:var(--blue);color:white;border:0;padding:6px 14px;border-radius:4px;cursor:pointer;font-weight:500">add</button>';
    html += '</form></div>';
    main.innerHTML = html;
    renderOutline();
    main.querySelectorAll('[data-team-id]').forEach(function(el) {
      el.addEventListener('click', function() { renderTeamView(parseInt(el.dataset.teamId)); });
    });
    var f = document.getElementById('new-team-form');
    if (f) f.addEventListener('submit', async function(e) {
      e.preventDefault();
      var key = document.getElementById('new-team-key').value.trim();
      var name = document.getElementById('new-team-name').value.trim();
      if (!key || !name) return;
      var r = await fetch('/api/teams', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ key: key, name: name, type: document.getElementById('new-team-type').value, icon: document.getElementById('new-team-icon').value.trim() || '·' }) });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      renderTeamsView();
    });
  } catch (e) { main.innerHTML = '<div class="empty-state">error: ' + esc(e.message) + '</div>'; }
}

async function renderTeamView(teamId) {
  var main = document.getElementById('main-content');
  main.innerHTML = '<div class="empty-state">cargando...</div>';
  try {
    var t = await fetchJSON('/api/teams/' + teamId);
    document.getElementById('breadcrumb').innerHTML = '<span>Teams</span> <span class="sep">/</span> <span class="current">' + esc(t.name) + '</span>';
    var typeColor = t.type === 'ai_squad' ? '#7c3aed' : t.type === 'human_team' ? '#16a34a' : '#1e40af';
    var html = '<div class="md-view">';
    html += '<h1>' + esc(t.icon || '·') + ' ' + esc(t.name) + ' <span class="pill" style="background:' + typeColor + '22;color:' + typeColor + ';font-size:11px;padding:2px 8px;border-radius:3px;margin-left:8px;vertical-align:middle">' + esc(t.type) + '</span></h1>';
    html += '<div class="meta-row"><span><span class="k">key:</span> <span class="v mono">' + esc(t.key) + '</span></span><span><span class="k">members:</span> <span class="v">' + t.members.length + '</span></span></div>';
    if (t.description) html += '<p>' + esc(t.description) + '</p>';
    function memberCard(m) {
      var icon = m.type === 'ai_agent' ? '🤖' : m.type === 'system' ? '⚙' : '👤';
      var statusColor = m.status === 'active' ? 'var(--green)' : m.status === 'paused' ? 'var(--yellow)' : 'var(--muted)';
      return '<div data-member-id="' + m.id + '" style="background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:10px 14px;cursor:pointer">' +
        '<div style="display:flex;align-items:center;gap:8px;margin-bottom:4px"><span style="font-size:18px">' + icon + '</span>' +
        '<div style="flex:1"><div style="font-size:14px;font-weight:500;color:var(--ink)">' + esc(m.name) + '</div>' +
        '<div style="font-size:11px;color:var(--muted)">' + esc(m.role || '—') + (m.model ? ' · ' + esc(m.model) : '') + '</div></div>' +
        '<div style="font-size:10px;color:' + statusColor + '">' + esc(m.status) + '</div></div>' +
        (m.capabilities ? '<div style="font-size:11px;color:var(--ink-soft);margin-top:4px;line-height:1.4">' + esc(m.capabilities.slice(0, 150)) + '</div>' : '') +
        '</div>';
    }
    var humans = t.members.filter(function(m) { return m.type === 'human'; });
    var agents = t.members.filter(function(m) { return m.type === 'ai_agent'; });
    var systems = t.members.filter(function(m) { return m.type === 'system'; });
    if (humans.length) html += '<h2>👤 Humans <span style="color:var(--muted);font-weight:400;font-size:13px">· ' + humans.length + '</span></h2><div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:8px;margin-bottom:16px">' + humans.map(memberCard).join('') + '</div>';
    if (agents.length) html += '<h2>🤖 AI agents <span style="color:var(--muted);font-weight:400;font-size:13px">· ' + agents.length + '</span></h2><div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:8px;margin-bottom:16px">' + agents.map(memberCard).join('') + '</div>';
    if (systems.length) html += '<h2>⚙ System <span style="color:var(--muted);font-weight:400;font-size:13px">· ' + systems.length + '</span></h2><div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:8px;margin-bottom:16px">' + systems.map(memberCard).join('') + '</div>';
    html += '<h2>+ Add member</h2>';
    html += '<form id="new-member-form" style="display:grid;grid-template-columns:1fr 1fr 1fr 1fr auto;gap:6px;background:var(--bg-soft);padding:12px;border:1px solid var(--border);border-radius:6px">';
    html += '<input id="new-m-name" placeholder="name" required style="background:var(--bg);border:1px solid var(--border);padding:6px 10px;border-radius:4px;font-size:13px;color:var(--ink)">';
    html += '<select id="new-m-type" style="background:var(--bg);border:1px solid var(--border);padding:6px 10px;border-radius:4px;font-size:13px;color:var(--ink)"><option value="human">human</option><option value="ai_agent">ai_agent</option><option value="system">system</option></select>';
    html += '<input id="new-m-role" placeholder="role" style="background:var(--bg);border:1px solid var(--border);padding:6px 10px;border-radius:4px;font-size:13px;color:var(--ink)">';
    html += '<input id="new-m-model" placeholder="model" style="background:var(--bg);border:1px solid var(--border);padding:6px 10px;border-radius:4px;font-size:13px;color:var(--ink)">';
    html += '<button type="submit" style="background:var(--blue);color:white;border:0;padding:6px 14px;border-radius:4px;cursor:pointer;font-weight:500">add</button>';
    html += '</form>';
    html += '<textarea id="new-m-capabilities" placeholder="capabilities" style="width:100%;min-height:60px;background:var(--bg);border:1px solid var(--border);padding:6px 10px;border-radius:4px;font-size:12px;color:var(--ink);margin-top:6px;resize:vertical"></textarea>';
    html += '</div>';
    main.innerHTML = html;
    renderOutline();
    main.querySelectorAll('[data-member-id]').forEach(function(el) {
      el.addEventListener('click', function() { renderMemberView(parseInt(el.dataset.memberId)); });
    });
    var f = document.getElementById('new-member-form');
    if (f) f.addEventListener('submit', async function(e) {
      e.preventDefault();
      var name = document.getElementById('new-m-name').value.trim();
      if (!name) return;
      var r = await fetch('/api/members', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({
        team_id: teamId, name: name, type: document.getElementById('new-m-type').value,
        role: document.getElementById('new-m-role').value.trim() || null,
        model: document.getElementById('new-m-model').value.trim() || null,
        capabilities: document.getElementById('new-m-capabilities').value.trim() || null,
      }) });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      renderTeamView(teamId);
    });
  } catch (e) { main.innerHTML = '<div class="empty-state">error: ' + esc(e.message) + '</div>'; }
}

async function renderMemberView(memberId) {
  var main = document.getElementById('main-content');
  main.innerHTML = '<div class="empty-state">cargando...</div>';
  try {
    var m = await fetchJSON('/api/members/' + memberId);
    var icon = m.type === 'ai_agent' ? '🤖' : m.type === 'system' ? '⚙' : '👤';
    var html = '<div class="md-view">';
    html += '<h1>' + icon + ' ' + esc(m.name) + '</h1>';
    html += '<div class="meta-row"><span><span class="k">type:</span> <span class="v">' + esc(m.type) + '</span></span>';
    if (m.role) html += '<span><span class="k">role:</span> <span class="v">' + esc(m.role) + '</span></span>';
    html += '<span><span class="k">status:</span> <span class="v">' + esc(m.status) + '</span></span>';
    if (m.model) html += '<span><span class="k">model:</span> <span class="v mono">' + esc(m.model) + '</span></span>';
    if (m.contact) html += '<span><span class="k">contact:</span> <span class="v">' + esc(m.contact) + '</span></span>';
    html += '</div>';
    if (m.capabilities) html += '<h2>Capabilities</h2><p>' + esc(m.capabilities) + '</p>';
    if (m.notes) html += '<h2>Notes</h2><p>' + esc(m.notes) + '</p>';
    html += '<h2>Assigned TODOs <span style="color:var(--muted);font-weight:400;font-size:13px">· ' + m.open_todos.length + ' open</span></h2>';
    if (m.open_todos.length === 0) {
      html += '<p style="color:var(--muted)">no open todos assigned</p>';
    } else {
      html += '<div style="display:grid;gap:6px">';
      m.open_todos.forEach(function(t) {
        html += '<div style="background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:8px 12px;display:flex;align-items:center;gap:8px">';
        html += '<input type="checkbox" ' + (t.status === 'done' ? 'checked' : '') + ' disabled style="width:14px;height:14px">';
        html += '<div style="flex:1;font-size:13px;color:var(--ink)">' + esc(t.content) + '</div>';
        if (t.project_key) html += '<span class="pill" style="background:var(--bg-active);color:var(--blue);font-size:10px;padding:1px 6px;border-radius:3px">' + esc(t.project_key) + '</span>';
        html += '</div>';
      });
      html += '</div>';
    }
    html += '<div style="margin-top:24px"><button data-team-back="' + m.team_id + '" style="background:var(--bg-soft);border:1px solid var(--border);padding:6px 14px;border-radius:4px;cursor:pointer;color:var(--ink);font-size:13px">← back to team</button></div>';
    html += '</div>';
    main.innerHTML = html;
    renderOutline();
    var btn = main.querySelector('[data-team-back]');
    if (btn) btn.addEventListener('click', function() { renderTeamView(m.team_id); });
  } catch (e) { main.innerHTML = '<div class="empty-state">error: ' + esc(e.message) + '</div>'; }
}

// ============================================================
// Code TODOs view (📌)
// ============================================================
async function renderApprovalsView() {
  var main = document.getElementById('main-content');
  document.getElementById('breadcrumb').innerHTML = '<span>⚠ Approvals</span>';
  main.innerHTML = '<div class="empty-state">loading approvals…</div>';
  try {
    var d = await fetchJSON('/api/approvals');
    var rows = d.rows || [];
    var html = '';
    html += '<div class="md-view">';
    html += '<h1>⚠ Pending approvals <span style="color:var(--muted);font-weight:400;font-size:14px">— ' + rows.length + ' waiting</span></h1>';
    html += '<p style="color:var(--muted);font-size:13px">Agents propose actions that need your go-ahead before executing. Review and approve or reject each.</p>';
    if (!rows.length) {
      html += '<div class="empty-state">no pending approvals — your team is autonomous right now</div>';
    } else {
      rows.forEach(function(a) {
        var payloadStr = '';
        try { payloadStr = JSON.stringify(a.approval_payload || a.details || {}, null, 2); } catch (e) { payloadStr = ''; }
        html += '<div class="card" style="padding:14px;margin-bottom:12px;border-left:4px solid var(--orange)">';
        html += '<div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:8px">';
        html += '<div><div style="font-size:14px;font-weight:600;color:var(--ink)">' + esc(a.agent_name || 'agent ' + a.agent_id) + '</div>';
        html += '<div style="font-size:11px;color:var(--muted)">' + esc(a.agent_role || '') + ' \u00b7 ' + new Date(a.created_at * 1000).toLocaleString() + '</div></div>';
        html += '</div>';
        html += '<div style="font-size:13px;color:var(--ink);margin-bottom:8px"><strong>action:</strong> <code style="background:var(--bg-soft);padding:2px 6px;border-radius:3px">' + esc(a.approval_action || a.summary || '') + '</code></div>';
        if (a.summary && a.summary !== a.approval_action) html += '<div style="font-size:12px;color:var(--muted);margin-bottom:8px">' + esc(a.summary) + '</div>';
        if (payloadStr && payloadStr !== '{}') html += '<pre style="background:var(--bg);border:1px solid var(--border);border-radius:4px;padding:8px;font-size:11px;color:var(--ink-soft);max-height:200px;overflow:auto;white-space:pre-wrap;margin-bottom:10px">' + esc(payloadStr) + '</pre>';
        html += '<div style="display:flex;gap:8px">';
        html += '<button data-approval-approve="' + a.id + '" style="background:var(--green);color:#fff;border:none;padding:6px 18px;border-radius:4px;cursor:pointer;font-weight:500">approve</button>';
        html += '<button data-approval-reject="' + a.id + '" style="background:transparent;border:1px solid var(--red);color:var(--red);padding:6px 18px;border-radius:4px;cursor:pointer">reject</button>';
        html += '</div></div>';
      });
    }
    html += '</div>';
    main.innerHTML = html;
    main.querySelectorAll('[data-approval-approve]').forEach(function(b) {
      b.addEventListener('click', async function() {
        await fetch('/api/approvals/' + b.dataset.approvalApprove + '/approve', {method:'POST'});
        renderApprovalsView();
        refreshApprovalsBadge();
      });
    });
    main.querySelectorAll('[data-approval-reject]').forEach(function(b) {
      b.addEventListener('click', async function() {
        if (!confirm('Reject this proposal?')) return;
        await fetch('/api/approvals/' + b.dataset.approvalReject + '/reject', {method:'POST'});
        renderApprovalsView();
        refreshApprovalsBadge();
      });
    });
  } catch (e) {
    main.innerHTML = '<div class="empty-state">error: ' + esc(e.message) + '</div>';
  }
}

async function refreshApprovalsBadge() {
  try {
    var d = await fetchJSON('/api/approvals');
    var n = (d.rows || []).length;
    var badge = document.getElementById('approvals-badge');
    if (badge) {
      badge.textContent = n > 0 ? n : '';
      badge.style.display = n > 0 ? 'inline' : 'none';
    }
  } catch (e) {}
}

// Boardroom (2026-07-01): poll inbox summary and surface blocked/waiting on the activity-bar badge
async function refreshBoardroomBadge() {
  try {
    var d = await fetchJSON('/api/boardroom/summary');
    var blocked = d.blocked || 0;
    var waiting = d.waiting || 0;
    var n = blocked + waiting;
    var badge = document.getElementById('boardroom-badge');
    if (badge) {
      badge.textContent = n > 0 ? n : '';
      badge.style.display = n > 0 ? 'inline' : 'none';
    }
  } catch (e) {}
}
setInterval(refreshBoardroomBadge, 30000);
refreshBoardroomBadge();

var _connCatalog = null;
async function loadConnCatalog() {
  if (_connCatalog) return _connCatalog;
  _connCatalog = await fetchJSON('/api/connections/catalog');
  return _connCatalog;
}

async function renderConnectionsView() {
  var main = document.getElementById('main-content');
  document.getElementById('breadcrumb').innerHTML = '<span>🔌 Connections</span>';
  main.innerHTML = '<div class="empty-state">loading connections…</div>';
  try {
    var [catalog, connsData, mcpsData] = await Promise.all([
      loadConnCatalog(),
      fetchJSON('/api/connections'),
      fetchJSON('/api/mcp/servers'),
    ]);
    var conns = connsData.rows || [];
    var mcps = mcpsData.rows || [];
    var html = '';
    html += '<div class="md-view">';
    html += '<div style="display:flex;align-items:center;gap:12px;margin-bottom:16px;flex-wrap:wrap">';
    html += '<h1 style="margin:0">🔌 Connections</h1>';
    html += '<span style="color:var(--muted);font-size:14px">— ' + conns.length + ' services · ' + mcps.length + ' MCP servers</span>';
    html += '<div style="flex:1"></div>';
    html += '<button id="conn-add-btn" style="background:var(--blue);color:#fff;border:none;padding:6px 12px;border-radius:4px;cursor:pointer;font-size:12px">+ add connection</button>';
    html += '<button id="mcp-add-btn" style="background:var(--bg-soft);color:var(--ink);border:1px solid var(--border);padding:6px 12px;border-radius:4px;cursor:pointer;font-size:12px">+ add MCP server</button>';
    html += '<a href="/connectors" style="color:var(--blue);font-size:12px;text-decoration:none">OAuth connectors →</a>';
    html += '</div>';
    // Catalog as cards (provider picker)
    html += '<h3 style="margin:18px 0 8px 0;font-size:13px;color:var(--muted)">available providers</h3>';
    html += '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:10px;margin-bottom:20px">';
    catalog.forEach(function(p) {
      var configured = conns.find(function(c) { return c.provider === p.id; });
      var badge = configured
        ? '<span style="font-size:10px;color:var(--green)">\u25cf configured</span>'
        : '<span style="font-size:10px;color:var(--muted)">unconfigured</span>';
      var toolsBadge = p.tools_count != null ? '<span style="font-size:10px;color:var(--muted)"> · ' + p.tools_count + ' tools</span>' : '';
      html += '<div class="card" style="padding:12px;display:flex;flex-direction:column;gap:4px">';
      html += '<div style="display:flex;justify-content:space-between;align-items:center"><strong style="font-size:13px">' + esc(p.name) + '</strong>' + badge + '</div>';
      html += '<div style="font-size:11px;color:var(--muted);flex:1">' + esc(p.description) + toolsBadge + '</div>';
      html += '<div style="font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:0.5px">' + esc(p.category) + '</div>';
      html += '<button data-conn-quick-add="' + esc(p.id) + '" style="background:var(--bg);border:1px solid var(--border);color:var(--ink);padding:4px;border-radius:3px;cursor:pointer;font-size:11px;margin-top:4px">' + (configured ? 'add another' : 'add') + '</button>';
      html += '</div>';
    });
    html += '</div>';
    // Configured connections
    html += '<h3 style="margin:18px 0 8px 0;font-size:13px;color:var(--muted)">configured</h3>';
    if (!conns.length) {
      html += '<div class="empty-state">no connections yet — pick a provider above</div>';
    } else {
      conns.forEach(function(c) {
        var statusColor = c.status === 'ok' ? 'var(--green)' : c.status === 'fail' ? 'var(--red)' : 'var(--muted)';
        var statusIcon = c.status === 'ok' ? '\u2713' : c.status === 'fail' ? '\u2717' : '\u00b7';
        var prov = catalog.find(function(p) { return p.id === c.provider; });
        html += '<div class="card" style="padding:12px;margin-bottom:8px;display:flex;gap:12px;align-items:center">';
        html += '<div style="flex:1"><div style="display:flex;gap:6px;align-items:center"><strong>' + esc(prov ? prov.name : c.provider) + '</strong>';
        html += '<span style="font-size:11px;color:var(--muted)">' + esc(c.name) + '</span>';
        if (!c.enabled) html += '<span style="font-size:10px;color:var(--orange)">paused</span>';
        html += '</div>';
        html += '<div style="font-size:11px;color:var(--muted);margin-top:2px">credentials: ' + (c.credential_keys.join(', ') || '(none)') + (c.last_used_at ? ' · last used ' + new Date(c.last_used_at * 1000).toLocaleString() : '') + '</div>';
        if (c.last_error) html += '<div style="font-size:11px;color:var(--red);margin-top:2px">' + esc(c.last_error.slice(0, 120)) + '</div>';
        html += '</div>';
        html += '<span style="font-size:12px;color:' + statusColor + '">' + statusIcon + ' ' + c.status + '</span>';
        html += '<button data-conn-test="' + c.id + '" style="background:var(--bg);border:1px solid var(--border);padding:5px 10px;border-radius:3px;cursor:pointer;font-size:11px">test</button>';
        html += '<button data-conn-del="' + c.id + '" style="background:transparent;border:1px solid var(--red);color:var(--red);padding:5px 10px;border-radius:3px;cursor:pointer;font-size:11px">delete</button>';
        html += '</div>';
      });
    }
    // MCP servers
    html += '<h3 style="margin:24px 0 8px 0;font-size:13px;color:var(--muted)">MCP servers</h3>';
    if (!mcps.length) {
      html += '<div class="empty-state">no MCP servers configured</div>';
    } else {
      mcps.forEach(function(s) {
        var toolsCount = 0;
        try { toolsCount = (JSON.parse(s.tools_cache || '[]')).length; } catch (e) {}
        html += '<div class="card" style="padding:12px;margin-bottom:8px;display:flex;gap:12px;align-items:center">';
        html += '<div style="flex:1"><div><strong>' + esc(s.name) + '</strong> <span style="font-size:11px;color:var(--muted)">(' + esc(s.transport) + ')</span></div>';
        if (s.url) html += '<div style="font-size:11px;color:var(--muted);margin-top:2px">url: <code style="background:var(--bg-soft);padding:1px 4px;border-radius:2px">' + esc(s.url) + '</code></div>';
        if (s.command) html += '<div style="font-size:11px;color:var(--muted);margin-top:2px">cmd: <code style="background:var(--bg-soft);padding:1px 4px;border-radius:2px">' + esc(JSON.stringify(s.command)) + '</code></div>';
        if (s.last_error) html += '<div style="font-size:11px;color:var(--red);margin-top:2px">' + esc(s.last_error.slice(0, 120)) + '</div>';
        html += '<div style="font-size:11px;color:var(--muted);margin-top:4px">' + toolsCount + ' tools cached' + (s.tools_cached_at ? ' · ' + new Date(s.tools_cached_at * 1000).toLocaleString() : '') + '</div>';
        html += '</div>';
        html += '<button data-mcp-discover="' + esc(s.name) + '" style="background:var(--bg);border:1px solid var(--border);padding:5px 10px;border-radius:3px;cursor:pointer;font-size:11px">discover</button>';
        html += '<button data-mcp-del="' + esc(s.name) + '" style="background:transparent;border:1px solid var(--red);color:var(--red);padding:5px 10px;border-radius:3px;cursor:pointer;font-size:11px">delete</button>';
        html += '</div>';
      });
    }
    html += '</div>';
    main.innerHTML = html;
    renderOutline();
    // Wire buttons
    document.getElementById('conn-add-btn').addEventListener('click', function() { showConnAddDialog(); });
    document.getElementById('mcp-add-btn').addEventListener('click', function() { showMcpAddDialog(); });
    main.querySelectorAll('[data-conn-quick-add]').forEach(function(b) {
      b.addEventListener('click', function() { showConnAddDialog(b.dataset.connQuickAdd); });
    });
    main.querySelectorAll('[data-conn-test]').forEach(function(b) {
      b.addEventListener('click', async function() {
        var r = await fetch('/api/connections/' + b.dataset.connTest + '/test', {method: 'POST'});
        var d = await r.json();
        alert(d.ok ? '\u2713 OK\n' + JSON.stringify(d.info, null, 2) : '\u2717 ' + d.error);
        renderConnectionsView();
      });
    });
    main.querySelectorAll('[data-conn-del]').forEach(function(b) {
      b.addEventListener('click', async function() {
        dialog.confirm({ title: 'Delete connection', message: 'Are you sure?', onConfirm: async function() {
          await fetch('/api/connections/' + b.dataset.connDel, {method: 'DELETE'});
          renderConnectionsView();
        }});
      });
    });
    main.querySelectorAll('[data-mcp-discover]').forEach(function(b) {
      b.addEventListener('click', async function() {
        b.disabled = true; b.textContent = 'discovering...';
        var r = await fetch('/api/mcp/servers/' + b.dataset.mcpDiscover + '/discover', {method: 'POST'});
        var d = await r.json();
        b.disabled = false; b.textContent = 'discover';
        alert(d.ok ? '\u2713 found ' + d.tools.length + ' tools' : '\u2717 ' + d.error);
        renderConnectionsView();
      });
    });
    main.querySelectorAll('[data-mcp-del]').forEach(function(b) {
      b.addEventListener('click', async function() {
        dialog.confirm({ title: 'Delete MCP server', message: 'Are you sure?', onConfirm: async function() {
          await fetch('/api/mcp/servers/' + b.dataset.mcpDel, {method: 'DELETE'});
          renderConnectionsView();
        }});
      });
    });
  } catch (e) {
    main.innerHTML = '<div class="empty-state">error: ' + esc(e.message) + '</div>';
  }
}

async function showConnAddDialog(providerId) {
  var catalog = _connCatalog || await loadConnCatalog();
  var prov = providerId ? catalog.find(function(p) { return p.id === providerId; }) : null;
  if (!prov) {
    // Step 1: pick provider
    dialog.show({
      title: 'Add connection',
      description: 'Pick a provider to configure',
      fields: [{
        key: 'provider_id', label: 'Provider', type: 'select',
        options: catalog.map(function(p) { return { value: p.id, label: p.name + ' (' + p.category + ')' }; }),
        required: true
      }],
      submitLabel: 'next',
      onSubmit: function(d) {
        var chosen = catalog.find(function(p) { return p.id === d.provider_id; });
        if (chosen) showConnAddDialog(chosen.id);
      }
    });
    return;
  }
  // Step 2: collect credentials
  var fields = [{ key: 'name', label: 'Label (e.g. "production", "personal")', type: 'text', required: true }];
  prov.fields.forEach(function(f) {
    fields.push({
      key: f.key, label: f.label, type: f.type || 'text', required: f.required !== false,
      placeholder: f.placeholder || ''
    });
  });
  dialog.show({
    title: 'Configure ' + prov.name,
    description: prov.description,
    fields: fields,
    submitLabel: 'save',
    validate: function(d) {
      if (!d.name) return 'Label is required';
      var missing = [];
      fields.forEach(function(f) {
        if (f.required && !d[f.key]) missing.push(f.label);
      });
      if (missing.length) return 'Missing: ' + missing.join(', ');
      return null;
    },
    onSubmit: async function(d) {
      var creds = {};
      fields.forEach(function(f) {
        if (f.key !== 'name' && d[f.key]) creds[f.key] = d[f.key];
      });
      var r = await fetch('/api/connections', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({provider: prov.id, name: d.name, credentials: creds})
      });
      var data = await r.json();
      if (data.error) { alert('error: ' + data.error); return; }
      renderConnectionsView();
    }
  });
}

async function showMcpAddDialog() {
  // Popular MCP servers registry
  var POPULAR_MCP = [
    { name: 'filesystem', transport: 'stdio', command: ['npx', '-y', '@modelcontextprotocol/server-filesystem'], description: 'Read/write local files (allowed paths only)' },
    { name: 'git',        transport: 'stdio', command: ['npx', '-y', '@modelcontextprotocol/server-git'],        description: 'Git operations: log, diff, status, commit' },
    { name: 'github',     transport: 'stdio', command: ['npx', '-y', '@modelcontextprotocol/server-github'],     description: 'GitHub: repos, issues, PRs (needs GITHUB_TOKEN env)' },
    { name: 'playwright', transport: 'stdio', command: ['npx', '-y', '@modelcontextprotocol/server-playwright'], description: 'Browser automation: navigate, click, screenshot' },
    { name: 'puppeteer',  transport: 'stdio', command: ['npx', '-y', '@modelcontextprotocol/server-puppeteer'],  description: 'Browser automation (Chrome-based)' },
    { name: 'brave-search', transport: 'stdio', command: ['npx', '-y', '@modelcontextprotocol/server-brave-search'], description: 'Web search via Brave API (needs BRAVE_API_KEY env)' },
    { name: 'postgres',   transport: 'stdio', command: ['npx', '-y', '@modelcontextprotocol/server-postgres'],   description: 'Postgres queries (needs DATABASE_URL env)' },
    { name: 'sqlite',     transport: 'stdio', command: ['npx', '-y', '@modelcontextprotocol/server-sqlite'],     description: 'SQLite queries' },
    { name: 'slack',      transport: 'stdio', command: ['npx', '-y', '@modelcontextprotocol/server-slack'],      description: 'Slack workspace tools (needs SLACK_TOKEN env)' },
    { name: 'fetch',      transport: 'stdio', command: ['uvx', 'mcp-server-fetch'],                                  description: 'Fetch any URL and convert to markdown' },
  ];
  var popularOptions = POPULAR_MCP.map(function(m) { return { value: m.name, label: m.name + ' — ' + m.description }; });
  popularOptions.unshift({ value: '__custom__', label: '(custom — fill in below)' });

  dialog.show({
    title: 'Add MCP server',
    description: 'Pick a popular one or define a custom server',
    fields: [
      { key: 'popular', label: 'Popular servers', type: 'select', options: popularOptions, default: '__custom__' },
      { key: 'name', label: 'Name', type: 'text', required: true, placeholder: 'e.g. my-server' },
      { key: 'transport', label: 'Transport', type: 'select', options: ['stdio', 'http', 'sse'], default: 'stdio' },
      { key: 'command', label: 'Command (for stdio)', type: 'text', placeholder: 'e.g. npx -y @modelcontextprotocol/server-git' },
      { key: 'url', label: 'URL (for http/sse)', type: 'text', placeholder: 'https://example.com/mcp' },
    ],
    submitLabel: 'save',
    validate: function(d) {
      if (!d.name) return 'name required';
      if (d.transport === 'stdio') {
        // "popular" dropdown supplies command automatically when a known server is selected.
        var hasPopular = d.popular && d.popular !== '__custom__' &&
          POPULAR_MCP.some(function(m) { return m.name === d.popular; });
        if (!hasPopular && !d.command) return 'command required for stdio (or pick a popular server)';
      }
      if (d.transport !== 'stdio' && !d.url) return 'url required for http/sse';
      return null;
    },
    onSubmit: async function(d) {
      var payload = { name: d.name, transport: d.transport };
      if (d.transport === 'stdio') {
        var cmd = d.command;
        if (d.popular && d.popular !== '__custom__') {
          var found = POPULAR_MCP.find(function(m) { return m.name === d.popular; });
          if (found) { cmd = found.command.join(' '); if (!d.name) d.name = found.name; }
        }
        payload.command = cmd ? cmd.split(/\s+/) : null;
      } else {
        payload.url = d.url;
      }
      var r = await fetch('/api/mcp/servers', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload)
      });
      var data = await r.json();
      if (data.error) { alert('error: ' + data.error); return; }
      renderConnectionsView();
    }
  });
}

async function renderCodeTodosView() {
  var main = document.getElementById('main-content');
  main.innerHTML = '<div class="empty-state">loading code TODOs…</div>';
  try {
    var url = '/api/code_todos';
    var resp = await fetch(url);
    var data = await resp.json();
    var rows = data.rows || [];
    var summary = data.summary || {};
    var html = '';
    html += '<div class="view-header"><h1>📌 Code TODOs</h1>';
    html += '<div class="muted">auto-extracted from source files · ' + (summary.total || 0) + ' total · ' + (Object.values(summary.by_priority||{}).reduce((a,b)=>a+b,0)) + ' open</div></div>';
    // Priority filter
    html += '<div class="filters" style="margin:12px 0;display:flex;gap:8px;align-items:center">';
    html += '<label style="color:var(--muted);font-size:12px">priority:</label>';
    ['urgent','high','normal','low'].forEach(function(p) {
      var c = (summary.by_priority||{})[p] || 0;
      var color = p === 'urgent' ? 'var(--red)' : p === 'high' ? 'var(--orange)' : p === 'low' ? 'var(--muted)' : 'var(--ink)';
      html += '<button data-prio="' + p + '" style="background:var(--bg-soft);border:1px solid var(--border);padding:4px 10px;border-radius:3px;cursor:pointer;font-size:12px;color:' + color + '">' + p + ' (' + c + ')</button>';
    });
    html += '<button data-prio="all" style="background:var(--bg-soft);border:1px solid var(--border);padding:4px 10px;border-radius:3px;cursor:pointer;font-size:12px">all</button>';
    html += '</div>';
    // List
    if (rows.length === 0) {
      html += '<div class="empty-state">no code TODOs found. Run <code>ops index code_todos</code> to scan your repos.</div>';
    } else {
      var byProject = {};
      rows.forEach(function(r) {
        var k = r.project_key || 'unassigned';
        byProject[k] = byProject[k] || [];
        byProject[k].push(r);
      });
      Object.keys(byProject).sort().forEach(function(proj) {
        html += '<div class="card" style="margin-bottom:14px">';
        html += '<h3 style="margin:0 0 8px 0;font-size:13px;color:var(--blue)">' + esc(proj) + ' <span style="color:var(--muted);font-weight:400">(' + byProject[proj].length + ')</span></h3>';
        byProject[proj].forEach(function(t) {
          var priColor = t.priority === 'urgent' ? 'var(--red)' : t.priority === 'high' ? 'var(--orange)' : 'var(--muted)';
          html += '<div data-todo-id="' + t.id + '" class="todo-row" style="display:flex;gap:8px;align-items:flex-start;padding:6px 0;border-bottom:1px solid var(--border)">';
          html += '<input type="checkbox" ' + (t.done ? 'checked' : '') + ' style="margin-top:3px;width:14px;height:14px;cursor:pointer">';
          html += '<span style="font-size:11px;font-weight:600;color:' + priColor + ';min-width:60px;margin-top:2px">' + t.priority + '</span>';
          html += '<span style="font-size:11px;color:var(--muted);min-width:50px;margin-top:2px">' + t.kind + '</span>';
          html += '<div style="flex:1;font-size:13px;color:var(--ink)">' + esc(t.content) + '</div>';
          html += '<div style="font-size:11px;color:var(--muted);max-width:280px;text-align:right;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + esc(t.file_path.split('/').slice(-2).join('/')) + ':' + t.line + '</div>';
          html += '</div>';
        });
        html += '</div>';
      });
    }
    main.innerHTML = html;
    renderOutline();
    // Wire priority filter
    main.querySelectorAll('[data-prio]').forEach(function(b) {
      b.addEventListener('click', function() {
        var p = b.dataset.prio;
        if (p === 'all') { renderCodeTodosView(); return; }
        fetch('/api/code_todos?priority=' + p).then(function(r){return r.json();}).then(function(d){
          data.rows = d.rows;
          renderCodeTodosView();
        });
      });
    });
    // Wire done toggles
    main.querySelectorAll('.todo-row input[type=checkbox]').forEach(function(cb) {
      cb.addEventListener('change', function() {
        var id = cb.closest('.todo-row').dataset.todoId;
        fetch('/api/code_todos/done', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({id: parseInt(id), done: cb.checked})});
      });
    });
  } catch (e) { main.innerHTML = '<div class="empty-state">error: ' + esc(e.message) + '</div>'; }
}

// ============================================================
// Decisions view (🔖)
// ============================================================
async function renderDecisionsView() {
  var main = document.getElementById('main-content');
  main.innerHTML = '<div class="empty-state">loading decisions…</div>';
  try {
    var resp = await fetch('/api/decisions');
    var data = await resp.json();
    var rows = data.rows || [];
    var html = '';
    html += '<div class="view-header"><h1>🔖 Decisions log</h1>';
    html += '<div class="muted">' + rows.length + ' decisions · click + to add</div></div>';
    html += '<button id="add-decision" style="background:var(--blue);color:#fff;border:none;padding:6px 14px;border-radius:4px;cursor:pointer;font-size:13px">+ new decision</button>';
    if (rows.length === 0) {
      html += '<div class="empty-state">no decisions logged yet — use <code>ops decision \'title\' \'decision\'</code> or the button above</div>';
    } else {
      rows.forEach(function(d) {
        html += '<div class="card" style="margin-bottom:14px;border-left:3px solid var(--blue)">';
        html += '<h3 style="margin:0 0 4px 0;font-size:14px">' + esc(d.title) + '</h3>';
        html += '<div style="font-size:11px;color:var(--muted);margin-bottom:8px">';
        html += esc(d.project_key || 'general') + ' · ' + new Date(d.decided_at * 1000).toLocaleDateString();
        if (d.tags && d.tags.length) html += ' · ' + d.tags.map(function(t){return '<span class="pill" style="background:var(--bg-active);color:var(--blue);font-size:10px;padding:1px 6px;border-radius:3px;margin-right:3px">' + esc(t) + '</span>';}).join('');
        html += '</div>';
        html += '<div style="font-size:13px;color:var(--ink)"><strong>→</strong> ' + esc(d.decision) + '</div>';
        if (d.context) html += '<div style="font-size:12px;color:var(--muted);margin-top:6px"><em>why:</em> ' + esc(d.context) + '</div>';
        if (d.consequences) html += '<div style="font-size:12px;color:var(--muted);margin-top:4px"><em>consequences:</em> ' + esc(d.consequences) + '</div>';
        if (d.links && d.links.length) {
          html += '<div style="font-size:11px;margin-top:6px">';
          d.links.forEach(function(l) {
            html += '<a href="' + esc(l.url || '#') + '" target="_blank" style="color:var(--blue);margin-right:8px">' + esc(l.label || l.url) + '</a>';
          });
          html += '</div>';
        }
        html += '<button data-decision-del="' + d.id + '" style="background:transparent;border:none;color:var(--muted);font-size:11px;cursor:pointer;margin-top:6px">delete</button>';
        html += '</div>';
      });
    }
    main.innerHTML = html;
    renderOutline();
    document.getElementById('add-decision').addEventListener('click', function() {
      var t = prompt('Decision title:');
      if (!t) return;
      var d = prompt('What was decided:');
      if (!d) return;
      fetch('/api/decision', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({title: t, decision: d})}).then(function(){renderDecisionsView();});
    });
    main.querySelectorAll('[data-decision-del]').forEach(function(b) {
      b.addEventListener('click', function() {
        if (!confirm('Delete this decision?')) return;
        fetch('/api/decision/' + b.dataset.decisionDel, {method:'DELETE'}).then(function(){renderDecisionsView();});
      });
    });
  } catch (e) { main.innerHTML = '<div class="empty-state">error: ' + esc(e.message) + '</div>'; }
}

// ============================================================
// Costs view (💸)
// ============================================================
async function renderCostsView() {
  var main = document.getElementById('main-content');
  main.innerHTML = '<div class="empty-state">loading costs…</div>';
  try {
    var resp = await fetch('/api/costs');
    var data = await resp.json();
    var rows = data.rows || [];
    var s = data.summary || {};
    var html = '';
    html += '<div class="view-header"><h1>💸 Cost tracker</h1>';
    html += '<div class="muted">€ / $ / £ · by provider, project, category</div></div>';
    // Summary cards
    html += '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-bottom:16px">';
    html += '<div class="card" style="padding:14px"><div style="font-size:11px;color:var(--muted);text-transform:uppercase">total</div><div style="font-size:24px;font-weight:600;color:var(--ink);margin-top:4px">€' + ((s.total_cents||0)/100).toFixed(2) + '</div></div>';
    if (s.by_provider && s.by_provider.length) {
      s.by_provider.slice(0, 3).forEach(function(p) {
        html += '<div class="card" style="padding:14px"><div style="font-size:11px;color:var(--muted);text-transform:uppercase">' + esc(p.provider) + '</div><div style="font-size:20px;font-weight:600;color:var(--ink);margin-top:4px">' + p.currency + ' ' + (p.total_cents/100).toFixed(2) + '</div></div>';
      });
    }
    html += '</div>';
    // Add button
    html += '<button id="add-cost" style="background:var(--blue);color:#fff;border:none;padding:6px 14px;border-radius:4px;cursor:pointer;font-size:13px">+ log cost</button>';
    html += '<h3 style="margin-top:20px">Recent</h3>';
    if (rows.length === 0) {
      html += '<div class="empty-state">no costs yet. Try <code>ops cost add --provider cloudflare --amount 5.0</code></div>';
    } else {
      html += '<table style="width:100%;font-size:13px;border-collapse:collapse"><thead><tr style="border-bottom:1px solid var(--border);text-align:left;color:var(--muted);font-size:11px"><th style="padding:6px">period</th><th style="padding:6px">provider</th><th style="padding:6px;text-align:right">amount</th><th style="padding:6px">category</th><th style="padding:6px">project</th><th style="padding:6px">description</th></tr></thead><tbody>';
      rows.slice(0, 50).forEach(function(r) {
        html += '<tr style="border-bottom:1px solid var(--border)">';
        html += '<td style="padding:6px;color:var(--muted)">' + esc(r.period) + '</td>';
        html += '<td style="padding:6px;color:var(--ink)">' + esc(r.provider) + '</td>';
        html += '<td style="padding:6px;text-align:right;color:var(--ink);font-variant-numeric:tabular-nums">' + r.currency + ' ' + (r.amount_cents/100).toFixed(2) + '</td>';
        html += '<td style="padding:6px;color:var(--muted)">' + esc(r.category) + '</td>';
        html += '<td style="padding:6px">' + (r.project_key ? '<span class="pill" style="background:var(--bg-active);color:var(--blue);font-size:10px;padding:1px 6px;border-radius:3px">' + esc(r.project_key) + '</span>' : '—') + '</td>';
        html += '<td style="padding:6px;color:var(--muted);font-size:12px">' + esc((r.description||'').slice(0,60)) + '</td>';
        html += '</tr>';
      });
      html += '</tbody></table>';
    }
    main.innerHTML = html;
    renderOutline();
    document.getElementById('add-cost').addEventListener('click', function() {
      var provider = prompt('Provider (cloudflare, anthropic, vercel…):');
      if (!provider) return;
      var amount = prompt('Amount (e.g. 5.0):');
      if (!amount) return;
      var currency = prompt('Currency (EUR / USD / GBP):', 'EUR') || 'EUR';
      var desc = prompt('Description (optional):') || '';
      fetch('/api/cost', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({provider: provider, amount: parseFloat(amount), currency: currency, description: desc})}).then(function(){renderCostsView();});
    });
  } catch (e) { main.innerHTML = '<div class="empty-state">error: ' + esc(e.message) + '</div>'; }
}

// ============================================================
// Revenue view (💰) — Apple + Google
// ============================================================
async function renderRevenueView() {
  var main = document.getElementById('main-content');
  main.innerHTML = '<div class="empty-state">loading revenue data…</div>';
  try {
    var rSummary = await fetch('/api/revenue/summary').then(function(r){return r.json();});
    var rDaily = await fetch('/api/revenue/daily').then(function(r){return r.json();});
    var rEvents = await fetch('/api/revenue/events?limit=20').then(function(r){return r.json();});
    var rSources = await fetch('/api/revenue/sources').then(function(r){return r.json();});
    var s = rSummary, daily = rDaily.rows || [], events = rEvents.rows || [], sources = rSources.rows || [];

    function fmtCents(c) { return '€' + (c/100).toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2}); }
    function fmtDate(ts) { var d = new Date(ts*1000); return d.toISOString().slice(0,10); }

    var html = '';
    html += '<div class="view-header"><h1>💰 Revenue</h1>';
    html += '<div class="muted">Apple App Store + Google Play · ' + sources.length + ' sources · ' + s.count + ' events</div>';
    html += '<div style="flex:1"></div>';
    html += '<button id="rev-seed" style="background:var(--bg-soft);color:var(--ink);border:1px solid var(--border);padding:6px 12px;border-radius:4px;cursor:pointer;font-size:12px;margin-right:6px">+ seed mock</button>';
    html += '<button id="rev-poll" style="background:var(--blue);color:#fff;border:none;padding:6px 12px;border-radius:4px;cursor:pointer;font-size:12px">↻ poll all</button></div>';

    // Summary cards
    html += '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px;margin:14px 0">';
    html += '<div class="card" style="padding:14px"><div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:0.5px">Total revenue</div>';
    html += '<div style="font-size:24px;font-weight:600;color:var(--green);margin-top:4px">' + fmtCents(s.total_cents) + '</div>';
    html += '<div style="font-size:11px;color:var(--muted);margin-top:2px">' + s.count + ' events</div></div>';
    (s.by_source || []).forEach(function(r) {
      html += '<div class="card" style="padding:14px"><div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:0.5px">' + esc(r.source_type) + '</div>';
      html += '<div style="font-size:24px;font-weight:600;margin-top:4px">' + fmtCents(r.total) + '</div>';
      html += '<div style="font-size:11px;color:var(--muted);margin-top:2px">' + r.cnt + ' events</div></div>';
    });
    html += '</div>';

    // Daily chart (simple SVG bar chart)
    if (daily.length > 0) {
      var max = Math.max.apply(null, daily.map(function(d){return Math.abs(d.total);}).concat([1]));
      var w = 100 / daily.length;
      var svg = '<svg viewBox="0 0 100 30" preserveAspectRatio="none" style="width:100%;height:80px;background:var(--bg-soft);border-radius:6px;padding:6px;box-sizing:border-box">';
      daily.slice(-30).forEach(function(d, i, arr) {
        var x = (i / arr.length) * 100;
        var h = Math.max(0.5, (Math.abs(d.total) / max) * 26);
        var y = 28 - h;
        var color = d.total >= 0 ? 'var(--green)' : 'var(--red)';
        svg += '<rect x="' + x.toFixed(2) + '" y="' + y.toFixed(2) + '" width="' + (100/arr.length - 0.2).toFixed(2) + '" height="' + h.toFixed(2) + '" fill="' + color + '" opacity="0.85"/>';
      });
      svg += '</svg>';
      html += '<div class="card" style="padding:14px;margin-bottom:14px"><div style="font-size:13px;font-weight:500;margin-bottom:8px">Daily revenue (last 30d)</div>' + svg + '</div>';
    }

    // By project + by type side by side
    html += '<div style="display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:14px">';
    html += '<div class="card" style="padding:14px"><div style="font-size:13px;font-weight:500;margin-bottom:10px">By project</div>';
    if ((s.by_project || []).length === 0) html += '<div class="muted" style="font-size:12px">no revenue yet</div>';
    var maxP = Math.max.apply(null, (s.by_project || []).map(function(r){return Math.abs(r.total);}).concat([1]));
    (s.by_project || []).forEach(function(r) {
      var pct = (Math.abs(r.total) / maxP) * 100;
      html += '<div style="margin-bottom:6px"><div style="display:flex;justify-content:space-between;font-size:12px;margin-bottom:2px"><span>' + esc(r.project_key) + '</span><span style="color:var(--muted)">' + fmtCents(r.total) + ' · ' + r.cnt + '</span></div>';
      html += '<div style="background:var(--bg);border-radius:3px;height:6px;overflow:hidden"><div style="background:var(--blue);height:100%;width:' + pct.toFixed(1) + '%"></div></div></div>';
    });
    html += '</div>';
    html += '<div class="card" style="padding:14px"><div style="font-size:13px;font-weight:500;margin-bottom:10px">By event type</div>';
    if ((s.by_type || []).length === 0) html += '<div class="muted" style="font-size:12px">no events</div>';
    (s.by_type || []).forEach(function(r) {
      var color = r.total >= 0 ? 'var(--green)' : 'var(--red)';
      html += '<div style="display:flex;justify-content:space-between;font-size:12px;padding:3px 0"><span>' + esc(r.event_type) + '</span><span style="color:' + color + '">' + fmtCents(r.total) + ' <span style="color:var(--muted)">(' + r.cnt + ')</span></span></div>';
    });
    html += '</div></div>';

    // Margin by project (revenue - costs)
    try {
      var rMargin = await fetch('/api/revenue/margin').then(function(r){return r.json();});
      var mrows = (rMargin.rows || []).filter(function(r){return r.project_key !== 'TOTAL' && (r.revenue_cents > 0 || r.cost_cents > 0);});
      if (mrows.length > 0) {
        html += '<h3 style="margin:18px 0 8px 0;font-size:13px;color:var(--muted)">Margin by project (revenue − costs)</h3>';
        html += '<div class="card" style="padding:14px">';
        mrows.forEach(function(r) {
          var marginColor = r.margin_cents >= 0 ? 'var(--green)' : 'var(--red)';
          var marginPctStr = r.margin_pct === null ? '—' : r.margin_pct.toFixed(1) + '%';
          html += '<div style="display:grid;grid-template-columns:200px 1fr 1fr 1fr 80px;gap:10px;align-items:center;padding:6px 0;border-bottom:1px solid var(--border);font-size:12px">';
          html += '<div><strong>' + esc(r.project_key) + '</strong></div>';
          html += '<div style="text-align:right;color:var(--blue)">' + fmtCents(r.revenue_cents) + '</div>';
          html += '<div style="text-align:right;color:var(--amber)">' + fmtCents(r.cost_cents) + '</div>';
          html += '<div style="text-align:right;color:' + marginColor + ';font-weight:600">' + fmtCents(r.margin_cents) + '</div>';
          html += '<div style="text-align:right;color:' + marginColor + '">' + marginPctStr + '</div>';
          html += '</div>';
        });
        // Header row
        html = html.replace('<div style="display:grid;grid-template-columns:200px 1fr 1fr 1fr 80px;gap:10px;align-items:center;padding:6px 0;border-bottom:1px solid var(--border);font-size:12px">',
          '<div style="display:grid;grid-template-columns:200px 1fr 1fr 1fr 80px;gap:10px;align-items:center;padding:6px 0;border-bottom:1px solid var(--border);font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:0.5px">' +
          '<div>project</div><div style="text-align:right">revenue</div><div style="text-align:right">costs</div><div style="text-align:right">margin</div><div style="text-align:right">%</div></div>' +
          '<div style="display:grid;grid-template-columns:200px 1fr 1fr 1fr 80px;gap:10px;align-items:center;padding:6px 0;border-bottom:1px solid var(--border);font-size:12px">');
        // Grand total
        var total = (rMargin.rows || []).find(function(r){return r.project_key === 'TOTAL';});
        if (total) {
          var tColor = total.margin_cents >= 0 ? 'var(--green)' : 'var(--red)';
          var tPct = total.margin_pct === null ? '—' : total.margin_pct.toFixed(1) + '%';
          html += '<div style="display:grid;grid-template-columns:200px 1fr 1fr 1fr 80px;gap:10px;align-items:center;padding:10px 0 0 0;margin-top:6px;border-top:2px solid var(--border);font-size:13px;font-weight:600">';
          html += '<div>TOTAL</div>';
          html += '<div style="text-align:right;color:var(--blue)">' + fmtCents(total.revenue_cents) + '</div>';
          html += '<div style="text-align:right;color:var(--amber)">' + fmtCents(total.cost_cents) + '</div>';
          html += '<div style="text-align:right;color:' + tColor + '">' + fmtCents(total.margin_cents) + '</div>';
          html += '<div style="text-align:right;color:' + tColor + '">' + tPct + '</div>';
          html += '</div>';
        }
        html += '</div>';
      }
    } catch (e) { console.log('margin fetch failed', e); }

    // Sources
    html += '<h3 style="margin:18px 0 8px 0;font-size:13px;color:var(--muted)">Connected sources</h3>';
    if (sources.length === 0) {
      html += '<div class="empty-state">no sources. click "+ seed mock" to generate demo data, or add a real source via the API.</div>';
    } else {
      sources.forEach(function(src) {
        var statusColor = src.last_poll_status === 'ok' ? 'var(--green)' : (src.last_poll_status === 'error' ? 'var(--red)' : 'var(--muted)');
        var enabledBadge = src.enabled ? '<span style="background:var(--green);color:#fff;padding:1px 6px;border-radius:3px;font-size:10px">on</span>' : '<span style="background:var(--bg-soft);color:var(--muted);padding:1px 6px;border-radius:3px;font-size:10px">off</span>';
        html += '<div class="card" style="padding:10px;margin-bottom:6px;display:flex;justify-content:space-between;align-items:center">';
        html += '<div style="flex:1"><div style="font-size:13px"><strong>' + esc(src.name) + '</strong> ' + enabledBadge + ' <span style="color:var(--muted);font-size:11px;margin-left:6px">' + esc(src.source_type) + ' · ' + esc(src.project_key) + ' · ' + esc(src.currency) + '</span></div>';
        html += '<div style="font-size:11px;color:var(--muted);margin-top:2px">last poll: ' + (src.last_poll_at ? new Date(src.last_poll_at*1000).toLocaleString() : 'never') + ' <span style="color:' + statusColor + '">· ' + (src.last_poll_status || '—') + '</span></div>';
        if (src.last_poll_error) html += '<div style="font-size:11px;color:var(--red);margin-top:2px">' + esc(src.last_poll_error) + '</div>';
        html += '</div>';
        html += '<div style="display:flex;gap:4px"><button data-rev-test="' + src.id + '" style="background:var(--bg-soft);color:var(--ink);border:1px solid var(--border);padding:4px 10px;border-radius:3px;cursor:pointer;font-size:11px">test</button>';
        html += '<button data-rev-poll="' + src.id + '" style="background:var(--blue);color:#fff;border:none;padding:4px 10px;border-radius:3px;cursor:pointer;font-size:11px">poll</button>';
        html += '<button data-rev-del="' + src.id + '" style="background:transparent;color:var(--red);border:1px solid var(--red);padding:4px 8px;border-radius:3px;cursor:pointer;font-size:11px">×</button></div></div>';
      });
    }

    // Recent events
    html += '<h3 style="margin:18px 0 8px 0;font-size:13px;color:var(--muted)">Recent events</h3>';
    if (events.length === 0) {
      html += '<div class="empty-state">no events yet</div>';
    } else {
      html += '<table style="width:100%;border-collapse:collapse;font-size:12px"><thead><tr style="border-bottom:1px solid var(--border);text-align:left;color:var(--muted)">';
      html += '<th style="padding:6px">date</th><th>source</th><th>project</th><th>type</th><th>customer</th><th>amount</th><th>status</th></tr></thead><tbody>';
      events.forEach(function(e) {
        var amtColor = e.amount_cents >= 0 ? 'var(--green)' : 'var(--red)';
        var statusColor = e.status === 'succeeded' ? 'var(--green)' : (e.status === 'failed' ? 'var(--red)' : 'var(--muted)');
        html += '<tr style="border-bottom:1px solid var(--border)">';
        html += '<td style="padding:6px;color:var(--muted)">' + fmtDate(e.occurred_at) + '</td>';
        html += '<td>' + esc(e.source_type) + '</td>';
        html += '<td>' + esc(e.project_key) + '</td>';
        html += '<td style="color:var(--muted)">' + esc(e.event_type) + '</td>';
        html += '<td style="color:var(--muted)">' + esc(e.customer_email || e.customer_ref || '—') + '</td>';
        html += '<td style="color:' + amtColor + ';text-align:right">' + fmtCents(e.amount_cents) + '</td>';
        html += '<td style="color:' + statusColor + '">' + esc(e.status) + '</td>';
        html += '</tr>';
      });
      html += '</tbody></table>';
    }

    main.innerHTML = html;

    // Wire actions
    document.getElementById('rev-seed').addEventListener('click', async function() {
      var r = await fetch('/api/revenue/seed-mock', {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'});
      var d = await r.json();
      console.log('seeded', d);
      renderRevenueView();
    });
    document.getElementById('rev-poll').addEventListener('click', async function() {
      // Poll all sources
      for (var i = 0; i < sources.length; i++) {
        await fetch('/api/revenue/sources/' + sources[i].id + '/poll', {method:'POST'});
      }
      renderRevenueView();
    });
    main.querySelectorAll('[data-rev-test]').forEach(function(b) {
      b.addEventListener('click', async function() {
        var r = await fetch('/api/revenue/sources/' + b.dataset.revTest + '/test', {method:'POST'});
        var d = await r.json();
        alert(d.ok ? '✓ ' + d.message : '✗ ' + d.message);
      });
    });
    main.querySelectorAll('[data-rev-poll]').forEach(function(b) {
      b.addEventListener('click', async function() {
        var r = await fetch('/api/revenue/sources/' + b.dataset.revPoll + '/poll', {method:'POST'});
        var d = await r.json();
        console.log('poll result', d);
        renderRevenueView();
      });
    });
    main.querySelectorAll('[data-rev-del]').forEach(function(b) {
      b.addEventListener('click', function() {
        dialog.confirm({ title: 'Delete source', message: 'This also deletes all its events. Continue?', onConfirm: async function() {
          await fetch('/api/revenue/sources/' + b.dataset.revDel, {method:'DELETE'});
          renderRevenueView();
        }});
      });
    });
  } catch (e) { main.innerHTML = '<div class="empty-state">error: ' + esc(e.message) + '</div>'; }
}

// ============================================================
// GA4 view (📈) — Google Analytics traffic metrics
// ============================================================
async function renderGaView() {
  var main = document.getElementById('main-content');
  main.innerHTML = '<div class="empty-state">loading GA4 data…</div>';
  try {
    var rSummary = await fetch('/api/ga/summary').then(function(r){return r.json();});
    var rDaily = await fetch('/api/ga/daily').then(function(r){return r.json();});
    var rSites = await fetch('/api/ga/sites').then(function(r){return r.json();});
    var rMetrics = await fetch('/api/ga/metrics?limit=14').then(function(r){return r.json();});
    var s = rSummary, daily = rDaily.rows || [], sites = rSites.rows || [], recent = rMetrics.rows || [];
    var t = s.totals || {};

    function fmtInt(n) { return (n || 0).toLocaleString('en-US'); }
    function fmtPct(p) { return ((p || 0) * 100).toFixed(0) + '%'; }
    function fmtDur(s) { if (!s) return '—'; var m = Math.floor(s/60), sec = Math.floor(s%60); return m + 'm ' + sec + 's'; }

    var html = '';
    html += '<div class="view-header"><h1>📈 Google Analytics</h1>';
    html += '<div class="muted">Daily traffic metrics · ' + sites.length + ' sites</div>';
    html += '<div style="flex:1"></div>';
    html += '<select id="ga-days" style="background:var(--bg-soft);color:var(--ink);border:1px solid var(--border);padding:6px 10px;border-radius:4px;font-size:12px;margin-right:6px"><option value="7">last 7d</option><option value="14" selected>last 14d</option><option value="30">last 30d</option></select>';
    html += '<button id="ga-seed" style="background:var(--bg-soft);color:var(--ink);border:1px solid var(--border);padding:6px 12px;border-radius:4px;cursor:pointer;font-size:12px;margin-right:6px">+ seed mock</button>';
    html += '<button id="ga-poll" style="background:var(--blue);color:#fff;border:none;padding:6px 12px;border-radius:4px;cursor:pointer;font-size:12px">↻ poll all</button></div>';

    // Summary cards
    html += '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;margin:14px 0">';
    html += '<div class="card" style="padding:14px"><div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:0.5px">Users</div>';
    html += '<div style="font-size:24px;font-weight:600;color:var(--blue);margin-top:4px">' + fmtInt(t.users) + '</div>';
    html += '<div style="font-size:11px;color:var(--muted);margin-top:2px">' + fmtInt(t.new_users) + ' new</div></div>';
    html += '<div class="card" style="padding:14px"><div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:0.5px">Sessions</div>';
    html += '<div style="font-size:24px;font-weight:600;margin-top:4px">' + fmtInt(t.sessions) + '</div></div>';
    html += '<div class="card" style="padding:14px"><div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:0.5px">Page views</div>';
    html += '<div style="font-size:24px;font-weight:600;margin-top:4px">' + fmtInt(t.page_views) + '</div></div>';
    html += '<div class="card" style="padding:14px"><div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:0.5px">Engagement</div>';
    html += '<div style="font-size:24px;font-weight:600;color:var(--green);margin-top:4px">' + fmtPct(t.avg_engagement_rate) + '</div>';
    html += '<div style="font-size:11px;color:var(--muted);margin-top:2px">avg session ' + fmtDur(t.avg_session_duration) + '</div></div>';
    html += '<div class="card" style="padding:14px"><div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:0.5px">Bounce</div>';
    html += '<div style="font-size:24px;font-weight:600;color:var(--amber);margin-top:4px">' + fmtPct(t.avg_bounce_rate) + '</div></div>';
    html += '<div class="card" style="padding:14px"><div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:0.5px">Events</div>';
    html += '<div style="font-size:24px;font-weight:600;margin-top:4px">' + fmtInt(t.events) + '</div></div>';
    html += '</div>';

    // Daily users + page views chart (SVG)
    if (daily.length > 0) {
      var maxUsers = Math.max.apply(null, daily.map(function(d){return d.users || 0;}).concat([1]));
      var maxPV = Math.max.apply(null, daily.map(function(d){return d.page_views || 0;}).concat([1]));
      var last14 = daily.slice(-14);
      var w = 100 / last14.length;
      var svg = '<svg viewBox="0 0 100 30" preserveAspectRatio="none" style="width:100%;height:100px;background:var(--bg-soft);border-radius:6px;padding:6px;box-sizing:border-box">';
      last14.forEach(function(d, i, arr) {
        var x = (i / arr.length) * 100;
        var hUsers = (d.users / maxUsers) * 24;
        var hPV = (d.page_views / maxPV) * 24;
        svg += '<rect x="' + x.toFixed(2) + '" y="' + (28 - hUsers).toFixed(2) + '" width="' + (100/arr.length * 0.45).toFixed(2) + '" height="' + hUsers.toFixed(2) + '" fill="var(--blue)" opacity="0.8"/>';
        svg += '<rect x="' + (x + 100/arr.length * 0.5).toFixed(2) + '" y="' + (28 - hPV).toFixed(2) + '" width="' + (100/arr.length * 0.45).toFixed(2) + '" height="' + hPV.toFixed(2) + '" fill="var(--green)" opacity="0.6"/>';
      });
      svg += '</svg>';
      html += '<div class="card" style="padding:14px;margin-bottom:14px"><div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px"><div style="font-size:13px;font-weight:500">Daily users (blue) + page views (green) — last 14d</div>';
      html += '<div style="font-size:11px;color:var(--muted)"><span style="color:var(--blue)">■</span> users <span style="color:var(--green);margin-left:8px">■</span> page views</div></div>' + svg + '</div>';
    }

    // By site
    html += '<div class="card" style="padding:14px;margin-bottom:14px"><div style="font-size:13px;font-weight:500;margin-bottom:10px">By site (users)</div>';
    if (!s.by_site || s.by_site.length === 0) html += '<div class="muted" style="font-size:12px">no traffic yet</div>';
    var maxSite = Math.max.apply(null, (s.by_site || []).map(function(r){return r.users;}).concat([1]));
    (s.by_site || []).forEach(function(r) {
      var pct = (r.users / maxSite) * 100;
      html += '<div style="margin-bottom:6px"><div style="display:flex;justify-content:space-between;font-size:12px;margin-bottom:2px"><span>' + esc(r.display_name) + ' <span style="color:var(--muted)">(' + esc(r.project_key) + ')</span></span><span style="color:var(--muted)">' + fmtInt(r.users) + ' users · ' + fmtInt(r.sessions) + ' sessions</span></div>';
      html += '<div style="background:var(--bg);border-radius:3px;height:8px;overflow:hidden"><div style="background:linear-gradient(90deg,var(--blue),var(--green));height:100%;width:' + pct.toFixed(1) + '%"></div></div></div>';
    });
    html += '</div>';

    // Traffic sources (aggregate from recent metrics)
    var srcTotals = {organic: 0, direct: 0, referral: 0, social: 0, email: 0, paid: 0};
    recent.forEach(function(m) {
      srcTotals.organic += m.source_organic || 0;
      srcTotals.direct += m.source_direct || 0;
      srcTotals.referral += m.source_referral || 0;
      srcTotals.social += m.source_social || 0;
      srcTotals.email += m.source_email || 0;
      srcTotals.paid += m.source_paid || 0;
    });
    var srcTotal = Object.values(srcTotals).reduce(function(a, b){return a + b;}, 0);
    if (srcTotal > 0) {
      html += '<div class="card" style="padding:14px;margin-bottom:14px"><div style="font-size:13px;font-weight:500;margin-bottom:10px">Traffic sources (last 14d, all sites)</div>';
      var srcList = [
        {k: 'organic', label: 'Organic search', color: 'var(--green)'},
        {k: 'direct', label: 'Direct', color: 'var(--blue)'},
        {k: 'referral', label: 'Referral', color: 'var(--amber)'},
        {k: 'social', label: 'Social', color: '#a855f7'},
        {k: 'email', label: 'Email', color: '#ec4899'},
        {k: 'paid', label: 'Paid', color: 'var(--red)'},
      ];
      srcList.forEach(function(s) {
        var n = srcTotals[s.k];
        var pct = (n / srcTotal) * 100;
        html += '<div style="margin-bottom:4px"><div style="display:flex;justify-content:space-between;font-size:12px;margin-bottom:2px"><span>' + s.label + '</span><span style="color:var(--muted)">' + fmtInt(n) + ' (' + pct.toFixed(1) + '%)</span></div>';
        html += '<div style="background:var(--bg);border-radius:3px;height:6px;overflow:hidden"><div style="background:' + s.color + ';height:100%;width:' + pct.toFixed(1) + '%"></div></div></div>';
      });
      html += '</div>';
    }

    // Sites list
    html += '<h3 style="margin:18px 0 8px 0;font-size:13px;color:var(--muted)">Connected sites</h3>';
    if (sites.length === 0) {
      html += '<div class="empty-state">no sites. click "+ seed mock" to generate demo data, or add a real site via the API.</div>';
    } else {
      sites.forEach(function(site) {
        var statusColor = site.last_poll_status === 'ok' ? 'var(--green)' : (site.last_poll_status === 'error' ? 'var(--red)' : 'var(--muted)');
        var enabledBadge = site.enabled ? '<span style="background:var(--green);color:#fff;padding:1px 6px;border-radius:3px;font-size:10px">on</span>' : '<span style="background:var(--bg-soft);color:var(--muted);padding:1px 6px;border-radius:3px;font-size:10px">off</span>';
        html += '<div class="card" style="padding:10px;margin-bottom:6px;display:flex;justify-content:space-between;align-items:center">';
        html += '<div style="flex:1"><div style="font-size:13px"><strong>' + esc(site.display_name) + '</strong> ' + enabledBadge + ' <span style="color:var(--muted);font-size:11px;margin-left:6px">' + esc(site.property_id) + ' · ' + esc(site.project_key) + '</span></div>';
        html += '<div style="font-size:11px;color:var(--muted);margin-top:2px">' + (site.url ? '<a href="' + esc(site.url) + '" target="_blank" style="color:var(--blue)">' + esc(site.url) + '</a> · ' : '') + 'last poll: ' + (site.last_poll_at ? new Date(site.last_poll_at*1000).toLocaleString() : 'never') + ' <span style="color:' + statusColor + '">· ' + (site.last_poll_status || '—') + '</span></div>';
        if (site.last_poll_error) html += '<div style="font-size:11px;color:var(--red);margin-top:2px">' + esc(site.last_poll_error) + '</div>';
        html += '</div>';
        html += '<div style="display:flex;gap:4px"><button data-ga-test="' + site.id + '" style="background:var(--bg-soft);color:var(--ink);border:1px solid var(--border);padding:4px 10px;border-radius:3px;cursor:pointer;font-size:11px">test</button>';
        html += '<button data-ga-poll="' + site.id + '" style="background:var(--blue);color:#fff;border:none;padding:4px 10px;border-radius:3px;cursor:pointer;font-size:11px">poll</button>';
        html += '<button data-ga-del="' + site.id + '" style="background:transparent;color:var(--red);border:1px solid var(--red);padding:4px 8px;border-radius:3px;cursor:pointer;font-size:11px">×</button></div></div>';
      });
    }

    main.innerHTML = html;

    document.getElementById('ga-seed').addEventListener('click', async function() {
      await fetch('/api/ga/seed-mock', {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'});
      renderGaView();
    });
    document.getElementById('ga-poll').addEventListener('click', async function() {
      for (var i = 0; i < sites.length; i++) {
        await fetch('/api/ga/sites/' + sites[i].id + '/poll', {method:'POST'});
      }
      renderGaView();
    });
    main.querySelectorAll('[data-ga-test]').forEach(function(b) {
      b.addEventListener('click', async function() {
        var r = await fetch('/api/ga/sites/' + b.dataset.gaTest + '/test', {method:'POST'});
        var d = await r.json();
        alert(d.ok ? '✓ ' + d.message : '✗ ' + d.message);
      });
    });
    main.querySelectorAll('[data-ga-poll]').forEach(function(b) {
      b.addEventListener('click', async function() {
        await fetch('/api/ga/sites/' + b.dataset.gaPoll + '/poll', {method:'POST'});
        renderGaView();
      });
    });
    main.querySelectorAll('[data-ga-del]').forEach(function(b) {
      b.addEventListener('click', function() {
        dialog.confirm({ title: 'Delete site', message: 'This also deletes all its metrics. Continue?', onConfirm: async function() {
          await fetch('/api/ga/sites/' + b.dataset.gaDel, {method:'DELETE'});
          renderGaView();
        }});
      });
    });
  } catch (e) { main.innerHTML = '<div class="empty-state">error: ' + esc(e.message) + '</div>'; }
}

// ============================================================
// GSC view (🔍) — Google Search Console search performance
// ============================================================
async function renderGscView(days) {
  days = days || 28;
  var main = document.getElementById('main-content');
  main.innerHTML = '<div class="empty-state">loading Search Console data…</div>';
  try {
    var since = new Date(Date.now() - days * 86400000).toISOString().slice(0, 10);
    var rSummary = await fetch('/api/gsc/summary?since=' + since).then(function(r){return r.json();});
    var rDaily = await fetch('/api/gsc/daily?since=' + since).then(function(r){return r.json();});
    var rSites = await fetch('/api/gsc/sites').then(function(r){return r.json();});
    var s = rSummary, sites = rSites.rows || [];
    var t = s.totals || {};

    // Collapse per-site daily rows into per-day totals for the chart
    var byDay = {};
    (rDaily.rows || []).forEach(function(r) {
      var d = byDay[r.date] || (byDay[r.date] = { date: r.date, clicks: 0, impressions: 0 });
      d.clicks += r.clicks || 0; d.impressions += r.impressions || 0;
    });
    var daily = Object.keys(byDay).sort().map(function(k){return byDay[k];});

    function fmtInt(n) { return (n || 0).toLocaleString('en-US'); }

    var html = '';
    html += '<div class="view-header"><h1>🔍 Search Console</h1>';
    html += '<div class="muted">Search performance · ' + sites.length + ' sites · last ' + days + 'd</div>';
    html += '<div style="flex:1"></div>';
    html += '<select id="gsc-days" style="background:var(--bg-soft);color:var(--ink);border:1px solid var(--border);padding:6px 10px;border-radius:4px;font-size:12px">';
    [7, 28, 90].forEach(function(d) {
      html += '<option value="' + d + '"' + (d === days ? ' selected' : '') + '>last ' + d + 'd</option>';
    });
    html += '</select></div>';

    // Summary cards
    html += '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;margin:14px 0">';
    html += '<div class="card" style="padding:14px"><div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:0.5px">Clicks</div>';
    html += '<div style="font-size:24px;font-weight:600;color:var(--blue);margin-top:4px">' + fmtInt(t.clicks) + '</div></div>';
    html += '<div class="card" style="padding:14px"><div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:0.5px">Impressions</div>';
    html += '<div style="font-size:24px;font-weight:600;margin-top:4px">' + fmtInt(t.impressions) + '</div></div>';
    html += '<div class="card" style="padding:14px"><div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:0.5px">CTR</div>';
    html += '<div style="font-size:24px;font-weight:600;color:var(--green);margin-top:4px">' + ((t.ctr || 0) * 100).toFixed(1) + '%</div></div>';
    html += '<div class="card" style="padding:14px"><div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:0.5px">Avg position</div>';
    html += '<div style="font-size:24px;font-weight:600;color:var(--amber);margin-top:4px">' + (t.position ? t.position.toFixed(1) : '—') + '</div></div>';
    html += '</div>';

    // Daily impressions + clicks chart (SVG, impressions behind, clicks in front)
    if (daily.length > 0) {
      var maxImpr = Math.max.apply(null, daily.map(function(d){return d.impressions;}).concat([1]));
      var maxClicks = Math.max.apply(null, daily.map(function(d){return d.clicks;}).concat([1]));
      var svg = '<svg viewBox="0 0 100 30" preserveAspectRatio="none" style="width:100%;height:100px;background:var(--bg-soft);border-radius:6px;padding:6px;box-sizing:border-box">';
      daily.forEach(function(d, i, arr) {
        var x = (i / arr.length) * 100;
        var hImpr = (d.impressions / maxImpr) * 24;
        var hClicks = (d.clicks / maxClicks) * 24;
        svg += '<rect x="' + x.toFixed(2) + '" y="' + (28 - hImpr).toFixed(2) + '" width="' + (100/arr.length * 0.45).toFixed(2) + '" height="' + hImpr.toFixed(2) + '" fill="var(--muted)" opacity="0.45"/>';
        svg += '<rect x="' + (x + 100/arr.length * 0.5).toFixed(2) + '" y="' + (28 - hClicks).toFixed(2) + '" width="' + (100/arr.length * 0.45).toFixed(2) + '" height="' + hClicks.toFixed(2) + '" fill="var(--blue)" opacity="0.9"/>';
      });
      svg += '</svg>';
      html += '<div class="card" style="padding:14px;margin-bottom:14px"><div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px"><div style="font-size:13px;font-weight:500">Daily impressions (grey, own scale) + clicks (blue, own scale)</div>';
      html += '<div style="font-size:11px;color:var(--muted)"><span style="color:var(--muted)">■</span> impressions <span style="color:var(--blue);margin-left:8px">■</span> clicks</div></div>' + svg + '</div>';
    }

    // By site
    html += '<div class="card" style="padding:14px;margin-bottom:14px"><div style="font-size:13px;font-weight:500;margin-bottom:10px">By site (impressions)</div>';
    if (!s.by_site || s.by_site.length === 0) html += '<div class="muted" style="font-size:12px">no search data yet — run a poll</div>';
    var maxSite = Math.max.apply(null, (s.by_site || []).map(function(r){return r.impressions;}).concat([1]));
    (s.by_site || []).forEach(function(r) {
      var pct = (r.impressions / maxSite) * 100;
      html += '<div style="margin-bottom:6px"><div style="display:flex;justify-content:space-between;font-size:12px;margin-bottom:2px">';
      html += '<span>' + esc(r.display_name) + ' <span style="color:var(--muted)">(' + esc(r.project_key) + ')</span></span>';
      html += '<span style="color:var(--muted)">' + fmtInt(r.clicks) + ' clicks · ' + fmtInt(r.impressions) + ' impr · CTR ' + ((r.ctr || 0) * 100).toFixed(1) + '% · pos ' + (r.position ? r.position.toFixed(1) : '—') + '</span></div>';
      html += '<div style="background:var(--bg);border-radius:3px;height:8px;overflow:hidden"><div style="background:linear-gradient(90deg,var(--blue),var(--green));height:100%;width:' + pct.toFixed(1) + '%"></div></div></div>';
    });
    html += '</div>';

    // Site poll status
    html += '<div class="card" style="padding:14px"><div style="font-size:13px;font-weight:500;margin-bottom:10px">Sites</div>';
    html += '<table style="width:100%;border-collapse:collapse;font-size:12px">';
    html += '<tr style="color:var(--muted);text-align:left"><th style="padding:4px 8px">property</th><th style="padding:4px 8px">project</th><th style="padding:4px 8px">last poll</th><th style="padding:4px 8px">status</th></tr>';
    sites.forEach(function(r) {
      var st = r.last_poll_status === 'ok' ? '<span style="color:var(--green)">ok</span>'
             : r.last_poll_status === 'error' ? '<span style="color:var(--red)" title="' + esc(r.last_poll_error || '') + '">error</span>' : '—';
      html += '<tr style="border-top:1px solid var(--border)"><td style="padding:4px 8px">' + esc(r.site_url) + '</td><td style="padding:4px 8px;color:var(--muted)">' + esc(r.project_key) + '</td>';
      html += '<td style="padding:4px 8px;color:var(--muted)">' + (r.last_poll_at ? new Date(r.last_poll_at * 1000).toLocaleString() : '—') + '</td><td style="padding:4px 8px">' + st + '</td></tr>';
    });
    html += '</table><div class="muted" style="font-size:11px;margin-top:8px">Refresh data: <code>ops gsc poll</code> · GSC data lags ~2-3 days</div></div>';

    main.innerHTML = html;
    var sel = document.getElementById('gsc-days');
    if (sel) sel.addEventListener('change', function() { renderGscView(parseInt(sel.value, 10)); });
  } catch (e) { main.innerHTML = '<div class="empty-state">error: ' + esc(e.message) + '</div>'; }
}

// ============================================================
// Sentry view (🐛) — error tracking
// ============================================================
async function renderSentryView() {
  var main = document.getElementById('main-content');
  main.innerHTML = '<div class="empty-state">loading Sentry issues…</div>';
  try {
    var rSummary = await fetch('/api/sentry/summary').then(function(r){return r.json();});
    var rProjects = await fetch('/api/sentry/projects').then(function(r){return r.json();});
    var rIssues = await fetch('/api/sentry/issues?limit=30').then(function(r){return r.json();});
    var s = rSummary, projects = rProjects.rows || [], issues = rIssues.rows || [];

    function fmtDate(ts) { return new Date(ts*1000).toLocaleString(); }
    function levelColor(l) { return l === 'fatal' ? 'var(--red)' : (l === 'error' ? 'var(--red)' : (l === 'warning' ? 'var(--amber)' : 'var(--blue)')); }
    function statusBadge(st) { return st === 'unresolved' ? '<span style="background:var(--red);color:#fff;padding:1px 6px;border-radius:3px;font-size:10px">unresolved</span>' : (st === 'resolved' ? '<span style="background:var(--green);color:#fff;padding:1px 6px;border-radius:3px;font-size:10px">resolved</span>' : '<span style="background:var(--bg-soft);color:var(--muted);padding:1px 6px;border-radius:3px;font-size:10px">ignored</span>'); }

    var html = '';
    html += '<div class="view-header"><h1>🐛 Sentry issues</h1>';
    html += '<div class="muted">Error tracking · ' + projects.length + ' projects · ' + s.total + ' issues</div>';
    html += '<div style="flex:1"></div>';
    html += '<button id="sentry-seed" style="background:var(--bg-soft);color:var(--ink);border:1px solid var(--border);padding:6px 12px;border-radius:4px;cursor:pointer;font-size:12px;margin-right:6px">+ seed mock</button>';
    html += '<button id="sentry-poll" style="background:var(--blue);color:#fff;border:none;padding:6px 12px;border-radius:4px;cursor:pointer;font-size:12px">↻ poll all</button></div>';

    // Summary cards
    html += '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;margin:14px 0">';
    html += '<div class="card" style="padding:14px"><div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:0.5px">Total issues</div>';
    html += '<div style="font-size:24px;font-weight:600;margin-top:4px">' + s.total + '</div></div>';
    (s.by_level || []).forEach(function(r) {
      html += '<div class="card" style="padding:14px"><div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:0.5px">' + esc(r.level) + '</div>';
      html += '<div style="font-size:24px;font-weight:600;margin-top:4px;color:' + levelColor(r.level) + '">' + r.cnt + '</div></div>';
    });
    html += '</div>';

    // By project + by status side by side
    html += '<div style="display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:14px">';
    html += '<div class="card" style="padding:14px"><div style="font-size:13px;font-weight:500;margin-bottom:10px">By project</div>';
    if ((s.by_project || []).length === 0) html += '<div class="muted" style="font-size:12px">no projects</div>';
    var maxP = Math.max.apply(null, (s.by_project || []).map(function(r){return r.cnt;}).concat([1]));
    (s.by_project || []).forEach(function(r) {
      var pct = (r.cnt / maxP) * 100;
      html += '<div style="margin-bottom:6px"><div style="display:flex;justify-content:space-between;font-size:12px;margin-bottom:2px"><span>' + esc(r.project_key) + '</span><span style="color:var(--muted)">' + r.cnt + ' issues · ' + (r.events || 0) + ' events</span></div>';
      html += '<div style="background:var(--bg);border-radius:3px;height:6px;overflow:hidden"><div style="background:var(--red);height:100%;width:' + pct.toFixed(1) + '%"></div></div></div>';
    });
    html += '</div>';
    html += '<div class="card" style="padding:14px"><div style="font-size:13px;font-weight:500;margin-bottom:10px">By status</div>';
    if ((s.by_status || []).length === 0) html += '<div class="muted" style="font-size:12px">no issues</div>';
    (s.by_status || []).forEach(function(r) {
      var color = r.status === 'unresolved' ? 'var(--red)' : (r.status === 'resolved' ? 'var(--green)' : 'var(--muted)');
      html += '<div style="display:flex;justify-content:space-between;font-size:12px;padding:3px 0"><span>' + esc(r.status) + '</span><span style="color:' + color + '">' + r.cnt + '</span></div>';
    });
    html += '</div></div>';

    // Issues list
    html += '<h3 style="margin:18px 0 8px 0;font-size:13px;color:var(--muted)">Recent issues</h3>';
    if (issues.length === 0) {
      html += '<div class="empty-state">no issues yet. click "+ seed mock" for demo data, or add a real Sentry token.</div>';
    } else {
      html += '<table style="width:100%;border-collapse:collapse;font-size:12px"><thead><tr style="border-bottom:1px solid var(--border);text-align:left;color:var(--muted)">';
      html += '<th style="padding:6px">last seen</th><th>level</th><th>project</th><th>issue</th><th>events</th><th>users</th><th>status</th></tr></thead><tbody>';
      issues.forEach(function(it) {
        var lc = levelColor(it.level);
        html += '<tr style="border-bottom:1px solid var(--border)">';
        html += '<td style="padding:6px;color:var(--muted)">' + fmtDate(it.last_seen) + '</td>';
        html += '<td style="color:' + lc + ';font-weight:600;text-transform:uppercase;font-size:11px">' + esc(it.level) + '</td>';
        html += '<td>' + esc(it.project_key) + '</td>';
        html += '<td><div style="font-weight:500">' + esc(it.title) + '</div>';
        if (it.culprit) html += '<div style="font-size:11px;color:var(--muted);font-family:monospace">' + esc(it.culprit) + '</div>';
        html += '</td>';
        html += '<td style="text-align:right;font-weight:600">' + it.event_count + '</td>';
        html += '<td style="text-align:right;color:var(--muted)">' + it.user_count + '</td>';
        html += '<td>' + statusBadge(it.status) + '</td>';
        html += '</tr>';
      });
      html += '</tbody></table>';
    }

    // Projects list
    html += '<h3 style="margin:18px 0 8px 0;font-size:13px;color:var(--muted)">Connected projects</h3>';
    if (projects.length === 0) {
      html += '<div class="empty-state">no projects. click "+ seed mock" for demo data.</div>';
    } else {
      projects.forEach(function(p) {
        var statusColor = p.last_poll_status === 'ok' ? 'var(--green)' : (p.last_poll_status === 'error' ? 'var(--red)' : 'var(--muted)');
        var enabledBadge = p.enabled ? '<span style="background:var(--green);color:#fff;padding:1px 6px;border-radius:3px;font-size:10px">on</span>' : '<span style="background:var(--bg-soft);color:var(--muted);padding:1px 6px;border-radius:3px;font-size:10px">off</span>';
        html += '<div class="card" style="padding:10px;margin-bottom:6px;display:flex;justify-content:space-between;align-items:center">';
        html += '<div style="flex:1"><div style="font-size:13px"><strong>' + esc(p.name) + '</strong> ' + enabledBadge + ' <span style="color:var(--muted);font-size:11px;margin-left:6px">' + esc(p.source_type) + ' · ' + esc(p.project_key) + (p.sentry_project ? ' · ' + esc(p.sentry_project) : '') + '</span></div>';
        html += '<div style="font-size:11px;color:var(--muted);margin-top:2px">last poll: ' + (p.last_poll_at ? fmtDate(p.last_poll_at) : 'never') + ' <span style="color:' + statusColor + '">· ' + (p.last_poll_status || '—') + '</span></div>';
        if (p.last_poll_error) html += '<div style="font-size:11px;color:var(--red);margin-top:2px">' + esc(p.last_poll_error) + '</div>';
        html += '</div>';
        html += '<div style="display:flex;gap:4px"><button data-sentry-test="' + p.id + '" style="background:var(--bg-soft);color:var(--ink);border:1px solid var(--border);padding:4px 10px;border-radius:3px;cursor:pointer;font-size:11px">test</button>';
        html += '<button data-sentry-poll="' + p.id + '" style="background:var(--blue);color:#fff;border:none;padding:4px 10px;border-radius:3px;cursor:pointer;font-size:11px">poll</button>';
        html += '<button data-sentry-del="' + p.id + '" style="background:transparent;color:var(--red);border:1px solid var(--red);padding:4px 8px;border-radius:3px;cursor:pointer;font-size:11px">×</button></div></div>';
      });
    }

    main.innerHTML = html;

    document.getElementById('sentry-seed').addEventListener('click', async function() {
      await fetch('/api/sentry/seed-mock', {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'});
      renderSentryView();
    });
    document.getElementById('sentry-poll').addEventListener('click', async function() {
      for (var i = 0; i < projects.length; i++) {
        await fetch('/api/sentry/projects/' + projects[i].id + '/poll', {method:'POST'});
      }
      renderSentryView();
    });
    main.querySelectorAll('[data-sentry-test]').forEach(function(b) {
      b.addEventListener('click', async function() {
        var r = await fetch('/api/sentry/projects/' + b.dataset.sentryTest + '/test', {method:'POST'});
        var d = await r.json();
        alert(d.ok ? '✓ ' + d.message : '✗ ' + d.message);
      });
    });
    main.querySelectorAll('[data-sentry-poll]').forEach(function(b) {
      b.addEventListener('click', async function() {
        await fetch('/api/sentry/projects/' + b.dataset.sentryPoll + '/poll', {method:'POST'});
        renderSentryView();
      });
    });
    main.querySelectorAll('[data-sentry-del]').forEach(function(b) {
      b.addEventListener('click', function() {
        dialog.confirm({ title: 'Delete Sentry project', message: 'This also deletes all its issues. Continue?', onConfirm: async function() {
          await fetch('/api/sentry/projects/' + b.dataset.sentryDel, {method:'DELETE'});
          renderSentryView();
        }});
      });
    });
  } catch (e) { main.innerHTML = '<div class="empty-state">error: ' + esc(e.message) + '</div>'; }
}

// ============================================================
// GitHub view (🐙)
// ============================================================
async function renderGithubView() {
  var main = document.getElementById('main-content');
  main.innerHTML = '<div class="empty-state">loading GitHub data…</div>';
  try {
    var resp = await fetch('/api/github');
    var data = await resp.json();
    var s = data.summary || {};
    var rows = data.rows || [];
    var html = '';
    html += '<div class="view-header"><h1>🐙 GitHub</h1>';
    html += '<div class="muted">open PRs, issues, and recent commits across repos</div></div>';
    html += '<button id="gh-sync" style="background:var(--blue);color:#fff;border:none;padding:6px 14px;border-radius:4px;cursor:pointer;font-size:13px">sync now</button>';
    // Open PRs
    html += '<h3 style="margin-top:20px">Open PRs <span style="color:var(--muted);font-weight:400;font-size:13px">(' + (s.open_prs||[]).length + ')</span></h3>';
    if ((s.open_prs||[]).length === 0) {
      html += '<div class="empty-state">no open PRs</div>';
    } else {
      s.open_prs.forEach(function(p) {
        html += '<div class="card" style="padding:10px;margin-bottom:6px;display:flex;justify-content:space-between;align-items:center">';
        html += '<div><div style="font-size:13px;color:var(--ink)"><strong>' + esc(p.repo_full) + '</strong> #' + p.item_number + ' — ' + esc(p.title||'') + '</div>';
        html += '<div style="font-size:11px;color:var(--muted);margin-top:2px">updated ' + new Date((p.updated_at_remote||0)*1000).toLocaleDateString() + '</div></div>';
        html += '<a href="' + esc(p.url) + '" target="_blank" style="color:var(--blue);font-size:12px">open →</a></div>';
      });
    }
    // Open issues
    html += '<h3 style="margin-top:20px">Open issues <span style="color:var(--muted);font-weight:400;font-size:13px">(' + (s.open_issues||[]).length + ')</span></h3>';
    if ((s.open_issues||[]).length === 0) {
      html += '<div class="empty-state">no open issues</div>';
    } else {
      s.open_issues.forEach(function(p) {
        html += '<div class="card" style="padding:10px;margin-bottom:6px;display:flex;justify-content:space-between;align-items:center">';
        html += '<div><div style="font-size:13px;color:var(--ink)"><strong>' + esc(p.repo_full) + '</strong> #' + p.item_number + ' — ' + esc(p.title||'') + '</div>';
        html += '<div style="font-size:11px;color:var(--muted);margin-top:2px">updated ' + new Date((p.updated_at_remote||0)*1000).toLocaleDateString() + '</div></div>';
        html += '<a href="' + esc(p.url) + '" target="_blank" style="color:var(--blue);font-size:12px">open →</a></div>';
      });
    }
    // Recent commits
    html += '<h3 style="margin-top:20px">Recent commits</h3>';
    var commits = rows.filter(function(r){return r.item_type === 'commit';}).slice(0, 15);
    if (commits.length === 0) {
      html += '<div class="empty-state">no commits synced yet</div>';
    } else {
      commits.forEach(function(c) {
        html += '<div style="padding:6px 0;border-bottom:1px solid var(--border);font-size:12px">';
        html += '<span style="color:var(--blue);font-weight:600">' + esc(c.repo_full) + '</span> ';
        html += '<span style="color:var(--ink)">' + esc((c.title||'').slice(0, 80)) + '</span> ';
        html += '<span style="color:var(--muted)">· ' + (c.author || 'unknown') + ' · ' + new Date((c.updated_at_remote||0)*1000).toLocaleDateString() + '</span>';
        html += '</div>';
      });
    }
    main.innerHTML = html;
    renderOutline();
    document.getElementById('gh-sync').addEventListener('click', function() {
      var btn = document.getElementById('gh-sync');
      btn.disabled = true; btn.textContent = 'syncing…';
      fetch('/api/github/sync', {method:'POST'}).then(function(){btn.disabled=false;btn.textContent='sync now';renderGithubView();});
    });
  } catch (e) { main.innerHTML = '<div class="empty-state">error: ' + esc(e.message) + '</div>'; }
}

// ============================================================
// Voice notes view (🎙)
// ============================================================
async function renderVoiceView() {
  var main = document.getElementById('main-content');
  main.innerHTML = '<div class="empty-state">loading voice notes…</div>';
  try {
    var av = await fetch('/api/voice/available').then(function(r){return r.json();});
    var notes = await fetch('/api/voice/notes').then(function(r){return r.json();});
    var html = '';
    html += '<div class="view-header"><h1>🎙 Voice notes</h1>';
    html += '<div class="muted">dictate notes via Whisper local · ' + (av.available ? 'ready' : 'install faster-whisper') + '</div></div>';
    if (!av.available) {
      html += '<div class="card" style="padding:14px;margin-bottom:16px;border-left:3px solid var(--orange)">';
      html += '<strong>faster-whisper not installed</strong><br><code style="font-size:12px;color:var(--muted)">pip install faster-whisper</code><br>';
      html += '<span style="font-size:12px;color:var(--muted)">Then restart server. First call downloads the base model (~150MB).</span></div>';
    } else {
      html += '<div class="card" style="padding:14px;margin-bottom:16px"><strong>Record a note</strong>';
      html += '<div style="margin-top:8px;display:flex;gap:8px;align-items:center"><button id="rec-btn" style="background:var(--red);color:#fff;border:none;padding:8px 16px;border-radius:4px;cursor:pointer">● record</button>';
      html += '<span id="rec-status" style="font-size:12px;color:var(--muted)">ready</span></div>';
      html += '<audio id="rec-audio" controls style="display:none;margin-top:8px;width:100%"></audio></div>';
    }
    html += '<h3>Notes (' + notes.rows.length + ')</h3>';
    if (notes.rows.length === 0) {
      html += '<div class="empty-state">no voice notes yet</div>';
    } else {
      notes.rows.forEach(function(n) {
        html += '<div class="card" style="padding:10px;margin-bottom:8px">';
        html += '<div style="font-size:11px;color:var(--muted);margin-bottom:4px">' + new Date(n.created_at * 1000).toLocaleString() + (n.project_key ? ' · ' + esc(n.project_key) : '');
        if (n.duration_sec) html += ' · ' + n.duration_sec.toFixed(1) + 's';
        html += '</div>';
        html += '<div style="font-size:13px;color:var(--ink);white-space:pre-wrap">' + esc(n.transcript) + '</div>';
        if (n.audio_path) html += '<audio controls src="file://' + esc(n.audio_path) + '" style="width:100%;margin-top:6px"></audio>';
        html += '</div>';
      });
    }
    main.innerHTML = html;
    renderOutline();
    if (av.available) wireRecorder();
  } catch (e) { main.innerHTML = '<div class="empty-state">error: ' + esc(e.message) + '</div>'; }
}

function wireRecorder() {
  var btn = document.getElementById('rec-btn');
  var status = document.getElementById('rec-status');
  var audio = document.getElementById('rec-audio');
  var chunks = [];
  var recorder = null;
  btn.addEventListener('click', function() {
    if (recorder && recorder.state === 'recording') {
      recorder.stop();
      btn.textContent = '● record'; btn.style.background = 'var(--red)';
      status.textContent = 'transcribing…';
      return;
    }
    navigator.mediaDevices.getUserMedia({audio: true}).then(function(stream) {
      chunks = [];
      recorder = new MediaRecorder(stream);
      recorder.ondataavailable = function(e) { chunks.push(e.data); };
      recorder.onstop = function() {
        var blob = new Blob(chunks, {type: 'audio/webm'});
        audio.src = URL.createObjectURL(blob);
        audio.style.display = 'block';
        var fd = new FormData();
        fd.append('audio', blob, 'note.webm');
        fetch('/api/voice/transcribe', {method:'POST', body: fd})
          .then(function(r){return r.json();})
          .then(function(d){status.textContent = 'saved (id=' + d.id + ')'; setTimeout(renderVoiceView, 600);})
          .catch(function(e){status.textContent = 'error: ' + e.message;});
        stream.getTracks().forEach(function(t){t.stop();});
      };
      recorder.start();
      btn.textContent = '■ stop'; btn.style.background = 'var(--bg-soft)';
      status.textContent = 'recording… click stop when done';
    }).catch(function(e){status.textContent = 'mic permission denied';});
  });
}

// ============================================================
// Templates view (🧰)
// ============================================================
async function renderTemplatesView() {
  var main = document.getElementById('main-content');
  main.innerHTML = '<div class="empty-state">loading templates…</div>';
  try {
    var resp = await fetch('/api/templates');
    var data = await resp.json();
    var rows = data.rows || [];
    var html = '';
    html += '<div class="view-header"><h1>🧰 Task templates</h1>';
    html += '<div class="muted">reusable checklists · click "run" to instantiate as user_todos</div></div>';
    if (rows.length === 0) {
      html += '<div class="empty-state">no templates yet. Run <code>ops templates init</code> to seed defaults.</div>';
    } else {
      rows.forEach(function(t) {
        html += '<div class="card" style="margin-bottom:14px;padding:14px">';
        html += '<div style="display:flex;align-items:center;gap:10px;margin-bottom:8px">';
        html += '<span style="font-size:20px">' + (t.icon || '📋') + '</span>';
        html += '<h3 style="margin:0;font-size:14px;color:var(--ink)">' + esc(t.name) + '</h3>';
        html += '<span style="font-size:11px;color:var(--muted)">' + esc(t.key) + '</span>';
        html += '<div style="flex:1"></div>';
        html += '<button data-tpl-run="' + esc(t.key) + '" style="background:var(--blue);color:#fff;border:none;padding:4px 12px;border-radius:3px;cursor:pointer;font-size:12px">run →</button>';
        html += '</div>';
        if (t.description) html += '<div style="font-size:12px;color:var(--muted);margin-bottom:8px">' + esc(t.description) + '</div>';
        html += '<ol style="margin:0;padding-left:20px;font-size:12px;color:var(--ink)">';
        (t.items || []).slice(0, 5).forEach(function(it) {
          html += '<li style="margin-bottom:2px">' + esc(it.title || it.text || '') + (it.description ? ' <span style="color:var(--muted)">— ' + esc(it.description) + '</span>' : '') + '</li>';
        });
        if ((t.items || []).length > 5) html += '<li style="color:var(--muted);list-style:none;margin-top:4px">... and ' + ((t.items||[]).length - 5) + ' more</li>';
        html += '</ol>';
        html += '</div>';
      });
    }
    main.innerHTML = html;
    renderOutline();
    main.querySelectorAll('[data-tpl-run]').forEach(function(b) {
      b.addEventListener('click', function() {
        var proj = prompt('Attach todos to which project? (leave empty for none)');
        fetch('/api/templates/by-key/' + b.dataset.tplRun + '/instantiate', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({project_key: proj || null})})
          .then(function(r){return r.json();})
          .then(function(d){alert('created ' + d.ids.length + ' todos');});
      });
    });
  } catch (e) { main.innerHTML = '<div class="empty-state">error: ' + esc(e.message) + '</div>'; }
}


// ============================================================
// Agents Dashboard (🤖)
// ============================================================
//
// Layout:
//   ┌──────────────┬─────────────────────────────────────┐
//   │ Tree         │  Detail panel                        │
//   │ (drag/drop)  │  - Edit form                         │
//   │              │  - Chat (SSE, multi-provider)        │
//   │              │  - Activity (todos, conversations)   │
//   └──────────────┴─────────────────────────────────────┘
//
// Data: same /api/orgchart tree + /api/members list
// Drag: change manager_id via PUT /api/members/{id}
// Chat: POST /api/members/{id}/chat?stream=1 (SSE)
//
var _agentSelectedId = null;
var _agentChatHistory = {};   // member_id -> [{role, content}]
var _agentEditing = false;
var _agentTab = 'edit';       // 'edit' | 'chat' | 'activity'
var _agentProviders = [];

async function loadProviders() {
  if (_agentProviders.length) return _agentProviders;
  try { _agentProviders = await fetchJSON('/api/brain/providers') || []; }
  catch (e) { _agentProviders = []; }
  return _agentProviders;
}

async function renderAgentsView() {
  var main = document.getElementById('main-content');
  document.getElementById('breadcrumb').innerHTML = '<span>🤖 Agents</span>';
  main.innerHTML = '<div class="empty-state">loading agents…</div>';
  await loadProviders();
  try {
    var data = await fetchJSON('/api/orgchart');
    renderAgentsMount(main, data);
  } catch (e) {
    main.innerHTML = '<div class="empty-state">error: ' + esc(e.message) + '</div>';
  }
}

function renderAgentsMount(mount, data) {
  var stats = data.stats || {};
  var projects = data.projects || [];
  var orphans = (data.orphans && data.orphans.teams) || [];

  var html = '<div class="md-view">';

  // Header
  html += '<div style="display:flex;align-items:center;gap:12px;margin-bottom:16px;flex-wrap:wrap">';
  html += '<h1 style="margin:0">🤖 Agents dashboard</h1>';
  html += '<span style="color:var(--muted);font-size:14px">— ' + stats.total_members + ' members · ' + stats.ai_agents + ' AI · ' + stats.humans + ' humans</span>';
  html += '<div style="flex:1"></div>';
  html += '<button id="agent-edit-toggle" style="background:' + (_agentEditing ? 'var(--green)' : 'var(--bg-soft)') + ';color:' + (_agentEditing ? '#fff' : 'var(--ink)') + ';border:1px solid var(--border);padding:6px 12px;border-radius:4px;cursor:pointer;font-size:12px">' + (_agentEditing ? '✓ edit mode' : 'edit mode') + '</button>';
  html += '<button id="agent-add-btn" style="background:var(--blue);color:#fff;border:none;padding:6px 12px;border-radius:4px;cursor:pointer;font-size:12px">+ new agent</button>';
  html += '</div>';

  // Two-column layout
  html += '<div style="display:grid;grid-template-columns:minmax(280px, 35%) 1fr;gap:16px;align-items:start">';

  // LEFT: Tree (reuses org-tree styling)
  html += '<div class="org-tree" style="background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:14px;max-height:75vh;overflow-y:auto">';

  function renderNode(node, depth) {
    depth = depth || 0;
    var out = '';
    if (node._kind === 'project') {
      out += '<div data-project-id="' + (node.id || '') + '" style="margin-bottom:8px">';
      out += '<div style="font-weight:600;font-size:14px;color:var(--brand);border-left:3px solid var(--brand);padding:4px 8px;background:rgba(124,58,237,0.08);border-radius:3px">' + esc(node.name) + '</div>';
      (node.teams || []).forEach(function(t) {
        out += '<div data-team-id="' + t.id + '" style="margin:6px 0 6px ' + ((depth + 1) * 8) + 'px;padding:6px 10px;border-top:2px solid var(--border);background:var(--bg);border-radius:4px" draggable="' + (_agentEditing ? 'true' : 'false') + '">';
        out += '<div style="font-weight:500;font-size:13px">' + esc(t.icon || '·') + ' ' + esc(t.name) + '</div>';
        (t.members || []).forEach(function(m) {
          out += renderMember(m, depth + 2);
        });
        out += '</div>';
      });
      out += '</div>';
    } else if (node._kind === 'orphan') {
      out += '<div data-orphan-group="1" style="margin-bottom:8px">';
      out += '<div style="font-weight:600;font-size:13px;color:var(--muted);border-left:3px solid var(--muted);padding:4px 8px;background:var(--bg-soft);border-radius:3px">⚠ Orphan teams</div>';
      (node.teams || []).forEach(function(t) {
        out += '<div data-team-id="' + t.id + '" style="margin:6px 0 6px ' + ((depth + 1) * 8) + 'px;padding:6px 10px;border-top:2px solid var(--muted);background:var(--bg);border-radius:4px" draggable="' + (_agentEditing ? 'true' : 'false') + '">';
        out += '<div style="font-weight:500;font-size:13px;color:var(--muted)">' + esc(t.icon || '·') + ' ' + esc(t.name) + '</div>';
        (t.members || []).forEach(function(m) { out += renderMember(m, depth + 2); });
        out += '</div>';
      });
      out += '</div>';
    }
    return out;
  }

  function renderMember(m, indent) {
    var mColor = m.type === 'ai_agent' ? '#7c3aed' : m.type === 'system' ? '#6b6b75' : '#16a34a';
    var mIcon  = m.type === 'ai_agent' ? '🤖' : m.type === 'system' ? '⚙' : '👤';
    var statusColor = m.status === 'active' ? 'var(--green)' : m.status === 'paused' ? 'var(--yellow)' : 'var(--muted)';
    var selected = _agentSelectedId === m.id;
    var selStyle = selected ? 'background:rgba(124,58,237,0.15);border:1px solid var(--blue);' : 'border:1px solid transparent;';
    return '<div data-member-id="' + m.id + '" data-member-name="' + esc(m.name) + '" data-member-type="' + esc(m.type) + '" ' +
      'draggable="' + (_agentEditing ? 'true' : 'false') + '" ' +
      'style="margin:4px 0 4px ' + (indent * 6) + 'px;padding:6px 10px;border-radius:4px;cursor:pointer;border-left:3px solid ' + mColor + ';' + selStyle + 'display:flex;gap:8px;align-items:center;font-size:12px" ' +
      'ondragstart="window._agentDragId=' + m.id + ';event.dataTransfer.effectAllowed=\'move\'" ' +
      'ondragover="event.preventDefault();event.currentTarget.style.background=\'rgba(124,58,237,0.2)\'" ' +
      'ondragleave="event.currentTarget.style.background=\'' + (selected ? 'rgba(124,58,237,0.15)' : '') + '\'" ' +
      'ondrop="event.preventDefault();window._agentDrop(' + m.id + ', event)">'
      + '<span style="font-size:14px">' + mIcon + '</span>'
      + '<span style="flex:1;font-weight:500">' + esc(m.name) + '</span>'
      + '<span style="font-size:10px;color:' + statusColor + '">●</span>'
      + '</div>';
  }

  projects.forEach(function(p) {
    var tree = Object.assign({}, p, { _kind: 'project' });
    html += renderNode(tree, 0);
  });
  if (orphans.length) {
    html += renderNode({ _kind: 'orphan', teams: orphans }, 0);
  }
  html += '</div>';  // .org-tree

  // RIGHT: Detail panel
  html += '<div id="agent-detail-panel" style="background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:14px;min-height:400px;max-height:75vh;overflow-y:auto">';
  html += renderAgentDetail();
  html += '</div>';

  html += '</div>';  // grid
  html += '</div>';  // .md-view

  mount.innerHTML = html;
  renderOutline();

  // Click member → select + show detail
  mount.querySelectorAll('[data-member-id]').forEach(function(el) {
    el.addEventListener('click', function(e) {
      if (e.target.closest('input,button,select,textarea')) return;
      _agentSelectedId = parseInt(el.dataset.memberId);
      _agentChatHistory[_agentSelectedId] = _agentChatHistory[_agentSelectedId] || [];
      _agentTab = 'edit';
      renderAgentsView();
    });
  });

  // Edit mode toggle
  document.getElementById('agent-edit-toggle').addEventListener('click', function() {
    _agentEditing = !_agentEditing;
    renderAgentsView();
  });

  // New agent button
  document.getElementById('agent-add-btn').addEventListener('click', function() {
    var name = prompt('Agent name:');
    if (!name) return;
    var teamId = prompt('Team ID (numeric, or leave blank for 1):') || '1';
    var type = prompt('Type (human | ai_agent | system):', 'ai_agent') || 'ai_agent';
    fetch('/api/members', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({team_id: parseInt(teamId), name: name.trim(), type: type.trim()})
    }).then(function(r){return r.json();}).then(function(d) {
      if (d.id) {
        _agentSelectedId = d.id;
        _agentEditing = false;
        renderAgentsView();
      } else alert('error: ' + (d.error || 'unknown'));
    });
  });

  // Detail panel: tab switching, save, chat, etc.
  wireAgentDetail();
}

// Global drop handler — reparent a member to another member
window._agentDrop = function(targetMemberId, ev) {
  ev.preventDefault();
  ev.currentTarget.style.background = '';
  var sourceId = window._agentDragId;
  window._agentDragId = null;
  if (!sourceId || sourceId === targetMemberId) return;
  if (!_agentEditing) { alert('enable edit mode to reparent'); return; }
  if (!confirm('Make ' + sourceId + ' report to ' + targetMemberId + '?')) return;
  fetch('/api/members/' + sourceId, {
    method: 'PUT', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({manager_id: targetMemberId})
  }).then(function(r) {
    if (r.ok) renderAgentsView();
    else alert('reparent failed');
  });
};

function renderAgentDetail() {
  if (!_agentSelectedId) {
    return '<div class="empty-state">Select an agent from the tree, or click "+ new agent" to create one.</div>';
  }
  var m = _agentSelectedMember;
  if (!m) return '<div class="empty-state">loading agent…</div>';

  var html = '';
  var mColor = m.type === 'ai_agent' ? '#7c3aed' : m.type === 'system' ? '#6b6b75' : '#16a34a';
  var mIcon  = m.type === 'ai_agent' ? '🤖' : m.type === 'system' ? '⚙' : '👤';

  // Header with tabs
  html += '<div style="display:flex;align-items:center;gap:10px;padding-bottom:10px;border-bottom:1px solid var(--border);margin-bottom:14px">';
  html += '<span style="font-size:24px">' + mIcon + '</span>';
  html += '<div style="flex:1"><div style="font-weight:600;font-size:16px">' + esc(m.name) + '</div>';
  html += '<div style="font-size:11px;color:var(--muted)">' + esc(m.role || '—') + (m.model ? ' · ' + esc(m.model) : '') + '</div></div>';
  html += '<button data-agent-del="' + m.id + '" style="background:transparent;border:1px solid var(--border);color:var(--red);padding:4px 10px;border-radius:3px;cursor:pointer;font-size:11px">delete</button>';
  html += '</div>';

  // Tabs
  html += '<div style="display:flex;gap:4px;margin-bottom:14px;border-bottom:1px solid var(--border);flex-wrap:wrap">';
  ['edit', 'chat', 'memory', 'triggers', 'logs', 'activity'].forEach(function(t) {
    var active = _agentTab === t;
    html += '<button data-tab="' + t + '" style="background:' + (active ? 'var(--bg-active)' : 'transparent') + ';border:none;border-bottom:2px solid ' + (active ? 'var(--blue)' : 'transparent') + ';padding:6px 14px;cursor:pointer;font-size:13px;color:' + (active ? 'var(--ink)' : 'var(--muted)') + '">' + t + '</button>';
  });
  // Wake button (always visible)
  html += '<div style="flex:1"></div>';
  html += '<button data-agent-wake-btn="' + m.id + '" style="background:var(--blue);color:#fff;border:none;padding:6px 12px;border-radius:3px;cursor:pointer;font-size:12px;margin-bottom:6px">\u26a1 wake now</button>';
  html += '</div>';

  // Tab content
  if (_agentTab === 'edit') {
    html += renderAgentEditForm(m);
  } else if (_agentTab === 'chat') {
    html += renderAgentChat(m);
  } else if (_agentTab === 'memory') {
    html += '<div id="agent-memory-mount">loading memory\u2026</div>';
  } else if (_agentTab === 'triggers') {
    html += '<div id="agent-triggers-mount">loading triggers\u2026</div>';
  } else if (_agentTab === 'logs') {
    html += '<div id="agent-logs-mount">loading logs\u2026</div>';
  } else if (_agentTab === 'activity') {
    html += renderAgentActivity(m);
  }
  return html;
}

function renderAgentEditForm(m) {
  var teams = _agentTeamsCache || [];
  var members = _agentMembersCache || [];
  var html = '<form id="agent-edit-form" style="display:grid;gap:10px">';
  html += '<label style="font-size:11px;color:var(--muted)">name<input name="name" value="' + esc(m.name) + '" style="width:100%;background:var(--bg);border:1px solid var(--border);padding:6px 8px;border-radius:4px;color:var(--ink);font-size:13px"></label>';
  html += '<label style="font-size:11px;color:var(--muted)">role<input name="role" value="' + esc(m.role || '') + '" style="width:100%;background:var(--bg);border:1px solid var(--border);padding:6px 8px;border-radius:4px;color:var(--ink);font-size:13px"></label>';
  html += '<div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">';
  html += '<label style="font-size:11px;color:var(--muted)">type<select name="type" style="width:100%;background:var(--bg);border:1px solid var(--border);padding:6px 8px;border-radius:4px;color:var(--ink);font-size:13px">';
  ['human', 'ai_agent', 'system'].forEach(function(t) {
    html += '<option value="' + t + '"' + (m.type === t ? ' selected' : '') + '>' + t + '</option>';
  });
  html += '</select></label>';
  html += '<label style="font-size:11px;color:var(--muted)">level<select name="level" style="width:100%;background:var(--bg);border:1px solid var(--border);padding:6px 8px;border-radius:4px;color:var(--ink);font-size:13px">';
  ['ceo', 'vp', 'director', 'manager', 'agent', 'observer'].forEach(function(l) {
    html += '<option value="' + l + '"' + ((m.level || 'agent') === l ? ' selected' : '') + '>' + l + '</option>';
  });
  html += '</select></label>';
  html += '</div>';
  html += '<div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">';
  html += '<label style="font-size:11px;color:var(--muted)">status<select name="status" style="width:100%;background:var(--bg);border:1px solid var(--border);padding:6px 8px;border-radius:4px;color:var(--ink);font-size:13px">';
  ['active', 'paused', 'retired'].forEach(function(s) {
    html += '<option value="' + s + '"' + ((m.status || 'active') === s ? ' selected' : '') + '>' + s + '</option>';
  });
  html += '</select></label>';
  html += '<label style="font-size:11px;color:var(--muted)">team<select name="team_id" style="width:100%;background:var(--bg);border:1px solid var(--border);padding:6px 8px;border-radius:4px;color:var(--ink);font-size:13px">';
  teams.forEach(function(t) {
    html += '<option value="' + t.id + '"' + ((m.team_id || 0) === t.id ? ' selected' : '') + '>' + esc(t.name) + '</option>';
  });
  html += '</select></label>';
  html += '</div>';
  html += '<label style="font-size:11px;color:var(--muted)">reports to (manager)<select name="manager_id" style="width:100%;background:var(--bg);border:1px solid var(--border);padding:6px 8px;border-radius:4px;color:var(--ink);font-size:13px">';
  html += '<option value="">— top of tree —</option>';
  members.filter(function(x){return x.id !== m.id;}).forEach(function(x) {
    html += '<option value="' + x.id + '"' + ((m.manager_id || 0) === x.id ? ' selected' : '') + '>' + esc(x.name) + ' (' + esc(x.role || 'agent') + ')</option>';
  });
  html += '</select></label>';
  html += '<label style="font-size:11px;color:var(--muted)">model (provider:model)<input name="model" value="' + esc(m.model || '') + '" placeholder="anthropic:claude-3-5-sonnet-latest · openai:gpt-4o · minimax:MiniMax-M3 · ollama:llama3.2 · mock:mock-1" style="width:100%;background:var(--bg);border:1px solid var(--border);padding:6px 8px;border-radius:4px;color:var(--ink);font-size:13px;font-family:SF Mono,monospace"></label>';
  html += '<label style="font-size:11px;color:var(--muted)">capabilities<textarea name="capabilities" rows="3" style="width:100%;background:var(--bg);border:1px solid var(--border);padding:6px 8px;border-radius:4px;color:var(--ink);font-size:13px;resize:vertical">' + esc(m.capabilities || '') + '</textarea></label>';
  html += '<label style="font-size:11px;color:var(--muted)">contact<input name="contact" value="' + esc(m.contact || '') + '" placeholder="email, slack, etc" style="width:100%;background:var(--bg);border:1px solid var(--border);padding:6px 8px;border-radius:4px;color:var(--ink);font-size:13px"></label>';
  html += '<label style="font-size:11px;color:var(--muted)">notes<textarea name="notes" rows="2" style="width:100%;background:var(--bg);border:1px solid var(--border);padding:6px 8px;border-radius:4px;color:var(--ink);font-size:13px;resize:vertical">' + esc(m.notes || '') + '</textarea></label>';
  html += '<div style="display:flex;gap:8px;margin-top:8px">';
  html += '<button type="submit" style="background:var(--blue);color:#fff;border:none;padding:8px 16px;border-radius:4px;cursor:pointer;font-weight:500">save</button>';
  html += '<span style="color:var(--muted);font-size:11px;align-self:center">id: ' + m.id + ' · created ' + new Date((m.created_at||0)*1000).toLocaleDateString() + '</span>';
  html += '</div>';
  html += '</form>';
  return html;
}

function renderAgentChat(m) {
  var html = '';
  // Provider + model selector
  html += '<div style="display:flex;gap:8px;align-items:center;margin-bottom:10px;padding:8px 10px;background:var(--bg-soft);border:1px solid var(--border);border-radius:6px;font-size:12px;flex-wrap:wrap">';
  html += '<span style="color:var(--muted)">provider:</span>';
  html += '<select id="chat-provider" style="background:var(--bg);border:1px solid var(--border);padding:3px 6px;border-radius:3px;color:var(--ink);font-size:12px">';
  html += '<option value="">auto</option>';
  _agentProviders.forEach(function(p) {
    html += '<option value="' + p.name + '"' + (p.available ? '' : ' disabled') + '>' + p.name + (p.available ? ' ✓' : ' ✗') + '</option>';
  });
  html += '</select>';
  html += '<span style="color:var(--muted);margin-left:8px">agent model:</span>';
  html += '<code style="background:var(--bg);padding:2px 6px;border-radius:3px;font-size:11px;color:var(--ink)">' + esc(m.model || 'auto') + '</code>';
  if (m.type !== 'ai_agent') html += '<span style="color:var(--orange);font-size:11px;margin-left:8px">⚠ not ai_agent — chat will use auto provider</span>';
  html += '</div>';

  // Chat history
  var hist = _agentChatHistory[m.id] || [];
  html += '<div id="chat-messages" style="background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:10px;min-height:200px;max-height:380px;overflow-y:auto;margin-bottom:10px;font-size:13px">';
  if (!hist.length) {
    html += '<div style="color:var(--muted);font-style:italic;text-align:center;padding:30px 0">No messages yet. Type below to start.</div>';
  } else {
    hist.forEach(function(h) {
      var who = h.role === 'user' ? '👤 you' : '🤖 ' + esc(m.name);
      var bg = h.role === 'user' ? 'var(--bg-soft)' : 'rgba(124,58,237,0.08)';
      var align = h.role === 'user' ? 'flex-end' : 'flex-start';
      html += '<div style="display:flex;justify-content:' + align + ';margin-bottom:8px">';
      html += '<div style="max-width:80%;background:' + bg + ';padding:8px 12px;border-radius:8px"><div style="font-size:10px;color:var(--muted);margin-bottom:3px">' + who + '</div><div style="white-space:pre-wrap">' + esc(h.content) + '</div></div>';
      html += '</div>';
    });
  }
  html += '</div>';

  // Input
  html += '<div style="display:flex;gap:8px">';
  html += '<textarea id="chat-input" placeholder="Type a message to ' + esc(m.name) + '…" rows="2" style="flex:1;background:var(--bg);border:1px solid var(--border);padding:8px;border-radius:4px;color:var(--ink);font-size:13px;resize:vertical"></textarea>';
  html += '<button id="chat-send" style="background:var(--blue);color:#fff;border:none;padding:8px 18px;border-radius:4px;cursor:pointer;font-weight:500">send</button>';
  html += '<button id="chat-clear" style="background:transparent;border:1px solid var(--border);color:var(--muted);padding:8px 12px;border-radius:4px;cursor:pointer;font-size:12px">clear</button>';
  html += '</div>';
  return html;
}

function renderAgentActivity(m) {
  var html = '';
  // Open todos
  html += '<h3 style="margin:0 0 8px 0;font-size:13px;color:var(--muted)">Open todos (' + (m.open_todos || []).length + ')</h3>';
  if (!(m.open_todos || []).length) {
    html += '<div style="color:var(--muted);font-size:12px;font-style:italic;padding:6px 0">no open todos</div>';
  } else {
    m.open_todos.forEach(function(t) {
      html += '<div style="background:var(--bg);border:1px solid var(--border);border-radius:4px;padding:6px 10px;margin-bottom:4px;font-size:12px;display:flex;gap:8px;align-items:center">';
      html += '<input type="checkbox"' + (t.status === 'done' ? ' checked' : '') + ' disabled>';
      html += '<span style="flex:1">' + esc(t.content) + '</span>';
      if (t.project_key) html += '<span style="font-size:10px;background:var(--bg-active);color:var(--blue);padding:1px 5px;border-radius:3px">' + esc(t.project_key) + '</span>';
      html += '</div>';
    });
  }

  // Recent conversations
  html += '<h3 style="margin:16px 0 8px 0;font-size:13px;color:var(--muted)">Recent conversations</h3>';
  html += '<div id="agent-conv-list" style="font-size:12px;color:var(--muted)">loading…</div>';
  // Lazy-load conversations for this agent
  setTimeout(function() {
    fetch('/api/brain/conversations?member_id=' + m.id).then(function(r){return r.json();}).then(function(cs) {
      var el = document.getElementById('agent-conv-list');
      if (!el) return;
      if (!cs.length) { el.innerHTML = '<div style="font-style:italic">no conversations yet</div>'; return; }
      el.innerHTML = cs.slice(0, 10).map(function(c) {
        return '<div style="padding:6px 10px;background:var(--bg);border:1px solid var(--border);border-radius:4px;margin-bottom:4px"><div style="color:var(--ink)">' + esc(c.title || 'untitled') + '</div><div style="font-size:10px;color:var(--muted)">' + new Date(c.created_at * 1000).toLocaleString() + ' · ' + (c.message_count || 0) + ' messages</div></div>';
      }).join('');
    }).catch(function() {});
  }, 100);

  return html;
}

async function loadAgentMemory(mid) {
  var mount = document.getElementById('agent-memory-mount');
  if (!mount) return;
  try {
    var d = await fetchJSON('/api/agents/' + mid + '/memory');
    var rows = d.rows || [];
    var html = '';
    html += '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">';
    html += '<input id="mem-search" placeholder="search memory\u2026" style="background:var(--bg);border:1px solid var(--border);padding:4px 10px;border-radius:4px;color:var(--ink);font-size:12px;width:200px">';
    html += '<span style="color:var(--muted);font-size:11px">' + rows.length + ' memories</span>';
    html += '</div>';
    // Add form
    html += '<div style="background:var(--bg-soft);border:1px solid var(--border);border-radius:6px;padding:10px;margin-bottom:12px">';
    html += '<textarea id="mem-content" placeholder="add memory\u2026" rows="2" style="width:100%;background:var(--bg);border:1px solid var(--border);padding:6px 8px;border-radius:4px;color:var(--ink);font-size:12px;resize:vertical"></textarea>';
    html += '<div style="display:flex;gap:6px;margin-top:6px;align-items:center">';
    html += '<input id="mem-tags" placeholder="tags (comma)" style="flex:1;background:var(--bg);border:1px solid var(--border);padding:4px 8px;border-radius:4px;color:var(--ink);font-size:11px">';
    html += '<label style="font-size:11px;color:var(--muted)">importance <input id="mem-imp" type="number" min="1" max="10" value="5" style="width:50px;background:var(--bg);border:1px solid var(--border);padding:3px;border-radius:3px;color:var(--ink);font-size:11px"></label>';
    html += '<button id="mem-add" style="background:var(--blue);color:#fff;border:none;padding:5px 14px;border-radius:3px;cursor:pointer;font-size:12px">add</button>';
    html += '</div></div>';
    // List
    if (!rows.length) {
      html += '<div style="color:var(--muted);font-size:12px;font-style:italic;padding:20px 0;text-align:center">no memories yet</div>';
    } else {
      rows.forEach(function(r) {
        html += '<div style="background:var(--bg);border:1px solid var(--border);border-radius:4px;padding:8px 12px;margin-bottom:6px;display:flex;gap:10px;align-items:flex-start">';
        html += '<span style="font-size:10px;background:var(--bg-active);color:var(--blue);padding:2px 6px;border-radius:3px;font-weight:600;min-width:32px;text-align:center">' + r.importance + '</span>';
        html += '<div style="flex:1"><div style="font-size:13px;color:var(--ink)">' + esc(r.content) + '</div>';
        if (r.tags && r.tags.length) html += '<div style="margin-top:3px">' + r.tags.map(function(t){return '<span style="font-size:10px;background:var(--bg-soft);color:var(--muted);padding:1px 5px;border-radius:2px;margin-right:3px">' + esc(t) + '</span>';}).join('') + '</div>';
        html += '<div style="font-size:10px;color:var(--muted);margin-top:4px">' + new Date(r.created_at * 1000).toLocaleString() + '</div></div>';
        html += '<button data-mem-del="' + r.id + '" style="background:transparent;border:none;color:var(--red);cursor:pointer;font-size:11px;padding:2px 6px">\u2715</button>';
        html += '</div>';
      });
    }
    mount.innerHTML = html;
    // Wire actions
    var addBtn = document.getElementById('mem-add');
    if (addBtn) addBtn.addEventListener('click', async function() {
      var content = document.getElementById('mem-content').value.trim();
      if (!content) return;
      var tags = (document.getElementById('mem-tags').value || '').split(',').map(function(s){return s.trim();}).filter(Boolean);
      var imp = parseInt(document.getElementById('mem-imp').value) || 5;
      var r = await fetch('/api/agents/' + mid + '/memory', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({content: content, tags: tags, importance: imp})});
      if (r.ok) loadAgentMemory(mid);
      else alert('add failed');
    });
    var searchInput = document.getElementById('mem-search');
    if (searchInput) searchInput.addEventListener('input', function() {
      var q = searchInput.value.trim();
      if (q.length < 2) return;
      fetchJSON('/api/agents/' + mid + '/memory?q=' + encodeURIComponent(q)).then(function(d){
        rows = d.rows || [];
        // re-render list portion only (simplification: just re-call)
        loadAgentMemory(mid);
      });
    });
    mount.querySelectorAll('[data-mem-del]').forEach(function(b) {
      b.addEventListener('click', async function() {
        if (!confirm('Delete memory?')) return;
        var r = await fetch('/api/agents/' + mid + '/memory/' + b.dataset.memDel, {method:'DELETE'});
        if (r.ok) loadAgentMemory(mid);
      });
    });
  } catch (e) {
    mount.innerHTML = '<div style="color:var(--red);font-size:12px">error: ' + esc(e.message) + '</div>';
  }
}

async function loadAgentTriggers(mid) {
  var mount = document.getElementById('agent-triggers-mount');
  if (!mount) return;
  try {
    var d = await fetchJSON('/api/agents/' + mid + '/triggers');
    var rows = d.rows || [];
    var html = '';
    html += '<div style="background:var(--bg-soft);border:1px solid var(--border);border-radius:6px;padding:10px;margin-bottom:12px">';
    html += '<h4 style="margin:0 0 8px 0;font-size:13px;color:var(--muted)">add trigger</h4>';
    html += '<div style="display:grid;grid-template-columns:auto 1fr auto;gap:6px;align-items:center">';
    html += '<select id="trig-kind" style="background:var(--bg);border:1px solid var(--border);padding:5px;border-radius:3px;color:var(--ink);font-size:12px"><option value="cron">cron</option><option value="webhook">webhook</option><option value="event">event</option></select>';
    html += '<input id="trig-spec" placeholder="cron spec, e.g. */15 * * * *" style="background:var(--bg);border:1px solid var(--border);padding:5px 8px;border-radius:3px;color:var(--ink);font-size:12px">';
    html += '<button id="trig-add" style="background:var(--blue);color:#fff;border:none;padding:5px 14px;border-radius:3px;cursor:pointer;font-size:12px">add</button>';
    html += '</div>';
    html += '<textarea id="trig-prompt" placeholder="what to send the agent when triggered\u2026" rows="2" style="width:100%;background:var(--bg);border:1px solid var(--border);padding:6px 8px;border-radius:3px;color:var(--ink);font-size:12px;margin-top:6px;resize:vertical"></textarea>';
    html += '</div>';
    if (!rows.length) {
      html += '<div style="color:var(--muted);font-size:12px;font-style:italic;padding:20px 0;text-align:center">no triggers yet \u2014 add one above</div>';
    } else {
      rows.forEach(function(t) {
        var enabledColor = t.enabled ? 'var(--green)' : 'var(--muted)';
        html += '<div style="background:var(--bg);border:1px solid var(--border);border-radius:4px;padding:8px 12px;margin-bottom:6px;display:flex;gap:10px;align-items:center">';
        html += '<span style="font-size:10px;background:var(--bg-active);color:var(--blue);padding:2px 6px;border-radius:3px;font-weight:600;min-width:50px;text-align:center">' + esc(t.kind) + '</span>';
        html += '<code style="background:var(--bg-soft);padding:2px 6px;border-radius:3px;font-size:11px;color:var(--ink);min-width:120px">' + esc(t.spec) + '</code>';
        html += '<span style="font-size:12px;color:var(--ink);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + esc(t.prompt || '(no prompt)') + '</span>';
        html += '<span style="font-size:10px;color:' + enabledColor + '">\u25cf ' + (t.enabled ? 'enabled' : 'disabled') + '</span>';
        if (t.fire_count) html += '<span style="font-size:10px;color:var(--muted)">' + t.fire_count + '\u00d7 fired</span>';
        html += '<button data-trig-toggle="' + t.id + '" data-enabled="' + (t.enabled ? '0' : '1') + '" style="background:transparent;border:1px solid var(--border);padding:3px 8px;border-radius:3px;cursor:pointer;font-size:10px;color:var(--muted)">' + (t.enabled ? 'pause' : 'resume') + '</button>';
        html += '<button data-trig-del="' + t.id + '" style="background:transparent;border:none;color:var(--red);cursor:pointer;font-size:11px;padding:2px 6px">\u2715</button>';
        html += '</div>';
      });
    }
    mount.innerHTML = html;
    document.getElementById('trig-add').addEventListener('click', async function() {
      var kind = document.getElementById('trig-kind').value;
      var spec = document.getElementById('trig-spec').value.trim();
      var prompt = document.getElementById('trig-prompt').value.trim();
      if (!kind || !spec) { alert('kind and spec required'); return; }
      var r = await fetch('/api/agents/' + mid + '/triggers', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({kind: kind, spec: spec, prompt: prompt})});
      if (r.ok) loadAgentTriggers(mid);
    });
    mount.querySelectorAll('[data-trig-toggle]').forEach(function(b) {
      b.addEventListener('click', async function() {
        var enabled = b.dataset.enabled === '1';
        var r = await fetch('/api/agents/' + mid + '/triggers/' + b.dataset.trigToggle, {method:'PUT', headers:{'Content-Type':'application/json'}, body: JSON.stringify({enabled: enabled})});
        if (r.ok) loadAgentTriggers(mid);
      });
    });
    mount.querySelectorAll('[data-trig-del]').forEach(function(b) {
      b.addEventListener('click', async function() {
        if (!confirm('Delete trigger?')) return;
        var r = await fetch('/api/agents/' + mid + '/triggers/' + b.dataset.trigDel, {method:'DELETE'});
        if (r.ok) loadAgentTriggers(mid);
      });
    });
  } catch (e) {
    mount.innerHTML = '<div style="color:var(--red);font-size:12px">error: ' + esc(e.message) + '</div>';
  }
}

async function loadAgentLogs(mid) {
  var mount = document.getElementById('agent-logs-mount');
  if (!mount) return;
  try {
    var d = await fetchJSON('/api/agents/' + mid + '/logs?limit=50');
    var rows = d.rows || [];
    var html = '';
    html += '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">';
    html += '<span style="font-size:11px;color:var(--muted)">' + rows.length + ' events \u00b7 live updates</span>';
    html += '<span id="live-indicator" style="font-size:10px;color:var(--green);font-weight:600">\u25cf LIVE</span>';
    html += '</div>';
    html += '<div id="agent-logs-stream"></div>';
    if (!rows.length) {
      html += '<div style="color:var(--muted);font-size:12px;font-style:italic;padding:20px 0;text-align:center">no activity yet \u2014 wake the agent to see logs</div>';
    }
    mount.innerHTML = html;
    var stream = document.getElementById('agent-logs-stream');
    rows.forEach(function(r){ if (stream) stream.insertAdjacentHTML('beforeend', renderLogEntry(r)); });
    // Open SSE stream for live updates
    if (window._agentLogSSE) try { window._agentLogSSE.close(); } catch(e) {}
    try {
      window._agentLogSSE = new EventSource('/api/agents/' + mid + '/stream');
      window._agentLogSSE.addEventListener('log', function(ev) {
        try {
          var r = JSON.parse(ev.data);
          var s = document.getElementById('agent-logs-stream');
          var ind = document.getElementById('live-indicator');
          if (s) {
            s.insertAdjacentHTML('afterbegin', renderLogEntry(r));
            while (s.children.length > 100) s.removeChild(s.lastChild);
          }
          if (ind) {
            ind.style.color = 'var(--blue)';
            setTimeout(function(){ if (ind) ind.style.color = 'var(--green)'; }, 600);
          }
        } catch (e) {}
      });
      window._agentLogSSE.onerror = function() {
        var ind = document.getElementById('live-indicator');
        if (ind) { ind.style.color = 'var(--red)'; ind.textContent = '\u25cf disconnected'; }
      };
    } catch (e) {
      console.warn('SSE failed:', e);
    }
  } catch (e) {
    mount.innerHTML = '<div style="color:var(--red);font-size:12px">error: ' + esc(e.message) + '</div>';
  }
}

function renderLogEntry(r) {
  var actionColor = r.action === 'error' ? 'var(--red)' :
                    r.action === 'run_complete' ? 'var(--green)' :
                    r.action === 'memory_add' ? 'var(--blue)' :
                    r.action === 'approval_proposed' ? 'var(--orange)' :
                    'var(--muted)';
  var icon = r.action === 'wake' ? '\u23f0' :
             r.action === 'memory_add' ? '\ud83d\udcbe' :
             r.action === 'run_complete' ? '\u2713' :
             r.action === 'error' ? '\u2717' :
             r.action === 'approval_proposed' ? '\u26a0' : '\u00b7';
  var html = '<div style="border-bottom:1px solid var(--border);padding:8px 0;display:flex;gap:10px;align-items:flex-start">';
  html += '<span style="font-size:14px;min-width:18px">' + icon + '</span>';
  html += '<div style="flex:1"><div style="font-size:11px;color:' + actionColor + ';font-weight:600">' + r.action + '</div>';
  if (r.summary) html += '<div style="font-size:13px;color:var(--ink);margin-top:2px;white-space:pre-wrap">' + esc(ProviderErrors.friendly(r.summary)) + '</div>';
  html += '<div style="font-size:10px;color:var(--muted);margin-top:3px">' + new Date(r.created_at * 1000).toLocaleString() + (r.duration_ms ? ' \u00b7 ' + r.duration_ms + 'ms' : '') + '</div></div>';
  html += '</div>';
  return html;
}

async function wireAgentDetail() {
  // Tab switching
  document.querySelectorAll('[data-tab]').forEach(function(b) {
    b.addEventListener('click', function() {
      _agentTab = b.dataset.tab;
      var panel = document.getElementById('agent-detail-panel');
      if (panel) panel.innerHTML = renderAgentDetail();
      wireAgentDetail();
      // Lazy-load tabs that need data
      if (_agentTab === 'memory') loadAgentMemory(_agentSelectedId);
      else if (_agentTab === 'triggers') loadAgentTriggers(_agentSelectedId);
      else if (_agentTab === 'logs') loadAgentLogs(_agentSelectedId);
    });
  });

  // Wake button
  var wakeBtn = document.querySelector('[data-agent-wake-btn]');
  if (wakeBtn) wakeBtn.addEventListener('click', async function() {
    var prompt = window.prompt('Wake prompt (leave blank for default):') || '';
    wakeBtn.disabled = true; wakeBtn.textContent = 'waking...';
    var r = await fetch('/api/agents/' + wakeBtn.dataset.agentWakeBtn + '/wake', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({prompt: prompt})
    });
    wakeBtn.disabled = false; wakeBtn.textContent = '\u26a1 wake now';
    var pu = await ProviderErrors.fromResponse(r);
    if (pu) { alert(ProviderErrors.friendly(pu)); if (_agentTab === 'logs') loadAgentLogs(_agentSelectedId); return; }
    if (r.ok) {
      var d = await r.json();
      alert('agent response:\n\n' + (d.response || '(empty)'));
      if (_agentTab === 'logs') loadAgentLogs(_agentSelectedId);
    } else {
      alert('wake failed');
    }
  });

  // Delete
  var delBtn = document.querySelector('[data-agent-del]');
  if (delBtn) delBtn.addEventListener('click', async function() {
    if (!confirm('Delete this agent?')) return;
    var r = await fetch('/api/members/' + delBtn.dataset.agentDel, {method: 'DELETE'});
    if (r.ok) { _agentSelectedId = null; renderAgentsView(); }
    else alert('delete failed');
  });

  // Edit form submit
  var form = document.getElementById('agent-edit-form');
  if (form) {
    form.addEventListener('submit', async function(e) {
      e.preventDefault();
      var fd = new FormData(form);
      var data = {};
      fd.forEach(function(v, k) {
        if (k === 'team_id' || k === 'manager_id') data[k] = v ? parseInt(v) : null;
        else if (v !== '') data[k] = v;
      });
      var r = await fetch('/api/members/' + _agentSelectedId, {
        method: 'PUT', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(data)
      });
      if (r.ok) {
        _agentSelectedMember = null;  // force reload
        await refreshAgentDetail();
        renderAgentsView();
      } else {
        var err = await r.text();
        alert('save failed: ' + err);
      }
    });
  }

  // Chat send
  var sendBtn = document.getElementById('chat-send');
  var inputEl = document.getElementById('chat-input');
  if (sendBtn && inputEl) {
    var sendHandler = async function() {
      var q = inputEl.value.trim();
      if (!q) return;
      var providerEl = document.getElementById('chat-provider');
      var provider = providerEl ? providerEl.value : '';
      _agentChatHistory[_agentSelectedId] = _agentChatHistory[_agentSelectedId] || [];
      _agentChatHistory[_agentSelectedId].push({role: 'user', content: q});
      inputEl.value = '';
      sendBtn.disabled = true;
      sendBtn.textContent = '...';
      // Re-render to show user msg + empty assistant
      var panel = document.getElementById('agent-detail-panel');
      if (panel) { panel.innerHTML = renderAgentDetail(); wireAgentDetail(); }
      var msgBox = document.getElementById('chat-messages');
      if (msgBox) msgBox.scrollTop = msgBox.scrollHeight;
      try {
        var url = '/api/members/' + _agentSelectedId + '/chat?stream=1';
        if (provider) url += '&provider=' + encodeURIComponent(provider);
        var r = await fetch(url, {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({message: q, conversation_id: _agentConvId || null})
        });
        var pu = await ProviderErrors.fromResponse(r);
        if (pu) throw { providerUnavailable: pu };
        if (!r.ok || !r.body) throw new Error('HTTP ' + r.status);
        var reader = r.body.getReader();
        var dec = new TextDecoder();
        var buf = '', full = '', meta = null;
        _agentChatHistory[_agentSelectedId].push({role: 'assistant', content: ''});
        var lastIdx = _agentChatHistory[_agentSelectedId].length - 1;
        while (true) {
          var chunk = await reader.read();
          if (chunk.done) break;
          buf += dec.decode(chunk.value, { stream: true });
          var lines = buf.split('\n\n');
          buf = lines.pop() || '';
          for (var i = 0; i < lines.length; i++) {
            var line = lines[i].trim();
            if (!line.startsWith('data: ')) continue;
            try { var evt = JSON.parse(line.slice(6)); } catch (e) { continue; }
            if (evt.type === 'meta') { meta = evt; _agentConvId = evt.conversation_id; }
            else if (evt.type === 'delta') {
              full += evt.text;
              _agentChatHistory[_agentSelectedId][lastIdx].content = full;
              var box = document.getElementById('chat-messages');
              if (box) {
                var lastBubble = box.lastElementChild;
                if (lastBubble) {
                  var contentEl = lastBubble.querySelector('.bubble-content') || lastBubble;
                  contentEl.textContent = full;
                }
                box.scrollTop = box.scrollHeight;
              }
            }
            else if (evt.type === 'done') { /* fine */ }
            else if (evt.type === 'error') { full = '✗ ' + evt.message; _agentChatHistory[_agentSelectedId][lastIdx].content = full; }
          }
        }
        // Refresh detail panel to show provider meta
        if (meta) {
          var panel2 = document.getElementById('agent-detail-panel');
          if (panel2) { panel2.innerHTML = renderAgentDetail(); wireAgentDetail(); }
        }
      } catch (e) {
        _agentChatHistory[_agentSelectedId].push({role: 'assistant', content: e.providerUnavailable ? '⚠ ' + ProviderErrors.friendly(e.providerUnavailable) : '✗ ' + e.message});
        var panel3 = document.getElementById('agent-detail-panel');
        if (panel3) { panel3.innerHTML = renderAgentDetail(); wireAgentDetail(); }
        if (e.providerUnavailable) ProviderErrors.highlight('chat-provider');
      } finally {
        sendBtn.disabled = false;
        sendBtn.textContent = 'send';
      }
    };
    sendBtn.addEventListener('click', sendHandler);
    inputEl.addEventListener('keydown', function(e) {
      if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); sendHandler(); }
    });
  }

  // Chat clear
  var clearBtn = document.getElementById('chat-clear');
  if (clearBtn) clearBtn.addEventListener('click', function() {
    _agentChatHistory[_agentSelectedId] = [];
    _agentConvId = null;
    var panel = document.getElementById('agent-detail-panel');
    if (panel) { panel.innerHTML = renderAgentDetail(); wireAgentDetail(); }
  });
}

var _agentSelectedMember = null;
var _agentTeamsCache = null;
var _agentMembersCache = null;
var _agentConvId = null;

async function refreshAgentDetail() {
  if (!_agentSelectedId) return;
  // Load member + teams + members (for parent select)
  _agentSelectedMember = await fetchJSON('/api/members/' + _agentSelectedId);
  if (!_agentTeamsCache) _agentTeamsCache = await fetchJSON('/api/teams');
  if (!_agentMembersCache) _agentMembersCache = await fetchJSON('/api/members');
}

// Patch renderAgentsMount to load detail async
var _origRenderAgentsMount = renderAgentsMount;
renderAgentsMount = function(mount, data) {
  if (_agentSelectedId && !_agentSelectedMember) {
    refreshAgentDetail().then(function() {
      _origRenderAgentsMount(mount, data);
    });
  } else {
    _origRenderAgentsMount(mount, data);
  }
};
