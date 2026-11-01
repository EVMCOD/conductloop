// Repairing a session that the server forgot.
//
// The window keeps its session token in a cookie. If the server was
// restarted with a different token, that cookie stops matching and every
// request answers 403 — the app looks broken and cannot be recovered
// without quitting it.
//
// The main page hands out a cookie that works, so a reload is enough. This
// does that reload, once: a timestamp in sessionStorage keeps it from
// looping if the reload does not help.
(function () {
  'use strict';
  var KEY = 'cl_session_reload_at';
  var MIN_GAP_MS = 5000;

  function reloadOnce() {
    var now = Date.now();
    var last = 0;
    try { last = Number(sessionStorage.getItem(KEY) || 0); } catch (e) { last = 0; }
    if (now - last < MIN_GAP_MS) return false;
    try { sessionStorage.setItem(KEY, String(now)); } catch (e) { /* private mode */ }
    location.reload();
    return true;
  }

  window.CLSession = { reloadOnce: reloadOnce };
})();
