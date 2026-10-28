// First-run welcome page. Talks to /api/onboarding/*, /api/library and
// /api/credentials/<name> only (enforced by tests/test_welcome_ui_static.py).
//
// The AI key:
//   * lives in a type=password field, is sent once in a POST body (never in a
//     URL), and the field is emptied as soon as the request is on its way;
//   * is never written to console, storage, the DOM or an error message: what is
//     shown for a failure comes from this file, not from the server's text.
// Server data is untrusted: only written with textContent. No HTML sinks.
// Language: navigator.language "es*" -> Spanish, anything else -> English.
(function () {
  'use strict';

  var ES = /^es\b/i.test((navigator && navigator.language) || '');
  var T = {
    es: {
      title: 'Te damos la bienvenida a ConductLoop',
      subtitle: 'Dos cosas rápidas y listo. Las dos son opcionales y todo se queda en este ordenador.',
      notes_title: '1 · Tus notas',
      notes_hint: 'ConductLoop lee tus notas para responder con ellas. Nunca escribe en tu carpeta de notas: sus propias notas (resumen diario, notas de los agentes) van a una carpeta aparte.',
      notes_label: 'Carpeta con tus notas, por ejemplo un vault de Obsidian (opcional)',
      notes_ph: '~/Documents/Obsidian/MiVault',
      write_label: 'Carpeta para las notas de ConductLoop',
      create_label: 'Crearla si no existe',
      save_folders: 'Guardar e indexar mis notas',
      key_title: '2 · Clave de IA (opcional)',
      key_hint: 'La búsqueda y las notas funcionan sin ella. Para chatear y resumir, pega una clave de tu proveedor de IA. Se guarda en el Llavero de este Mac, o en un archivo cifrado si no hay, y no se vuelve a mostrar.',
      provider_label: 'Proveedor',
      key_label: 'Clave de API',
      save_key: 'Guardar clave',
      open_credentials: 'Abrir Credenciales',
      finish: 'Abrir ConductLoop',
      skip: 'Ahora no',
      open_setup: 'Ver la lista completa de configuración',
      write_env: 'Esta carpeta la fija una variable de entorno de este equipo: aquí no se puede cambiar.',
      saved_folders: 'Guardado. Indexando tus notas en segundo plano.',
      saved_folders_only: 'Guardado.',
      saved_key_keychain: 'Clave guardada en el Llavero.',
      saved_key_encrypted: 'Clave guardada en un archivo cifrado.',
      saved_key: 'Clave guardada.',
      key_empty: 'Pega primero la clave.',
      key_locked: 'El archivo cifrado está bloqueado. Ábrelo en Credenciales con tu contraseña y vuelve a pegar la clave.',
      key_failed: 'No se pudo guardar la clave. Inténtalo de nuevo o usa la página de Credenciales.',
      key_bad: 'Esa clave no se ha aceptado. Revisa que la has copiado entera.',
      failed: 'No se pudo completar. Inténtalo de nuevo.',
      nothing: 'No hay nada que guardar: escribe una carpeta.'
    },
    en: {
      write_env: 'This folder is set by an environment variable on this computer: it cannot be changed here.',
      saved_folders: 'Saved. Indexing your notes in the background.',
      saved_folders_only: 'Saved.',
      saved_key_keychain: 'Key saved in the Keychain.',
      saved_key_encrypted: 'Key saved in an encrypted file.',
      saved_key: 'Key saved.',
      key_empty: 'Paste the key first.',
      key_locked: 'The encrypted file is locked. Open it in Credentials with your passphrase, then paste the key again.',
      key_failed: 'The key could not be saved. Try again or use the Credentials page.',
      key_bad: 'That key was not accepted. Check that you copied all of it.',
      failed: 'That did not work. Try again.',
      nothing: 'Nothing to save: type a folder.'
    }
  };
  function t(key) {
    var d = ES ? T.es : T.en;
    return d[key] !== undefined ? d[key] : (T.es[key] || key);
  }

  function $(id) { return document.getElementById(id); }

  function applyLanguage() {
    document.documentElement.lang = ES ? 'es' : 'en';
    if (!ES) return;  // the HTML is already in English
    var nodes = document.querySelectorAll('[data-i18n]');
    for (var i = 0; i < nodes.length; i++) nodes[i].textContent = t(nodes[i].getAttribute('data-i18n'));
    var phs = document.querySelectorAll('[data-i18n-ph]');
    for (var j = 0; j < phs.length; j++) phs[j].setAttribute('placeholder', t(phs[j].getAttribute('data-i18n-ph')));
  }

  function csrfToken() {
    var m = /(?:^|;\s*)ops_csrf=([^;]+)/.exec(document.cookie || '');
    if (!m) return '';
    try { return decodeURIComponent(m[1]); } catch (e) { return m[1]; }
  }

  // Resolves {status, data}; never throws on an HTTP error, so the caller can map it.
  function api(method, path, body) {
    var opt = { method: method, headers: {} };
    if (body !== undefined) {
      opt.headers['Content-Type'] = 'application/json';
      opt.body = JSON.stringify(body);
    }
    if (method !== 'GET') { var c = csrfToken(); if (c) opt.headers['X-CSRF-Token'] = c; }
    return fetch(path, opt).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (data) {
        return { status: r.status, data: data || {} };
      });
    });
  }

  function say(id, text, kind) {
    var el = $(id);
    el.textContent = text || '';
    el.className = 'status' + (kind ? ' ' + kind : '');
  }

  function home() { window.location.replace('/'); }

  var envLocked = false;

  function loadFolders() {
    return api('GET', '/api/library').then(function (r) {
      if (r.status !== 200) return;
      var w = $('write-folder');
      w.value = r.data.write_vault || '';
      envLocked = r.data.write_vault_source === 'env';
      if (envLocked) {
        w.disabled = true;
        $('create-folder').disabled = true;
        var note = $('write-note');
        note.textContent = t('write_env');
        note.hidden = false;
      }
    });
  }

  function saveFolders() {
    var notes = $('notes-folder').value.trim();
    var write = $('write-folder').value.trim();
    if (!notes && (!write || envLocked)) { say('folders-status', t('nothing'), 'bad'); return; }
    var btn = $('save-folders');
    btn.disabled = true;
    say('folders-status', '');
    api('GET', '/api/library').then(function (cur) {
      var body = {};
      if (notes) {
        var folders = (cur.data.index_folders || []).slice();
        if (folders.indexOf(notes) < 0) folders.push(notes);
        body.index_folders = folders;
      }
      if (write && !envLocked) {
        body.write_vault = write;
        body.create_write_vault = $('create-folder').checked;
      }
      return api('POST', '/api/library', body);
    }).then(function (r) {
      if (r.status !== 200) {
        // The server's message here is about a folder path: "not a folder: ...".
        say('folders-status', (r.data && r.data.message) ? String(r.data.message) : t('failed'), 'bad');
        return null;
      }
      if (!notes) { say('folders-status', t('saved_folders_only'), 'ok'); return null; }
      return api('POST', '/api/library/index').then(function () {
        say('folders-status', t('saved_folders'), 'ok');
      });
    }).catch(function () {
      say('folders-status', t('failed'), 'bad');
    }).then(function () { btn.disabled = false; });
  }

  function saveKey() {
    var field = $('api-key');
    var value = field.value.trim();
    if (!value) { say('key-status', t('key_empty'), 'bad'); return; }
    var provider = $('provider').value;
    var btn = $('save-key');
    btn.disabled = true;
    $('key-link').hidden = true;
    say('key-status', '');
    var req = api('POST', '/api/credentials/byok.' + encodeURIComponent(provider) + '.default', { value: value });
    field.value = '';   // the key is in flight; do not keep it in the page
    value = '';
    req.then(function (r) {
      if (r.status === 200) {
        var where = r.data && r.data.where;
        say('key-status', where === 'keychain' ? t('saved_key_keychain')
          : where === 'encrypted' ? t('saved_key_encrypted') : t('saved_key'), 'ok');
      } else if (r.status === 423) {
        say('key-status', t('key_locked'), 'bad');
        $('key-link').hidden = false;
      } else if (r.status === 400) {
        say('key-status', t('key_bad'), 'bad');
      } else {
        say('key-status', t('key_failed'), 'bad');
        $('key-link').hidden = false;
      }
    }).catch(function () {
      say('key-status', t('key_failed'), 'bad');
    }).then(function () { btn.disabled = false; });
  }

  function leave(kind) {
    api('POST', '/api/onboarding/' + kind).then(home, home);
  }

  function start() {
    applyLanguage();
    $('save-folders').addEventListener('click', saveFolders);
    $('save-key').addEventListener('click', saveKey);
    $('finish').addEventListener('click', function () { leave('complete'); });
    $('skip').addEventListener('click', function () { leave('dismiss'); });
    loadFolders().catch(function () {
      var e = $('global-error');
      e.textContent = t('failed');
      e.hidden = false;
    });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();
