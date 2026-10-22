// Browser design PR 6 — "Send to vault": extract the readable content of the
// page open in a browser tab. Injected by browser_extract (Rust) with
// eval_with_callback; the returned object is JSON-serialised by Tauri.
//
// Runs in the page's main world (no isolated world through Tauri), so the page
// could tamper with it: the backend re-validates and sanitises everything.
(function () {
  "use strict";
  /*__READABILITY__*/
  /*__CL_VISIBILITY__*/
  var MAX_HTML = 2000000, MAX_TEXT = 200000, MAX_NODES = 50000;
  var marked = [], clone = null, scanned = clVisibility().scan(document.body, MAX_NODES);
  try {
    scanned.set.forEach(function (el) { el.setAttribute("data-cl-hidden", "1"); marked.push(el); });
    clone = document.cloneNode(true);
  } finally {
    for (var j = 0; j < marked.length; j++) marked[j].removeAttribute("data-cl-hidden");
  }
  // Hidden text is a classic prompt-injection carrier: drop it, and anything
  // that is not content.
  var drop = clone.querySelectorAll("[data-cl-hidden],script,style,noscript,iframe,frame,object,embed," +
    "input,textarea,select,button,template,svg,canvas,link,meta,dialog");
  for (var k = 0; k < drop.length; k++) drop[k].remove();
  var art = null;
  try { art = new Readability(clone, { charThreshold: 200 }).parse(); } catch (e) { art = null; }
  var body = clone.body;
  var html = (art && art.content) || (body ? body.innerHTML : "");
  var text = (art && art.textContent) || (body ? body.textContent : "");
  return {
    url: String(location.href),
    title: String((art && art.title) || document.title || ""),
    byline: String((art && art.byline) || ""),
    site_name: String((art && art.siteName) || ""),
    lang: String(document.documentElement.lang || ""),
    html: String(html).slice(0, MAX_HTML),
    text: String(text).slice(0, MAX_TEXT),
    truncated: html.length > MAX_HTML || text.length > MAX_TEXT,
    readability: !!art,
    hidden: scanned.counts
  };
})()
