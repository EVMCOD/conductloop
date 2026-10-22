// i18n — internationalization for ops-dashboard.
//
// Usage:
//   const t = window._i18n.t;
//   t('sidebar.projects')  // → "Projects" or "Proyectos" depending on locale
//   t('greeting', { name: 'Alex' })  // → "Hello Alex!"
//
// Default locale: 'en' (English). Falls back to 'en' for any missing key.
// Translations live in /api/i18n/<locale>.json (server-side route).
// Locale is persisted in localStorage as 'ops-dashboard.locale'.

const FALLBACK = 'en';
const STORAGE_KEY = 'ops-dashboard.locale';
// English-only for the public launch. Other locales can be added back later
// by extending this list AND shipping a matching i18n/<locale>.json file.
const SUPPORTED = ['en'];

const _cache = {};
let _currentLocale = null;
const _listeners = new Set();

function detectInitialLocale() {
  // Always English. We previously honored a stored value (e.g. 'es' from a
  // pre-launch install) — reset those so existing users don't get a half-broken
  // UI when we ship without es.json.
  if (typeof localStorage !== 'undefined') {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored && !SUPPORTED.includes(stored)) {
      localStorage.removeItem(STORAGE_KEY);
    }
  }
  return FALLBACK;
}

async function loadLocale(locale) {
  if (_cache[locale]) return _cache[locale];
  try {
    const r = await fetch(`/api/i18n/${locale}.json`);
    if (!r.ok) {
      _cache[locale] = {};
      return _cache[locale];
    }
    _cache[locale] = await r.json();
    return _cache[locale];
  } catch (e) {
    _cache[locale] = {};
    return _cache[locale];
  }
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
  if (typeof localStorage !== 'undefined') {
    localStorage.setItem(STORAGE_KEY, locale);
  }
  if (typeof document !== 'undefined') {
    document.documentElement.lang = locale;
  }
  _listeners.forEach(function(fn) { try { fn(locale); } catch (e) {} });
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

// Pre-fetch initial locale + fallback on script load
(function init() {
  _currentLocale = detectInitialLocale();
  loadLocale(_currentLocale);
  loadLocale(FALLBACK);
})();

window._i18n = {
  t: t,
  setLocale: setLocale,
  getLocale: getLocale,
  getSupported: getSupported,
  onLocaleChange: onLocaleChange,
  _cache: _cache,
};
