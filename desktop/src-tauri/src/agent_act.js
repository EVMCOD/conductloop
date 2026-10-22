// Browser design PR 9 — describe or act on ONE element chosen by the agent
// through its `ref` (data-cl-ref from the last read). Injected by
// agent_bridge.rs, which replaces the ARGS placeholder in the last line with a JSON object:
//   {"ref": n, "op": "describe" | "click" | "type", "text": "...", "submit": bool}
// No selectors, no eval from the model. Synthetic events (isTrusted=false).
(function (args) {
  "use strict";
  var el = document.querySelector('[data-cl-ref="' + String(parseInt(args.ref, 10)) + '"]');
  if (!el) return { ok: false, error: "no_such_ref" };
  function clip(s, n) { s = String(s || "").replace(/\s+/g, " ").trim(); return s.length > n ? s.slice(0, n) : s; }
  var tag = el.tagName.toLowerCase();
  if (args.op === "describe") {
    var name = el.getAttribute("aria-label") || el.getAttribute("title") || el.getAttribute("alt") ||
      (el.labels && el.labels.length ? el.labels[0].textContent : "") ||
      (tag !== "input" && tag !== "textarea" ? el.textContent : "") ||
      el.getAttribute("placeholder") || el.getAttribute("name") || "";
    return {
      ok: true, tag: tag, type: (el.getAttribute("type") || "").toLowerCase(),
      role: el.getAttribute("role") || "", name: clip(name, 200),
      autocomplete: (el.getAttribute("autocomplete") || "").toLowerCase().slice(0, 40),
      in_form: !!(el.form || el.closest("form")),
      href: tag === "a" ? String(el.href || el.getAttribute("href") || "").slice(0, 500) : null,
      payment: (el.getAttribute("autocomplete") || "").toLowerCase().indexOf("cc-") === 0,
      disabled: !!el.disabled,
      page_host: String(location.hostname),
      has_password: !!document.querySelector("input[type=password]"),
      has_payment: !!document.querySelector("[autocomplete^=cc-]")
    };
  }
  if (el.disabled) return { ok: false, error: "disabled" };
  el.scrollIntoView({ block: "center", inline: "nearest" });
  if (args.op === "click") {
    var o = { bubbles: true, cancelable: true, view: window };
    el.dispatchEvent(new PointerEvent("pointerdown", o));
    el.dispatchEvent(new MouseEvent("mousedown", o));
    el.dispatchEvent(new PointerEvent("pointerup", o));
    el.dispatchEvent(new MouseEvent("mouseup", o));
    el.click();
    return { ok: true };
  }
  if (args.op === "type") {
    if (el.type === "password") return { ok: false, error: "password_field" };
    el.focus();
    var text = String(args.text || "");
    if (el.isContentEditable) {
      el.textContent = text;
    } else {
      // Native setter so frameworks (React…) see the change.
      var proto = tag === "textarea" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      var setter = Object.getOwnPropertyDescriptor(proto, "value").set;
      setter.call(el, text);
    }
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    if (args.submit) {
      var k = { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true, cancelable: true };
      var go = el.dispatchEvent(new KeyboardEvent("keydown", k));
      el.dispatchEvent(new KeyboardEvent("keyup", k));
      var form = el.form || el.closest("form");
      if (go && form) { if (form.requestSubmit) form.requestSubmit(); else form.submit(); }
    }
    return { ok: true };
  }
  return { ok: false, error: "unknown_op" };
})(__ARGS__)
