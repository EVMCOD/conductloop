  // Browser design, follow-up to PR 10: what the user cannot see must not
  // reach the model. Inlined (Rust replaces the VISIBILITY placeholder) at the top
  // of agent_snapshot.js and browser_extract.js, inside their IIFE, so the page
  // cannot replace it. Returns "" for visible, or the reason it is hidden.
  // The page could still patch getComputedStyle & co.: this is one layer of
  // several (the backend wraps page text as untrusted anyway).
  function clVisibility() {
    var cs = window.getComputedStyle.bind(window), cache = new Map();
    var de = document.documentElement;
    var docW = Math.max(de.scrollWidth, window.innerWidth || 0);
    var docH = Math.max(de.scrollHeight, window.innerHeight || 0);
    function style(el) { var s = cache.get(el); if (!s) { s = cs(el); cache.set(el, s); } return s; }
    function rgba(c) {
      var m = /rgba?\(([^)]+)\)/.exec(c || "");
      if (!m) return null;
      var p = m[1].split(/[\s,\/]+/).filter(Boolean).map(parseFloat);
      return [p[0], p[1], p[2], p.length > 3 ? p[3] : 1];
    }
    function background(el) {
      for (var e = el; e; e = e.parentElement) {
        var s = style(e);
        if (s.backgroundImage && s.backgroundImage !== "none") return null;  // unknown: keep
        var b = rgba(s.backgroundColor);
        if (b && b[3] > 0.5) return b;
      }
      return [255, 255, 255, 1];  // default canvas
    }
    function ownText(el) {
      for (var c = el.firstChild; c; c = c.nextSibling) if (c.nodeType === 3 && c.nodeValue.trim()) return true;
      return false;
    }
    function clipZero(s) {
      var m = /rect\(([^)]+)\)/.exec(s.clip || "");
      if (!m || !/absolute|fixed/.test(s.position)) return false;
      var v = m[1].split(/[\s,]+/).map(parseFloat);
      return v.length === 4 && (v[2] - v[0] <= 1 || v[1] - v[3] <= 1);
    }
    function clipPathZero(cp) {
      if (!cp || cp === "none") return false;
      var m = /inset\(\s*([\d.]+)%/.exec(cp);
      if (m && parseFloat(m[1]) >= 50) return true;
      return /circle\(\s*0(px|%)?[\s)]/.test(cp) || /ellipse\(\s*0(px|%)?\s+0/.test(cp);
    }
    function reason(el) {
      if (el.hidden) return "hidden_attr";
      if (el.getAttribute("aria-hidden") === "true") return "aria_hidden";
      var s = style(el);
      if (s.display === "none") return "display";
      if (s.visibility === "hidden" || s.visibility === "collapse") return "visibility";
      if (parseFloat(s.opacity) < 0.05) return "opacity";
      if (clipZero(s)) return "clip";
      if (clipPathZero(s.clipPath)) return "clip_path";
      if (/^matrix\(0, 0, 0, 0,/.test(s.transform || "")) return "transform";
      var own = ownText(el);
      if (own && parseFloat(s.fontSize) < 1) return "font_size";
      var r = el.getBoundingClientRect();
      if (r.width === 0 && r.height === 0 && el.getClientRects().length === 0 && s.display !== "contents") return "size";
      if ((r.width < 1 || r.height < 1) && /hidden|clip/.test(s.overflow + " " + s.overflowX + " " + s.overflowY)) return "size";
      var ax = r.left + window.scrollX, ay = r.top + window.scrollY;
      if (r.width >= 1 && r.height >= 1 && (ax + r.width <= 0 || ay + r.height <= 0 || ax >= docW || ay >= docH)) return "offscreen";
      if (own && parseFloat(s.textIndent) <= -999) return "text_indent";
      if (own) {
        var fg = rgba(s.color);
        if (fg && fg[3] < 0.1) return "color";
        var bg = background(el);
        if (fg && bg && Math.abs(fg[0] - bg[0]) + Math.abs(fg[1] - bg[1]) + Math.abs(fg[2] - bg[2]) < 24) return "color";
      }
      return "";
    }
    // Hidden elements of `root` (max `limit`): {set, counts:{total, reasons}}.
    // Counts are the top-most hidden elements that carry text.
    function scan(root, limit) {
      var set = new Set(), counts = { total: 0, reasons: {} };
      var all = root ? root.getElementsByTagName("*") : [];
      for (var i = 0; i < all.length && i < limit; i++) {
        var el = all[i], why = reason(el);
        if (!why) continue;
        set.add(el);
        var top = true;
        for (var p = el.parentElement; p && p !== root; p = p.parentElement) if (set.has(p)) { top = false; break; }
        if (top && (el.textContent || "").trim()) {
          counts.total += 1;
          counts.reasons[why] = (counts.reasons[why] || 0) + 1;
        }
      }
      // Past `limit` nothing was inspected: treat the tail as hidden (dropped)
      // instead of letting unchecked nodes through as if they were visible.
      if (all.length > limit) {
        for (var t = limit; t < all.length; t++) set.add(all[t]);
        counts.reasons.uninspected = all.length - limit;
      }
      return { set: set, counts: counts };
    }
    return { reason: reason, scan: scan };
  }
