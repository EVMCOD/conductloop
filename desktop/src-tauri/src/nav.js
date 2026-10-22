// Back / Home controls for the app's own pages. The desktop window has no
// browser chrome, so pages without their own link back (Agents, Runs, the
// Boardroom...) were dead ends. lib.rs evaluates this in the main webview after
// every page load, with __CL_ORIGIN__ replaced by the bundled server's origin;
// browser tabs (other webviews, other sites) never get it.
// Styles go through CSSOM (element.style), which the pages' strict CSP allows.
(function () {
  'use strict';
  if (location.origin !== '__CL_ORIGIN__') return;
  if (location.pathname === '/' || location.pathname === '/index.html') return;
  if (document.getElementById('cl-nav') || !document.body) return;

  var bar = document.createElement('nav');
  bar.id = 'cl-nav';
  bar.setAttribute('aria-label', 'App navigation');

  function button(label, title, onClick) {
    var b = document.createElement('button');
    b.type = 'button';
    b.textContent = label;
    b.title = title;
    b.setAttribute('aria-label', title);
    // Every property a page's own `button { ... }` rule might set (margin-top,
    // padding, shadows...) is pinned here, or it leaks into the controls.
    Object.assign(b.style, {
      all: 'unset', boxSizing: 'border-box', display: 'inline-flex', alignItems: 'center',
      margin: '0', padding: '8px 12px', height: '32px', borderRadius: '999px', cursor: 'pointer',
      color: 'inherit', background: 'transparent',
      font: '500 13px/1 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif'
    });
    b.addEventListener('mouseenter', function () { b.style.background = 'rgba(127,127,127,.18)'; });
    b.addEventListener('mouseleave', function () { b.style.background = 'transparent'; });
    b.addEventListener('click', onClick);
    return b;
  }

  // Plain titles: the Go menu shows the shortcuts, localized to the keyboard
  // (on a Spanish layout Back is ⌘Ñ, not ⌘[).
  bar.appendChild(button('← Back', 'Back', function () {
    if (history.length > 1) history.back(); else location.assign('/');
  }));
  bar.appendChild(button('⌂', 'Home', function () { location.assign('/'); }));

  Object.assign(bar.style, {
    all: 'unset', boxSizing: 'border-box',
    position: 'fixed', left: '14px', bottom: '14px', zIndex: '2147483000',
    display: 'flex', alignItems: 'center', gap: '2px', margin: '0', padding: '3px', borderRadius: '999px',
    backdropFilter: 'blur(10px)', webkitBackdropFilter: 'blur(10px)'
  });

  // Match the page: light or dark from its own background colour.
  function paint() {
    var bg = getComputedStyle(document.body).backgroundColor || '';
    var rgb = (bg.match(/\d+(\.\d+)?/g) || ['255', '255', '255']).map(Number);
    var dark = (0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]) < 128 && rgb[3] !== 0;
    Object.assign(bar.style, dark
      ? { background: 'rgba(32,34,40,.86)', color: '#e8e9ec', border: '1px solid rgba(255,255,255,.12)',
          boxShadow: '0 8px 24px rgba(0,0,0,.45)' }
      : { background: 'rgba(255,255,255,.88)', color: '#15171c', border: '1px solid rgba(0,0,0,.08)',
          boxShadow: '0 8px 24px rgba(20,22,30,.14)' });
  }
  paint();
  new MutationObserver(paint).observe(document.body, { attributes: true, attributeFilter: ['class'] });

  document.body.appendChild(bar);
})();
