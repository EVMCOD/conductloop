// Friendly rendering of the brain's 503 `provider_unavailable` (multi-agent PR 1).
// Backend contract (ops_dashboard/provider_errors.py):
//   {error:"provider_unavailable", code, provider, model, retry_after, reason, message}
// Stored run/agent-log errors use the text form "provider_unavailable:<provider> — …".
// Language follows the same rule as the BYOK banner: navigator.language "es*" → ES.
// Always writes with textContent (never innerHTML) and never shows raw codes.
(function (root) {
  'use strict';
  var CODE = 'provider_unavailable';
  var TEXT_RE = /^provider_unavailable:([A-Za-z0-9_.-]+)/;

  function lang() { return (root.navigator && root.navigator.language) || ''; }

  function message(provider, language) {
    var p = provider || '?';
    return /^es\b/i.test(language == null ? lang() : language)
      ? 'El proveedor ' + p + ' no está disponible ahora mismo, elige otro'
      : 'Provider ' + p + ' is unavailable right now, choose another one';
  }

  // Provider name if `x` is a provider_unavailable payload (or wraps one, like the
  // task stream `done` event), or a stored "provider_unavailable:<p>" text.
  function providerOf(x) {
    if (!x) return null;
    if (typeof x === 'string') { var m = TEXT_RE.exec(x); return m ? m[1] : null; }
    if (x.provider_unavailable) return providerOf(x.provider_unavailable);
    if (x.error === CODE || x.code === CODE) return x.provider || '?';
    return null;
  }

  // Friendly text for any error value: translated if provider_unavailable, else as-is.
  function friendly(x, language) {
    var p = providerOf(x);
    return p ? message(p, language) : (typeof x === 'string' ? x : (x && (x.message || x.error)) || '');
  }

  // Resolve a fetch Response to the payload when it is a 503 provider_unavailable.
  async function fromResponse(r) {
    if (!r || r.status !== 503) return null;
    try { var d = await r.clone().json(); return providerOf(d) ? d : null; } catch (e) { return null; }
  }

  // Point at a nearby provider selector: outline + focus for a few seconds.
  function highlight(selector) {
    var el = typeof selector === 'string'
      ? (root.document && root.document.getElementById(selector)) : selector;
    if (!el) return false;
    var prev = el.style.outline;
    el.style.outline = '2px solid var(--amber,#f59e0b)';
    el.style.outlineOffset = '2px';
    el.setAttribute('aria-invalid', 'true');
    try { el.focus(); } catch (e) { /* not focusable */ }
    setTimeout(function () { el.style.outline = prev; el.removeAttribute('aria-invalid'); }, 6000);
    return true;
  }

  // Render the message into `target`. opts.selector: id/element of the provider picker.
  function show(target, payload, opts) {
    var p = providerOf(payload);
    if (!target || !p) return false;
    var text = message(p);
    var hasSel = !!(opts && opts.selector) && highlight(opts.selector);
    if (hasSel) text += /^es\b/i.test(lang()) ? ' (selector de proveedor resaltado)' : ' (provider selector highlighted)';
    target.textContent = '⚠ ' + text;
    target.style.fontStyle = 'normal';
    target.style.color = 'var(--amber,#f59e0b)';
    target.setAttribute('role', 'alert');
    target.dataset.providerUnavailable = p;
    return true;
  }

  var api = { CODE: CODE, message: message, providerOf: providerOf, friendly: friendly,
              fromResponse: fromResponse, highlight: highlight, show: show };
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.ProviderErrors = api;
})(typeof window !== 'undefined' ? window : globalThis);
