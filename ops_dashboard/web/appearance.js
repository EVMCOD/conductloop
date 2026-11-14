// Light or dark, for every page.
//
// By default the app follows the system (theme.css switches its tokens under
// prefers-color-scheme). A person who picks one with the toggle gets it on
// every page: the choice is stored as 'ops-theme' and turned into a class on
// <body> (`light` or `dark`), which theme.css reads. Load it as the first
// thing inside <body> so the page never flashes the other appearance.
(function () {
  'use strict';
  var KEY = 'ops-theme';
  var media = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;

  function stored() {
    try { return localStorage.getItem(KEY); } catch (e) { return null; }
  }

  function apply(choice) {
    var b = document.body;
    if (!b) return;
    b.classList.toggle('dark', choice === 'dark');
    b.classList.toggle('light', choice === 'light');
  }

  function isDark() {
    var b = document.body;
    if (b && b.classList.contains('dark')) return true;
    if (b && b.classList.contains('light')) return false;
    return !!(media && media.matches);
  }

  // Flip what is on screen now and remember it.
  function toggle() {
    var next = isDark() ? 'light' : 'dark';
    try { localStorage.setItem(KEY, next); } catch (e) { /* storage off */ }
    apply(next);
    return next === 'dark';
  }

  apply(stored());
  window.clAppearance = { isDark: isDark, toggle: toggle };
})();
