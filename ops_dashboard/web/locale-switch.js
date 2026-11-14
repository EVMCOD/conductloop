// The language selector, for the pages that do not have one in their header.
//
// The language is the person's choice: it is remembered in this browser and
// in the app's configuration, so the server speaks it too. The selector is
// the same control everywhere — the dashboard has its own in the top bar,
// and this one mounts into any page that wants it:
//
//   <script src="/locale-switch.js" data-locale-switch="#controls" defer></script>
//
// Without a target it appends to the page's header, and if there is no
// header it falls back to the body (top right, out of the way).
(function () {
  'use strict';

  var SCRIPT = document.currentScript;
  var TARGET = (SCRIPT && SCRIPT.getAttribute('data-locale-switch')) || '';

  // The language each one calls itself in that language. The server knows
  // the same list; this is the fallback for a page that loads before it.
  var NAMES = { en: 'English', es: 'Español' };

  // The label of the selector is in the chosen language too ("Idioma",
  // not "Language"). The dictionary may not have arrived yet, so the
  // English label is the fallback until it does.
  function labelText() {
    var i18n = window._i18n;
    if (i18n && typeof i18n.t === 'function') {
      var v = i18n.t('common.language');
      if (v && v !== 'common.language') return v;
    }
    return 'Language';
  }

  function label(el) {
    var text = labelText();
    el.setAttribute('aria-label', text);
    el.title = text;
  }

  function pick() {
    var el = document.createElement('select');
    el.id = el.id || 'locale-picker';
    el.className = 'locale-picker';
    label(el);
    el.style.cssText = 'background:var(--bg-soft,transparent);color:var(--ink,inherit);' +
      'border:1px solid var(--border,#444);padding:4px 8px;border-radius:4px;' +
      'font-size:12px;cursor:pointer;max-width:120px';
    var supported = (window._i18n && window._i18n.getSupported()) || ['en', 'es'];
    supported.forEach(function (code) {
      var o = document.createElement('option');
      o.value = code;
      o.textContent = NAMES[code] || code.toUpperCase();
      el.appendChild(o);
    });
    var current = (window._i18n && window._i18n.getLocale()) || 'en';
    el.value = supported.indexOf(current) >= 0 ? current : 'en';
    el.addEventListener('change', function () {
      if (!window._i18n) return;
      window._i18n.setLocale(el.value).then(function () {
        // The page redraws itself in the new language.
        if (window._i18n.apply) window._i18n.apply();
        window.dispatchEvent(new CustomEvent('localechange', { detail: el.value }));
      });
    });
    return el;
  }

  function mount() {
    if (document.getElementById('locale-picker')) return;   // the page has its own
    var host = null;
    if (TARGET) host = document.querySelector(TARGET);
    if (!host) host = document.querySelector('header .controls, .topbar .controls, header');
    if (!host) host = document.body;
    var el = pick();
    if (host === document.body) {
      el.style.cssText += ';position:fixed;top:8px;right:8px;z-index:9999;';
    }
    host.appendChild(el);
    if (window._i18n) {
      window._i18n.onLocaleChange(function () {
        el.value = window._i18n.getLocale();
        label(el);
      });
      // The dictionary arrives over the network: the label is set again when
      // it does, so the selector never keeps the fallback.
      if (window._i18n.ready) window._i18n.ready().then(function () { label(el); });
    }
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mount);
  else mount();
})();
