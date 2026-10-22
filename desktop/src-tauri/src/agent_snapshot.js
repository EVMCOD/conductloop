// Browser design PR 8 — read-only page snapshot for the agent (`read` op).
// Visible text + numbered interactive elements. No values of any field (and
// never of password fields), no hidden nodes, no free selectors: the agent
// refers to elements by `ref` (data-cl-ref), PR 9 acts on those refs only.
// Runs in the page's world: the backend treats everything as untrusted.
(function () {
  "use strict";
  /*__CL_VISIBILITY__*/
  var MAX_TEXT = 20000, MAX_ELEMENTS = 300, MAX_NODES = 50000;
  function clip(s, n) { s = String(s || "").replace(/\s+/g, " ").trim(); return s.length > n ? s.slice(0, n) + "…" : s; }
  // Text of `el` without its invisible parts (a visible button can carry a
  // hidden span with instructions).
  function visibleText(el) {
    var out = [], w = document.createTreeWalker(el, NodeFilter.SHOW_TEXT, null);
    while (w.nextNode()) if (!isHidden(w.currentNode.parentElement)) out.push(w.currentNode.nodeValue);
    return out.join(" ");
  }
  function name(el) {
    var l = el.getAttribute("aria-label") || el.getAttribute("title") || el.getAttribute("alt") || "";
    if (!l && el.labels && el.labels.length) l = visibleText(el.labels[0]);
    if (!l && el.tagName !== "INPUT" && el.tagName !== "TEXTAREA" && el.tagName !== "SELECT") l = visibleText(el);
    if (!l) l = el.getAttribute("placeholder") || el.getAttribute("name") || "";
    return clip(l, 120);
  }
  var SEL = "a[href],button,input,textarea,select,summary,[role=button],[role=link],[role=checkbox],[role=tab],[role=menuitem],[contenteditable=true]";
  var old = document.querySelectorAll("[data-cl-ref]");
  for (var o = 0; o < old.length; o++) old[o].removeAttribute("data-cl-ref");
  var scanned = clVisibility().scan(document.body, MAX_NODES), hiddenSet = scanned.set;
  function isHidden(el) { for (var e = el; e && e !== document.body; e = e.parentElement) if (hiddenSet.has(e)) return true; return false; }
  var els = [], n = 0, cands = document.querySelectorAll(SEL);
  for (var j = 0; j < cands.length && els.length < MAX_ELEMENTS; j++) {
    var el = cands[j];
    if (isHidden(el) || el.disabled) continue;
    n += 1;
    el.setAttribute("data-cl-ref", String(n));
    var tag = el.tagName.toLowerCase(), type = (el.getAttribute("type") || "").toLowerCase();
    var item = { ref: n, tag: tag, role: el.getAttribute("role") || "", name: name(el) };
    if (type) item.type = type;
    if (el.form || el.closest("form")) item.in_form = true;
    if (tag === "a") item.href = String(el.href || el.getAttribute("href") || "").slice(0, 500);
    if (tag === "input" || tag === "textarea" || tag === "select") item.filled = !!el.value;
    var ac = (el.getAttribute("autocomplete") || "").toLowerCase();
    if (ac.indexOf("cc-") === 0) item.payment = true;
    if (ac.indexOf("cc-") === 0 || ac.indexOf("password") >= 0) item.ac = ac.slice(0, 40);
    els.push(item);
  }
  // Visible text, skipping hidden subtrees and script/style.
  var parts = [], len = 0;
  var walker = document.createTreeWalker(document.body || document.documentElement, NodeFilter.SHOW_TEXT, {
    acceptNode: function (t) {
      var p = t.parentElement;
      if (!p || /^(SCRIPT|STYLE|NOSCRIPT|TEMPLATE)$/.test(p.tagName) || isHidden(p)) return NodeFilter.FILTER_REJECT;
      return t.nodeValue.trim() ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
    }
  });
  while (len < MAX_TEXT && walker.nextNode()) { var v = clip(walker.currentNode.nodeValue, 2000); parts.push(v); len += v.length + 1; }
  var text = parts.join("\n");
  return {
    url: String(location.href),
    title: clip(document.title, 300),
    text: text.slice(0, MAX_TEXT),
    truncated: len >= MAX_TEXT || els.length >= MAX_ELEMENTS,
    elements: els,
    hidden: scanned.counts,
    has_password: !!document.querySelector("input[type=password]"),
    has_payment: !!document.querySelector("[autocomplete^=cc-]")
  };
})()
