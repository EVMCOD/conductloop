// A message the user can actually see.
//
// alert() is a no-op in the desktop app. Tauri v2 disables the JavaScript
// dialogs unless the permission is granted, and the window is served over
// http, so it is not granted there either. Every error reported with
// alert() was therefore invisible: the action did nothing, the message went
// nowhere, and the user was told the app was broken.
//
// This is a plain element in the page. It works in the app, in a browser,
// and anywhere else the UI runs.
(function () {
  'use strict';

  var ROOT_ID = 'toast-root';
  var LIFETIME_MS = 7000;

  function root() {
    var r = document.getElementById(ROOT_ID);
    if (r) return r;
    if (!document.body) return null;
    r = document.createElement('div');
    r.id = ROOT_ID;
    document.body.appendChild(r);
    return r;
  }

  function notify(message, kind) {
    var text = message === null || message === undefined ? '' : String(message);
    if (!text) return null;
    var r = root();
    if (!r) return null;
    var el = document.createElement('div');
    el.className = 'toast' + (kind ? ' ' + kind : '');
    el.setAttribute('role', kind === 'error' ? 'alert' : 'status');
    el.textContent = text;
    r.appendChild(el);
    try {
      setTimeout(function () {
        if (el.parentNode) el.parentNode.removeChild(el);
      }, LIFETIME_MS);
    } catch (e) { /* no timers in this environment */ }
    return el;
  }

  // Errors read as errors; everything else is information.
  function notifyError(message) { return notify(message, 'error'); }

  window.notify = notify;
  window.notifyError = notifyError;
})();
