function pollServices() {
  if (!S.consoleSessionLoaded) { setTimeout(pollServices, SVC_MS); return; }
  if (S.consoleUser && !consoleCan('view.machines_ai')) return;
  fetch('/v1/services', { cache: 'no-store' }).then(function (r) { return r.json(); }).then(function (d) {
    S.services = d.services || []; S.svcError = d.error || '';
    if (S.view === 'cc' && S.data) renderDashboard();
    if (S.view === 'machines' && S.data) renderMachines();
  }).catch(function (e) { S.svcError = String(e.message || e); })
    .then(function () { setTimeout(pollServices, SVC_MS); });
}
function pollAnalytics() {
  fetch('/v1/analytics/daily?days=' + ANALYTICS_DAYS, { cache: 'no-store' }).then(function (r) {
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  }).then(function (d) {
    S.analytics = d.rows || []; S.anError = '';
    if ((S.view === 'delivery' || S.view === 'work') && S.data) renderDBody();
    if (S.view === 'cc' && S.data) renderDashboard();
  }).catch(function (e) { S.anError = String(e.message || e); })
    .then(function () { setTimeout(pollAnalytics, ANALYTICS_MS); });
}
function pollConflicts() {
  fetch('/v1/analytics/conflicts?days=' + ANALYTICS_DAYS, { cache: 'no-store' }).then(function (r) {
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  }).then(function (d) {
    S.conflicts = d; S.conflictsError = '';
    if ((S.view === 'delivery' || S.view === 'work') && S.data) renderDBody();
    if (S.view === 'cc' && S.data) renderDashboard();
  }).catch(function (e) { S.conflictsError = String(e.message || e); })
    .then(function () { setTimeout(pollConflicts, ANALYTICS_MS); });
}
function pollDashboardUsage() {
  fetch('/v1/analytics/usage/daily?days=' + ANALYTICS_DAYS, { cache: 'no-store' }).then(function (r) {
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  }).then(function (d) {
    S.dashboardUsage = d.rows || []; S.dashboardUsageError = '';
    if (S.view === 'cc' && S.data) renderDashboard();
  }).catch(function (e) { S.dashboardUsageError = String(e.message || e); })
    .then(function () { setTimeout(pollDashboardUsage, ANALYTICS_MS); });
}
function loadCapacity(force) {
  var capacity = S.capacity;
  if (capacity.loading || (S.consoleUser && !consoleCan('view.machines_ai')) ||
      (!force && capacity.data && Date.now() - capacity.loadedAt < ANALYTICS_MS)) return;
  capacity.loading = true;
  fetch('/v1/capacity', { cache: 'no-store' }).then(function (r) {
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  }).then(function (data) {
    capacity.data = data; capacity.error = ''; capacity.loadedAt = Date.now();
  }).catch(function (e) {
    capacity.error = String(e.message || e); capacity.loadedAt = Date.now();
  }).then(function () {
    capacity.loading = false;
    if (S.view === 'cc' && S.data) renderDashboard();
  });
}
function loadDashboardWaiting(force) {
  var waiting = S.dashboardWaiting;
  if (waiting.loading || (S.consoleUser && !consoleCan('view.dashboard'))) return;
  if (!force && waiting.keys !== null && Date.now() - waiting.loadedAt < 12000) return;
  waiting.loading = true;
  if (waiting.keys === null && S.view === 'cc' && S.data) renderDashboard();
  fetch('/v1/dashboard/waiting', { cache: 'no-store' }).then(function (r) {
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  }).then(function (d) {
    waiting.keys = Array.isArray(d.ticket_keys) ? d.ticket_keys : [];
    waiting.error = ''; waiting.loadedAt = Date.now();
  }).catch(function (e) {
    waiting.keys = null; waiting.error = String(e.message || e); waiting.loadedAt = Date.now();
  }).then(function () {
    waiting.loading = false;
    if (S.view === 'cc' && S.data) renderDashboard();
    else if ((S.view === 'work' || S.view === 'delivery') && S.data) renderDBody();
  });
}
function loadDashboardDisagreements() {
  var state = S.dashboardDisagreements;
  if (state.loading || (S.consoleUser && !consoleCan('view.dashboard'))) return;
  if (state.rows !== null && Date.now() - state.loadedAt < 30000) return;
  state.loading = true;
  fetch('/v1/dashboard/disagreements', { cache: 'no-store' }).then(function (r) {
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  }).then(function (d) { state.rows = d.tickets || []; state.error = ''; })
    .catch(function (e) { state.error = String(e.message || e); if (state.rows === null) state.rows = []; })
    .then(function () {
      state.loading = false; state.loadedAt = Date.now();
      if (S.view === 'cc' && S.data) renderDashboard();
    });
}
function disagreementsPanel() {
  loadDashboardFlowFailures();
  var state = S.dashboardDisagreements, rows = state.rows || [];
  var h = '<section class="panel" data-block="disagreements" id="p-disagreements"><div class="panel-h"><h2>Jira and GitHub disagree</h2><span class="hint">More than five minutes apart · the flow keeps fixing them</span></div>';
  if (state.error) h += '<div class="stakeholder-error">' + esc(state.error) + '</div>';
  if (state.rows === null) return h + '<div class="muted">Loading…</div></section>' + flowFailuresPanel();
  if (!rows.length) return h + '<div class="empty">Every ticket matches its pull request.</div></section>' + flowFailuresPanel();
  h += '<div class="tablebox"><table><thead><tr><th>Ticket</th><th>Jira</th><th>Pull request</th><th>The flow will set</th><th>For</th><th>Why it is not fixed yet</th></tr></thead><tbody>';
  rows.forEach(function (r) {
    h += '<tr><td><button class="linkbtn mono" data-act="open-ticket" data-key="' + esc(r.key) + '">' + esc(r.key) + '</button><div class="small">' + esc(r.summary) + '</div></td><td>' + esc(r.jira_status) + '</td><td>' + (r.pr_url ? '<a href="' + esc(safeHref(r.pr_url)) + '" target="_blank" rel="noopener">PR #' + esc(String(r.pr_url).split('/').pop()) + '</a> · ' : '') + esc(r.github_state) + '</td><td>' + esc(r.expected) + '</td><td class="mono">' + mmss(r.age_s) + '</td><td class="small" style="white-space:normal">' + esc(r.detail) + '</td></tr>';
  });
  return h + '</tbody></table></div></section>' + flowFailuresPanel();
}
function loadDashboardFlowFailures(force) {
  var state = S.dashboardFlowFailures, user = S.consoleUser && S.consoleUser.user || '';
  if (state.user !== user) { state.data = null; state.error = ''; state.loadedAt = 0; state.user = user; }
  if (state.loading || (S.consoleUser && !consoleCan('view.dashboard')) ||
      (!force && state.data && Date.now() - state.loadedAt < 30000)) return;
  state.loading = true;
  fetch('/v1/dashboard/flow-failures', { cache: 'no-store' }).then(function (r) {
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  }).then(function (data) { state.data = data; state.error = ''; })
    .catch(function (e) { state.error = String(e.message || e); if (!state.data) state.data = { signatures: [] }; })
    .then(function () {
      state.loading = false; state.loadedAt = Date.now();
      if (S.view === 'cc' && S.data) renderDashboard();
    });
}
function flowFailuresPanel() {
  var state = S.dashboardFlowFailures, data = state.data, rows = data && data.signatures || [];
  var share = data ? (100 * Number(data.bugged_run_share || 0)).toFixed(1) : '—';
  var h = '<section class="panel" data-block="flow-failures"><div class="panel-h"><h2>Repeated failures</h2><span class="hint">Last 24 hours · ' + share + '% of terminal runs match a known bug</span></div>';
  if (state.error) h += '<div class="stakeholder-error">Failure data unavailable: ' + esc(state.error) + '</div>';
  if (!data) return h + '<div class="muted">Loading…</div></section>';
  if (!rows.length) return h + '<div class="empty">No repeating failure signatures in the last 24 hours.</div></section>';
  h += '<div class="tablebox"><table><thead><tr><th>Signature</th><th>Count</th><th>Tickets</th><th>Tokens</th><th>Cost</th><th>Root-cause bug</th></tr></thead><tbody>';
  rows.forEach(function (row) {
    var bug = row.bug ? '<button class="linkbtn mono" data-act="open-ticket" data-key="' + esc(row.bug.ticket) + '">' + esc(row.bug.ticket) + '</button><div class="small">' + esc(row.bug.state) + ' · ' + esc(row.bug.regressions) + ' regressions</div>' : '<span class="muted">Not filed</span>';
    h += '<tr><td><span class="mono">' + esc(row.signature) + '</span><div class="small">' + esc(row.pattern || '') + '</div></td><td>' + esc(row.count) + '</td><td>' + esc(row.tickets.join(', ')) + '</td><td class="mono">' + tok(row.tokens) + '</td><td class="mono">' + usd(row.cost_usd) + '</td><td>' + bug + '</td></tr>';
  });
  return h + '</tbody></table></div></section>';
}
function loadDashboardReviewRounds(force) {
  var state = S.dashboardReviewRounds;
  if (state.loading || (S.consoleUser && !consoleCan('view.dashboard'))) return;
  if (!force && state.data !== null && Date.now() - state.loadedAt < ANALYTICS_MS) return;
  state.loading = true;
  fetch('/v1/dashboard/review-rounds', { cache: 'no-store' }).then(function (r) {
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  }).then(function (data) { state.data = data; state.error = ''; })
    .catch(function (e) { state.error = String(e.message || e); if (state.data === null) state.data = {}; })
    .then(function () {
      state.loading = false; state.loadedAt = Date.now();
      if (S.view === 'cc' && S.data) renderDashboard();
    });
}
function pollDashboardReviewRounds() {
  if (S.view === 'cc') loadDashboardReviewRounds();
  setTimeout(pollDashboardReviewRounds, 15000);
}
function loadDashboardQuota(force) {
  if (!S.consoleUser || !consoleCan('view.dashboard') || consoleCan('view.machines_ai')) return;
  var quota = S.dashboardQuota, user = S.consoleUser.user || '';
  if (quota.user !== user) {
    quota.rows = null; quota.error = ''; quota.loadedAt = 0; quota.user = user;
  }
  if (quota.loading || (!force && quota.rows !== null && Date.now() - quota.loadedAt < ANALYTICS_MS)) return;
  quota.loading = true;
  fetch('/v1/dashboard/quota', { cache: 'no-store' }).then(function (r) {
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  }).then(function (d) {
    quota.rows = Array.isArray(d.rows) ? d.rows : []; quota.error = ''; quota.loadedAt = Date.now();
  }).catch(function (e) {
    quota.rows = null; quota.error = String(e.message || e); quota.loadedAt = Date.now();
  }).then(function () {
    quota.loading = false;
    if (S.view === 'cc' && S.data) renderDashboard();
  });
}
function pollDashboardWaiting() {
  if (S.consoleSessionLoaded && (S.view === 'cc' || S.view === 'work' || S.view === 'delivery')) {
    loadDashboardWaiting();
    if (S.view === 'cc') loadDashboardPipelineFailures();
  }
  setTimeout(pollDashboardWaiting, 15000);
}
function loadDashboardPipelineFailures(force) {
  var state = S.pipelineFailures;
  if (state.loading || (S.consoleUser && !consoleCan('view.dashboard'))) return;
  if (!force && state.data && Date.now() - state.loadedAt < ANALYTICS_MS) return;
  state.loading = true;
  fetch('/v1/dashboard/pipeline-failures?days=' + ANALYTICS_DAYS, { cache: 'no-store' }).then(function (r) {
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  }).then(function (data) { state.data = data; state.error = ''; })
    .catch(function (e) { state.error = String(e.message || e); })
    .then(function () {
      state.loading = false; state.loadedAt = Date.now();
      if (S.view === 'cc' && S.data) renderDashboard();
    });
}
function loadReviews() {
  fetch('/v1/reviews' + (S.reviewProject ? '?project=' + encodeURIComponent(S.reviewProject) : ''), { cache: 'no-store' }).then(function (r) {
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  }).then(function (d) { S.reviews = d.tickets || []; S.reviewMetrics = d.metrics || null; S.reviewProjects = d.projects || [];
    S.reviewThreads = d.threads || [];
    S.reviewsError = ''; if (S.view === 'review') renderReviews(); })
    .catch(function (e) { S.reviewsError = String(e.message || e); if (S.view === 'review') renderReviews(); });
}
function pollReviews() {
  if (S.view === 'review') loadReviews();
  setTimeout(pollReviews, 15000);
}
function notify(prevInc, prevRuns) {
  var d = D();
  var inc = {}; d.incidents.forEach(function (i) { inc[i.id] = i; });
  var runs = {}; d.recent_runs.forEach(function (r) { runs[r.id] = r; });
  if (prevInc) {
    d.incidents.forEach(function (i) { if (!prevInc[i.id]) toast(i.title, i.detail, i.severity === 'critical' ? 'crit' : 'warn'); });
    Object.keys(prevInc).forEach(function (id) { if (!inc[id]) toast('Resolved: ' + prevInc[id].title, '', 'ok'); });
  }
  if (prevRuns) {
    d.recent_runs.forEach(function (r) { if (!prevRuns[r.id]) toast(r.ticket + ' · ' + r.outcome, (r.account || '') + ' · ' + mmss(r.duration_s) + (r.pr_url ? ' · PR opened' : ''), runTone(r.outcome) === 'ok' ? 'ok' : runTone(r.outcome) === 'crit' ? 'crit' : 'info'); });
  }
  S.seenInc = inc; S.seenRuns = runs;
}

