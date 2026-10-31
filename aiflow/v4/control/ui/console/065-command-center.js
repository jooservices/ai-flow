/* ---------- Command Center ---------- */
function dashboardProjectCounts(projectKey) {
  var allTickets = D().tickets;
  var allRows = allTickets.filter(function (t) { return t.project === projectKey; });
  var rows = allRows.filter(function (t) { return t.type !== 'Epic'; });
  return { total: rows.length, open: rows.filter(function (t) { return t.status !== 'Done'; }).length,
    building: rows.filter(function (t) { return ['In Progress', 'In Verify', 'Human review'].indexOf(t.status) >= 0; }).length,
    waiting: dashboardVisibleWaitingTickets(allTickets, S.dashboardWaiting.keys).filter(function (t) { return t.project === projectKey; }).length,
    done: rows.filter(function (t) { return t.status === 'Done'; }).length };
}
function providerUsageChart(rows) {
  var days = utcDays(ANALYTICS_DAYS), byDay = {}, providers = {};
  days.forEach(function (day) { byDay[day] = {}; });
  (rows || []).forEach(function (row) {
    var provider = row.key;
    if (!provider || !byDay[row.day]) return;
    providers[provider] = true;
    byDay[row.day][provider] = (byDay[row.day][provider] || 0) + Number(row.tokens || 0);
  });
  var keys = Object.keys(PROVIDER).filter(function (key) { return providers[key]; });
  Object.keys(providers).forEach(function (key) { if (!PROVIDER[key]) keys.push(key); });
  var max = 1;
  days.forEach(function (day) { max = Math.max(max, keys.reduce(function (n, key) { return n + (byDay[day][key] || 0); }, 0)); });
  var width = 30, gap = 10, height = 112, labelHeight = 18;
  var svg = '<svg class="provider-chart" viewBox="0 0 ' + (days.length * (width + gap)) + ' ' + (height + labelHeight) + '" role="img" aria-label="AI tokens per day by provider over the last ' + days.length + ' days">';
  days.forEach(function (day, index) {
    var x = index * (width + gap) + gap / 2, y = height;
    keys.forEach(function (provider) {
      var tokens = byDay[day][provider] || 0;
      if (!tokens) return;
      var h = height * tokens / max; y -= h;
      svg += '<rect class="provider-' + esc(provider) + '" x="' + x + '" y="' + y.toFixed(1) + '" width="' + width + '" height="' + h.toFixed(1) + '"><title>' + esc(day) + ' · ' + esc(PROVIDER[provider] || provider) + ': ' + tok(tokens) + ' tokens</title></rect>';
    });
    svg += '<text x="' + (x + width / 2) + '" y="' + (height + labelHeight - 2) + '" text-anchor="middle">' + esc(day.slice(8)) + '</text>';
  });
  return svg + '</svg>';
}
function dashboardSpark(values, color) {
  values = (values || []).slice(-7);
  while (values.length < 7) values.unshift(0);
  var width = 88, height = 30, max = Math.max.apply(null, values.concat([1]));
  var points = values.map(function (value, index) {
    return [index * (width / (values.length - 1)), height - 3 - Number(value || 0) / max * (height - 8)];
  });
  var line = points.map(function (point, index) { return (index ? 'L' : 'M') + point[0].toFixed(1) + ' ' + point[1].toFixed(1); }).join(' ');
  return '<svg class="kpi-trend" viewBox="0 0 ' + width + ' ' + height + '" aria-hidden="true"><path d="' + line + '" fill="none" stroke="' + color + '" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"></path></svg>';
}
function dashboardUsageTotals(rows) {
  var byProvider = {}, today = new Date().toISOString().slice(0, 10), tokensToday = 0, costToday = 0, tokenDays = {}, runDays = {};
  (rows || []).forEach(function (row) {
    var provider = row.key || 'unknown';
    byProvider[provider] = (byProvider[provider] || 0) + Number(row.tokens || 0);
    tokenDays[row.day] = (tokenDays[row.day] || 0) + Number(row.tokens || 0);
    runDays[row.day] = (runDays[row.day] || 0) + Number(row.runs || 0);
    if (row.day === today) { tokensToday += Number(row.tokens || 0); costToday += Number(row.cost_usd || 0); }
  });
  return { byProvider: byProvider, tokenDays: tokenDays, runDays: runDays, tokensToday: tokensToday, costToday: costToday };
}
function dashboardUsageShare(rows) {
  var totals = dashboardUsageTotals(rows), providers = Object.keys(totals.byProvider).sort();
  var grand = providers.reduce(function (sum, key) { return sum + totals.byProvider[key]; }, 0), h = '<div class="usage-share"><h3>Share of usage · last ' + ANALYTICS_DAYS + ' days</h3>';
  if (!grand) return h + '<div class="dashboard-empty">No recorded AI usage in this period.</div></div>';
  h += '<div class="sharebar" role="img" aria-label="Share of recorded tokens by AI provider">';
  providers.forEach(function (key) { h += '<i class="provider-' + esc(key) + '" style="width:' + (100 * totals.byProvider[key] / grand).toFixed(2) + '%;background:var(--provider-' + esc(key) + ')"></i>'; });
  h += '</div><div class="share-legend">';
  providers.forEach(function (key) { h += '<span><i class="provider-' + esc(key) + '" style="background:var(--provider-' + esc(key) + ')"></i>' + esc(PROVIDER[key] || key) + ' · ' + Math.round(100 * totals.byProvider[key] / grand) + '% · ' + tok(totals.byProvider[key]) + ' tokens</span>'; });
  return h + '</div></div>';
}
function capacityPanel() {
  var state = S.capacity, data = state.data;
  var h = '<section class="panel" id="d-capacity" aria-labelledby="h-capacity"><div class="panel-h"><h2 id="h-capacity">Capacity plan</h2><span class="hint">Next seven days · quota and completed ticket history</span><span class="spacer"></span><button class="btn sm" data-act="capacity-refresh">' + (data ? 'Refresh' : 'Load plan') + '</button></div>';
  if (state.error) h += '<div class="dashboard-empty">Capacity plan unavailable: ' + esc(state.error) + '</div>';
  if (!data) return h + '<div class="dashboard-empty">' + (state.loading ? 'Loading estimates and quota…' : 'Capacity data is not available yet.') + '</div></section>';
  var limit = data.limiting_ai;
  h += '<div class="facts"><div class="fact"><span class="fl">Ready tickets</span><span class="fv mono">' + Number(data.ready_tickets || 0) + '</span></div>';
  h += '<div class="fact"><span class="fl">Finishable with current quota</span><span class="fv mono">' + Number(data.tickets_finishable || 0) + '</span></div>';
  h += '<div class="fact"><span class="fl">Quota capacity</span><span class="fv mono">' + Number(data.quota_capacity_tickets || 0) + ' tickets</span></div>';
  h += '<div class="fact"><span class="fl">Limiting AI</span><span class="fv mono">' + (limit ? esc(limit.account) + ' · ' + Number(limit.tickets) + ' tickets' : 'No quota forecast') + '</span></div></div>';
  if (data.accounts && data.accounts.length) {
    h += '<div class="tablebox"><table style="min-width:560px"><thead><tr><th>AI account</th><th>Tokens for seven days</th><th>Estimated tickets</th><th>Limit</th><th>Forecast</th></tr></thead><tbody>';
    data.accounts.forEach(function (account) {
      h += '<tr style="cursor:default"><td class="mono">' + esc(account.account) + ' <span class="small">' + esc(PROVIDER[account.provider] || account.provider) + '</span></td>';
      h += '<td class="mono">' + tok(account.tokens_available) + '</td><td class="mono">' + Number(account.tickets || 0) + '</td>';
      h += '<td>' + esc(account.limiting_window || '—') + '</td><td>' + esc(account.quota_source) + ' · ' + esc(account.confidence) + '</td></tr>';
    });
    h += '</tbody></table></div>';
  }
  h += '<div class="dsec"><h4>Why tickets were assigned</h4>';
  if (!data.decisions || !data.decisions.length) h += '<div class="empty">Assignment explanations will appear after a development run starts.</div>';
  else {
    h += '<ul class="feed">';
    data.decisions.slice(0, 8).forEach(function (decision) {
      h += '<li><span class="f-time">' + esc(when(decision.assigned_at)) + '</span><span class="f-type">' + esc(decision.account) + '</span>';
      h += '<span class="f-text"><button class="linkbtn mono" data-act="open-ticket" data-key="' + esc(decision.ticket) + '">' + esc(decision.ticket) + '</button> ' + esc(decision.reason) + '</span></li>';
    });
    h += '</ul>';
  }
  h += '</div><div class="dsec"><h4>Estimate error</h4>';
  if (!data.errors || !data.errors.length) h += '<div class="empty">Completed ticket estimates will be compared with recorded tokens, time and rework here.</div>';
  else {
    h += '<div class="tablebox"><table style="min-width:600px"><thead><tr><th>Ticket</th><th>Tokens</th><th>AI time</th><th>Rework</th></tr></thead><tbody>';
    data.errors.slice(0, 8).forEach(function (row) {
      var tokens = row.actual.tokens, minutes = row.actual.time_minutes, risk = row.actual.rework;
      h += '<tr><td><button class="linkbtn mono" data-act="open-ticket" data-key="' + esc(row.ticket) + '">' + esc(row.ticket) + '</button></td>';
      h += '<td class="mono">' + tok(tokens.actual) + ' · ' + (tokens.within_range ? 'in range' : signedPct(tokens.error_pct)) + '</td>';
      h += '<td class="mono">' + mmss(minutes.actual * 60) + ' · ' + (minutes.within_range ? 'in range' : signedPct(minutes.error_pct)) + '</td>';
      h += '<td>' + (risk.actual ? 'rework' : 'no rework') + ' · predicted ' + Number(risk.predicted_pct.low).toFixed(0) + '–' + Number(risk.predicted_pct.high).toFixed(0) + '%</td></tr>';
    });
    h += '</tbody></table></div>';
  }
  return h + '</div></section>';
}
function signedPct(value) { return (Number(value) > 0 ? '+' : '') + Number(value || 0).toFixed(0) + '%'; }
function dashboardWaitingTickets(tickets, queueKeys) {
  var allTickets = tickets || [], byKey = {};
  if (Array.isArray(queueKeys)) {
    var queue = {}; queueKeys.forEach(function (key) { queue[String(key)] = true; });
    return allTickets.filter(function (ticket) { return !!queue[ticket.key]; });
  }
  allTickets.forEach(function (ticket) { byKey[ticket.key] = ticket; });
  return allTickets.filter(function (ticket) {
    if (ticket.status === 'Done' || ticket.status === 'Closed') return false;
    if (ticket.status === 'Needs owner' || ticket.status === 'Human review') return true;
    return (ticket.blocked_by || []).some(function (key) {
      var blocker = byKey[key]; return blocker && blocker.status !== 'Done' && blocker.status !== 'Closed';
    });
  });
}
function dashboardVisibleWaitingTickets(tickets, queueKeys) {
  var waiting = dashboardWaitingTickets(tickets, queueKeys);
  return consoleCan('view.human_review') ? waiting : waiting.filter(function (ticket) { return ticket.type !== 'Epic'; });
}
function setDashboardTab(tab) {
  S.dashboardTab = tab === 'ai' ? 'ai' : 'work';
  if (S.view === 'cc') {
    renderDashboard();
    var button = $('#dashboard-tab-' + S.dashboardTab); if (button) button.focus();
  }
}
function openDashboardWorkFilter(filter) {
  S.project = 'all';
  S.workStatusFilter = filter;
  S.search = ''; S.epic = 'all'; S.typeFilter = 'all'; S.aiFilter = 'all';
  savePrefs(); setView('work');
}
function openDashboardCard(card) {
  if (card === 'ai-usage') { setDashboardTab('ai'); return; }
  if (card === 'waiting' && consoleCan('view.human_review')) { S.reviewProject = ''; setView('review'); return; }
  openDashboardWorkFilter(card === 'done' ? 'done' : card === 'in-flight' ? 'in-flight' : 'waiting');
}
function renderDashboard() {
  var canSeeMachines = !S.consoleUser || consoleCan('view.machines_ai');
  var root = $('#view-cc'); if (!root) return;
  if (!canSeeMachines) loadDashboardQuota(false);
  loadDashboardDisagreements();
  var oldSearch = $('#dash-project-search'), focusSearch = oldSearch && document.activeElement === oldSearch;
  var cursor = focusSearch && oldSearch.selectionStart;
  var d = D(), tickets = d.tickets.filter(function (t) { return t.type !== 'Epic'; }), totals = d.totals || {};
  var inFlightStatuses = ['In Progress', 'In Verify', 'Human review', 'Ready to merge'];
  var inFlight = tickets.filter(function (t) { return inFlightStatuses.indexOf(t.status) >= 0; }).length;
  var waiting = dashboardVisibleWaitingTickets(d.tickets, S.dashboardWaiting.keys);
  var waitingCount = waiting.length;
  var done = tickets.filter(function (t) { return t.status === 'Done'; }), days = utcDays(7), usage = dashboardUsageTotals(S.dashboardUsage);
  var doneTrend = days.map(function (day) { return done.filter(function (t) { return t.done_at && String(t.done_at).slice(0, 10) === day; }).length; });
  var waitingTrend = days.map(function () { return 0; }), inFlightTrend = days.map(function () { return 0; }), byWaitingDay = {};
  days.forEach(function (day, index) { byWaitingDay[day] = index; });
  (d.events || []).forEach(function (event) {
    var date = String(event.ts || '').slice(0, 10), index = byWaitingDay[date];
    if (index == null || ['ticket.status', 'jira.transition'].indexOf(event.type) < 0) return;
    if (event.outcome === 'Needs owner' || event.outcome === 'Human review') waitingTrend[index] += 1;
    if (inFlightStatuses.indexOf(event.outcome) >= 0) inFlightTrend[index] += 1;
  });
  var doneThisWeek = doneTrend.reduce(function (sum, count) { return sum + count; }, 0);
  var selectedProject = d.projects.filter(function (p) { return p.key === S.project; })[0] || d.projects[0];
  var selectedKey = selectedProject ? selectedProject.key : '';
  var q = S.dashboardQuery.trim().toLowerCase();
  var projectRows = d.projects.filter(function (p) { return !q || (p.key + ' ' + p.repo).toLowerCase().indexOf(q) >= 0; }).map(function (p) {
    return { project: p, counts: dashboardProjectCounts(p.key) };
  });
  projectRows.sort(function (a, b) {
    var key = S.dashboardSort, left = key === 'key' ? a.project.key.toLowerCase() : a.counts[key];
    var right = key === 'key' ? b.project.key.toLowerCase() : b.counts[key];
    if (typeof left === 'string') return left.localeCompare(right) * S.dashboardSortDir;
    return (left - right) * S.dashboardSortDir;
  });
  var recentRuns = d.active_runs.slice().sort(function (a, b) { return (a.elapsed_s || 0) - (b.elapsed_s || 0); }).slice(0, 8);
  var flowCounts = STATUS_COLS.map(function (c) { return tickets.filter(function (t) { return inColumn(t, c[0]); }).length; });
  var flowMax = Math.max.apply(null, flowCounts.concat([1]));
  var flow = STATUS_COLS.map(function (c, index) {
    var count = flowCounts[index], color = c[0] === 'Done' ? 'var(--good)' :
      (c[0] === 'In Progress' || c[0] === 'In Verify') ? 'var(--provider-codex)' :
        (c[0] === 'Human review' || c[0] === 'Needs owner') ? 'var(--warn)' : 'var(--muted)';
    var width = count ? Math.max(3, 100 * count / flowMax) : 0;
    return '<button type="button" class="flow-step" data-act="flow-step" data-status="' + esc(c[0]) + '" aria-label="Open Work board filtered to ' + esc(c[1]) + ' (' + count + ' tickets)"><span class="flow-label">' + esc(c[1]) + '</span><span class="flow-track" aria-hidden="true"><i style="width:' + width.toFixed(1) + '%;background:' + color + '"></i></span><b>' + count + '</b></button>';
  }).join('');
  var total = tickets.length, donePct = total ? Math.round(100 * done.length / total) : 0;
  var h = '<h1 class="dash-title">Dashboard</h1><div class="dashboard-tabs" role="tablist" aria-label="Dashboard sections">';
  h += '<button type="button" role="tab" class="dashboard-tab" id="dashboard-tab-work" aria-controls="dashboard-panel" aria-selected="' + (S.dashboardTab !== 'ai') + '" data-act="dashboard-tab" data-tab="work">Work</button>';
  h += '<button type="button" role="tab" class="dashboard-tab" id="dashboard-tab-ai" aria-controls="dashboard-panel" aria-selected="' + (S.dashboardTab === 'ai') + '" data-act="dashboard-tab" data-tab="ai">AI and machines</button></div>';
  h += '<div id="dashboard-panel" role="tabpanel" aria-labelledby="dashboard-tab-' + (S.dashboardTab === 'ai' ? 'ai' : 'work') + '">';
  h += '<section class="dash-section" data-dashboard-panel="work"' + (S.dashboardTab === 'ai' ? ' hidden' : '') + '><h2 class="dash-section-title" id="dashboard-work-title">Work</h2>';
  h += '<div data-block="first-run-setup">' + (window.AIFlowSetup ? AIFlowSetup.panel() : '') + '</div>';
  h += '<div class="dash-kpis" data-block="kpis" aria-label="Work summary">';
  h += '<button type="button" class="dash-kpi" data-act="dashboard-card" data-card="done"><span class="label">Done</span><span class="kpi-main"><span class="value">' + done.length + '</span>' + dashboardSpark(doneTrend, 'var(--good)') + '</span><span class="kpi-delta">+' + doneThisWeek + ' this week</span><span class="detail">of ' + total + ' tickets · ' + donePct + '% complete</span></button>';
  h += '<button type="button" class="dash-kpi" data-act="dashboard-card" data-card="in-flight"><span class="label">In flight</span><span class="kpi-main"><span class="value">' + inFlight + '</span>' + dashboardSpark(inFlightTrend, 'var(--provider-codex)') + '</span><span class="kpi-delta">' + Number(d.active_runs.length || 0) + ' live runs</span><span class="detail">In progress, verify or human review</span></button>';
  h += '<button type="button" class="dash-kpi" data-act="dashboard-card" data-card="waiting"><span class="label">Waiting for you</span><span class="kpi-main"><span class="value">' + (S.dashboardWaiting.keys === null && S.dashboardWaiting.loading ? '—' : waitingCount) + '</span>' + dashboardSpark(waitingTrend, 'var(--warn)') + '</span><span class="kpi-delta">' + (waitingCount ? 'Needs a person' : 'All clear') + '</span><span class="detail">Questions, decisions and reviews</span></button>';
  h += '<button type="button" class="dash-kpi" data-act="dashboard-card" data-card="ai-usage"><span class="label">AI usage today</span><span class="kpi-main"><span class="value">' + tok(usage.tokensToday) + ' <small>tokens</small></span>' + dashboardSpark(days.map(function (day) { return usage.tokenDays[day] || 0; }), 'var(--provider-claude)') + '</span><span class="kpi-delta">' + (usage.costToday ? 'Approx. $' + usage.costToday.toFixed(4) : 'Approx. $0.0000') + '</span><span class="detail">Approximate cost today</span></button></div>';
  h += '<section class="panel" data-block="conflict-loss">' + conflictLossBlock() + '</section>';
  h += '<section class="panel" data-block="flow" aria-labelledby="dashboard-flow-title"><div class="panel-h"><h2 id="dashboard-flow-title">Flow at a glance</h2><span class="hint">Tickets across every project</span></div><div class="dash-flow">' + flow + '</div></section>';
  h += '<section class="panel" data-block="projects"><div class="panel-h"><h2>Projects</h2><span class="hint">' + projectRows.length + ' shown · choose a project to open its board</span></div>';
  h += '<div class="dash-tools"><label for="dash-project-search">Search projects</label><input class="in" id="dash-project-search" type="search" placeholder="Name or key" value="' + esc(S.dashboardQuery) + '"></div>';
  h += '<div class="tablebox"><table class="dash-table"><thead><tr><th><button class="sort-btn" data-act="dashboard-sort" data-key="key">Project' + (S.dashboardSort === 'key' ? (S.dashboardSortDir > 0 ? ' ↑' : ' ↓') : '') + '</button></th><th><button class="sort-btn" data-act="dashboard-sort" data-key="open">Open' + (S.dashboardSort === 'open' ? (S.dashboardSortDir > 0 ? ' ↑' : ' ↓') : '') + '</button></th><th><button class="sort-btn" data-act="dashboard-sort" data-key="building">Building' + (S.dashboardSort === 'building' ? (S.dashboardSortDir > 0 ? ' ↑' : ' ↓') : '') + '</button></th><th><button class="sort-btn" data-act="dashboard-sort" data-key="waiting">Waiting' + (S.dashboardSort === 'waiting' ? (S.dashboardSortDir > 0 ? ' ↑' : ' ↓') : '') + '</button></th><th><button class="sort-btn" data-act="dashboard-sort" data-key="done">Done' + (S.dashboardSort === 'done' ? (S.dashboardSortDir > 0 ? ' ↑' : ' ↓') : '') + '</button></th></tr></thead><tbody>';
  projectRows.forEach(function (row) {
    var p = row.project, c = row.counts;
    h += '<tr><td><button class="linkbtn" data-act="dashboard-project" data-p="' + esc(p.key) + '" aria-label="Open ' + esc(p.key) + ' on the Work board">' + esc(p.key) + '</button><div class="small">' + esc(p.repo) + (selectedKey === p.key ? ' · selected' : '') + '</div></td><td>' + c.open + '</td><td>' + c.building + '</td><td>' + c.waiting + '</td><td>' + c.done + '</td></tr>';
  });
  if (!projectRows.length) h += '<tr><td colspan="5" class="empty">No matching projects.</td></tr>';
  h += '</tbody></table></div></section>';
  h += '<section class="panel" data-block="building"><div class="panel-h"><h2>What is being built</h2><span class="hint">Live runs and the selected project roadmap · ' + esc(selectedKey || 'no project selected') + '</span><span class="spacer"></span><button class="btn sm" data-act="view" data-view="work">Open Work board</button></div>';
  if (recentRuns.length) {
    h += '<div class="att">';
    recentRuns.forEach(function (run) { h += '<button class="att-item" data-act="open-ticket" data-key="' + esc(run.ticket) + '"><span class="pill" data-tone="info">Running</span><span class="t"><span class="mono muted">' + esc(run.ticket) + '</span> ' + esc(run.summary || '') + '<span class="d">' + esc(run.project) + ' · ' + esc(PROVIDER[run.provider] || run.provider || '') + ' · ' + esc(run.step) + '</span></span><span class="mono">' + mmss(run.elapsed_s) + '</span></button>'; });
    h += '</div>';
  } else h += '<div class="empty">No AI work is running now.</div>';
  var roadmap = selectedKey ? epicProgress(projTicketsFor(selectedKey)) : [];
  h += '<h3>Roadmap' + (selectedKey ? ' · ' + esc(selectedKey) : '') + '</h3>';
  if (!roadmap.length) h += '<div class="empty">No epics are available for this project yet.</div>';
  else {
    h += '<div class="roadmap-list">';
    roadmap.slice(0, 8).forEach(function (row) {
      h += '<button class="roadmap-row" data-act="epic-dashboard" data-key="' + esc(row.epic.key) + '" aria-label="Open Work board filtered by epic ' + esc(row.epic.key) + '"><span class="roadmap-label"><span class="mono muted">' + esc(row.epic.key) + '</span> ' + esc(row.epic.summary) + '</span><span class="roadmap-track" aria-hidden="true"><i style="width:' + row.pct + '%"></i></span><span class="mono">' + row.pct + '% · ' + row.counts.done + '/' + row.items + '</span></button>';
    });
    h += '</div>';
  }
  h += '</section><section class="panel" data-block="waiting"><div class="panel-h"><h2>Waiting for you</h2><span class="hint">Human review or owner decision</span></div><div class="att">';
  if (!waiting.length) h += '<div class="empty">Nothing is waiting on a person.</div>';
  waiting.slice(0, 12).forEach(function (t) { h += '<button class="att-item" data-act="open-ticket" data-key="' + esc(t.key) + '"><span class="pill" data-tone="' + (t.status === 'Needs owner' ? 'warn' : 'info') + '">' + esc(t.status) + '</span><span class="t"><span class="mono muted">' + esc(t.key) + '</span> ' + esc(t.summary) + '<span class="d">' + esc(t.project) + '</span></span><span class="muted">Open</span></button>'; });
  h += '</div></section>' + disagreementsPanel() + escalationsPanel() + '</section>';
  var tokenTotal = (S.dashboardUsage || []).reduce(function (n, row) { return n + Number(row.tokens || 0); }, 0);
  var costTotal = (S.dashboardUsage || []).reduce(function (n, row) { return n + Number(row.cost_usd || 0); }, 0);
  var providers = Object.keys(PROVIDER).filter(function (key) { return (S.dashboardUsage || []).some(function (row) { return row.key === key; }) || d.accounts.some(function (a) { return a.provider === key; }); });
  h += '<section class="dash-section" data-dashboard-panel="ai"' + (S.dashboardTab !== 'ai' ? ' hidden' : '') + '><h2 class="dash-section-title" id="dashboard-ai-title">AI and machines</h2>';
  h += '<section class="panel" data-block="usage"><div class="panel-h"><h2>AI usage</h2><span class="hint">Tokens per day by AI · last ' + ANALYTICS_DAYS + ' days</span></div>';
  if (S.dashboardUsageError) h += '<div class="dashboard-empty">Usage chart unavailable: ' + esc(S.dashboardUsageError) + '</div>';
  else if (!S.dashboardUsage.length) h += '<div class="dashboard-empty">No recorded AI usage in this period.</div>';
  h += providerUsageChart(S.dashboardUsage);
  h += '<div class="provider-legend" aria-label="AI provider colours">' + providers.map(function (key) { return '<span><i class="provider-swatch provider-' + esc(key) + '"></i><span class="provider-name">' + esc(PROVIDER[key] || key) + '</span></span>'; }).join('') + '</div>' + dashboardUsageShare(S.dashboardUsage) + '</section>';
  h += '<section class="panel" data-block="tokens"><div class="panel-h"><h2>Tokens and cost</h2><span class="hint">Recorded run usage · UTC</span></div><div class="facts"><div class="fact"><span class="fl">Tokens today</span><span class="fv mono">' + tok(usage.tokensToday) + '</span></div><div class="fact"><span class="fl">Approximate cost today</span><span class="fv mono">$' + usage.costToday.toFixed(4) + '</span></div><div class="fact"><span class="fl">Tokens · last 14 days</span><span class="fv mono">' + tok(tokenTotal) + '</span></div><div class="fact"><span class="fl">Cost · last 14 days</span><span class="fv mono">' + usd(costTotal) + '</span></div></div></section>';
  h += capacityPanel();
  h += '<section class="panel" data-block="quota"><div class="panel-h"><h2>Quota by AI</h2><span class="hint">Per account · current quota and next reset time</span></div>';
  if (!canSeeMachines && S.dashboardQuota.error) h += '<div class="dashboard-empty">Quota summary unavailable: ' + esc(S.dashboardQuota.error) + '</div>';
  else if (!canSeeMachines && S.dashboardQuota.rows === null) h += '<div class="dashboard-empty">' + (S.dashboardQuota.loading ? 'Loading project-scoped quota…' : 'Project-scoped quota is unavailable.') + '</div>';
  else if (!canSeeMachines && !S.dashboardQuota.rows.length) h += '<div class="dashboard-empty">No AI accounts serve your visible projects.</div>';
  else if (canSeeMachines && !d.accounts.length) h += '<div class="dashboard-empty">No AI accounts are configured.</div>';
  else if (canSeeMachines) {
    h += '<div class="quota-ai-list">';
    providers.forEach(function (provider) {
      var accounts = d.accounts.filter(function (account) { return account.provider === provider; });
      if (!accounts.length) return;
      h += '<section class="quota-ai"><div class="quota-ai-head"><h3 class="provider-name provider-' + esc(provider) + '">' + esc(PROVIDER[provider] || provider) + '</h3><span class="hint">' + accounts.length + ' account' + (accounts.length === 1 ? '' : 's') + '</span></div><div class="tablebox"><table class="quota-table"><thead><tr><th>Account</th><th>Status</th><th>Quota windows</th></tr></thead><tbody>';
      accounts.forEach(function (account) {
        h += '<tr><td class="mono">' + esc(account.id) + '<div class="small">' + esc(account.node) + '</div></td><td>' + pill(accTone(account), accLabel(account), account.status === 'working') + '</td><td>' + qwin(account) + '<span class="qnote">Next quota reset: ' + esc(quotaResetText(account)) + '</span></td></tr>';
      });
      h += '</tbody></table></div></section>';
    });
    h += '</div>';
  } else {
    var scopedProviders = {};
    S.dashboardQuota.rows.forEach(function (account) {
      (scopedProviders[account.provider] || (scopedProviders[account.provider] = [])).push(account);
    });
    h += '<div class="quota-ai-list">';
    Object.keys(scopedProviders).sort().forEach(function (provider) {
      var accounts = scopedProviders[provider];
      h += '<section class="quota-ai"><div class="quota-ai-head"><h3 class="provider-name provider-' + esc(provider) + '">' + esc(PROVIDER[provider] || provider) + '</h3><span class="hint">' + accounts.length + ' account' + (accounts.length === 1 ? '' : 's') + ' in your project scope</span></div><div class="tablebox"><table class="quota-table"><thead><tr><th>AI account</th><th>Quota windows</th></tr></thead><tbody>';
      accounts.forEach(function (account) {
        var windows = account.windows || [], resets = [];
        windows.forEach(function (window) { if (window.reset_at) resets.push(window.reset_at); });
        resets.sort(function (a, b) { return quotaDateMs(a) - quotaDateMs(b); });
        var quotaHtml = windows.length ? '<div class="qcell">' : '<div class="qcell"><span class="qnote">Quota not reported</span>';
        windows.forEach(function (window) {
          var used = window.used_pct == null ? null : Math.max(0, Math.min(100, Number(window.used_pct)));
          quotaHtml += '<div class="qrow" title="' + esc(window.label) + '"><span class="ql">' + esc(window.label) + (window.estimated ? ' · estimate' : '') + '</span><span class="track"><i style="width:' + (used == null ? 0 : used).toFixed(1) + '%" data-tone="' + (used == null ? 'muted' : tone(used)) + '"></i></span><span class="qv">' + (used == null ? '—' : Math.round(used) + '%') + '</span></div>';
        });
        quotaHtml += '<span class="qnote">Next quota reset: ' + (resets.length ? esc(quotaWhen(resets[0])) : 'Not reported') + '</span></div>';
        h += '<tr><td>' + esc(account.label || 'AI account') + '</td><td>' + quotaHtml + '</td></tr>';
      });
      h += '</tbody></table></div></section>';
    });
    h += '</div>';
  }
  h += '</section>';
  if (canSeeMachines) h += toolWaitsPanel(d);
  if (canSeeMachines) h += flowDoctorPanel(d, esc, when);
  if (consoleCan('view.machines_ai')) h += masterAutonomyPanel();
  h += '</section></div>';
  root.innerHTML = h;
  if (focusSearch) { var next = $('#dash-project-search'); if (next) { next.focus(); if (next.setSelectionRange && cursor != null) next.setSelectionRange(cursor, cursor); } }
  applyLayout('cc', root);
}
function projTicketsFor(projectKey) { return D().tickets.filter(function (t) { return t.project === projectKey; }); }
function quotaResetText(account) {
  var assessed = account.quota_awareness && account.quota_awareness.windows || [], known = [];
  assessed.forEach(function (window) { if (window.reset_at) known.push(window.reset_at); });
  if (known.length) return when(known.sort()[0]);
  var windows = account.quota && account.quota.windows || {}, resets = [];
  Object.keys(windows).forEach(function (key) { if (windows[key] && windows[key].resets_at) resets.push(windows[key].resets_at); });
  if (resets.length) return quotaWhen(resets.sort(function (a, b) { return quotaDateMs(a) - quotaDateMs(b); })[0]);
  if (account.cooldown_until) return when(account.cooldown_until);
  return 'Not reported';
}
function masterAutonomyPanel() {
  if (!consoleReader()) return '<section class="panel"><div class="panel-h"><h2>Master autonomy</h2></div><div class="muted">Sign in to view the autonomy report and daily budget.</div></section>';
  if (!S.autonomy.data && !S.autonomy.busy) loadMasterAutonomy(false);
  var data = S.autonomy.data, budget = data && data.budget, report = data && data.report;
  var h = '<section class="panel" data-block="autonomy" id="p-master-autonomy"><div class="panel-h"><h2>Master autonomy</h2><span class="hint">Weekly report and daily model time budget</span></div>';
  if (S.autonomy.error) h += '<div class="stakeholder-error">' + esc(S.autonomy.error) + '</div>';
  if (!data) return h + '<div class="muted">' + (S.autonomy.busy ? 'Loading autonomy report…' : 'Autonomy report unavailable.') + '</div></section>';
  h += '<div class="grid-2"><div class="review-fact"><div class="label">Today · UTC</div><div class="value">' + esc(budget.used_minutes) + ' / ' + esc(budget.daily_budget_minutes) + ' minutes used · ' + esc(budget.remaining_minutes) + ' remaining</div></div>';
  h += '<div class="review-fact"><div class="label">Daily budget</div><div class="value">';
  if (consoleCan('console.write')) h += '<label for="master-budget">Minutes <input id="master-budget" data-master-budget type="number" min="0" max="1440" step="1" value="' + esc(budget.daily_budget_minutes) + '" style="width:110px;min-height:36px;border:1px solid var(--line);border-radius:8px;padding:5px 8px;background:var(--surface-2)"></label> <button class="btn sm primary" data-act="master-budget-save"' + (S.autonomy.saving ? ' disabled' : '') + '>Save</button>';
  else h += esc(budget.daily_budget_minutes) + ' minutes';
  h += '</div></div></div>';
  h += '<div class="panel-h"><h3 style="margin:4px 0">Weekly autonomy · week of ' + esc(report.week_start) + '</h3></div>';
  h += '<div class="tablebox"><table><thead><tr><th>Week</th><th>Finished without a person</th><th>Person needed</th><th>Repeated failures</th></tr></thead><tbody>';
  (report.trend || []).forEach(function (row) { h += '<tr><td class="mono">' + esc(row.week_start) + '</td><td>' + esc(row.finished_without_person) + '</td><td>' + esc(row.needed_person) + '</td><td>' + esc(row.repeated_failures) + '</td></tr>'; });
  h += '</tbody></table></div>';
  h += '<div class="grid-2"><div><h3>Tickets finished without a person</h3>';
  if (!(report.finished_without_person || []).length) h += '<div class="muted">No AI-finished tickets recorded this week.</div>';
  else (report.finished_without_person || []).forEach(function (t) { h += '<div class="feed"><button class="btn sm" data-act="open-ticket" data-key="' + esc(t.key) + '">' + esc(t.key) + '</button> ' + esc(t.summary || '') + '</div>'; });
  h += '</div><div><h3>When a person was needed</h3>';
  if (!(report.needed_person || []).length) h += '<div class="muted">No owner decisions requested this week.</div>';
  else (report.needed_person || []).forEach(function (item) { h += '<div class="review-fact"><div class="label">' + esc(item.ticket || item.observation_key) + ' · ' + esc(when(item.sent_at)) + '</div><div class="value">' + esc(item.reason) + '</div><div class="muted">Options: ' + esc((item.options || []).join(' · ')) + '</div></div>'; });
  h += '</div></div><div><h3>Repeated flow failures</h3>';
  if (!(report.repeated_failures || []).length) h += '<div class="muted">No repeated failure Bugs updated this week.</div>';
  else (report.repeated_failures || []).forEach(function (item) { h += '<div class="review-fact"><b class="mono">' + esc(item.ticket_key) + '</b> · ' + esc(item.summary || item.signature) + ' · ' + esc(item.repeat_count) + ' additional runs</div>'; });
  h += '</div><section class="panel" style="margin-top:18px"><div class="panel-h"><h3>Weekly quota and usage</h3><span class="hint">Current account values · seven-day history</span></div>';
  if (!(report.quota_accounts || []).length) h += '<div class="muted">No AI accounts are configured.</div>';
  (report.quota_accounts || []).forEach(function (account) {
    h += '<div class="review-fact"><div class="label mono">' + esc(account.id) + ' · ' + esc(PROVIDER[account.provider] || account.provider) + '</div>' + quotaAwareness(account) + '</div>';
  });
  return h + '</section></section>';
}
function renderCC() {
  var d = D();
  if (S.consoleUser && !consoleCan('view.machines_ai')) {
    var h = '<section class="panel"><div class="panel-h"><h2>My projects</h2><span class="hint">Projects available to your account</span><span class="spacer"></span>' +
      '<button class="btn primary sm" data-act="view" data-view="work">Open work board</button></div>';
    if (!(d.projects || []).length) h += '<div class="empty">No projects are assigned to your account.</div>';
    else h += '<div class="project-table"><table><thead><tr><th>Project</th><th>Repository</th><th>Open tickets</th><th>Completed</th></tr></thead><tbody>' +
      d.projects.map(function (p) {
        var counts = p.counts || {}, total = Object.keys(counts).reduce(function (sum, key) { return sum + Number(counts[key] || 0); }, 0);
        return '<tr><td><b class="mono">' + esc(p.key) + '</b></td><td>' + esc((p.repositories || [p.repo]).join(', ')) +
          '</td><td class="num">' + Math.max(0, total - Number(counts.Done || 0)) + '</td><td class="num">' + Number(counts.Done || 0) + '</td></tr>';
      }).join('') + '</tbody></table></div>';
    $('#view-cc').innerHTML = h + '</section>';
    return;
  }
  var online = d.nodes.filter(function (n) { return n.status === 'online'; });
  var offline = d.nodes.filter(function (n) { return n.status === 'offline'; });
  var hasAcc = {}; d.nodes.forEach(function (n) {
    if (d.accounts.some(function (a) { return accountAssignedTo(a, n.name); })) hasAcc[n.name] = 1;
  });
  var slots = d.nodes.reduce(function (s, n) { return s + (n.status === 'online' && hasAcc[n.name] ? n.slots_total : 0); }, 0);
  var idleSlots = d.nodes.reduce(function (s, n) { return s + (n.status === 'online' && !hasAcc[n.name] ? n.slots_total : 0); }, 0);
  var used = d.active_runs.length;
  var ready = d.accounts.filter(function (a) { return a.status === 'idle' || a.status === 'working' || a.status === 'probing'; });
  var cool = d.accounts.filter(function (a) { return a.status === 'cooldown'; }).length;
  var out = d.accounts.filter(function (a) { return a.status === 'signed_out' || a.status === 'error'; }).length;
  var crit = d.incidents.filter(function (i) { return i.severity === 'critical'; }).length;
  var svcDown = S.services.filter(function (s) { return !s.up; });
  var t = d.totals || {};
  var hi = d.accounts.map(maxUtil).filter(function (x) { return x != null; });
  var topQ = hi.length ? Math.max.apply(null, hi) : null;

  var h = '<section class="kpis" aria-label="Summary">';
  h += kpi('Nodes online', online.length + ' / ' + d.nodes.length, offline.length ? esc(offline.map(function (n) { return n.name; }).join(', ')) + ' down' : (d.nodes.length ? 'all heartbeating' : 'no node joined yet'), offline.length ? 'crit' : '', 'p-nodes');
  h += kpi('Slots in use', used + ' / ' + slots, (used ? used + ' AI run' + (used > 1 ? 's' : '') + ' now' : 'idle') + (idleSlots ? ' · ' + idleSlots + ' on nodes without AI' : ''), '', 'p-nodes');
  h += kpi('Accounts ready', ready.length + ' / ' + d.accounts.length, (cool ? cool + ' without quota' : 'all have quota') + (out ? ' · ' + out + ' failing' : ''), cool || out ? 'warn' : '', 'p-accounts');
  h += kpi('Highest quota use', topQ == null ? '—' : Math.round(topQ * 100) + '%', 'across reported windows', topQ != null && topQ >= 0.9 ? 'crit' : topQ != null && topQ >= 0.75 ? 'warn' : '', 'p-accounts');
  h += kpi('Runs · 24 h', String(t.runs_24h || 0), (t.done_24h || 0) + ' done · ' + (t.failed_24h || 0) + ' not done', t.failed_24h ? 'warn' : '', 'p-runs');
  h += kpi('Tokens · 24 h', tok((t.tokens_in_24h || 0) + (t.tokens_out_24h || 0)), 'cost ' + usd(t.cost_24h), '', 'p-runs');
  h += kpi('Open incidents', String(d.incidents.length), crit ? crit + ' critical' : d.incidents.length ? 'warnings only' : 'none open', crit ? 'crit' : d.incidents.length ? 'warn' : '', 'p-incidents');
  h += kpi('Services', S.services.length ? (S.services.length - svcDown.length) + ' / ' + S.services.length : '—', svcDown.length ? esc(svcDown.map(function (s) { return s.name; }).join(', ')) + ' down' : (S.svcError ? 'checks unreachable' : 'all checks passing'), svcDown.length || S.svcError ? 'crit' : '', 'p-central');
  h += '</section>';

  var alerts = d.incidents.filter(function (i) { return i.severity === 'critical' || i.severity === 'warning'; });
  if (alerts.length) {
    h += '<section class="alerts" aria-label="Alerts">';
    alerts.forEach(function (i) {
      h += '<div class="alert"><span class="pill" data-tone="' + (i.severity === 'critical' ? 'crit' : 'warn') + '">' + esc(KIND[i.kind] || i.kind) + '</span><span class="a-title">' + esc(i.title) + '</span><span class="a-detail">' + esc(i.detail) + '</span><span class="spacer"></span><span class="a-detail mono">' + esc(when(i.opened_at)) + '</span><button class="btn sm" data-act="open-inc" data-id="' + esc(i.id) + '">Details</button></div>';
    });
    h += '</section>';
  }

  h += toolWaitsPanel(d);
  h += masterAutonomyPanel();
  h += escalationsPanel();

  h += '<div class="grid-2">';
  h += '<section class="panel" id="p-nodes" aria-labelledby="h-nodes"><div class="panel-h"><h2 id="h-nodes">Nodes</h2><span class="hint">Each account on a node is one worker pulling ready tickets</span><span class="spacer"></span>' + (consoleCan('nodes.manage') ? '<button class="btn sm" data-act="add-node">Add node</button>' : '') + '</div><div class="nodes">';
  if (!d.nodes.length) h += '<div class="empty">No node has joined. Create a one-use join command below and run it on the new machine.</div>';
  d.nodes.forEach(function (n) {
    var accs = d.accounts.filter(function (a) { return accountAssignedTo(a, n.name); });
    var runs = d.active_runs.filter(function (r) { return r.node === n.name; }).length;
    var sl = ''; for (var i = 0; i < n.slots_total; i++) sl += '<span class="slot"' + (i < runs ? ' data-used' : '') + '></span>';
    var off = n.status === 'offline';
    h += '<button class="node" data-act="open-node" data-id="' + esc(n.name) + '"' + (off ? ' data-off' : '') + ' aria-label="Open node ' + esc(n.name) + '">';
    h += '<span class="node-top"><span class="n-name">' + esc(n.name) + '</span><span class="mono muted" style="font-size:12px">' + esc(n.ip) + (n.name === 'cc' ? ' · control plane' : '') + '</span><span class="spacer"></span>' + (!off && !accs.length ? pill('warn', 'No AI') : '') + pill(nodeTone(n), nodeLabel(n), n.status !== 'online') + '</span>';
    if (off) h += '<span class="offnote">No heartbeat for ' + mmss((n.heartbeat_age_s || 0) + since()) + '. Its runs go back to To Do when their lease ends.</span>';
    else if (!accs.length) h += '<span class="offnote" style="color:var(--warn)">No AI account on this node: it adds ' + n.slots_total + ' slots but cannot take work. Add an account on Machines and AI.</span>';
    var mm = n.metrics || {};
    h += '<span class="small mono">' + esc(mm.hostname || '—') + (n.purpose ? ' · ' + esc(n.purpose) : '') + '</span>';
    h += nodeMetrics(n);
    h += '<span class="slots">Slots ' + sl + '<span>' + runs + ' / ' + n.slots_total + '</span><span class="spacer"></span><span class="mono">' + relLabel(n) + '</span> · <span class="mono" data-since="' + (n.heartbeat_age_s == null ? '' : n.heartbeat_age_s) + '" data-fmt="ago"></span></span>';
    h += '<span class="node-accs">';
    if (!accs.length) h += '<span class="node-acc"><span class="what">No accounts assigned. Manage accounts on Machines and AI.</span></span>';
    accs.forEach(function (a) { h += '<span class="node-acc"><span class="sdot" data-tone="' + accTone(a) + '"></span><span class="who">' + esc(a.id) + '</span><span class="what">' + esc(doing(a)) + '</span></span>'; });
    h += '</span></button>';
  });
  h += '</div></section>';

  h += '<section class="panel" id="p-feed" aria-labelledby="h-feed"><div class="panel-h"><h2 id="h-feed">Live events</h2>' + pill('info', 'from the registry', true) + '</div><ul class="feed">';
  if (!d.events.length) h += '<li><span class="f-text muted" style="grid-column:1/-1">No events yet.</span></li>';
  d.events.forEach(function (e) {
    var tn = /offline|lost|error|failed/.test(e.type + e.outcome) ? 'crit' : /incident\.opened|quota|cooldown/.test(e.type + e.outcome) ? 'warn' : /finished|online|closed/.test(e.type) && (!e.outcome || e.outcome === 'done' || e.outcome === 'ok') ? 'ok' : 'info';
    var subj = [e.ticket, e.account, e.node].filter(Boolean).join(' · ');
    h += '<li><span class="f-time">' + hhmm(e.ts) + '</span><span class="f-type" data-tone="' + tn + '">' + esc(e.type) + '</span><span class="f-text">' + (subj ? '<b>' + esc(subj) + '</b> ' : '') + esc([e.outcome, e.reason].filter(Boolean).join(' · ')) + '</span></li>';
  });
  h += '</ul></section></div>';
  h += '<div id="node-onboarding"></div>';

  h += '<section class="panel" id="p-accounts" aria-labelledby="h-acc"><div class="panel-h"><h2 id="h-acc">AI accounts</h2><span class="hint">Quota belongs to an account. Each worker checks its account every 30 minutes and after any limit.</span><span class="spacer"></span><div class="seg" role="group" aria-label="Filter accounts">';
  [['all', 'All'], ['working', 'Working'], ['idle', 'Ready'], ['cooldown', 'No quota'], ['bad', 'Failing']].forEach(function (f) {
    var c = d.accounts.filter(function (a) { return accMatch(a, f[0]); }).length;
    h += '<button data-act="accfilter" data-f="' + f[0] + '" aria-pressed="' + (S.accFilter === f[0]) + '">' + f[1] + ' ' + c + '</button>';
  });
  h += '</div></div><div class="tablebox"><table><thead><tr><th scope="col">Account</th><th scope="col">Provider · tier</th><th scope="col">Node</th><th scope="col">Status</th><th scope="col">Quota used</th><th scope="col">Model</th><th scope="col">Last check</th><th scope="col">Now</th></tr></thead><tbody>';
  d.accounts.filter(function (a) { return accMatch(a, S.accFilter); }).forEach(function (a) {
    var pr = (a.worker && a.worker.probe) || null;
    h += '<tr tabindex="0" data-act="open-acc" data-id="' + esc(a.id) + '"><td class="mono">' + esc(a.id) + '</td><td>' + esc(PROVIDER[a.provider] || a.provider) + ' <span class="muted">· ' + esc(a.tier) + (a.explicit_only ? ' · on request' : '') + '</span></td><td class="mono">' + (a.shared ? esc(a.machines.length + ' machines') + ' <span class="muted">· ' + a.runs_active + '/' + a.session_limit + ' sessions</span>' : esc(a.node || '—')) + '</td>';
    h += '<td>' + pill(accTone(a), accLabel(a), a.status === 'working') + '</td><td>' + qwin(a) + '</td><td class="mono">' + esc(a.model || '—') + (a.effort ? ' · ' + esc(a.effort) : '') + '</td>';
    h += '<td class="mono">' + (pr && pr.at ? '<span title="' + (pr.ok ? 'passed' : 'failed') + '">' + (pr.ok ? '✓ ' : '✗ ') + when(new Date(pr.at * 1000).toISOString()) + '</span>' : '—') + '</td>';
    h += '<td><span class="muted">' + esc(doing(a)) + '</span></td></tr>';
  });
  h += '</tbody></table></div>';
  if (consoleCan('nodes.manage')) {
    h += '<div id="sa-create" class="flexrow" style="padding:8px 12px"><label for="sa-new-id" class="small">New shared account</label> <input id="sa-new-id" placeholder="claude-02" maxlength="40" style="width:9em"> <select id="sa-new-provider" aria-label="Provider">' +
      Object.keys(PROVIDER).map(function (k) { return '<option value="' + k + '">' + PROVIDER[k] + '</option>'; }).join('') +
      '</select> <label for="sa-new-limit" class="small">Total sessions</label> <input id="sa-new-limit" type="number" min="1" max="50" value="1" style="width:5em"> <button class="btn sm" data-act="acc-create">Create</button> <span class="hint">Then open the account to enable it on machines.</span></div>';
  }
  h += '</section>';

  h += roleHealthPanel(d.role_health || { roles: [], settings: {} });

  h += progressPanel(d.release_progress || [], 'en', 'p-ready', true) + scaleProofPanel(d.scale_proof, (S.biz || {}).jira_site);

  h += '<section class="panel" id="p-runs" aria-labelledby="h-runs"><div class="panel-h"><h2 id="h-runs">Runs</h2><span class="hint">Running now and the last 30 finished</span></div>' + runsTable(d.active_runs.map(function (r) { return Object.assign({ outcome: 'running', duration_s: r.elapsed_s, live: true }, r); }).concat(d.recent_runs), 12) + '</section>';

  h += '<div class="grid-2">';
  var cc = nodeBy('cc');
  h += '<section class="panel" id="p-central" aria-labelledby="h-central"><div class="panel-h"><h2 id="h-central">Control plane</h2><span class="mono muted" style="font-size:12px">cc · 10.44.0.10</span></div>';
  h += cc ? nodeMetrics(cc) : '<div class="empty">The control-plane machine has not joined as a node, so its resources are not reported here.</div>';
  h += '<div><div class="panel-h" style="padding:6px 0 0"><h2 style="font-size:14px">Services</h2><span class="hint">Active checks by Gatus every 30 s</span></div>';
  if (S.svcError) h += '<div class="empty" style="color:var(--crit)">Health checks unreachable: ' + esc(S.svcError) + '</div>';
  var groups = {};
  S.services.forEach(function (s) { (groups[s.group] = groups[s.group] || []).push(s); });
  Object.keys(groups).sort().forEach(function (g) {
    h += '<div class="svcgroup">' + esc(g) + '</div>';
    groups[g].forEach(function (s) {
      h += '<div class="svc"><span class="sdot" data-tone="' + (s.up ? 'ok' : 'crit') + '"></span><span class="mono">' + esc(s.name) + (s.up ? '' : ' <span style="color:var(--crit)">· down</span>') + '</span><span class="s-group">' + esc((s.errors || []).join('; ')) + '</span><span class="s-mem">' + s.ms + ' ms</span></div>';
    });
  });
  h += '</div></section>';

  h += '<div class="stack"><section class="panel" id="p-incidents" aria-labelledby="h-inc"><div class="panel-h"><h2 id="h-inc">Incidents</h2><span class="hint">Opened and closed by the watchdog; critical ones are emailed</span></div>';
  if (!d.incidents.length) h += '<div class="empty">Nothing open.</div>';
  d.incidents.forEach(function (i) { h += incCard(i, false); });
  (d.incidents_recent || []).slice(0, 6).forEach(function (i) { h += incCard(i, true); });
  h += '</section>';

  h += '<section class="panel" aria-labelledby="h-ver"><div class="panel-h"><h2 id="h-ver">Versions</h2><span class="hint">Pinned in aiflow-control/versions.lock, reported by each node</span></div>' + versionsBox() + '</section></div></div>';

  var oldJoin = $('#node-onboarding'), oldCreate = $('#sa-create');
  var focusId = oldJoin && oldJoin.contains(document.activeElement) ? document.activeElement.id : '';
  var focusStart = focusId && document.activeElement.selectionStart;
  var focusEnd = focusId && document.activeElement.selectionEnd;
  var oldRh = $('#rh-settings');
  var rhFocus = oldRh && oldRh.contains(document.activeElement) ? document.activeElement.id : '';
  $('#view-machines').innerHTML = h;
  if (oldRh && $('#rh-settings')) $('#rh-settings').replaceWith(oldRh);
  if (rhFocus && $('#' + rhFocus)) $('#' + rhFocus).focus();
  renderRoleHealthSettings();
  if (oldJoin) $('#node-onboarding').replaceWith(oldJoin);
  if (oldCreate && $('#sa-create')) $('#sa-create').replaceWith(oldCreate);
  if (focusId) {
    var focusTarget = $('#' + focusId);
    if (focusTarget) { focusTarget.focus(); if (focusTarget.setSelectionRange && focusStart != null) focusTarget.setSelectionRange(focusStart, focusEnd); }
  }
  renderNodeJoin();
}

