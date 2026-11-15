// ============================================================
// A form for a new agent (browser and desktop app)
// ============================================================
//
// Why this exists: "New agent" asked three questions in a row through
// promptAction(): the name, the "team id" (a NUMBER, left empty by default
// so it became 1) and the type, typed by hand. Nobody knows the numeric id
// of a team, and a wrong one files the agent under the wrong team with
// nothing on the screen to show it (2026-11-15, prompt-minimax-2026-11-15
// P1-1).
//
// So the questions are asked in one dialog, with the choices that can be
// picked: the team from the teams that exist, the type from the types the
// product knows, and the boss from the people of the organisation. The boss
// defaults to the agent whose branch is being looked at, because that is
// where a new agent usually belongs.
//
// The promise resolves with the values, or with null when the form was
// cancelled. It never rejects: a cancelled form is an answer, not an error.
//
// The dialog is the same shape as the one confirm.js opens (a <dialog> with
// showModal(), no permission and no IPC), so it behaves the same in the
// browser and in the desktop app.
(function (global) {
  'use strict';

  // The types a member can have. The order is the order they are offered.
  var TYPES = ['ai_agent', 'human', 'system'];

  function t(key, vars) {
    return global._i18n ? global._i18n.t(key, vars) : key;
  }

  function el(tag, className, text) {
    var e = global.document.createElement(tag);
    if (className) e.className = className;
    if (text !== null && text !== undefined) e.textContent = text;
    return e;
  }

  function field(label, control) {
    var wrap = el('label', 'confirm__field');
    wrap.appendChild(el('span', 'confirm__label', label));
    wrap.appendChild(control);
    return wrap;
  }

  function option(value, label, selected) {
    var o = el('option', null, label);
    o.value = value;
    if (selected) o.selected = true;
    return o;
  }

  // opts: { teams: [{id, name, icon}], members: [{id, name}], defaultTeamId,
  //         defaultManagerId }
  // Resolves with { name, team_id, type, manager_id } or with null.
  function openNewAgentDialog(opts) {
    opts = opts || {};
    return new Promise(function (resolve) {
      if (!global.document || !global.document.body) { resolve(null); return; }

      var dlg = global.document.createElement('dialog');
      dlg.id = 'agent-new-dialog';
      dlg.className = 'confirm';

      var name = el('input', 'confirm__input');
      name.type = 'text';
      name.id = 'agent-new-name';
      name.autocomplete = 'off';

      var team = el('select', 'confirm__input');
      team.id = 'agent-new-team';
      (opts.teams || []).forEach(function (tm) {
        team.appendChild(option(String(tm.id),
          (tm.icon ? tm.icon + ' ' : '') + tm.name,
          opts.defaultTeamId != null && tm.id === opts.defaultTeamId));
      });

      var type = el('select', 'confirm__input');
      type.id = 'agent-new-type';
      TYPES.forEach(function (ty) {
        type.appendChild(option(ty, t('dashboard.agent_type_' + ty), ty === 'ai_agent'));
      });

      var boss = el('select', 'confirm__input');
      boss.id = 'agent-new-boss';
      boss.appendChild(option('', t('dashboard.agent_boss_none'), opts.defaultManagerId == null));
      (opts.members || []).forEach(function (m) {
        boss.appendChild(option(String(m.id), m.name,
          opts.defaultManagerId != null && m.id === opts.defaultManagerId));
      });

      dlg.appendChild(field(t('dashboard.agent_name_label'), name));
      dlg.appendChild(field(t('dashboard.agent_team_label'), team));
      dlg.appendChild(field(t('dashboard.agent_type_label'), type));
      dlg.appendChild(field(t('dashboard.agent_boss_label'), boss));

      var actions = el('div', 'confirm__actions');
      var no = el('button', 'confirm__btn confirm__btn--ghost', t('dashboard.dlg_cancel'));
      no.type = 'button';
      no.id = 'agent-new-cancel';
      var yes = el('button', 'confirm__btn confirm__btn--primary', t('dashboard.agent_create'));
      yes.type = 'button';
      yes.id = 'agent-new-create';
      actions.appendChild(no);
      actions.appendChild(yes);
      dlg.appendChild(actions);

      function done(value) {
        try { dlg.close(); } catch (e) { /* already closed */ }
        if (dlg.parentNode) dlg.parentNode.removeChild(dlg);
        resolve(value);
      }

      no.addEventListener('click', function () { done(null); });
      yes.addEventListener('click', function () {
        var n = name.value.trim();
        if (!n) { name.focus(); return; }
        done({
          name: n,
          team_id: team.value === '' ? null : parseInt(team.value, 10),
          type: type.value,
          manager_id: boss.value === '' ? null : parseInt(boss.value, 10)
        });
      });
      // A modal keeps its keystrokes: the page behind it must not react to
      // them (the dashboard's Escape handler navigates back, and a native
      // dialog used to swallow the key while it was up).
      dlg.addEventListener('keydown', function (e) { e.stopPropagation(); });
      dlg.addEventListener('click', function (e) { if (e.target === dlg) done(null); });
      dlg.addEventListener('cancel', function (e) { e.preventDefault(); done(null); });
      name.addEventListener('keydown', function (e) {
        if (e.key === 'Enter') { e.preventDefault(); yes.click(); }
      });

      global.document.body.appendChild(dlg);
      dlg.showModal();
      try { name.focus(); } catch (e) { /* no focus in this environment */ }
    });
  }

  global.openNewAgentDialog = openNewAgentDialog;
})(window);
