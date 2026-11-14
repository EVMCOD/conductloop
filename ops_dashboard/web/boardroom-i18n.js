// Boardroom i18n — the app's language, for every boardroom page.
//
// Each boardroom page asks for its own dictionaries with its script tag:
//
//   <script src="/boardroom-i18n.js" data-i18n-pages="boardroom boardroom-thread" defer></script>
//
// This shim loads /i18n.js with those dictionaries (the "boardroom" one holds
// the navigation every page shares), waits for the words — they travel over
// the network — and mounts the language picker.
//
// The page uses it like this:
//
//   BoardroomI18n.ready(fn)   → fn() when the dictionary is here (or soon)
//   BoardroomI18n.t(key, en)  → the word in the chosen language, English if not
//   BoardroomI18n.onChange(fn) → fn() when the person changes the language
//
// The static words of the page are marked with data-i18n / data-i18n-ph /
// data-i18n-title / data-i18n-label: i18n.js translates them itself.
(function () {
  'use strict';

  var SCRIPT = document.currentScript;
  var PAGES = (SCRIPT && SCRIPT.getAttribute('data-i18n-pages')) || '';

  var resolveReady = null;
  var readyPromise = new Promise(function (res) { resolveReady = res; });
  var changeFns = [];
  var settled = false;

  function settle() {
    if (settled) return;
    settled = true;
    resolveReady();
  }

  // The chosen language, or the English text written in the page when the
  // dictionary has not arrived (or is not there at all).
  function t(key, fallback, vars) {
    var i18n = window._i18n;
    if (i18n && typeof i18n.t === 'function') {
      var v = i18n.t(key, vars);
      if (v && v !== key) return v;
    }
    if (fallback === undefined) return key;
    return vars ? fallback.replace(/\{(\w+)\}/g, function (_, k) {
      return vars[k] !== undefined ? String(vars[k]) : '{' + k + '}';
    }) : fallback;
  }

  function loadI18n() {
    if (window._i18n) { whenReady(); return; }
    var s = document.createElement('script');
    s.src = '/i18n.js';
    if (PAGES) s.setAttribute('data-i18n-pages', PAGES);
    s.onload = whenReady;
    s.onerror = settle;
    document.head.appendChild(s);
  }

  function whenReady() {
    var i18n = window._i18n;
    if (!i18n || typeof i18n.ready !== 'function') { settle(); return; }
    i18n.ready().then(function () {
      settle();
      if (typeof i18n.onLocaleChange === 'function') {
        i18n.onLocaleChange(function () {
          changeFns.forEach(function (fn) { try { fn(); } catch (e) { /* keep going */ } });
          if (picker) picker.update();
        });
      }
      if (picker) picker.update();
    });
  }

  // The language picker: the same control the rest of the app has. It changes
  // the language of the app (the server is told too) and the page repaints.
  var picker = null;
  function mountPicker() {
    if (document.getElementById('localePicker')) return;
    var host = document.querySelector('.topbar .actions') || document.querySelector('.topbar')
             || document.querySelector('.head') || document.body;
    var sel = document.createElement('select');
    sel.id = 'localePicker';
    sel.className = 'locale-picker';
    sel.style.cssText = 'background:var(--cl-bg-2,#ececf0);border:0;color:var(--cl-ink,#1d1d1f);border-radius:7px;padding:4px 8px;font:inherit;font-size:12px;margin-left:8px';
    sel.innerHTML = '<option value="en">EN</option><option value="es">ES</option>';
    if (host === document.body) {
      sel.style.cssText += ';position:fixed;top:8px;right:8px;z-index:9999;';
    }
    function label() {
      var text = t('common.language', 'Language');
      sel.setAttribute('aria-label', text);
      sel.title = text;
    }
    function current() {
      var i18n = window._i18n;
      var l = i18n && typeof i18n.getLocale === 'function' ? i18n.getLocale() : null;
      if (l !== 'en' && l !== 'es') {
        try { l = localStorage.getItem('ops-dashboard.locale'); } catch (e) { l = null; }
      }
      sel.value = l === 'es' ? 'es' : 'en';
    }
    sel.addEventListener('change', function () {
      if (!window._i18n || typeof window._i18n.setLocale !== 'function') return;
      window._i18n.setLocale(sel.value);
    });
    current();
    label();
    host.appendChild(sel);
    picker = { update: function () { current(); label(); } };
  }

  window.BoardroomI18n = {
    t: t,
    ready: function (fn) {
      var arrived = false;
      readyPromise.then(function () { arrived = true; fn(); });
      // No i18n.js (or a dictionary that never arrives): the page still shows
      // its default text instead of staying blank.
      setTimeout(function () { if (!arrived) fn(); }, 1500);
    },
    onChange: function (fn) { changeFns.push(fn); }
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { mountPicker(); });
  } else {
    mountPicker();
  }
  loadI18n();
})();
