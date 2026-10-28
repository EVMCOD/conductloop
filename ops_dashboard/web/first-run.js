// Sends someone who has just installed the app to /welcome, once.
//
// "Just installed" is decided by the server (GET /api/onboarding/state: nothing
// indexed, no key, no folder chosen, and the page not already finished or
// skipped), so an upgrade never lands here. Any failure leaves the dashboard
// as it is. Only runs on the dashboard itself.
(function () {
  'use strict';
  var p = window.location.pathname;
  if (p !== '/' && p !== '/index.html') return;
  fetch('/api/onboarding/state', { headers: { 'Accept': 'application/json' } })
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (s) { if (s && s.fresh === true) window.location.replace('/welcome'); })
    .catch(function () {});
})();
