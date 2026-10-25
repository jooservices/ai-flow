/* Dashboard panel for first-run setup. The setup screen (/setup) only creates the Administrator; every later step is
   checked, skipped, resumed and finished here, with links to Integrations, Machines and AI, and Projects.
   Served by control/setup_wizard.py and used by console.html (window.AIFlowSetup). */
(function () {
  'use strict';
  var state = { data: null, loading: false, loadedAt: 0, busy: false, message: '' };
  function esc(v) { return String(v == null ? '' : v).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function canSee() { return typeof S !== 'undefined' && S.consoleUser && consoleCan('integrations.manage'); }
  function redraw() { if (typeof S !== 'undefined' && S.view === 'cc' && S.data) renderDashboard(); }
  function call(path, body) {
    return fetch(path, { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (d) {
          if (!r.ok) throw new Error(typeof d.detail === 'string' ? d.detail : 'The request could not be completed.');
          return d;
        });
      });
  }
  function poll(force) {
    if (!canSee() || state.loading || (!force && Date.now() - state.loadedAt < 10000)) return;
    state.loading = true;
    fetch('/v1/setup/status', { cache: 'no-store', credentials: 'same-origin' }).then(function (r) {
      return r.json().then(function (d) { if (!r.ok) throw new Error(d.detail || 'Setup status is unavailable'); return d; });
    }).then(function (d) { state.data = d; }).catch(function () { /* keep the last known state */ })
      .then(function () { state.loading = false; state.loadedAt = Date.now(); redraw(); });
  }
  function stepHtml(step, data) {
    var body = step.ready ? 'Ready.' : (step.missing || 'This step is not complete yet.');
    var html = '<article class="setup-step" data-setup-step="' + esc(step.id) + '" data-ready="' + step.ready + '" data-skipped="' + step.skipped + '">';
    html += '<h3>' + esc(step.title) + (step.skipped && !step.ready ? ' <span class="hint">(skipped)</span>' : '') + '</h3><p class="' + (step.ready ? 'muted' : 'err') + '">' + esc(body) + '</p>';
    if (!step.ready) {
      if (step.action) html += '<a class="btn sm primary" href="' + esc(step.action.url) + '">' + esc(step.action.label) + '</a> ';
      if (step.id === 'machine') html += '<p class="hint">Machines and AI shows a one-time command to run on the new machine. It is the only setup step that uses a terminal.</p>';
      if (step.id === 'sample_ticket') {
        var projects = data.projects || [];
        html += '<select data-setup-project aria-label="Project for the sample ticket">' + projects.map(function (p) { return '<option value="' + esc(p) + '">' + esc(p) + '</option>'; }).join('') + '</select> ';
        html += '<button type="button" class="btn sm primary" data-setup-send' + (projects.length ? '' : ' disabled') + '>Send sample ticket to Intake</button> ';
        if (data.sample_ticket) html += '<span class="hint">Sample ticket ' + esc(data.sample_ticket) + ': ' + esc(data.sample_status || 'not confirmed') + '</span>';
      }
      html += '<button type="button" class="btn sm" data-setup-skip="' + esc(step.id) + '" data-skipped="' + (!step.skipped) + '">' + (step.skipped ? 'Resume this step' : 'Skip for now') + '</button>';
    }
    return html + '</article>';
  }
  function panel() {
    var data = state.data;
    if (!data || data.completed || !canSee()) return '';
    var steps = (data.steps || []).slice(2);
    var left = steps.filter(function (s) { return !s.ready; }).length;
    var h = '<section class="panel"><div class="panel-h"><h2>Finish setting up AI Flow</h2><span class="hint">' + left +
      ' of ' + steps.length + ' steps left. Skip a step and resume it here later.</span></div>';
    h += steps.map(function (s) { return stepHtml(s, data); }).join('');
    if (state.message) h += '<div class="muted" role="status">' + esc(state.message) + '</div>';
    return h + '</section>';
  }
  document.addEventListener('click', function (e) {
    var skip = e.target.closest ? e.target.closest('[data-setup-skip]') : null;
    var send = e.target.closest ? e.target.closest('[data-setup-send]') : null;
    if (!skip && !send) return;
    if (state.busy) return;
    state.busy = true; state.message = '';
    var req = skip ? call('/v1/setup/steps/' + encodeURIComponent(skip.getAttribute('data-setup-skip')), { skipped: skip.getAttribute('data-skipped') === 'true' })
      : call('/v1/setup/sample-ticket', { project: (document.querySelector('[data-setup-project]') || {}).value || '' });
    req.then(function (d) { if (send) state.message = d.message || ''; })
      .catch(function (err) { state.message = String(err.message || err); })
      .then(function () { state.busy = false; poll(true); redraw(); });
  });
  window.AIFlowSetup = { poll: poll, panel: panel };
  poll(true);
})();
