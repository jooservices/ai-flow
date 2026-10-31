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
  var state = S.dashboardDisagreements, rows = state.rows || [];
  var h = '<section class="panel" data-block="disagreements" id="p-disagreements"><div class="panel-h"><h2>Jira and GitHub disagree</h2><span class="hint">More than five minutes apart · the flow keeps fixing them</span></div>';
  if (state.error) h += '<div class="stakeholder-error">' + esc(state.error) + '</div>';
  if (state.rows === null) return h + '<div class="muted">Loading…</div></section>';
  if (!rows.length) return h + '<div class="empty">Every ticket matches its pull request.</div></section>';
  h += '<div class="tablebox"><table><thead><tr><th>Ticket</th><th>Jira</th><th>Pull request</th><th>The flow will set</th><th>For</th><th>Why it is not fixed yet</th></tr></thead><tbody>';
  rows.forEach(function (r) {
    h += '<tr><td><button class="linkbtn mono" data-act="open-ticket" data-key="' + esc(r.key) + '">' + esc(r.key) + '</button><div class="small">' + esc(r.summary) + '</div></td><td>' + esc(r.jira_status) + '</td><td>' + (r.pr_url ? '<a href="' + esc(safeHref(r.pr_url)) + '" target="_blank" rel="noopener">PR #' + esc(String(r.pr_url).split('/').pop()) + '</a> · ' : '') + esc(r.github_state) + '</td><td>' + esc(r.expected) + '</td><td class="mono">' + mmss(r.age_s) + '</td><td class="small" style="white-space:normal">' + esc(r.detail) + '</td></tr>';
  });
  return h + '</tbody></table></div></section>';
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
  if (S.consoleSessionLoaded && (S.view === 'cc' || S.view === 'work' || S.view === 'delivery')) loadDashboardWaiting();
  setTimeout(pollDashboardWaiting, 15000);
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

