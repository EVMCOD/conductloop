// "Aprendizaje" page (learning PR L3). Talks only to /api/learning/*.
//
// Security rules (enforced by tests/test_learning_ui_static.py):
//   * Server data (answer excerpts, comments, provider/model names, codes) is
//     untrusted: only written with textContent / createTextNode. No innerHTML,
//     outerHTML, insertAdjacentHTML, document.write, eval or string handlers.
// Language: navigator.language "es*" → Spanish, anything else → English
// (same rule as sessions.js / provider-errors.js).
(function () {
  'use strict';

  var ES = /^es\b/i.test((navigator && navigator.language) || '');
  var L = {
    es: {
      tab_metrics: '¿Aprendo bien?', m_title: '¿Estoy aprendiendo bien?', m_7: '7 días', m_30: '30 días', m_90: '90 días',
      m_hint: 'Calculado solo con los datos de este ordenador. Con un único usuario son tendencias, no estadística: por debajo de 20 respuestas no se saca ninguna conclusión.',
      m_down: '👎 por 100 respuestas', m_with: 'con lecciones', m_without: 'sin lecciones', m_details: 'Detalle', m_settings: 'Ajustes',
      m_ab: 'Prueba A/B local: % de respuestas sin lecciones (0 = apagada)',
      m_curator: 'Curador diario: proponer archivar skills sin uso en 60 días (nunca borra)',
      m_n: '{0} (n={1})', m_low: 'poca señal todavía (menos de {0} respuestas)',
      s_downvote_trend_better: 'Los 👎 bajan: {0} → {1} por 100 respuestas.', s_downvote_trend_worse: 'Los 👎 suben: {0} → {1} por 100 respuestas.',
      s_downvote_trend_flat: 'Los 👎 se mantienen ({0} → {1} por 100).', s_downvote_trend_low_signal: 'Tendencia de 👎: poca señal todavía.',
      s_lessons_effect_helps: 'Las respuestas con lecciones reciben menos 👎 ({0} frente a {1} por 100).',
      s_lessons_effect_hurts: 'Las respuestas con lecciones reciben más 👎 ({0} frente a {1} por 100): revisa las lecciones.',
      s_lessons_effect_no_difference: 'Con y sin lecciones, los 👎 son parecidos ({0} frente a {1}).',
      s_lessons_effect_low_signal: 'Efecto de las lecciones: poca señal todavía.',
      s_ab_effect_helps: 'A/B: el grupo con lecciones va mejor ({0} frente a {1} 👎 por 100).',
      s_ab_effect_hurts: 'A/B: el grupo sin lecciones va mejor ({0} frente a {1} 👎 por 100).',
      s_ab_effect_no_difference: 'A/B: sin diferencia clara ({0} frente a {1}).', s_ab_effect_low_signal: 'A/B: poca señal todavía.',
      s_lessons_to_review: '{0} lecciones activas acumulan 👎: revísalas en "Lo que he aprendido".',
      s_unused_skills: '{0} skills sin uso en 60 días: puedes proponer archivarlas.',
      d_lessons: 'Lecciones: {0} activas · {1} archivadas ({2} %) · {3} propuestas', d_lesson: 'L{0}: {1} usos · {2} respuestas · {3} 👎',
      d_skills: 'Skills: {0} indexadas · {1} activaciones · {2} sin uso', d_props: 'Propuestas de skills: {0} aprobadas · {1} rechazadas ({2} % aprobadas) · ~{3} €',
      d_skill_delta: 'Diferencia con/sin skill: necesita evals en vivo (todavía no disponibles).',
      d_sessions: 'Sesiones: {0} % de tareas aceptadas a la primera (n={1})', d_sessions_ba: 'Antes de las lecciones: {0} % (n={1}) · después: {2} % (n={3})',
      d_sessions_na: 'Sesiones: no disponibles en esta versión.', d_evals: 'Evals offline (referencia): {0} ({1}/{2})', d_evals_na: 'Evals offline: no incluidas.',
      d_ab: 'A/B: con lecciones {0} · sin lecciones {1}', saved: 'Guardado.',
      tab_lessons: 'Lo que he aprendido', tab_skills: 'Skills', tab_feedback: 'Valoraciones', tab_failures: 'Fallos', tab_evals: 'Evals',
      nav_brain: 'Brain', evals: 'Evals', read_only: 'solo lectura',
      evals_hint: 'Casos de evaluación offline (respuestas grabadas, sin red ni claves). Esta página solo los muestra: no ejecuta nada ni llama a ningún modelo.',
      evals_none: 'Esta instalación no incluye los casos de evaluación (solo vienen con el código fuente). No hay nada que mostrar.',
      evals_no_harness: 'Casos encontrados en {0}. El arnés para ejecutarlos todavía no está en esta versión; aquí solo se leen los ficheros.',
      evals_harness: 'Casos en {0}. Para ejecutarlos (offline, sin claves) usa la terminal:',
      evals_score: 'puntuación de referencia', evals_passed: 'casos que pasan', evals_suites: 'suites',
      evals_cases: '{0} casos · {1} obligatorios', evals_baseline: 'referencia: {0}/{1} ({2})',
      evals_required: 'obligatorio', evals_pass: 'pasa', evals_fail: 'falla', evals_runs: 'Ejecuciones locales',
      evals_no_runs: 'Sin ejecuciones locales (las evals en vivo llegarán en otra versión).',
      sp_title: 'Skills propuestas', sp_curator: 'Buscar skills sin uso (60 días)',
      sp_hint: 'El agente solo propone. Nada se escribe en tu vault hasta que apruebas, y solo se proponen skills cuando pulsas un botón. Las skills sin uso nunca se borran: como mucho se propone archivarlas.',
      sp_note: 'Nota del vault', sp_folder: 'Carpeta local', sp_ref_ph: 'nota del vault (Carpeta/Nota.md) o carpeta local',
      sp_mode_h: 'Borrador sin IA', sp_mode_o: 'Con Ollama', sp_create: 'Crear skill desde…', sp_none: 'No hay propuestas pendientes.',
      sp_counts: '{0} pendientes', sp_a_create: 'nueva', sp_a_patch: 'mejora', sp_a_archive: 'archivar', sp_a_delete: 'borrar (a la papelera)',
      st_pending: 'pendiente', st_approved: 'aprobada', st_superseded: 'sustituida', st_failed: 'fallida',
      sp_blocked: 'Bloqueada por el linter: edítala antes de aprobar.', sp_stale: 'La skill cambió desde la propuesta: al aprobar fallará; vuelve a proponerla.',
      sp_save: 'Guardar cambios', sp_reject_why: 'Motivo del rechazo (opcional):', sp_none_made: 'No hay skills sin uso desde hace {0} días.',
      sp_made: 'Propuestas creadas: {0}. Revísalas en "Skills propuestas".', sp_from_conv: 'Crear skill (mismo modelo)', sp_from_conv_o: 'Crear skill (Ollama)',
      sp_improve: 'Mejorar skill', sp_improve_o: 'Mejorar (Ollama)', sp_pick: '— skill —', sp_archive: 'Proponer archivar', sp_delete: 'Proponer borrar',
      sp_send_warn: 'Se enviará el contexto a {0} para redactar la propuesta (tope {1} €). ¿Seguir?', sp_same_model: 'ese mismo proveedor (el de la respuesta)',
      sp_dup: 'Ya existía una propuesta igual (pendiente o rechazada).', sp_written: 'Escrita en {0}',
      lessons: 'Lo que he aprendido', lessons_enabled: 'Usar lecciones en las respuestas',
      lessons_hint: 'Reglas que tú apruebas. Nada se activa solo: cada propuesta espera tu aprobación y solo se crean cuando pulsas un botón.',
      lessons_mirror: 'Copia de solo lectura en el vault', caps: 'máx. {0} lecciones · ~{1} tokens',
      counts: '{0} propuestas · {1} activas', budget: 'Presupuesto por propuesta con IA (EUR)', save: 'Guardar',
      price_provider: 'proveedor', price_model: 'modelo', price_in: '€ entrada / 1M', price_out: '€ salida / 1M',
      price_save: 'Guardar precio', prices_lbl: 'Precios: {0}', no_prices: 'Sin precios: las propuestas con IA de pago se bloquean (Ollama es gratis).',
      lesson_new_ph: 'Nueva lección…', scope_ph: 'global · project:clave · repo:/ruta', lesson_add: 'Añadir',
      no_lessons: 'Aún no hay lecciones.', approve: 'Aprobar', reject: 'Rechazar', archive: 'Archivar', edit: 'Editar',
      save_version: 'Guardar como nueva versión', uses: '{0} usos', review_lbl: '👎 en {0} respuestas que usaron esta lección: revísala',
      st_proposed: 'propuesta', st_active: 'activa', st_rejected: 'rechazada', st_archived: 'archivada',
      src_feedback: '👎', src_correction: 'corrección', src_manual: 'manual', src_session_review: 'revisión de sesión',
      propose_h: 'Proponer lección (sin IA)', propose_llm: 'Proponer con el mismo modelo', propose_ollama: 'Proponer con Ollama',
      find_corrections: 'Buscar correcciones', proposed_ok: 'Propuesta creada; revísala en "Lo que he aprendido".',
      no_corrections: 'No he encontrado correcciones en esa conversación.', cost: '~{0} €',
      skills: 'Skills', reindex: 'Reindexar', no_skills: 'No hay skills indexadas.',
      skills_hint: 'Skills en formato agentskills.io. Viven en tu vault (carpeta skills); nunca se descargan de Internet y sus scripts nunca se ejecutan.',
      skills_external: 'Leer también ~/.agents/skills y ~/.claude/skills (solo lectura)',
      skills_vault: 'Carpeta: {0}', enabled_lbl: 'activa', invalid: 'no válida', activations: '{0} usos',
      export_skill: 'Exportar a ~/.agents/skills', exported_skill: 'Exportada a {0}',
      reindexed: 'Reindexadas: {0}',
      trusted_repos: 'Repos de confianza (skills de proyecto)',
      trusted_hint: 'Las skills de <repo>/.agents/skills solo se usan en repos que marques aquí. Desactivado por defecto.',
      trust_ph: '/ruta/al/repo', trust_add: 'Confiar', untrust: 'Quitar', no_trusted: 'Ningún repo marcado.',
      s_vault: 'vault', s_agents_dir: '~/.agents', s_claude_dir: '~/.claude', s_project: 'proyecto',
      title: 'Aprendizaje',
      subtitle: 'Lo que le has dicho a ConductLoop sobre sus respuestas. Todo se guarda solo en este ordenador.',
      nav_home: 'Panel', nav_ai: 'Conectar IA', summary: 'Resumen', failures_short: 'fallos',
      privacy: 'Privacidad',
      privacy_feedback: 'Las valoraciones 👍/👎 se guardan siempre en local cuando las pulsas. Nunca se envían a ningún sitio.',
      failure_toggle: 'Registrar fallos en este ordenador',
      failure_hint: 'Desactivado por defecto. Solo guarda códigos de error (proveedor, modelo, tipo), nunca preguntas, respuestas ni notas. Se borran a los 30 días; al desactivarlo se borra todo.',
      export: 'Exportar a un fichero local', export_comments: 'incluir comentarios',
      exported: 'Exportado (no se ha enviado a ningún sitio): {0}',
      feedback: 'Valoraciones', all: 'Todas', delete: 'Borrar',
      no_feedback: 'Aún no hay valoraciones. Usa 👍/👎 bajo las respuestas del Brain.',
      failures: 'Registro de fallos', clear: 'Borrar todo', when: 'Cuándo', kind: 'Tipo', code: 'Código',
      provider: 'Proveedor', model: 'Modelo', no_failures: 'Sin fallos registrados.',
      failures_off: 'El registro de fallos está desactivado.',
      confirm_clear: '¿Borrar todo el registro de fallos?', error: 'Error: {0}',
      r_wrong: 'incorrecta', r_missing_citation: 'falta cita', r_bad_citation: 'cita errónea',
      r_outdated: 'desactualizada', r_style: 'estilo', r_unsafe: 'insegura', r_other: 'otro',
      k_provider_error: 'proveedor', k_timeout: 'tiempo agotado', k_broken_citation: 'cita rota',
      k_tool_error: 'herramienta', k_budget: 'presupuesto', k_eval_regression: 'regresión de eval'
    },
    en: {
      tab_metrics: 'Am I learning?', m_title: 'Am I learning well?', m_7: '7 days', m_30: '30 days', m_90: '90 days',
      m_hint: 'Computed only from the data on this computer. With a single user these are trends, not statistics: below 20 answers no conclusion is drawn.',
      m_down: '👎 per 100 answers', m_with: 'with lessons', m_without: 'without lessons', m_details: 'Details', m_settings: 'Settings',
      m_ab: 'Local A/B test: % of answers without lessons (0 = off)',
      m_curator: 'Daily curator: propose archiving skills unused for 60 days (never deletes)',
      m_n: '{0} (n={1})', m_low: 'too little signal yet (fewer than {0} answers)',
      s_downvote_trend_better: '👎 are going down: {0} → {1} per 100 answers.', s_downvote_trend_worse: '👎 are going up: {0} → {1} per 100 answers.',
      s_downvote_trend_flat: '👎 are stable ({0} → {1} per 100).', s_downvote_trend_low_signal: '👎 trend: too little signal yet.',
      s_lessons_effect_helps: 'Answers with lessons get fewer 👎 ({0} vs {1} per 100).',
      s_lessons_effect_hurts: 'Answers with lessons get more 👎 ({0} vs {1} per 100): review your lessons.',
      s_lessons_effect_no_difference: 'With and without lessons, 👎 are similar ({0} vs {1}).',
      s_lessons_effect_low_signal: 'Effect of lessons: too little signal yet.',
      s_ab_effect_helps: 'A/B: the lessons group does better ({0} vs {1} 👎 per 100).',
      s_ab_effect_hurts: 'A/B: the no-lessons group does better ({0} vs {1} 👎 per 100).',
      s_ab_effect_no_difference: 'A/B: no clear difference ({0} vs {1}).', s_ab_effect_low_signal: 'A/B: too little signal yet.',
      s_lessons_to_review: '{0} active lessons collect 👎: review them in "What I have learned".',
      s_unused_skills: '{0} skills unused for 60 days: you can propose archiving them.',
      d_lessons: 'Lessons: {0} active · {1} archived ({2} %) · {3} proposed', d_lesson: 'L{0}: {1} uses · {2} answers · {3} 👎',
      d_skills: 'Skills: {0} indexed · {1} activations · {2} unused', d_props: 'Skill proposals: {0} approved · {1} rejected ({2} % approved) · ~{3} €',
      d_skill_delta: 'With/without skill difference: needs live evals (not available yet).',
      d_sessions: 'Sessions: {0} % of tasks accepted on the first try (n={1})', d_sessions_ba: 'Before lessons: {0} % (n={1}) · after: {2} % (n={3})',
      d_sessions_na: 'Sessions: not available in this version.', d_evals: 'Offline evals (baseline): {0} ({1}/{2})', d_evals_na: 'Offline evals: not included.',
      d_ab: 'A/B: with lessons {0} · without lessons {1}', saved: 'Saved.',
      tab_lessons: 'What I have learned', tab_skills: 'Skills', tab_feedback: 'Ratings', tab_failures: 'Failures', tab_evals: 'Evals',
      nav_brain: 'Brain', evals: 'Evals', read_only: 'read only',
      evals_hint: 'Offline evaluation cases (recorded answers, no network, no keys). This page only shows them: it runs nothing and calls no model.',
      evals_none: 'This installation does not include the evaluation cases (they only ship with the source code). Nothing to show.',
      evals_no_harness: 'Cases found in {0}. The harness that runs them is not in this version yet; this page only reads the files.',
      evals_harness: 'Cases in {0}. To run them (offline, no keys) use the terminal:',
      evals_score: 'baseline score', evals_passed: 'cases passing', evals_suites: 'suites',
      evals_cases: '{0} cases · {1} required', evals_baseline: 'baseline: {0}/{1} ({2})',
      evals_required: 'required', evals_pass: 'passes', evals_fail: 'fails', evals_runs: 'Local runs',
      evals_no_runs: 'No local runs (live evals arrive in a later version).',
      sp_title: 'Proposed skills', sp_curator: 'Find unused skills (60 days)',
      sp_hint: 'The agent only proposes. Nothing is written to your vault until you approve, and skills are only proposed when you click a button. Unused skills are never deleted: at most, archiving is proposed.',
      sp_note: 'Vault note', sp_folder: 'Local folder', sp_ref_ph: 'vault note (Folder/Note.md) or local folder',
      sp_mode_h: 'Draft without AI', sp_mode_o: 'With Ollama', sp_create: 'Create skill from…', sp_none: 'No pending proposals.',
      sp_counts: '{0} pending', sp_a_create: 'new', sp_a_patch: 'improvement', sp_a_archive: 'archive', sp_a_delete: 'delete (to trash)',
      st_pending: 'pending', st_approved: 'approved', st_superseded: 'superseded', st_failed: 'failed',
      sp_blocked: 'Blocked by the linter: edit it before approving.', sp_stale: 'The skill changed since this proposal: approving will fail; propose it again.',
      sp_save: 'Save changes', sp_reject_why: 'Reason for rejecting (optional):', sp_none_made: 'No skills unused for {0} days.',
      sp_made: 'Proposals created: {0}. Review them in "Proposed skills".', sp_from_conv: 'Create skill (same model)', sp_from_conv_o: 'Create skill (Ollama)',
      sp_improve: 'Improve skill', sp_improve_o: 'Improve (Ollama)', sp_pick: '— skill —', sp_archive: 'Propose archiving', sp_delete: 'Propose deleting',
      sp_send_warn: 'The context will be sent to {0} to write the proposal (cap {1} €). Continue?', sp_same_model: 'the same provider as the answer',
      sp_dup: 'An identical proposal already existed (pending or rejected).', sp_written: 'Written to {0}',
      lessons: 'What I have learned', lessons_enabled: 'Use lessons in answers',
      lessons_hint: 'Rules you approve. Nothing activates itself: every proposal waits for your approval and is only created when you click a button.',
      lessons_mirror: 'Read-only copy in the vault', caps: 'max {0} lessons · ~{1} tokens',
      counts: '{0} proposed · {1} active', budget: 'Budget per AI proposal (EUR)', save: 'Save',
      price_provider: 'provider', price_model: 'model', price_in: '€ in / 1M', price_out: '€ out / 1M',
      price_save: 'Save price', prices_lbl: 'Prices: {0}', no_prices: 'No prices: paid AI proposals are blocked (Ollama is free).',
      lesson_new_ph: 'New lesson…', scope_ph: 'global · project:key · repo:/path', lesson_add: 'Add',
      no_lessons: 'No lessons yet.', approve: 'Approve', reject: 'Reject', archive: 'Archive', edit: 'Edit',
      save_version: 'Save as new version', uses: '{0} uses', review_lbl: '👎 on {0} answers that used this lesson: review it',
      st_proposed: 'proposed', st_active: 'active', st_rejected: 'rejected', st_archived: 'archived',
      src_feedback: '👎', src_correction: 'correction', src_manual: 'manual', src_session_review: 'session review',
      propose_h: 'Propose lesson (no AI)', propose_llm: 'Propose with the same model', propose_ollama: 'Propose with Ollama',
      find_corrections: 'Find corrections', proposed_ok: 'Proposal created; review it under "What I have learned".',
      no_corrections: 'No corrections found in that conversation.', cost: '~{0} €',
      skills: 'Skills', reindex: 'Reindex', no_skills: 'No skills indexed.',
      skills_hint: 'agentskills.io-format skills. They live in your vault (skills folder); never downloaded from the Internet and their scripts are never run.',
      skills_external: 'Also read ~/.agents/skills and ~/.claude/skills (read only)',
      skills_vault: 'Folder: {0}', enabled_lbl: 'enabled', invalid: 'invalid', activations: '{0} uses',
      export_skill: 'Export to ~/.agents/skills', exported_skill: 'Exported to {0}',
      reindexed: 'Reindexed: {0}',
      trusted_repos: 'Trusted repos (project skills)',
      trusted_hint: 'Skills in <repo>/.agents/skills are only used for repos you mark here. Off by default.',
      trust_ph: '/path/to/repo', trust_add: 'Trust', untrust: 'Remove', no_trusted: 'No repo marked.',
      s_vault: 'vault', s_agents_dir: '~/.agents', s_claude_dir: '~/.claude', s_project: 'project',
      title: 'Learning',
      subtitle: 'What you told ConductLoop about its answers. Everything stays on this computer.',
      nav_home: 'Dashboard', nav_ai: 'Connect AI', summary: 'Summary', failures_short: 'failures',
      privacy: 'Privacy',
      privacy_feedback: '👍/👎 ratings are always stored locally when you click them. They are never sent anywhere.',
      failure_toggle: 'Log failures on this computer',
      failure_hint: 'Off by default. Stores error codes only (provider, model, kind), never questions, answers or notes. Deleted after 30 days; turning it off deletes everything.',
      export: 'Export to a local file', export_comments: 'include comments',
      exported: 'Exported (nothing was sent anywhere): {0}',
      feedback: 'Ratings', all: 'All', delete: 'Delete',
      no_feedback: 'No ratings yet. Use 👍/👎 under Brain answers.',
      failures: 'Failure log', clear: 'Clear all', when: 'When', kind: 'Kind', code: 'Code',
      provider: 'Provider', model: 'Model', no_failures: 'No failures logged.',
      failures_off: 'The failure log is off.',
      confirm_clear: 'Delete the whole failure log?', error: 'Error: {0}',
      r_wrong: 'wrong', r_missing_citation: 'missing citation', r_bad_citation: 'bad citation',
      r_outdated: 'outdated', r_style: 'style', r_unsafe: 'unsafe', r_other: 'other',
      k_provider_error: 'provider', k_timeout: 'timeout', k_broken_citation: 'broken citation',
      k_tool_error: 'tool', k_budget: 'budget', k_eval_regression: 'eval regression'
    }
  };
  var T = ES ? L.es : L.en;
  function t(key) {
    var s = Object.prototype.hasOwnProperty.call(T, key) ? T[key] : key;
    for (var i = 1; i < arguments.length; i++) s = s.replace('{' + (i - 1) + '}', String(arguments[i]));
    return s;
  }
  function $(id) { return document.getElementById(id); }
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = String(text);
    return e;
  }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
  function fmtTime(sec) {
    try { return new Date(sec * 1000).toLocaleString(ES ? 'es-ES' : 'en-US'); } catch (e) { return String(sec); }
  }
  function showError(msg) {
    var g = $('global-error');
    g.textContent = t('error', msg);
    g.hidden = false;
  }

  async function api(method, path, body) {
    var opts = { method: method, headers: {} };
    if (body !== undefined) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
    var r = await fetch(path, opts);
    var data = null;
    try { data = await r.json(); } catch (e) { data = null; }
    if (!r.ok || !data || data.ok === false) {
      throw new Error((data && (data.message || data.error)) || ('HTTP ' + r.status));
    }
    return data;
  }

  function applyI18n() {
    document.documentElement.lang = ES ? 'es' : 'en';
    document.title = t('title') + ' — ConductLoop';
    var nodes = document.querySelectorAll('[data-i18n]');
    for (var i = 0; i < nodes.length; i++) nodes[i].textContent = t(nodes[i].getAttribute('data-i18n'));
    var ph = document.querySelectorAll('[data-i18n-ph]');
    for (var j = 0; j < ph.length; j++) ph[j].setAttribute('placeholder', t(ph[j].getAttribute('data-i18n-ph')));
  }

  async function loadSummary() {
    var d = await api('GET', '/api/learning/summary');
    $('stat-up').textContent = String(d.feedback.up);
    $('stat-down').textContent = String(d.feedback.down);
    var nFail = 0;
    (d.failure_log.top || []).forEach(function (r) { nFail += r.n; });
    $('stat-fail').textContent = String(nFail);
    var ul = $('reasons');
    clear(ul);
    Object.keys(d.feedback.down_reasons || {}).forEach(function (k) {
      var li = el('li');
      li.appendChild(el('span', null, '👎 ' + t('r_' + k)));
      li.appendChild(el('span', null, d.feedback.down_reasons[k]));
      ul.appendChild(li);
    });
    $('failure-toggle').checked = !!d.failure_log.enabled;
  }

  async function loadFeedback() {
    var rating = $('rating-filter').value;
    var d = await api('GET', '/api/learning/feedback?limit=100' + (rating !== '0' ? '&rating=' + encodeURIComponent(rating) : ''));
    var ul = $('feedback-list');
    clear(ul);
    $('feedback-empty').hidden = d.feedback.length > 0;
    d.feedback.forEach(function (f) {
      var li = el('li');
      li.setAttribute('data-id', String(f.id));
      var meta = el('div', 'meta');
      meta.appendChild(el('span', null, f.rating === 1 ? '👍' : '👎'));
      if (f.reason_code) meta.appendChild(el('span', 'pill', t('r_' + f.reason_code)));
      if (f.provider) meta.appendChild(el('span', 'pill', f.provider + (f.model ? ' · ' + f.model : '')));
      meta.appendChild(el('span', null, fmtTime(f.updated_at)));
      var del = el('button', 'ghost small', t('delete'));
      del.type = 'button';
      del.addEventListener('click', function () {
        api('POST', '/api/learning/feedback/delete', { id: f.id }).then(refresh).catch(function (e) { showError(e.message); });
      });
      meta.appendChild(del);
      li.appendChild(meta);
      if (f.answer_excerpt) li.appendChild(el('div', 'excerpt', f.answer_excerpt));
      if (f.rating === -1) li.appendChild(proposeRow(f));
      if (f.conversation_id || f.rating === -1) li.appendChild(skillProposeRow(f));
      if (f.comment) li.appendChild(el('div', 'comment', f.comment));
      ul.appendChild(li);
    });
  }

  function btn(label, cls, onClick) {
    var b = el('button', cls || 'ghost small', label); b.type = 'button';
    b.addEventListener('click', onClick);
    return b;
  }
  function act(path, body) {
    return api('POST', path, body).then(refresh).catch(function (e) { showError(e.message); });
  }

  function proposeRow(f) {
    var row = el('div', 'row');
    function go(mode) {
      api('POST', '/api/learning/lessons/propose', { feedback_id: f.id, mode: mode })
        .then(function () { refresh(); window.alert(t('proposed_ok')); })
        .catch(function (e) { showError(e.message); });
    }
    row.appendChild(btn(t('propose_h'), null, function () { go('heuristic'); }));
    row.appendChild(btn(t('propose_llm'), null, function () { go('llm'); }));
    row.appendChild(btn(t('propose_ollama'), null, function () { go('ollama'); }));
    if (f.conversation_id) {
      row.appendChild(btn(t('find_corrections'), null, function () {
        api('POST', '/api/learning/lessons/propose-corrections', { conversation_id: f.conversation_id })
          .then(function (d) { refresh(); window.alert(d.lessons.length ? t('proposed_ok') : t('no_corrections')); })
          .catch(function (e) { showError(e.message); });
      }));
    }
    return row;
  }

  function lessonItem(l) {
    var li = el('li');
    var meta = el('div', 'meta');
    meta.appendChild(el('strong', null, 'L' + l.id));
    meta.appendChild(el('span', 'pill st-' + l.status, t('st_' + l.status)));
    meta.appendChild(el('span', 'pill', l.scope));
    meta.appendChild(el('span', null, t('src_' + l.source_kind) + (l.source_ref ? ' · ' + l.source_ref : '')));
    if (l.provider) meta.appendChild(el('span', 'pill', l.provider + (l.model ? ' · ' + l.model : '') + (l.cost_cents_est ? ' · ' + t('cost', (l.cost_cents_est / 100).toFixed(4)) : '')));
    if (l.use_count) meta.appendChild(el('span', null, t('uses', l.use_count)));
    li.appendChild(meta);
    if (l.status === 'proposed' || l.status === 'active') {
      var ta = el('textarea', 'lesson'); ta.value = l.text; ta.maxLength = 600; ta.rows = 2;
      var sc = el('input'); sc.type = 'text'; sc.value = l.scope;
      li.appendChild(ta);
      var row = el('div', 'row');
      row.appendChild(sc);
      if (l.status === 'proposed') {
        row.appendChild(btn(t('approve'), 'small', function () { act('/api/learning/lessons/approve', { id: l.id, text: ta.value, scope: sc.value }); }));
        row.appendChild(btn(t('reject'), 'danger small', function () { act('/api/learning/lessons/reject', { id: l.id }); }));
      } else {
        row.appendChild(btn(t('save_version'), null, function () { act('/api/learning/lessons/edit', { id: l.id, text: ta.value, scope: sc.value }); }));
        row.appendChild(btn(t('archive'), null, function () { act('/api/learning/lessons/archive', { id: l.id }); }));
      }
      row.appendChild(btn(t('delete'), 'danger small', function () { act('/api/learning/lessons/delete', { id: l.id }); }));
      li.appendChild(row);
    } else {
      li.appendChild(el('div', 'excerpt', l.text));
      var r2 = el('div', 'row');
      r2.appendChild(btn(t('delete'), 'danger small', function () { act('/api/learning/lessons/delete', { id: l.id }); }));
      li.appendChild(r2);
    }
    return li;
  }

  async function loadLessons() {
    var d = await api('GET', '/api/learning/lessons');
    var s = d.settings;
    $('lessons-enabled').checked = !!s.enabled;
    $('lessons-mirror').checked = !!s.vault_mirror;
    $('lessons-mirror-path').textContent = d.mirror_path;
    $('lessons-caps').textContent = t('caps', s.max_inject, s.max_tokens);
    $('lessons-counts').textContent = t('counts', d.summary.proposed, d.summary.active);
    if (document.activeElement !== $('lessons-budget')) $('lessons-budget').value = String(s.proposal_budget_eur);
    var pk = Object.keys(s.prices || {});
    $('prices').textContent = pk.length ? t('prices_lbl', pk.map(function (k) {
      return k + ' (' + s.prices[k].eur_in_per_mtok + ' / ' + s.prices[k].eur_out_per_mtok + ')';
    }).join(', ')) : t('no_prices');
    var rv = $('review-list');
    clear(rv);
    d.review.forEach(function (r) {
      var li = el('li');
      li.appendChild(el('span', null, 'L' + r.id + ' · ' + t('review_lbl', r.downvotes)));
      rv.appendChild(li);
    });
    var ul = $('lessons-list');
    clear(ul);
    $('lessons-empty').hidden = d.lessons.length > 0;
    d.lessons.forEach(function (l) { ul.appendChild(lessonItem(l)); });
  }

  async function loadFailures() {
    var d = await api('GET', '/api/learning/failures?limit=200');
    var body = $('failures-body');
    clear(body);
    d.failures.forEach(function (f) {
      var tr = el('tr');
      tr.appendChild(el('td', null, fmtTime(f.created_at)));
      tr.appendChild(el('td', null, t('k_' + f.kind)));
      tr.appendChild(el('td', 'mono', f.code));
      tr.appendChild(el('td', null, f.provider || '—'));
      tr.appendChild(el('td', null, f.model || '—'));
      body.appendChild(tr);
    });
    var empty = $('failures-empty');
    empty.hidden = d.failures.length > 0;
    empty.textContent = d.enabled ? t('no_failures') : t('failures_off');
    $('clear-failures').disabled = d.failures.length === 0;
  }

  function skillMsg(text) { var m = $('skills-msg'); m.textContent = text; m.hidden = false; }

  function renderSkills(d) {
    $('skills-external').checked = !!d.external_dirs_enabled;
    $('skills-vault').textContent = t('skills_vault', d.dirs.vault);
    var ul = $('skills-list');
    clear(ul);
    $('skills-empty').hidden = d.skills.length > 0;
    SP.skills = d.skills.filter(function (sk) { return sk.source_kind === 'vault' && sk.loadable; }).map(function (sk) { return sk.key; });
    d.skills.forEach(function (sk) {
      var li = el('li');
      var meta = el('div', 'meta');
      var lbl = el('label', 'check inline');
      var cb = el('input'); cb.type = 'checkbox'; cb.checked = sk.enabled && sk.loadable; cb.disabled = !sk.loadable;
      cb.addEventListener('change', function () {
        api('POST', '/api/learning/skills/enable', { key: sk.key, enabled: cb.checked }).catch(function (e) { showError(e.message); });
      });
      lbl.appendChild(cb);
      lbl.appendChild(el('strong', null, sk.key));
      meta.appendChild(lbl);
      meta.appendChild(el('span', 'pill', t('s_' + sk.source_kind)));
      if (!sk.loadable) meta.appendChild(el('span', 'pill err', t('invalid')));
      if (sk.activations) meta.appendChild(el('span', null, t('activations', sk.activations)));
      if (sk.source_kind === 'vault' && sk.loadable) {
        var ex = el('button', 'ghost small', t('export_skill')); ex.type = 'button';
        ex.addEventListener('click', function () {
          api('POST', '/api/learning/skills/export', { key: sk.key })
            .then(function (r) { skillMsg(t('exported_skill', r.path)); })
            .catch(function (e) { showError(e.message); });
        });
        meta.appendChild(ex);
      }
      if (sk.source_kind === 'vault' && /^[a-z0-9]+(-[a-z0-9]+)*$/.test(sk.key)) {
        meta.appendChild(btn(t('sp_archive'), null, function () { spGo('/api/learning/skills/proposals/archive', { skill_name: sk.key }); }));
        if (SP.agent.indexOf(sk.key) >= 0) {
          meta.appendChild(btn(t('sp_delete'), 'danger small', function () { spGo('/api/learning/skills/proposals/delete', { skill_name: sk.key }); }));
        }
      }
      li.appendChild(meta);
      if (sk.description) li.appendChild(el('div', 'excerpt', sk.description));
      if (sk.diagnostics && sk.diagnostics.length) {
        var dl = el('ul', 'diag');
        sk.diagnostics.forEach(function (g) { dl.appendChild(el('li', g.level === 'error' ? 'error' : 'warning', g.code + ': ' + g.message)); });
        li.appendChild(dl);
      }
      ul.appendChild(li);
    });
    var tl = $('trusted-list');
    clear(tl);
    if (!d.trusted_repos.length) tl.appendChild(el('li', null, t('no_trusted')));
    d.trusted_repos.forEach(function (r) {
      var li = el('li');
      li.appendChild(el('span', 'mono', r.repo_path));
      var rm = el('button', 'ghost small', t('untrust')); rm.type = 'button';
      rm.addEventListener('click', function () {
        api('POST', '/api/learning/skills/trust', { repo_path: r.repo_path, trusted: false }).then(loadSkills).catch(function (e) { showError(e.message); });
      });
      li.appendChild(rm);
      tl.appendChild(li);
    });
  }

  async function loadSkills() { renderSkills(await api('GET', '/api/learning/skills')); }

  // ---- L8: "¿Estoy aprendiendo bien?" ----
  function num(v) { return v === null || v === undefined ? '—' : String(v); }
  function rateTxt(r, min) { return r.enough ? t('m_n', num(r.per_100), r.answers) : t('m_low', min); }

  async function loadMetrics() {
    var d = await api('GET', '/api/learning/metrics?days=' + encodeURIComponent($('m-days').value));
    var dv = d.downvotes;
    $('m-down').textContent = num(dv.all.per_100);
    $('m-with').textContent = num(dv.with_lessons.per_100);
    $('m-without').textContent = num(dv.without_lessons.per_100);
    var sg = $('m-signals');
    clear(sg);
    d.signals.forEach(function (s) {
      var key = 's_' + s.code + (typeof s.value === 'string' ? '_' + s.value : '');
      var txt = s.code === 'downvote_trend' ? t(key, num(s.before), num(s.now))
        : (s.code === 'lessons_effect' || s.code === 'ab_effect') ? t(key, num(s['with']), num(s.without))
        : t(key, s.value);
      sg.appendChild(el('li', null, txt));
    });
    var ul = $('m-details');
    clear(ul);
    function add(text) { ul.appendChild(el('li', null, text)); }
    add(t('m_down') + ': ' + rateTxt(dv.all, d.min_sample));
    if (d.settings.ab_holdout_pct > 0 || dv.ab.holdout.answers) add(t('d_ab', rateTxt(dv.ab.lessons, d.min_sample), rateTxt(dv.ab.holdout, d.min_sample)));
    if (d.lessons.available) {
      var c = d.lessons.counts;
      add(t('d_lessons', c.active, c.archived, num(d.lessons.archived_pct), c.proposed));
      d.lessons.top.slice(0, 5).forEach(function (l) { add(t('d_lesson', l.id, l.uses, l.answers, l.down) + ' — ' + l.text); });
    }
    var sk = d.skills;
    add(t('d_skills', num(sk.indexed), num(sk.activations), num(sk.unused)));
    if (sk.proposals) add(t('d_props', sk.proposals.approved || 0, sk.proposals.rejected || 0, num(sk.approval_pct), num(sk.cost_eur)));
    add(t('d_skill_delta'));
    var ss = d.sessions;
    if (ss.available) {
      add(t('d_sessions', num(ss.all.first_try_pct), ss.all.done));
      if (ss.before_lessons) add(t('d_sessions_ba', num(ss.before_lessons.first_try_pct), ss.before_lessons.done, num(ss.after_lessons.first_try_pct), ss.after_lessons.done));
    } else add(t('d_sessions_na'));
    if (d.evals.available && d.evals.overall) add(t('d_evals', d.evals.overall.score.toFixed(2), d.evals.overall.passed, d.evals.overall.total));
    else add(t('d_evals_na'));
    if (document.activeElement !== $('m-ab')) $('m-ab').value = String(d.settings.ab_holdout_pct);
    $('m-curator').checked = !!d.settings.curator_enabled;
  }

  // ---- L7: tabs + read-only Evals ----
  var TABS = ['lessons', 'skills', 'feedback', 'failures', 'evals', 'metrics'];
  function showTab(name) {
    if (TABS.indexOf(name) < 0) name = 'lessons';
    var panels = document.querySelectorAll('[data-panel]');
    for (var i = 0; i < panels.length; i++) panels[i].hidden = panels[i].getAttribute('data-panel') !== name;
    var tabs = document.querySelectorAll('#tabs [data-tab]');
    for (var j = 0; j < tabs.length; j++) {
      var on = tabs[j].getAttribute('data-tab') === name;
      tabs[j].setAttribute('aria-selected', on ? 'true' : 'false');
      tabs[j].tabIndex = on ? 0 : -1;
    }
    if (window.history && window.history.replaceState) window.history.replaceState(null, '', '#' + name);
  }
  function wireTabs() {
    var bar = $('tabs');
    bar.addEventListener('click', function (ev) {
      var b = ev.target.closest('[data-tab]');
      if (b) showTab(b.getAttribute('data-tab'));
    });
    bar.addEventListener('keydown', function (ev) {
      if (ev.key !== 'ArrowRight' && ev.key !== 'ArrowLeft') return;
      var sel = document.querySelector('#tabs [aria-selected="true"]');
      var cur = TABS.indexOf(sel ? sel.getAttribute('data-tab') : 'lessons');
      var next = TABS[(cur + (ev.key === 'ArrowRight' ? 1 : TABS.length - 1)) % TABS.length];
      showTab(next);
      $('tab-' + next).focus();
    });
    showTab((window.location.hash || '').replace('#', ''));
  }

  async function loadEvals() {
    var d = await api('GET', '/api/learning/evals');
    var st = $('evals-status');
    $('evals-cmd').hidden = true;
    $('evals-overall').hidden = true;
    $('evals-policy').hidden = true;
    clear($('evals-suites'));
    if (!d.available) {
      st.textContent = t('evals_none');
    } else if (!d.harness_installed) {
      st.textContent = t('evals_no_harness', d.dir);
    } else {
      st.textContent = t('evals_harness', d.dir);
      $('evals-cmd').textContent = d.run_command;
      $('evals-cmd').hidden = false;
    }
    if (d.overall) {
      $('evals-score').textContent = d.overall.score.toFixed(2);
      $('evals-passed').textContent = d.overall.passed + '/' + d.overall.total;
      $('evals-overall').hidden = false;
    }
    $('evals-suites-n').textContent = String(d.suites.length);
    if (d.policy) { $('evals-policy').textContent = d.policy; $('evals-policy').hidden = false; }
    d.suites.forEach(function (s) {
      var li = el('li');
      var meta = el('div', 'meta');
      meta.appendChild(el('strong', null, s.suite));
      if (s.target) meta.appendChild(el('span', 'pill mono', s.target));
      meta.appendChild(el('span', null, t('evals_cases', s.cases.length, s.required)));
      if (s.baseline) meta.appendChild(el('span', 'pill', t('evals_baseline', s.baseline.passed, s.baseline.total, s.baseline.score.toFixed(2))));
      li.appendChild(meta);
      var ul = el('ul', 'diag');
      s.cases.forEach(function (c) {
        var it = el('li');
        it.appendChild(el('span', 'mono', c.key + ' '));
        if (c.required) it.appendChild(el('span', 'pill req', t('evals_required')));
        if (c.baseline_passed !== null) it.appendChild(el('span', 'pill ' + (c.baseline_passed ? 'pass' : 'fail'), t(c.baseline_passed ? 'evals_pass' : 'evals_fail')));
        it.appendChild(el('span', null, ' ' + c.prompt + (c.checks.length ? ' · ' + c.checks.join(', ') : '')));
        ul.appendChild(it);
      });
      li.appendChild(ul);
      $('evals-suites').appendChild(li);
    });
    var runs = $('evals-runs');
    clear(runs);
    if (!d.local_runs.length) runs.appendChild(el('li', null, t('evals_no_runs')));
    d.local_runs.forEach(function (r) {
      var li = el('li');
      li.appendChild(el('span', null, (r.suite || '') + ' · ' + (r.mode || '') + (r.provider ? ' · ' + r.provider : '')));
      li.appendChild(el('span', null, (r.passed || 0) + '/' + ((r.passed || 0) + (r.failed || 0)) + (r.started_at ? ' · ' + fmtTime(r.started_at) : '')));
      runs.appendChild(li);
    });
  }

  // ---- L6: skill proposals ----
  var SP = { budget: 0.05, skills: [], agent: [] };

  function spConfirm(mode) {
    return window.confirm(t('sp_send_warn', mode === 'ollama' ? 'Ollama' : t('sp_same_model'), SP.budget));
  }
  function spGo(path, body, mode) {
    if (mode && mode !== 'heuristic' && !spConfirm(mode)) return Promise.resolve();
    return api('POST', path, body)
      .then(function (d) { refresh(); window.alert(d.proposal && d.proposal.duplicate ? t('sp_dup') : t('sp_made', 1)); })
      .catch(function (e) { showError(e.message); });
  }

  function skillProposeRow(f) {
    var row = el('div', 'row');
    if (f.conversation_id) {
      row.appendChild(btn(t('sp_from_conv'), null, function () { spGo('/api/learning/skills/proposals/from-conversation', { conversation_id: f.conversation_id, mode: 'llm' }, 'llm'); }));
      row.appendChild(btn(t('sp_from_conv_o'), null, function () { spGo('/api/learning/skills/proposals/from-conversation', { conversation_id: f.conversation_id, mode: 'ollama' }, 'ollama'); }));
    }
    if (f.rating === -1 && SP.skills.length) {
      var sel = el('select');
      sel.appendChild(el('option', null, t('sp_pick'))).value = '';
      SP.skills.forEach(function (k) { var o = el('option', null, k); o.value = k; sel.appendChild(o); });
      row.appendChild(sel);
      row.appendChild(btn(t('sp_improve'), null, function () { if (sel.value) spGo('/api/learning/skills/proposals/patch', { skill_name: sel.value, feedback_id: f.id, mode: 'llm' }, 'llm'); }));
      row.appendChild(btn(t('sp_improve_o'), null, function () { if (sel.value) spGo('/api/learning/skills/proposals/patch', { skill_name: sel.value, feedback_id: f.id, mode: 'ollama' }, 'ollama'); }));
    }
    return row;
  }

  function proposalItem(p) {
    var li = el('li');
    var meta = el('div', 'meta');
    meta.appendChild(el('strong', null, 'S' + p.id + ' · ' + p.skill_name));
    meta.appendChild(el('span', 'pill', t('sp_a_' + p.action)));
    meta.appendChild(el('span', 'pill st-' + p.status, t('st_' + p.status)));
    meta.appendChild(el('span', null, p.source_kind + (p.source_ref ? ' · ' + p.source_ref : '')));
    if (p.provider) meta.appendChild(el('span', 'pill', p.provider + (p.model ? ' · ' + p.model : '') + (p.cost_cents_est ? ' · ' + t('cost', (p.cost_cents_est / 100).toFixed(4)) : '')));
    li.appendChild(meta);
    if (p.review_note) li.appendChild(el('div', 'comment', p.review_note));
    if (p.lint && p.lint.length) {
      var dl = el('ul', 'diag');
      p.lint.forEach(function (g) { dl.appendChild(el('li', g.level, g.code + ': ' + g.message)); });
      li.appendChild(dl);
    }
    if (p.blocked) li.appendChild(el('div', 'comment', t('sp_blocked')));
    if (p.stale) li.appendChild(el('div', 'comment', t('sp_stale')));
    var ta = null;
    if (p.diff) li.appendChild(el('pre', 'skill', p.diff));
    if (p.content && p.status === 'pending') {
      ta = el('textarea', 'skill'); ta.value = p.content; ta.spellcheck = false;
      li.appendChild(ta);
    } else if (p.content) {
      li.appendChild(el('pre', 'skill', p.content));
    }
    if (p.status === 'pending') {
      var row = el('div', 'row');
      if (ta) row.appendChild(btn(t('sp_save'), null, function () { act('/api/learning/skills/proposals/edit', { id: p.id, content: ta.value }); }));
      row.appendChild(btn(t('approve'), 'small', function () {
        api('POST', '/api/learning/skills/proposals/approve', ta ? { id: p.id, content: ta.value } : { id: p.id })
          .then(function (d) { refresh(); if (d.proposal.review_note) window.alert(t('sp_written', d.proposal.review_note)); })
          .catch(function (e) { refresh(); showError(e.message); });
      }));
      row.appendChild(btn(t('reject'), 'danger small', function () {
        var why = window.prompt(t('sp_reject_why'), '');
        if (why === null) return;
        act('/api/learning/skills/proposals/reject', { id: p.id, note: why });
      }));
      li.appendChild(row);
    }
    return li;
  }

  async function loadProposals() {
    var d = await api('GET', '/api/learning/skills/proposals');
    SP.budget = d.budget_eur;
    SP.agent = d.agent_skills || [];
    $('sp-counts').textContent = t('sp_counts', d.summary.pending);
    var ul = $('sp-list');
    clear(ul);
    var shown = d.proposals.filter(function (p) { return p.status === 'pending' || p.status === 'failed'; }).slice(0, 30);
    $('sp-empty').hidden = shown.length > 0;
    shown.forEach(function (p) { ul.appendChild(proposalItem(p)); });
  }


  function refresh() {
    $('global-error').hidden = true;
    return loadProposals().then(loadSkills).then(function () {
      return Promise.all([loadSummary(), loadFeedback(), loadFailures(), loadLessons(), loadEvals(), loadMetrics()]);
    }).catch(function (e) { showError(e.message); });
  }

  function wire() {
    $('rating-filter').addEventListener('change', function () { loadFeedback().catch(function (e) { showError(e.message); }); });
    $('failure-toggle').addEventListener('change', function (ev) {
      api('POST', '/api/learning/settings', { failure_log_enabled: !!ev.target.checked })
        .then(refresh).catch(function (e) { showError(e.message); });
    });
    $('clear-failures').addEventListener('click', function () {
      if (!window.confirm(t('confirm_clear'))) return;
      api('POST', '/api/learning/failures/clear', {}).then(refresh).catch(function (e) { showError(e.message); });
    });
    $('lessons-enabled').addEventListener('change', function (ev) {
      act('/api/learning/lessons/settings', { enabled: !!ev.target.checked });
    });
    $('lessons-mirror').addEventListener('change', function (ev) {
      act('/api/learning/lessons/settings', { vault_mirror: !!ev.target.checked });
    });
    $('lessons-budget-save').addEventListener('click', function () {
      act('/api/learning/lessons/settings', { proposal_budget_eur: parseFloat($('lessons-budget').value) });
    });
    $('price-save').addEventListener('click', function () {
      act('/api/learning/lessons/price', { provider: $('price-provider').value.trim(), model: $('price-model').value.trim(),
        eur_in_per_mtok: parseFloat($('price-in').value), eur_out_per_mtok: parseFloat($('price-out').value) });
    });
    $('lesson-add').addEventListener('click', function () {
      var txt = $('lesson-new').value.trim();
      if (!txt) return;
      api('POST', '/api/learning/lessons/create', { text: txt, scope: $('lesson-new-scope').value.trim() || 'global' })
        .then(function () { $('lesson-new').value = ''; return refresh(); })
        .catch(function (e) { showError(e.message); });
    });
    $('skills-external').addEventListener('change', function (ev) {
      api('POST', '/api/learning/skills/settings', { external_dirs: !!ev.target.checked })
        .then(function (d) { renderSkills(d); skillMsg(t('reindexed', d.counters.indexed)); })
        .catch(function (e) { showError(e.message); });
    });
    $('skills-reindex').addEventListener('click', function () {
      api('POST', '/api/learning/skills/reindex', {})
        .then(function (d) { renderSkills(d); skillMsg(t('reindexed', d.counters.indexed)); })
        .catch(function (e) { showError(e.message); });
    });
    $('m-days').addEventListener('change', function () { loadMetrics().catch(function (e) { showError(e.message); }); });
    $('m-ab-save').addEventListener('click', function () {
      var v = parseInt($('m-ab').value, 10);
      api('POST', '/api/learning/metrics/settings', { ab_holdout_pct: isNaN(v) ? 0 : v })
        .then(function () { return loadMetrics(); }).catch(function (e) { showError(e.message); });
    });
    $('m-curator').addEventListener('change', function (ev) {
      api('POST', '/api/learning/metrics/settings', { curator_enabled: !!ev.target.checked })
        .then(function () { return loadMetrics(); }).catch(function (e) { showError(e.message); });
    });
    $('sp-curator').addEventListener('click', function () {
      api('POST', '/api/learning/skills/proposals/curator', {})
        .then(function (d) { refresh(); window.alert(d.proposals.length ? t('sp_made', d.proposals.length) : t('sp_none_made', 60)); })
        .catch(function (e) { showError(e.message); });
    });
    $('sp-create').addEventListener('click', function () {
      var ref = $('sp-ref').value.trim();
      if (!ref) return;
      var mode = $('sp-mode').value;
      spGo('/api/learning/skills/proposals/from-source', { kind: $('sp-kind').value, ref: ref, mode: mode }, mode)
        .then(function () { $('sp-ref').value = ''; });
    });
    $('trust-add').addEventListener('click', function () {
      var p = $('trust-path').value.trim();
      if (!p) return;
      api('POST', '/api/learning/skills/trust', { repo_path: p, trusted: true })
        .then(function () { $('trust-path').value = ''; return loadSkills(); })
        .catch(function (e) { showError(e.message); });
    });
    $('export-btn').addEventListener('click', function () {
      api('POST', '/api/learning/export', { include_comments: !!$('export-comments').checked })
        .then(function (d) { var p = $('export-path'); p.textContent = t('exported', d.path); p.hidden = false; })
        .catch(function (e) { showError(e.message); });
    });
  }

  document.addEventListener('DOMContentLoaded', function () {
    applyI18n();
    wireTabs();
    wire();
    refresh();
  });
})();
