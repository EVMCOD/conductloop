// ============================================================
// Asking the user, in the page (browser and desktop app)
// ============================================================
//
// Why this exists: the desktop app (Tauri v2) overrides window.confirm with
// an async IPC call to the dialog plugin, so in the app confirm() returns a
// PROMISE, not a boolean. A truthiness guard -- if (!confirm(...)) return;
// -- then sees a truthy promise, never fires, and the destructive action runs
// without asking. In a browser the same line is correct, which is why only
// the desktop app is affected and why browser-only tests never caught it.
//
// Awaiting the global is not the fix either: the plugin's message command
// needs the dialog:allow-message permission, which this repo grants to
// NOBODY on purpose (tests/test_tauri_capabilities.py). Without it the
// awaited promise rejects with an ACL error instead of resolving false.
//
// window.prompt is not overridden by the plugin, but wry ships no
// text-input panel either, so a prompt is not a reliable input: the value
// comes back empty and the flow stops without the user ever being asked.
//
// So the question is asked in the page: a <dialog> opened with showModal(),
// the same shape web/sessions.js already used. It needs no permission, no
// IPC and no capability, and it behaves the same in the browser and in the
// app.
//
// Usage:
//
//   if (!await confirmAction('Delete this todo?')) return;
//   var name = await promptAction('Agent name:');
//   if (name === null) return;      // the user cancelled
//
// The promises never reject: a cancelled question is an answer, not an
// error. confirmAction() resolves true when the user confirms and false
// when they cancel (Escape and a click on the backdrop are cancels too).
// promptAction() resolves the text the user typed, or null when they
// cancelled.
//
// Labels: a page with its own dictionary passes its own words (opts.confirm
// / opts.cancel / opts.ok), so the buttons speak the page's language.
// Without them the built-in English pair is used -- the default copy of
// this product is English, and a page that ships another language passes
// its own.
(function (global) {
  'use strict';

  var DEFAULT_LABELS = { confirm: 'Confirm', cancel: 'Cancel', ok: 'OK' };

  function word(opts, key) {
    if (opts && opts[key]) return opts[key];
    return DEFAULT_LABELS[key];
  }

  function makeButton(id, className, text) {
    var b = global.document.createElement('button');
    b.type = 'button';
    b.id = id;
    b.className = className;
    b.textContent = text;
    return b;
  }

  function makeMessage(text) {
    var p = global.document.createElement('p');
    p.className = 'confirm__message';
    p.textContent = text === null || text === undefined ? '' : String(text);
    return p;
  }

  // Opens the dialog and resolves with whatever the user answered.
  // A click on the backdrop (the dialog element itself, not a child) and
  // Escape are cancels: the user dismissed the question.
  function ask(dlg, wire, resolve, cancelValue) {
    function done(value) {
      try { dlg.close(); } catch (e) { /* already closed */ }
      if (dlg.parentNode) dlg.parentNode.removeChild(dlg);
      resolve(value);
    }
    wire(done);
    // A modal keeps its keystrokes. The page behind it must not react to
    // them: the dashboard's global Escape handler navigates back, and a
    // native dialog used to swallow the key while it was up. Handling the
    // dialog's own 'cancel' event is not enough -- that is a different
    // event, and the keydown has already bubbled to the document by then.
    dlg.addEventListener('keydown', function (e) {
      e.stopPropagation();
    });
    dlg.addEventListener('click', function (e) {
      if (e.target === dlg) done(cancelValue);
    });
    dlg.addEventListener('cancel', function (e) {
      e.preventDefault();
      done(cancelValue);
    });
    global.document.body.appendChild(dlg);
    dlg.showModal();
  }

  function confirmAction(message, opts) {
    return new Promise(function (resolve) {
      // No body yet: there is nobody to ask. Answer "no" -- the safe side of
      // a destructive action.
      if (!global.document || !global.document.body) { resolve(false); return; }

      var dlg = global.document.createElement('dialog');
      dlg.id = 'confirm-dialog';
      dlg.className = 'confirm';

      var no = makeButton('confirm-no', 'confirm__btn confirm__btn--ghost', word(opts, 'cancel'));
      var yes = makeButton('confirm-yes', 'confirm__btn confirm__btn--primary', word(opts, 'confirm'));

      var actions = global.document.createElement('div');
      actions.className = 'confirm__actions';
      actions.appendChild(no);
      actions.appendChild(yes);

      dlg.appendChild(makeMessage(message));
      dlg.appendChild(actions);

      ask(dlg, function (done) {
        no.addEventListener('click', function () { done(false); });
        yes.addEventListener('click', function () { done(true); });
      }, resolve, false);
    });
  }

  function promptAction(message, opts) {
    opts = opts || {};
    return new Promise(function (resolve) {
      if (!global.document || !global.document.body) { resolve(null); return; }

      var dlg = global.document.createElement('dialog');
      dlg.id = 'prompt-dialog';
      dlg.className = 'confirm';

      var input = global.document.createElement('input');
      input.type = 'text';
      input.id = 'prompt-input';
      input.className = 'confirm__input';
      if (opts.defaultValue !== null && opts.defaultValue !== undefined) {
        input.value = String(opts.defaultValue);
      }
      if (opts.placeholder) input.placeholder = opts.placeholder;

      var no = makeButton('prompt-no', 'confirm__btn confirm__btn--ghost', word(opts, 'cancel'));
      var yes = makeButton('prompt-yes', 'confirm__btn confirm__btn--primary', word(opts, 'ok'));

      var actions = global.document.createElement('div');
      actions.className = 'confirm__actions';
      actions.appendChild(no);
      actions.appendChild(yes);

      dlg.appendChild(makeMessage(message));
      dlg.appendChild(input);
      dlg.appendChild(actions);

      ask(dlg, function (done) {
        no.addEventListener('click', function () { done(null); });
        yes.addEventListener('click', function () { done(input.value); });
        input.addEventListener('keydown', function (e) {
          if (e.key === 'Enter') { e.preventDefault(); done(input.value); }
        });
      }, resolve, null);

      if (typeof input.focus === 'function') {
        try { input.focus(); } catch (e) { /* no focus in this environment */ }
      }
    });
  }

  global.confirmAction = confirmAction;
  global.promptAction = promptAction;
})(window);
