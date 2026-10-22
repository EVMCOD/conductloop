// Boardroom i18n shim — loads /i18n ops-dashboard i18n.js, then patches the
// current page's nav links + titles + buttons to use boardroom.* keys.
//
// Falls back silently: if a key is missing in the user's locale, the original
// hardcoded English text remains. This way the feature is opt-in for each HTML.
//
// Usage: include <script src="boardroom-i18n.js" defer></script> at the bottom
// of any boardroom-*.html before its own script.
(function() {
  function $(sel) { return document.querySelectorAll(sel); }
  function t(str, key) {
    const fn = window._i18n && window._i18n.t;
    if (!fn) return str;
    try { return fn(key) || str; } catch (_) { return str; }
  }
  async function apply() {
    // wait for ops-dashboard's i18n.js to define _i18n
    let waited = 0;
    while (!window._i18n && waited < 1500) {
      await new Promise(r => setTimeout(r, 50));
      waited += 50;
    }
    if (!window._i18n) {
      try {
        const m = await fetch('/api/i18n/en.json').then(r => r.json());
        window._i18n = { t(k) { return k.split('.').reduce((o, p) => o && o[p], m) || undefined; } };
      } catch (_) { return; }
    }
    try { await window._i18n.init && window._i18n.init(); } catch (_) {}

    const set = (sel, key) => {
      const n = document.querySelector(sel);
      if (!n) return;
      const cur = (n.textContent || '').trim();
      const translated = t(cur, key);
      if (translated && translated !== cur) n.textContent = translated;
    };

    // 1. Honors data-i18n-key attribute (preferred path, controlled by HTML)
    let dataKeyCount = 0;
    document.querySelectorAll('[data-i18n-key]').forEach(n => {
      const key = n.getAttribute('data-i18n-key');
      if (!key) return;
      const cur = (n.textContent || '').trim();
      const translated = t(cur, key);
      if (translated && translated !== cur) {
        n.textContent = translated;
        dataKeyCount++;
      }
    });
    if (window._i18n && window._i18n.debug) console.log('[boardroom-i18n] applied', dataKeyCount, 'data-i18n-key swaps');

    // Title attributes (each <a> in .nav)
    const navMap = [
      ['/boardroom',         'boardroom.nav_inbox'],
      ['/boardroom/directory',  'boardroom.nav_directory'],
      ['/boardroom/vault',    'boardroom.nav_vault'],
      ['/boardroom/portfolio','boardroom.nav_portfolio'],
      ['/boardroom/recap',    'boardroom.nav_recap'],
      ['/boardroom/create',   'boardroom.nav_create'],
    ];
    document.querySelectorAll('.nav a').forEach(a => {
      const href = a.getAttribute('href') || '';
      for (const [match, key] of navMap) {
        if (href === match || (href.startsWith(match) && match !== '/boardroom')) {
          const cur = (a.textContent || '').trim();
          const translated = t(cur, key);
          if (translated) a.textContent = translated;
          break;
        }
      }
    });
    // Page-level title
    const h1 = document.querySelector('h1');
    if (h1) {
      const map = {
        'Boardroom': 'boardroom.inbox_title',
        'Daily Recap': 'boardroom.recap_title',
        'Create New CEO': 'boardroom.create_title',
        'Agents Directory': 'boardroom.directory_title',
        'Vault': 'boardroom.vault_title',
        'Autonomy': 'boardroom.autonomy_title',
        'Portfolio': 'boardroom.portfolio_title',
      };
      for (const k of Object.keys(map)) {
        if (h1.textContent.includes(k)) {
          const x = t(k, map[k]);
          if (x) h1.textContent = h1.textContent.replace(k, x);
          break;
        }
      }
    }
    // Add a small locale picker in the topbar if there's a .topbar
    const topbar = document.querySelector('.topbar') || document.querySelector('.head');
    if (topbar && !document.getElementById('localePicker')) {
      try {
        const cur = window._i18n.getLocale ? window._i18n.getLocale() : (localStorage.getItem('ops-dashboard.locale') || 'en');
        const picker = document.createElement('select');
        picker.id = 'localePicker';
        picker.style.cssText = 'background:#0b0d12;border:1px solid var(--border);color:var(--ink);border-radius:7px;padding:5px 8px;font-size:12px;margin-left:8px';
        picker.innerHTML = '<option value="en">EN</option><option value="es">ES</option>';
        picker.value = cur;
        picker.onchange = () => {
          localStorage.setItem('ops-dashboard.locale', picker.value);
          location.reload();
        };
        const actions = topbar.querySelector('.actions') || topbar;
        actions.appendChild(picker);
      } catch (_) {}
    }
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => {
      if (!window._i18n) {
        const s = document.createElement('script');
        s.src = '/i18n.js';
        s.onload = apply;
        document.head.appendChild(s);
      } else {
        apply();
      }
    });
  } else {
    if (!window._i18n) {
      const s = document.createElement('script');
      s.src = '/i18n.js';
      s.onload = apply;
      document.head.appendChild(s);
    } else {
      apply();
    }
  }
})();
