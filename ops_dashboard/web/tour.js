// The quick tour (/tour): the first run walkthrough.
//
// The words of the page live in its own dictionary (web/i18n/tour.*.json),
// so the tour speaks the language the person chose. The dictionary arrives
// over the network: the page paints when the words are here, and paints
// again when the language changes.
(function () {
  'use strict';

  const $ = id => document.getElementById(id);
  const STORAGE_KEY = 'conductloop.tour.progress';

  const tr = (key, vars) => (window._i18n && window._i18n.t ? window._i18n.t(key, vars) : key);

  function loadProgress() {
    try { return JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}'); } catch { return {}; }
  }
  function saveProgress(p) {
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(p)); } catch {}
  }

  let progress = loadProgress();
  let currentStep = Object.keys(progress).filter(k => progress[k]).length;

  function showStep(n) {
    document.querySelectorAll('.step').forEach(s => s.classList.add('hidden'));
    document.querySelector(`.step[data-step="${n}"]`).classList.remove('hidden');
    document.querySelectorAll('.step-dot').forEach(d => {
      const ds = parseInt(d.dataset.step);
      d.className = 'step-dot';
      if (ds < n) d.classList.add('done');
      else if (ds === n) d.classList.add('active');
    });
    // Mark previously-completed steps with the ✓ badge
    document.querySelectorAll('.step-done').forEach(d => {
      if (progress[d.dataset.done]) d.classList.add('visible');
    });
    updateSummary();
    window.scrollTo({top: 0, behavior: 'smooth'});
  }

  function updateSummary() {
    const done = Object.keys(progress).filter(k => progress[k]).length;
    $('progress-summary').textContent = tr('tour.step_progress', {n: done});
  }

  function markStepDone(n) {
    progress['step' + n] = true;
    saveProgress(progress);
    currentStep++;
    if (currentStep < 5) {
      setTimeout(() => showStep(currentStep + 1), 600);
    } else {
      setTimeout(() => showStep(5), 600);
    }
  }

  // Wire actions
  document.querySelectorAll('.step-action').forEach(a => {
    a.addEventListener('click', () => {
      const stepNum = parseInt(a.closest('.step').dataset.step);
      // Open link in same tab
      setTimeout(() => markStepDone(stepNum), 100);
    });
  });
  document.querySelectorAll('.step-skip').forEach(b => {
    b.addEventListener('click', () => {
      const stepNum = parseInt(b.closest('.step').dataset.step);
      markStepDone(stepNum);
    });
  });

  function start() {
    document.title = tr('tour.title');
    updateSummary();
    // Resume from where the user left off
    if (currentStep > 0 && currentStep < 5) {
      showStep(currentStep + 1);
    } else if (currentStep >= 5) {
      showStep(5);
    }
    if (window._i18n && window._i18n.onLocaleChange) {
      window._i18n.onLocaleChange(function () {
        document.title = tr('tour.title');
        updateSummary();
      });
    }
  }

  // The words arrive over the network: the page paints when they are here.
  if (window._i18n && window._i18n.ready) window._i18n.ready().then(start, start);
  else if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();
