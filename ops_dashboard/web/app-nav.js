// The app's sidebar, on every page.
//
// ConductLoop is one macOS window, so every page carries the same source list
// on the left: the dashboard's views and the pages that live on their own
// (/chat, /sessions, /boardroom…). This file is the one place the list is
// written. It also carries the line icons (an SVG sprite, SF Symbols-like)
// that the sidebar and the rest of the UI draw with <use href="#i-…">.
//
// On the dashboard (index.html) it fills the empty <aside id="activity-bar">
// and app.js wires it (views switch in place, the pin toggle). On any other
// page it adds the sidebar itself; a view entry is then a link to
// /?view=<name>, and the pin toggle is wired here. Labels carry data-i18n
// keys of the shared dictionary, which i18n.js translates on every page.
//
// Load it synchronously as early as possible in <body> (after appearance.js)
// so the page never draws without it.
(function () {
  'use strict';

  var SPRITE = [
    ['now', '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>'],
    ['briefing', '<rect x="4" y="3" width="16" height="18" rx="2.5"/><path d="M8 8h8M8 12h8M8 16h5"/>'],
    ['activity', '<path d="M3 12h4l3-8 4 16 3-8h4"/>'],
    ['calendar', '<rect x="3" y="5" width="18" height="16" rx="2.5"/><path d="M3 10h18M8 3v4M16 3v4"/>'],
    ['mic', '<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/>'],
    ['approve', '<circle cx="12" cy="12" r="9"/><path d="m8 12 3 3 5-6"/>'],
    ['folder', '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>'],
    ['sparkle', '<path d="M11 3l1.8 5.2L18 10l-5.2 1.8L11 17l-1.8-5.2L4 10l5.2-1.8z"/><path d="M18.5 15l.7 1.8 1.8.7-1.8.7-.7 1.8-.7-1.8-1.8-.7 1.8-.7z"/>'],
    ['graph', '<circle cx="6" cy="12" r="2.5"/><circle cx="18" cy="6" r="2.5"/><circle cx="18" cy="18" r="2.5"/><path d="m8.2 10.8 7.6-3.6M8.2 13.2l7.6 3.6"/>'],
    ['code', '<path d="m8 8-4 4 4 4M16 8l4 4-4 4M13.5 5l-3 14"/>'],
    ['bolt', '<path d="M13 3 5 14h6l-1 7 8-11h-6z"/>'],
    ['flag', '<path d="M5 21V4h11l-2 4 2 4H5"/>'],
    ['team', '<circle cx="9" cy="8" r="3.2"/><path d="M3 20a6 6 0 0 1 12 0"/><circle cx="17" cy="9" r="2.5"/><path d="M16.5 14a5 5 0 0 1 4.5 5"/>'],
    ['tasks', '<path d="m4 6 1.5 1.5L8 5M4 12l1.5 1.5L8 11M4 18l1.5 1.5L8 17M11 6h9M11 12h9M11 18h9"/>'],
    ['agent', '<rect x="4" y="8" width="16" height="12" rx="3.5"/><path d="M12 4v4M9.5 13v2M14.5 13v2"/>'],
    ['link', '<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1"/><path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/>'],
    ['template', '<rect x="3" y="3" width="18" height="18" rx="2.5"/><path d="M3 9h18M9 9v12"/>'],
    ['org', '<rect x="9" y="3" width="6" height="5" rx="1.2"/><rect x="3" y="16" width="6" height="5" rx="1.2"/><rect x="15" y="16" width="6" height="5" rx="1.2"/><path d="M12 8v4M6 16v-4h12v4"/>'],
    ['tree', '<path d="M5 3v13a2 2 0 0 0 2 2h4M5 8h6"/><rect x="13" y="5" width="8" height="6" rx="1.5"/><rect x="13" y="15" width="8" height="6" rx="1.5"/>'],
    ['chat', '<path d="M20 15a2 2 0 0 1-2 2H8l-4 4V6a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2z"/>'],
    ['sessions', '<path d="M14 9a2 2 0 0 1-2 2H6l-3 3V5a2 2 0 0 1 2-2h7a2 2 0 0 1 2 2z"/><path d="M18 9h1a2 2 0 0 1 2 2v10l-3-3h-6a2 2 0 0 1-2-2v-1"/>'],
    ['runs', '<path d="M3 12a9 9 0 0 1 15.5-6.2L21 8M21 3v5h-5M21 12a9 9 0 0 1-15.5 6.2L3 16M3 21v-5h5"/>'],
    ['book', '<path d="M3 5a2 2 0 0 1 2-2h5a2 2 0 0 1 2 2v16a2 2 0 0 0-2-2H3zM21 5a2 2 0 0 0-2-2h-5a2 2 0 0 0-2 2v16a2 2 0 0 1 2-2h7z"/>'],
    ['plug', '<path d="M9 3v5M15 3v5M6 8h12v3a6 6 0 0 1-12 0zM12 17v4"/>'],
    ['server', '<rect x="3" y="4" width="18" height="7" rx="2"/><rect x="3" y="13" width="18" height="7" rx="2"/><path d="M7 7.5h.01M7 16.5h.01"/>'],
    ['key', '<circle cx="8" cy="15" r="4"/><path d="m11 12 9-9M17 6l3 3M14 9l2 2"/>'],
    ['photo', '<rect x="3" y="4" width="18" height="16" rx="2.5"/><circle cx="9" cy="10" r="2"/><path d="m21 16-5-5-9 9"/>'],
    ['globe', '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/>'],
    ['gear', '<circle cx="12" cy="12" r="3"/><path d="M12 2.5v3M12 18.5v3M2.5 12h3M18.5 12h3M5.3 5.3l2.1 2.1M16.6 16.6l2.1 2.1M5.3 18.7l2.1-2.1M16.6 7.4l2.1-2.1"/>'],
    ['landmark', '<path d="M3 21h18M5 21v-10M9.5 21v-10M14.5 21v-10M19 21v-10M2.5 10h19L12 3.5z"/>'],
    ['person', '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>'],
    ['archive', '<rect x="3" y="4" width="18" height="5" rx="1.2"/><path d="M5 9v10a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V9M10 13h4"/>'],
    ['briefcase', '<rect x="3" y="7" width="18" height="13" rx="2.5"/><path d="M9 7V5a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2v2M3 13h18"/>'],
    ['chart', '<path d="M5 20V11M11 20V5M17 20v-6M3 20h18"/>'],
    ['plus', '<path d="M12 5v14M5 12h14"/>'],
    ['card', '<rect x="2.5" y="5" width="19" height="14" rx="2.5"/><path d="M2.5 10h19M6 15h4"/>'],
    ['trend', '<path d="m3 17 6-6 4 4 8-8M15 7h6v6"/>'],
    ['search', '<circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/>'],
    ['branch', '<circle cx="6" cy="5" r="2"/><circle cx="6" cy="19" r="2"/><circle cx="18" cy="7" r="2"/><path d="M6 7v10M18 9c0 4-3 6-6 6H8"/>'],
    ['alert', '<path d="M12 3.5 2.5 20h19z"/><path d="M12 10v4M12 17h.01"/>'],
    ['appearance', '<circle cx="12" cy="12" r="9"/><path d="M12 3a9 9 0 0 1 0 18z" fill="currentColor"/>'],
    ['sidebar', '<rect x="3" y="4" width="18" height="16" rx="2.5"/><path d="M9 4v16"/>'],
    ['inspector', '<rect x="3" y="4" width="18" height="16" rx="2.5"/><path d="M15 4v16"/>'],
    ['back', '<path d="m15 18-6-6 6-6"/>'],
    ['doc', '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/>'],
    ['send', '<path d="M21 3 3 10.5l7.5 3L14 21z"/><path d="m10.5 13.5 4-4"/>']
  ];

  // [section key, English label, section icon, items]. An item is
  // ['view', name, key, label, icon, extra] — a view of the dashboard — or
  // ['link', href, key, label, icon, extra] — a page of its own.
  // extra: {badge: id, id: id, hidden: true, action: true, hint: '⌘1'}.
  var SECTIONS = [
    ['sidebar.section_day', 'Day', 'now', [
      ['view', 'now', 'sidebar.now', 'Now', 'now', { badge: 'now-badge' }],
      ['view', 'briefing', 'sidebar.briefing', 'Daily briefing', 'briefing', { hint: '⌘4' }],
      ['view', 'activity', 'sidebar.activity', 'Activity', 'activity', { hint: '⌘5' }],
      ['view', 'calendar', 'sidebar.calendar', 'Calendar', 'calendar', { hint: '⌘7' }],
      ['view', 'voice', 'sidebar.voice', 'Voice notes', 'mic'],
      ['view', 'approvals', 'sidebar.approvals', 'Approvals', 'approve', { badge: 'approvals-badge' }]
    ]],
    ['sidebar.section_team', 'Team', 'team', [
      ['link', '/chat', 'nav.chat', 'Chat', 'chat'],
      ['view', 'agents', 'sidebar.agents_panel', 'Agent panel', 'agent', { hint: '⇧A' }],
      ['link', '/agents', 'nav.agents', 'Agents', 'sparkle'],
      ['link', '/sessions', 'nav.sessions', 'Sessions', 'sessions'],
      ['link', '/runs', 'nav.runs', 'Runs', 'runs'],
      ['view', 'teams', 'sidebar.teams', 'Teams', 'team', { hint: '⌘8' }],
      ['view', 'tasks', 'sidebar.tasks', 'Tasks', 'tasks', { hint: '⌘9' }],
      ['view', 'orgchart', 'sidebar.orgchart', 'Org chart', 'org', { hint: '⌘0' }],
      ['view', 'hierarchy', 'sidebar.hierarchy', 'Hierarchy', 'tree', { hint: '⌘-' }],
      ['view', 'connections', 'sidebar.connections', 'Connections', 'link'],
      ['view', 'templates', 'sidebar.templates', 'Templates', 'template']
    ]],
    ['sidebar.section_search', 'Search', 'search', [
      ['view', 'files', 'sidebar.files', 'Files', 'folder', { hint: '⌘1' }],
      ['view', 'brain', 'sidebar.brain', 'Brain', 'sparkle', { hint: '⌘3' }],
      ['view', 'graph', 'sidebar.graph', 'Graph', 'graph', { hint: '⌘2' }],
      ['view', 'skills', 'sidebar.skills', 'Skills', 'bolt', { hint: '⌘6' }],
      ['view', 'decisions', 'sidebar.decisions', 'Decisions', 'flag'],
      ['view', 'todos', 'sidebar.code_todos', 'Code TODOs', 'code', { hint: '⌘=' }],
      ['link', '/learning', 'nav.learning', 'Learning', 'book']
    ]],
    ['sidebar.section_boardroom', 'Boardroom', 'landmark', [
      ['link', '/boardroom', 'sidebar.boardroom', 'Boardroom', 'landmark', { badge: 'boardroom-badge' }],
      ['link', '/boardroom/directory', 'sidebar.directory', 'Directory', 'person'],
      ['link', '/boardroom/vault', 'sidebar.vault', 'Vault', 'archive'],
      ['link', '/boardroom/portfolio', 'sidebar.portfolio', 'Portfolio', 'briefcase'],
      ['link', '/boardroom/recap', 'sidebar.daily_recap', 'Daily Recap', 'chart'],
      ['link', '/boardroom/create', 'sidebar.new_ceo', 'New CEO', 'plus', { action: true }]
    ]],
    ['sidebar.section_business', 'Business', 'trend', [
      ['view', 'costs', 'sidebar.costs', 'Costs', 'card'],
      ['view', 'revenue', 'sidebar.revenue', 'Revenue', 'trend'],
      ['view', 'ga', 'sidebar.ga4_traffic', 'GA4 traffic', 'chart'],
      ['view', 'gsc', 'sidebar.gsc', 'Search Console', 'search'],
      ['view', 'github', 'sidebar.github', 'GitHub', 'branch'],
      ['view', 'sentry', 'sidebar.sentry', 'Sentry', 'alert']
    ]],
    ['sidebar.settings', 'Settings', 'gear', [
      ['link', '/setup', 'nav.setup', 'Setup', 'approve'],
      ['link', '/ai', 'nav.connect_ai', 'Connect AI', 'sparkle'],
      ['link', '/connectors', 'nav.connectors', 'Connectors', 'plug'],
      ['link', '/channels', 'nav.channels', 'Channels', 'send'],
      ['link', '/servers', 'nav.servers', 'Servers', 'server'],
      ['link', '/credentials', 'nav.credentials', 'Credentials', 'key'],
      ['link', '/media', 'nav.media', 'Media', 'photo'],
      ['link', '/browser', 'nav.browser', 'Browser', 'globe', { id: 'nav-browser', hidden: true }],
      ['link', '/jobs', 'nav.jobs', 'Jobs', 'gear']
    ]]
  ];

  // Pages that are not part of the app window stand on their own: the
  // first-run guide, the tour, device pairing, and the in-app browser (its
  // tabs are native views laid out against the window edges).
  var STANDALONE = ['/welcome', '/tour', '/pair', '/browser'];

  function esc(s) {
    return String(s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }

  function icon(name) {
    return '<svg class="cl-icon" aria-hidden="true"><use href="#i-' + name + '"/></svg>';
  }

  function addSprite() {
    if (document.getElementById('cl-sprite')) return;
    var svg = '<svg id="cl-sprite" class="cl-sprite" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">' +
      SPRITE.map(function (s) {
        return '<symbol id="i-' + s[0] + '" viewBox="0 0 24 24">' + s[1] + '</symbol>';
      }).join('') + '</svg>';
    document.body.insertAdjacentHTML('afterbegin', svg);
  }

  function item(it, onDashboard) {
    var kind = it[0], target = it[1], key = it[2], label = it[3], ic = it[4], x = it[5] || {};
    var tail = x.badge
      ? ' <span id="' + x.badge + '" class="item-hint nav-badge"></span>'
      : ' <span class="item-hint">' + esc(x.hint || '') + '</span>';
    var text = icon(ic) + '<span class="nav-text" data-i18n="' + key + '">' + esc(label) + '</span>' + tail;
    var attrs = (x.id ? ' id="' + x.id + '"' : '') + (x.hidden ? ' hidden' : '');
    if (kind === 'view' && onDashboard) {
      return '<button data-view="' + target + '"' + attrs + '>' + text + '</button>';
    }
    var href = kind === 'view' ? '/?view=' + target : target;
    var cls = 'nav-link' + (x.action ? ' nav-link--action' : '');
    return '<a class="' + cls + '" href="' + href + '" data-nav-path="' + (kind === 'view' ? '' : target) + '"' + attrs + '>' + text + '</a>';
  }

  function render(aside, onDashboard) {
    var html = '<div class="activity-bar-head">' +
      '<button class="activity-bar-pin" id="activity-bar-pin" title="Pin / unpin the activity bar"' +
      ' data-i18n-title="common.pin_activity_bar" aria-label="Pin / unpin the activity bar"' +
      ' data-i18n-label="common.pin_activity_bar">' + icon('sidebar') + '</button></div>';
    SECTIONS.forEach(function (s) {
      html += '<div class="nav-section"><div class="nav-section-label">' + icon(s[2]) +
        '<span data-i18n="' + s[0] + '">' + esc(s[1]) + '</span></div><div class="nav-section-items">' +
        s[3].map(function (it) { return item(it, onDashboard); }).join('') + '</div></div>';
    });
    html += '<div class="spacer"></div>';
    if (onDashboard) html += '<button id="install-btn" class="activity-bar-tool" hidden title="Install as app">i</button>';
    html += '<button id="theme-toggle" class="activity-bar-tool" title="Toggle theme (Ctrl/Cmd+J)" aria-label="Toggle theme"' +
      ' data-i18n-title="common.toggle_theme" data-i18n-label="common.toggle_theme">' + icon('appearance') + '</button>';
    aside.innerHTML = html;
  }

  function pinned() {
    try { return localStorage.getItem('ops-bar-pinned') !== '0'; } catch (e) { return true; }
  }

  function markActivePage(aside) {
    var path = window.location.pathname.replace(/\/+$/, '') || '/';
    var best = null;
    aside.querySelectorAll('a[data-nav-path]').forEach(function (a) {
      var p = a.getAttribute('data-nav-path');
      if (!p) return;
      // The longest path that contains this page wins: /boardroom/vault
      // marks Vault, a thread under /boardroom marks Boardroom.
      if ((path === p || path.indexOf(p + '/') === 0) && (!best || p.length > best.getAttribute('data-nav-path').length)) best = a;
    });
    if (best) best.classList.add('active');
  }

  function showBrowserEntry() {
    var el = document.getElementById('nav-browser');
    if (!el || !window.__TAURI_INTERNALS__) return;
    el.hidden = false;
    // Only macOS has the in-app browser: hide the entry when the server says no.
    fetch('/api/platform', { credentials: 'same-origin' }).then(function (r) { return r.json(); }).then(function (p) {
      if (p && p.browser && p.browser.available === false) el.hidden = true;
    }).catch(function () {});
  }

  function init() {
    var path = window.location.pathname;
    if (STANDALONE.some(function (p) { return path === p || path.indexOf(p + '/') === 0; })) return;
    addSprite();
    var aside = document.getElementById('activity-bar');
    var onDashboard = !!aside;  // index.html ships the empty <aside>
    if (!aside) {
      aside = document.createElement('aside');
      aside.className = 'activity-bar app-nav';
      aside.id = 'activity-bar';
      aside.setAttribute('aria-label', 'ConductLoop');
      document.body.insertBefore(aside, document.body.firstChild);
    }
    render(aside, onDashboard);
    aside.classList.toggle('pinned', pinned());
    if (onDashboard) return;  // app.js wires the dashboard's own sidebar

    document.body.classList.add('has-app-nav');
    markActivePage(aside);
    showBrowserEntry();
    document.getElementById('activity-bar-pin').addEventListener('click', function () {
      var next = !aside.classList.contains('pinned');
      aside.classList.toggle('pinned', next);
      try { localStorage.setItem('ops-bar-pinned', next ? '1' : '0'); } catch (e) { /* storage off */ }
    });
    document.getElementById('theme-toggle').addEventListener('click', function () {
      if (window.clAppearance) window.clAppearance.toggle();
    });
    // The row of links some pages carry at the top (#controls, .nav) only
    // repeats the sidebar now: hide the links the sidebar already has.
    var inSidebar = { '/': true };
    aside.querySelectorAll('a[data-nav-path]').forEach(function (a) { inSidebar[a.getAttribute('data-nav-path')] = true; });
    function hideRepeats() {
      document.querySelectorAll('#controls a[href], .nav a[href]').forEach(function (a) {
        if (aside.contains(a)) return;
        var path = (a.getAttribute('href') || '').replace(/[?#].*$/, '').replace(/(.)\/+$/, '$1');
        if (inSidebar[path]) a.classList.add('is-in-sidebar');
      });
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', hideRepeats);
    else hideRepeats();
  }

  init();
})();
