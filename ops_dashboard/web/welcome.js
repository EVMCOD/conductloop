// First-run welcome page. Talks to /api/onboarding/*, /api/library and
// /api/credentials/<name> only (enforced by tests/test_welcome_ui_static.py).
//
// Two screens: the first says what the app is and lets the user see it with
// sample data before it asks for a single thing; the second asks for the two
// things that configure it.
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
      lead: 'Tu ordenador se convierte en un segundo cerebro de IA privado. Tus notas, tu código y tus cifras se quedan en tu máquina — y puedes preguntarle desde el móvil.',
      value1: 'Lee tus notas y responde con ellas, con un enlace a la nota que usó cada respuesta.',
      value2: 'Un equipo de agentes trabaja en tus proyectos mientras tú haces otra cosa.',
      value3: 'Nada sale de tu ordenador: ni cuenta, ni nube, ni suscripción. Nunca.',
      try_demo: 'Probarlo con datos de ejemplo',
      setup_now: 'Configurar con mis notas',
      try_demo_hint: 'Los datos de ejemplo son ficticios y se borran en cuanto indexes tus propias notas.',
      setup_title: 'Dos cosas rápidas y listo',
      subtitle: 'Las dos son opcionales y todo se queda en este ordenador.',
      notes_title: '1 · Tus notas',
      notes_hint: 'ConductLoop lee tus notas para responder con ellas. Nunca escribe en tu carpeta de notas: sus propias notas (resumen diario, notas de los agentes) van a una carpeta aparte.',
      notes_label: 'Carpeta con tus notas, por ejemplo un vault de Obsidian (opcional)',
      notes_ph: '~/Documents/Obsidian/MiVault',
      write_label: 'Carpeta para las notas de ConductLoop',
      create_label: 'Crearla si no existe',
      save_folders: 'Guardar e indexar mis notas',
      key_title: '2 · Clave de IA (opcional)',
      key_hint: 'La búsqueda y las notas funcionan sin ella. Para chatear y resumir, pega una clave de tu proveedor de IA. Se guarda en el llavero del sistema (el Llavero en un Mac) o, si no hay, en un archivo cifrado, y no se vuelve a mostrar.',
      provider_label: 'Proveedor',
      key_label: 'Clave de API',
      save_key: 'Guardar clave',
      open_credentials: 'Abrir Credenciales',
      finish: 'Abrir ConductLoop',
      back: 'Atrás',
      skip: 'Ahora no',
      open_setup: 'Ver la lista completa de configuración',
      write_env: 'Esta carpeta la fija una variable de entorno de este equipo: aquí no se puede cambiar.',
      saved_folders: 'Guardado. Indexando tus notas en segundo plano.',
      saved_folders_only: 'Guardado.',
      indexed: '{n} notas indexadas. El Brain ya responde con ellas.',
      saved_key_keychain: 'Clave guardada en el Llavero.',
      saved_key_encrypted: 'Clave guardada en un archivo cifrado.',
      saved_key: 'Clave guardada.',
      key_empty: 'Pega primero la clave.',
      key_locked: 'El archivo cifrado está bloqueado. Ábrelo en Credenciales con tu contraseña y vuelve a pegar la clave.',
      key_failed: 'No se pudo guardar la clave. Inténtalo de nuevo o usa la página de Credenciales.',
      key_bad: 'Esa clave no se ha aceptado. Revisa que la has copiado entera.',
      failed: 'No se pudo completar. Inténtalo de nuevo.',
      nothing: 'No hay nada que guardar: escribe una carpeta.',
      choose: 'Elegir…',
      choose_failed: 'No se pudo abrir el selector de carpetas: escribe la ruta.',
      pass_hint: 'En este ordenador no hay llavero del sistema. Elige una contraseña para guardar la clave en un archivo cifrado. Te la pediremos de nuevo la próxima vez que abras la app.',
      pass_label: 'Contraseña del archivo cifrado',
      save_pass: 'Crear archivo cifrado',
      pass_empty: 'Escribe primero una contraseña.',
      pass_ok: 'Archivo cifrado listo. Ya puedes pegar la clave.',
      pass_bad: 'Esa contraseña no abre el archivo cifrado.',
      pass_failed: 'No se pudo crear el archivo cifrado. Inténtalo de nuevo.',
      saved_key_plaintext: 'Clave guardada en texto claro — ábrela en Credenciales para cifrarla.',
      demo_failed: 'No se pudieron cargar los datos de ejemplo.',
      demo_present: 'Hay datos de ejemplo cargados ({n} notas ficticias). Se borran en cuanto indexes tus propias notas.',
      demo_cleared: 'Datos de ejemplo borrados.'
    },
    en: {
      write_env: 'This folder is set by an environment variable on this computer: it cannot be changed here.',
      saved_folders: 'Saved. Indexing your notes in the background.',
      saved_folders_only: 'Saved.',
      indexed: '{n} notes indexed. The Brain answers from them now.',
      saved_key_keychain: 'Key saved in the Keychain.',
      saved_key_encrypted: 'Key saved in an encrypted file.',
      saved_key: 'Key saved.',
      key_empty: 'Paste the key first.',
      key_locked: 'The encrypted file is locked. Open it in Credentials with your passphrase, then paste the key again.',
      key_failed: 'The key could not be saved. Try again or use the Credentials page.',
      key_bad: 'That key was not accepted. Check that you copied all of it.',
      failed: 'That did not work. Try again.',
      nothing: 'Nothing to save: type a folder.',
      choose: 'Choose…',
      choose_failed: 'The folder picker could not open: type the path.',
      pass_hint: 'This computer has no system keychain available. Choose a passphrase so the key is stored in an encrypted file. You will be asked for it again next time you open the app.',
      pass_label: 'Passphrase for the encrypted file',
      save_pass: 'Create encrypted file',
      pass_empty: 'Type a passphrase first.',
      pass_ok: 'Encrypted file ready. You can paste the key now.',
      pass_bad: 'That passphrase does not open the encrypted file.',
      pass_failed: 'The encrypted file could not be created. Try again.',
      saved_key_plaintext: 'Key saved in plain text — open Credentials to encrypt it.',
      demo_failed: 'The sample data could not be loaded.',
      demo_present: 'Sample data is loaded ({n} fictional notes). It is removed the moment you index your own notes.',
      demo_cleared: 'Sample data removed.',
      back: 'Back'
    }
  };
  function t(key) {
    var d = ES ? T.es : T.en;
    return d[key] !== undefined ? d[key] : (T.es[key] || key);
  }

  function $(id) { return document.getElementById(id); }

  // The language is the person's choice: the app remembers it, and this page
  // offers the same selector. The browser's language is only the guess for
  // someone who has not chosen yet.
  function chosenLocale() {
    if (window._i18n && window._i18n.getLocale) {
      var l = window._i18n.getLocale();
      if (l === 'es' || l === 'en') return l;
    }
    return ES ? 'es' : 'en';
  }

  // The page is written in English; the Spanish text replaces it. The
  // original words are kept so that switching back to English puts them back.
  var originals = null;

  function applyLanguage() {
    var locale = chosenLocale();
    ES = locale === 'es';
    document.documentElement.lang = locale;
    var nodes = document.querySelectorAll('[data-welcome]');
    var phs = document.querySelectorAll('[data-welcome-ph]');
    if (!originals) {
      originals = [];
      for (var k = 0; k < nodes.length; k++) originals.push([nodes[k], nodes[k].textContent]);
    } else {
      for (var m = 0; m < originals.length; m++) originals[m][0].textContent = originals[m][1];
    }
    if (!ES) return;  // the HTML is already in English
    for (var i = 0; i < nodes.length; i++) nodes[i].textContent = t(nodes[i].getAttribute('data-welcome'));
    for (var j = 0; j < phs.length; j++) phs[j].setAttribute('placeholder', t(phs[j].getAttribute('data-welcome-ph')));
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
        var pick = w.parentNode.querySelector('button');
        if (pick) pick.disabled = true;
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
      // The user's own notes replace the sample ones: two sets of notes in one
      // workspace would make every answer half fiction.
      return removeSample().then(function () {
        return api('POST', '/api/library/index');
      }).then(function () {
        say('folders-status', t('saved_folders'), 'ok');
        return countRealNotes();
      }).then(function (before) {
        return watchIndex(before);
      });
    }).catch(function () {
      say('folders-status', t('failed'), 'bad');
    }).then(function () { btn.disabled = false; });
  }

  function showPassphrase(needed) {
    var block = $('passphrase-block');
    if (!block) return;
    block.hidden = !needed;
  }

  function loadVaultState() {
    return api('GET', '/api/credentials').then(function (r) {
      if (r.status !== 200) return;
      // No keychain and the encrypted vault is not open yet: ask for a
      // passphrase here so the key never lands in config.json as plain text.
      var need = r.data && r.data.keychain === false && r.data.open === false;
      showPassphrase(!!need);
    });
  }

  function savePassphrase() {
    var field = $('passphrase');
    var value = field.value;
    if (!value) { say('pass-status', t('pass_empty'), 'bad'); return; }
    var btn = $('save-passphrase');
    btn.disabled = true;
    say('pass-status', '');
    api('POST', '/api/credentials/unlock', { passphrase: value }).then(function (r) {
      field.value = '';
      if (r.status === 200) {
        say('pass-status', t('pass_ok'), 'ok');
        showPassphrase(false);
      } else if (r.status === 401) {
        say('pass-status', t('pass_bad'), 'bad');
      } else {
        say('pass-status', t('pass_failed'), 'bad');
      }
    }).catch(function () {
      say('pass-status', t('pass_failed'), 'bad');
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
          : where === 'encrypted' ? t('saved_key_encrypted')
          : where === 'plaintext' ? t('saved_key_plaintext')
          : t('saved_key'), where === 'plaintext' ? 'bad' : 'ok');
        if (where === 'plaintext') $('key-link').hidden = false;
      } else if (r.status === 423) {
        say('key-status', t('key_locked'), 'bad');
        showPassphrase(true);
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

  // Inside the desktop app, a "Choose…" button next to each folder field opens
  // the native macOS folder picker (Tauri command pick_folder, granted to this
  // origin only by capabilities/folder-picker.json). In a plain browser there
  // is no button: the path is typed, as before.
  function addFolderPickers() {
    var ipc = window.__TAURI_INTERNALS__;
    if (!ipc || typeof ipc.invoke !== 'function') return;
    ['notes-folder', 'write-folder'].forEach(function (id) {
      var input = $(id);
      var wrap = document.createElement('div');
      wrap.className = 'picker';
      input.parentNode.insertBefore(wrap, input);
      wrap.appendChild(input);
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'ghost';
      btn.textContent = t('choose');
      btn.addEventListener('click', function () {
        btn.disabled = true;
        ipc.invoke('pick_folder', {}).then(function (path) {
          if (path) input.value = path;
        }, function () {
          say('folders-status', t('choose_failed'), 'bad');
        }).then(function () { btn.disabled = input.disabled; });
      });
      btn.disabled = input.disabled;
      wrap.appendChild(btn);
    });
  }

  // --- the two screens ----------------------------------------------------

  function showScreen(name) {
    $('screen-intro').hidden = name !== 'intro';
    $('screen-setup').hidden = name !== 'setup';
    if (name === 'setup') {
      var f = $('notes-folder');
      if (f && !f.disabled) f.focus();
    } else {
      window.scrollTo(0, 0);
    }
  }

  function countRealNotes() {
    return api('GET', '/api/onboarding/state').then(function (r) {
      if (r.status !== 200) return 0;
      return r.data.real_documents || 0;
    }, function () { return 0; });
  }

  // The index runs in the background, so the count arrives later. Wait a
  // little for it: "42 notes indexed" is the first proof that the app is
  // working, and it is worth a few seconds of waiting.
  function watchIndex(before) {
    var tries = 0;
    function poll() {
      tries += 1;
      api('GET', '/api/onboarding/state').then(function (r) {
        if (r.status !== 200) return;
        var n = r.data.real_documents || 0;
        if (n > before) {
          say('folders-status', t('indexed').replace('{n}', String(n)), 'ok');
          return;
        }
        if (tries >= 20) return;
        setTimeout(poll, 1000);
      }, function () {});
    }
    poll();
  }

  function removeSample() {
    return api('POST', '/api/onboarding/demo/clear', {}).then(function (r) {
      return r.status === 200;
    }, function () { return false; });
  }

  function tryDemo() {
    var btn = $('try-demo');
    btn.disabled = true;
    say('demo-status', '');
    // The sample workspace is written in the language the person chose: a
    // Spanish app gets a Spanish second brain, not someone else's laptop.
    api('POST', '/api/onboarding/demo', { lang: chosenLocale() }).then(function (r) {
      if (r.status !== 200) {
        say('demo-status', t('demo_failed'), 'bad');
        btn.disabled = false;
        return;
      }
      // The guide has done its job: the sample notes are the workspace now,
      // and the welcome page must not grab the user on every launch.
      api('POST', '/api/onboarding/complete', {}).then(home, home);
    }, function () {
      say('demo-status', t('demo_failed'), 'bad');
      btn.disabled = false;
    });
  }

  function applyState(st) {
    if (!st) return;
    var sample = st.demo_documents || 0;
    var real = st.real_documents || 0;
    // The sample data can only be loaded into a workspace that has no real
    // notes of its own, and it is not worth offering twice.
    if (real > 0 || sample > 0) {
      $('try-demo').hidden = true;
      $('demo-hint').hidden = true;
    }
    if (sample > 0) {
      var note = $('demo-note');
      note.textContent = t('demo_present').replace('{n}', String(sample));
      note.hidden = false;
    }
  }

  function loadState() {
    return api('GET', '/api/onboarding/state').then(function (r) {
      if (r.status !== 200) return null;
      applyState(r.data);
      return r.data;
    }, function () { return null; });
  }

  function start() {
    applyLanguage();
    // The person can change the language from this very page.
    window.addEventListener('localechange', function () {
      applyLanguage();
      if ($('screen-setup').hidden) showScreen('intro');
    });
    addFolderPickers();
    $('save-folders').addEventListener('click', saveFolders);
    $('save-key').addEventListener('click', saveKey);
    var passBtn = $('save-passphrase');
    if (passBtn) passBtn.addEventListener('click', savePassphrase);
    $('finish').addEventListener('click', function () { leave('complete'); });
    $('skip').addEventListener('click', function () { leave('dismiss'); });
    var skipSetup = $('skip-setup');
    if (skipSetup) skipSetup.addEventListener('click', function () { leave('dismiss'); });
    $('setup').addEventListener('click', function () { showScreen('setup'); });
    $('back').addEventListener('click', function () { showScreen('intro'); });
    $('try-demo').addEventListener('click', tryDemo);
    loadState().then(loadFolders).then(loadVaultState).catch(function () {
      var e = $('global-error');
      e.textContent = t('failed');
      e.hidden = false;
    });
  }

  // The chosen language arrives with the dictionaries: wait for them, so the
  // first screen is already in the right language.
  if (window._i18n && window._i18n.ready) window._i18n.ready().then(start, start);
  else if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();