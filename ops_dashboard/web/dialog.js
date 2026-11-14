// ============================================================
// Reusable dialog helper (replaces window.prompt/confirm with a proper UI)
// ============================================================
//
// Usage:
//   dialog.show({ title, fields: [{key,label,type,default,options,required}], submitLabel, onSubmit })
//   dialog.confirm({ title, message, onConfirm })
//   dialog.close()
//
(function(global){
  // The dialog speaks the language the person chose, like the rest of the
  // panel. i18n.js loads after this file, so the words are asked for when
  // the dialog opens, not when it is defined.
  function t(key, fallback) {
    var i18n = window._i18n;
    if (i18n && typeof i18n.t === 'function') {
      var v = i18n.t(key);
      if (v && v !== key) return v;
    }
    return fallback;
  }

  function ensureRoot() {
    var root = document.getElementById('app-dialog-root');
    if (root) return root;
    root = document.createElement('div');
    root.id = 'app-dialog-root';
    root.style.cssText = 'position:fixed;inset:0;display:none;align-items:center;justify-content:center;background:rgba(0,0,0,0.55);z-index:9999;';
    document.body.appendChild(root);
    return root;
  }

  // The validation error belongs to the form the user is looking at, so it
  // goes into the card itself. alert() is invisible in the desktop app.
  function showFieldError(card, msg) {
    var e = card.querySelector('.dialog-error');
    if (!e) {
      e = document.createElement('div');
      e.className = 'dialog-error';
      card.insertBefore(e, card.firstChild);
    }
    e.textContent = msg;
  }

  function close() {
    var root = ensureRoot();
    root.style.display = 'none';
    root.innerHTML = '';
  }

  function show(opts) {
    var root = ensureRoot();
    root.innerHTML = '';
    var card = document.createElement('div');
    card.style.cssText = 'background:var(--bg);border:1px solid var(--border);border-radius:10px;padding:20px;width:480px;max-width:90vw;max-height:85vh;overflow-y:auto;box-shadow:0 20px 50px rgba(0,0,0,0.5)';
    card.addEventListener('click', function(e){ e.stopPropagation(); });

    var html = '';
    if (opts.title) html += '<h3 style="margin:0 0 14px 0;font-size:15px;color:var(--ink)">' + esc(opts.title) + '</h3>';
    if (opts.description) html += '<p style="margin:0 0 14px 0;color:var(--muted);font-size:12px">' + esc(opts.description) + '</p>';

    var fieldsHtml = '';
    (opts.fields || []).forEach(function(f) {
      var required = f.required !== false;
      fieldsHtml += '<div style="margin-bottom:10px"><label style="display:block;font-size:11px;color:var(--muted);margin-bottom:3px">' + esc(f.label) + (required ? '' : ' <span style="color:var(--muted);opacity:0.6">(' + esc(t('common.optional', 'optional')) + ')</span>') + '</label>';
      if (f.type === 'select') {
        fieldsHtml += '<select data-field="' + esc(f.key) + '" style="width:100%;background:var(--bg-soft);border:1px solid var(--border);padding:6px 10px;border-radius:4px;color:var(--ink);font-size:13px">';
        (f.options || []).forEach(function(o) {
          var val = typeof o === 'object' ? o.value : o;
          var lab = typeof o === 'object' ? o.label : o;
          fieldsHtml += '<option value="' + esc(val) + '"' + (f.default === val ? ' selected' : '') + '>' + esc(lab) + '</option>';
        });
        fieldsHtml += '</select>';
      } else if (f.type === 'textarea') {
        fieldsHtml += '<textarea data-field="' + esc(f.key) + '" placeholder="' + esc(f.placeholder || '') + '" rows="' + (f.rows || 3) + '" style="width:100%;background:var(--bg-soft);border:1px solid var(--border);padding:6px 10px;border-radius:4px;color:var(--ink);font-size:13px;resize:vertical">' + esc(f.default || '') + '</textarea>';
      } else if (f.type === 'checkbox') {
        fieldsHtml += '<label style="display:flex;align-items:center;gap:6px;font-size:13px;color:var(--ink);cursor:pointer"><input type="checkbox" data-field="' + esc(f.key) + '"' + (f.default ? ' checked' : '') + '> ' + esc(f.checkbox_label || f.label) + '</label>';
      } else {
        fieldsHtml += '<input type="' + (f.type === 'password' ? 'password' : 'text') + '" data-field="' + esc(f.key) + '" value="' + esc(f.default || '') + '" placeholder="' + esc(f.placeholder || '') + '" style="width:100%;background:var(--bg-soft);border:1px solid var(--border);padding:6px 10px;border-radius:4px;color:var(--ink);font-size:13px">';
      }
      fieldsHtml += '</div>';
    });
    html += fieldsHtml;

    if (opts.body) html += '<div style="margin-bottom:14px;font-size:13px;color:var(--ink)">' + opts.body + '</div>';

    html += '<div style="display:flex;gap:8px;justify-content:flex-end;margin-top:14px">';
    html += '<button data-dialog-cancel style="background:transparent;border:1px solid var(--border);color:var(--muted);padding:6px 14px;border-radius:4px;cursor:pointer;font-size:13px">' + esc(t('common.cancel', 'Cancel')) + '</button>';
    html += '<button data-dialog-submit style="background:var(--blue);color:#fff;border:none;padding:6px 18px;border-radius:4px;cursor:pointer;font-weight:500;font-size:13px">' + esc(opts.submitLabel || t('common.submit', 'Submit')) + '</button>';
    html += '</div>';

    card.innerHTML = html;
    root.appendChild(card);
    root.style.display = 'flex';

    root.addEventListener('click', function(e) {
      if (e.target === root) close();
    });
    card.querySelector('[data-dialog-cancel]').addEventListener('click', close);
    card.querySelector('[data-dialog-submit]').addEventListener('click', function() {
      var data = {};
      (opts.fields || []).forEach(function(f) {
        var el = card.querySelector('[data-field="' + f.key + '"]');
        if (!el) return;
        if (f.type === 'checkbox') data[f.key] = el.checked;
        else if (f.type === 'select') data[f.key] = el.value;
        else data[f.key] = el.value.trim();
      });
      var err = (opts.validate ? opts.validate(data) : null);
      if (err) { showFieldError(card, err); return; }
      close();
      if (opts.onSubmit) opts.onSubmit(data);
    });
    // Focus first input
    var first = card.querySelector('input,textarea,select');
    if (first) setTimeout(function(){ first.focus(); }, 50);
    // Enter to submit
    card.addEventListener('keydown', function(e) {
      // The overlay is modal: it keeps its keystrokes. The dashboard's
      // global Escape handler would navigate back while the user is only
      // closing the dialog.
      e.stopPropagation();
      if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
        card.querySelector('[data-dialog-submit]').click();
      } else if (e.key === 'Escape') close();
    });
  }

  function confirm(opts) {
    show({
      title: opts.title || t('common.confirm', 'Confirm'),
      description: opts.message,
      fields: [],
      submitLabel: opts.submitLabel || t('common.confirm', 'Confirm'),
      onSubmit: function() { if (opts.onConfirm) opts.onConfirm(); }
    });
  }

  global.dialog = { show: show, confirm: confirm, close: close };
})(window);