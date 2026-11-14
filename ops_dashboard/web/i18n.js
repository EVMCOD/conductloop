// i18n — internationalization for ops-dashboard.
//
// Usage:
//   const t = window._i18n.t;
//   t('sidebar.projects')  // → "Projects" or "Proyectos" depending on locale
//   t('greeting', { name: 'Alex' })  // → "Hello Alex!"
//
// The language is the person's choice, not ours: it is picked with the
// selector in the top bar and it is remembered (in this browser and in the
// app's configuration, so the server speaks it too). The browser's language
// is only the starting point for someone who has not chosen yet.
//
// Default locale: 'en' (English). Falls back to 'en' for any missing key.
// Translations live in /api/i18n/<locale>.json (server-side route).
// Locale is persisted in localStorage as 'ops-dashboard.locale'.
//
// Everything here is inside a closure: a page with words of its own may
// declare a global `t` (or a `const t`), and that used to be a redeclaration
// error that killed this file — the page then stayed in English without
// saying why. The only thing this file puts in the window is `_i18n`.
(function () {

const FALLBACK = 'en';
const STORAGE_KEY = 'ops-dashboard.locale';
// The languages the app is translated into. Add a language here AND ship a
// matching i18n/<locale>.json file.
const SUPPORTED = ['en', 'es'];

// A page can ask for extra dictionaries of its own:
//   <script src="/i18n.js" data-i18n-pages="chat" defer></script>
// They live in /i18n/<page>.<locale>.json and are merged into the locale.
const PAGE_DICTS = ((document.currentScript &&
  document.currentScript.getAttribute('data-i18n-pages')) || '')
  .split(/[\s,]+/).filter(Boolean);

const _cache = {};
let _currentLocale = null;
let _serverLocale = null;
let _ready = Promise.resolve();
const _listeners = new Set();
// Did somebody pick a language on purpose? A browser that only *guessed*
// (its own language, on a fresh install) must never overwrite the choice
// the app already knows about.
let _explicitChoice = false;

function detectInitialLocale() {
  // The choice this browser made last time wins. If there is none, the
  // browser's own language is the best guess for a first run.
  let stored = null;
  try {
    stored = localStorage.getItem(STORAGE_KEY);
  } catch (e) { /* storage off */ }
  if (stored && SUPPORTED.includes(stored)) {
    _explicitChoice = true;
    return stored;
  }
  try {
    if (stored) localStorage.removeItem(STORAGE_KEY);
  } catch (e) { /* storage off */ }
  const lang = (navigator.language || '').toLowerCase();
  const head = lang.split('-')[0];
  if (SUPPORTED.includes(head)) return head;
  return FALLBACK;
}

function mergeInto(target, extra) {
  for (const k in extra) {
    const v = extra[k];
    if (v && typeof v === 'object' && !Array.isArray(v)) {
      if (!target[k] || typeof target[k] !== 'object') target[k] = {};
      mergeInto(target[k], v);
    } else {
      target[k] = v;
    }
  }
  return target;
}

async function loadLocale(locale) {
  if (_cache[locale]) return _cache[locale];
  const dict = {};
  try {
    const r = await fetch(`/api/i18n/${locale}.json`);
    if (r.ok) mergeInto(dict, await r.json());
  } catch (e) { /* offline: the fallback covers it */ }
  for (const page of PAGE_DICTS) {
    try {
      const r = await fetch(`/api/i18n/${page}.${locale}.json`);
      if (r.ok) mergeInto(dict, await r.json());
    } catch (e) { /* a page without its own dictionary still works */ }
  }
  _cache[locale] = dict;
  return _cache[locale];
}

function interpolate(str, vars) {
  if (!vars) return str;
  return str.replace(/\{(\w+)\}/g, function(_, k) {
    return vars[k] !== undefined ? String(vars[k]) : `{${k}}`;
  });
}

function t(key, vars) {
  // Dotted-path lookup: 'billing.plan_lifetime' -> obj['billing']['plan_lifetime'].
  // Falls back to flat-key search if the dotted path doesn't exist (legacy strings).
  function lookupDotted(obj, dottedKey) {
    if (!obj) return undefined;
    const parts = dottedKey.split('.');
    let cur = obj;
    for (const part of parts) {
      if (cur == null || typeof cur !== 'object' || cur[part] === undefined) {
        return undefined;
      }
      cur = cur[part];
    }
    return cur;
  }
  function lookupFlat(obj, k) {
    if (!obj) return undefined;
    if (obj[k] !== undefined) return obj[k];
    for (const key in obj) {
      if (typeof obj[key] === 'object' && obj[key] !== null) {
        const v = lookupFlat(obj[key], k);
        if (v !== undefined) return v;
      }
    }
    return undefined;
  }
  const cur = _cache[_currentLocale] || {};
  let val = lookupDotted(cur, key);
  if (val === undefined) val = lookupFlat(cur, key);
  if (val === undefined && _currentLocale !== FALLBACK) {
    val = lookupDotted(_cache[FALLBACK], key);
    if (val === undefined) val = lookupFlat(_cache[FALLBACK], key);
  }
  if (val === undefined) return key;
  return interpolate(val, vars);
}

async function setLocale(locale) {
  if (!SUPPORTED.includes(locale)) locale = FALLBACK;
  // Inline fetch + assign (avoids any closure/module-scope issue with loadLocale)
  if (!_cache[locale]) {
    try {
      const r = await fetch(`/api/i18n/${locale}.json`);
      if (r.ok) _cache[locale] = await r.json();
      else _cache[locale] = {};
    } catch (e) { _cache[locale] = {}; }
    for (const page of PAGE_DICTS) {
      try {
        const r = await fetch(`/api/i18n/${page}.${locale}.json`);
        if (r.ok) mergeInto(_cache[locale], await r.json());
      } catch (e) { /* a page without its own dictionary still works */ }
    }
  }
  // Always ensure fallback loaded
  if (!_cache[FALLBACK]) {
    try {
      const r = await fetch(`/api/i18n/${FALLBACK}.json`);
      if (r.ok) _cache[FALLBACK] = await r.json();
      else _cache[FALLBACK] = {};
    } catch (e) { _cache[FALLBACK] = {}; }
  }
  _currentLocale = locale;
  _explicitChoice = true;
  try {
    localStorage.setItem(STORAGE_KEY, locale);
  } catch (e) { /* storage off */ }
  if (typeof document !== 'undefined') {
    document.documentElement.lang = locale;
  }
  // The server speaks the same language: it writes the agent roles and the
  // answers, and those must be in the language the person chose.
  try {
    await fetch('/api/locale', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ locale: locale }),
    });
  } catch (e) { /* offline: the page keeps its language */ }
  _listeners.forEach(function(fn) { try { fn(locale); } catch (e) {} });
  apply();   // the page shows itself in the new language
}

function onLocaleChange(fn) {
  _listeners.add(fn);
  return function() { _listeners.delete(fn); };
}

function getLocale() {
  return _currentLocale;
}

function getSupported() {
  return SUPPORTED.slice();
}

// Mark an element with data-i18n="key" (its text), data-i18n-ph="key" (its
// placeholder) or data-i18n-title="key" (its tooltip) and it is translated
// when the language changes. The same convention the other pages already
// use, in one place.
function apply(root) {
  const scope = root || document;
  const nodes = scope.querySelectorAll('[data-i18n]');
  for (let i = 0; i < nodes.length; i++) {
    nodes[i].textContent = t(nodes[i].getAttribute('data-i18n'));
  }
  const phs = scope.querySelectorAll('[data-i18n-ph]');
  for (let j = 0; j < phs.length; j++) {
    phs[j].setAttribute('placeholder', t(phs[j].getAttribute('data-i18n-ph')));
  }
  const titles = scope.querySelectorAll('[data-i18n-title]');
  for (let k = 0; k < titles.length; k++) {
    titles[k].setAttribute('title', t(titles[k].getAttribute('data-i18n-title')));
  }
  const labels = scope.querySelectorAll('[data-i18n-label]');
  for (let m = 0; m < labels.length; m++) {
    labels[m].setAttribute('aria-label', t(labels[m].getAttribute('data-i18n-label')));
  }
}

// Pre-fetch initial locale + fallback on script load
(function init() {
  _currentLocale = detectInitialLocale();
  // The page says what language it is in, always — the browser's language
  // until somebody chooses, the app's language after that.
  if (typeof document !== 'undefined') {
    document.documentElement.lang = _currentLocale;
  }
  _ready = Promise.all([loadLocale(_currentLocale), loadLocale(FALLBACK)]).then(function () {
    apply();   // the page shows itself in the chosen language
  });
  // The app's configuration is the truth about the language — but only when
  // somebody *chose* it. A browser that merely guessed (its own language,
  // on a fresh install) never overwrites a real choice.
  try {
    fetch('/api/locale', { headers: { 'Accept': 'application/json' } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data || !data.locale) return;
        _serverLocale = data.locale;
        if (data.chosen) {
          // Somebody chose this language: the app speaks it everywhere.
          if (data.locale !== _currentLocale) setLocale(data.locale);
          return;
        }
        // Nobody has chosen yet. If *this* browser picked one, the server
        // has to learn it, so the roles and the answers match the page.
        if (_explicitChoice && _currentLocale !== data.locale) {
          setLocale(_currentLocale);
        }
      })
      .catch(function () {});
  } catch (e) { /* the browser's guess stands */ }
})();

// The dictionaries arrive over the network: a page that writes strings has
// to wait for them, or it would show the keys instead of the words.
function ready() {
  return _ready;
}

window._i18n = {
  t: t,
  setLocale: setLocale,
  getLocale: getLocale,
  getSupported: getSupported,
  onLocaleChange: onLocaleChange,
  apply: apply,
  ready: ready,
  _cache: _cache,
};

})();
