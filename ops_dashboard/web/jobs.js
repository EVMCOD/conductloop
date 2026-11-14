// The jobs of the app: what they are, how the last run went, and the button
// that runs one now.
//
// The words are the person's: every word the page writes comes from the
// dictionary (i18n/jobs.<locale>.json), and the page repaints itself when
// the language changes. The times ("hace 2 minutos", "2 minutes ago") are the
// browser's own, so they need no words of ours.
(function () {
  'use strict';

  var jobs = [];
  var listFailed = false;

  function t(key, vars) {
    if (window._i18n && window._i18n.t) return window._i18n.t(key, vars);
    return key;
  }

  function whenWords(fn) {
    var go = function () {
      if (window._i18n && window._i18n.ready) window._i18n.ready().then(fn, fn);
      else fn();
    };
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', go);
    else go();
  }

  // Job names and error messages end up in innerHTML: escape them.
  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  // The name of each job, in the language of the person. The keys are
  // written out: a key built by concatenation would be a key nobody checks.
  var JOB_KEYS = {
    backup: 'jobs.job_backup',
    learning_maintenance: 'jobs.job_learning_maintenance',
    briefing: 'jobs.job_briefing',
    obsidian_export: 'jobs.job_obsidian_export',
    analytics_poll: 'jobs.job_analytics_poll',
    library_index: 'jobs.job_library_index',
    daily_note: 'jobs.job_daily_note'
  };

  function labelFor(name) {
    var key = JOB_KEYS[name];
    if (!key) return name;
    var word = t(key);
    // The dictionary does not know this job yet: show the name, not the key.
    return word === key ? name : word;
  }

  function stateLabel(state) {
    if (state === 'ok') return t('jobs.state_ok');
    if (state === 'error') return t('jobs.state_error');
    if (state === 'overdue') return t('jobs.state_overdue');
    return t('jobs.state_never');
  }

  // Why the run happened: on its schedule, because it missed a slot while
  // the app was closed, or because the person asked for it. The keys are
  // written out: a key built by concatenation would be a key nobody checks.
  var KIND_KEYS = {
    scheduled: 'jobs.run_scheduled',
    catchup: 'jobs.run_catchup',
    manual: 'jobs.run_manual'
  };

  function kindLabel(kind) {
    var key = KIND_KEYS[kind];
    if (!key) return '';
    var word = t(key);
    return word === key ? '' : word;
  }

  // "3 minutes ago" in the language the person chose, without a word of our
  // own: the browser knows how to say it.
  function ago(ts) {
    if (!ts) return '';
    var locale = (window._i18n && window._i18n.getLocale) ? window._i18n.getLocale() : 'en';
    var secs = Math.max(0, Math.round((Date.now() / 1000) - ts));
    var value = -secs, unit = 'second';
    if (secs >= 86400) { value = -Math.round(secs / 86400); unit = 'day'; }
    else if (secs >= 3600) { value = -Math.round(secs / 3600); unit = 'hour'; }
    else if (secs >= 60) { value = -Math.round(secs / 60); unit = 'minute'; }
    try {
      return new Intl.RelativeTimeFormat(locale, { numeric: 'auto' }).format(value, unit);
    } catch (e) {
      return t('jobs.never');
    }
  }

  function duration(ms) {
    if (ms == null) return '';
    if (ms < 1000) return ms + ' ms';
    return (ms / 1000).toFixed(1) + ' s';
  }

  function render() {
    var counts = { ok: 0, error: 0, never: 0, overdue: 0 };
    jobs.forEach(function (j) { counts[j.state] = (counts[j.state] || 0) + 1; });
    document.getElementById('c-ok').textContent = counts.ok;
    document.getElementById('c-error').textContent = counts.error;
    document.getElementById('c-overdue').textContent = counts.overdue;
    document.getElementById('c-never').textContent = counts.never;

    var el = document.getElementById('list');
    if (listFailed) {
      el.innerHTML = '<span class="hint">' + esc(t('jobs.could_not_load')) + '</span>';
      return;
    }
    if (!jobs.length) {
      el.innerHTML = '<span class="hint">' + esc(t('jobs.no_jobs')) + '</span>';
      return;
    }
    el.innerHTML = jobs.map(function (j) {
      var last = j.last;
      var meta = '<code>' + esc(j.cron) + '</code>';
      if (last && last.ran_at) {
        meta += ' &middot; ' + esc(t('jobs.last_run')) + ' ' + esc(ago(last.ran_at));
        if (last.duration_ms != null) meta += ' &middot; ' + esc(duration(last.duration_ms));
      } else {
        meta += ' &middot; ' + esc(t('jobs.never'));
      }
      if (j.failures_7d) meta += ' &middot; ' + esc(t('jobs.failed_n_times', { n: j.failures_7d }));
      var why = (last && last.kind) ? kindLabel(last.kind) : '';
      if (why) meta += ' &middot; ' + esc(why);
      var err = (last && last.error) ? '<div class="job-error">' + esc(last.error) + '</div>' : '';
      // What the run said it did: "rows saved: 12". The words are the
      // server's, in the language of the person.
      var did = j.did ? '<div class="job-did">' + esc(j.did) + '</div>' : '';
      return '<div class="job"><div>' +
        '<div class="job-name">' + esc(labelFor(j.name)) + '</div>' +
        '<div class="job-meta">' + meta + '</div>' + did + err +
        '</div><div class="job-side">' +
        '<span class="badge ' + esc(j.state) + '">' + esc(stateLabel(j.state)) + '</span>' +
        '<button data-run="' + esc(j.name) + '">' + esc(t('jobs.run_now')) + '</button>' +
        '</div></div>';
    }).join('');
  }

  async function load() {
    try {
      var r = await fetch('/api/system-jobs');
      var d = await r.json();
      jobs = (d && d.jobs) ? d.jobs : [];
      listFailed = false;
      render();
    } catch (e) {
      listFailed = true;
      render();
    }
  }

  document.getElementById('list').addEventListener('click', async function (ev) {
    var btn = ev.target.closest('button[data-run]');
    if (!btn) return;
    var name = btn.getAttribute('data-run');
    btn.disabled = true;
    try {
      var r = await fetch('/api/system-jobs/' + encodeURIComponent(name) + '/run', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}'
      });
      var d = await r.json();
      await load();
      if (d && d.ok) notify(t('jobs.ran_ok', { name: labelFor(name) }));
      else notifyError(t('jobs.ran_error', {
        name: labelFor(name),
        msg: (d && (d.message || (d.result && d.result.error))) || 'error'
      }));
    } catch (e) {
      notifyError(t('jobs.ran_error', { name: labelFor(name), msg: e.message }));
    } finally {
      btn.disabled = false;
    }
  });

  whenWords(function () {
    // When the person switches the language, the words around the page
    // change too.
    if (window._i18n && window._i18n.onLocaleChange) window._i18n.onLocaleChange(function () { render(); });
    load();
  });
})();
