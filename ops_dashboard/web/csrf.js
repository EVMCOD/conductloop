/* CSRF echo for every state-changing API call.
 *
 * The server issues an `ops_csrf` cookie on login (deliberately not HttpOnly)
 * and requires it back in the `X-CSRF-Token` header on any POST/PUT/DELETE
 * that does not come from loopback. The cookie is readable by the page on
 * purpose: that is how a page proves it is the one that logged in.
 *
 * Rather than touching every call site, this patches window.fetch once, the
 * way index.html already did. Any page that forgets it breaks every write
 * from mobile / LAN / relay with a 403 — which is what happened: only
 * index.html and sessions.js sent the header, so the rest of the app could
 * not write at all outside the Mac. 2026-10-23.
 */
(function () {
  if (window.__clCsrfPatched) return;
  window.__clCsrfPatched = true;

  function token() {
    var m = document.cookie.match(/(?:^|;\s*)ops_csrf=([^;]+)/);
    if (!m) return '';
    try { return decodeURIComponent(m[1]); } catch (e) { return m[1]; }
  }

  function isStateChange(method) {
    return method === 'POST' || method === 'PUT' || method === 'PATCH' || method === 'DELETE';
  }

  function isApi(url) {
    if (!url) return false;
    // Relative ("/api/x") and absolute ("http://host/api/x") both count.
    var path = url;
    if (/^https?:\/\//i.test(url)) {
      try { path = new URL(url, window.location.href).pathname; } catch (e) { return false; }
    }
    return path.indexOf('/api/') === 0 || path.indexOf('api/') === 0;
  }

  var realFetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    init = init || {};
    var method = (init.method || (input && input.method) || 'GET').toUpperCase();
    var url = typeof input === 'string' ? input : (input && input.url) || '';
    if (!isStateChange(method) || !isApi(url)) {
      return realFetch(input, init);
    }
    var t = token();
    if (!t) {
      return realFetch(input, init);
    }
    // Keep the existing headers whatever shape they have: a Headers instance
    // is not enumerable, so copying it with Object.assign would drop them
    // (and a Request carries its own headers that init.headers would replace).
    if (typeof Headers !== 'undefined' && init.headers instanceof Headers) {
      var h = new Headers(init.headers);
      h.set('X-CSRF-Token', t);
      init.headers = h;
    } else if (init.headers && typeof init.headers === 'object' && !Array.isArray(init.headers)) {
      var copy = {};
      for (var k in init.headers) { if (Object.prototype.hasOwnProperty.call(init.headers, k)) copy[k] = init.headers[k]; }
      copy['X-CSRF-Token'] = t;
      init.headers = copy;
    } else {
      init.headers = { 'X-CSRF-Token': t };
    }
    if (typeof Request !== 'undefined' && input instanceof Request && !init.method) {
      init.method = input.method;
    }
    return realFetch(input, init);
  };
})();
