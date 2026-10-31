/* ---------- Delivery ---------- */
function projTickets() { return D().tickets.filter(function (t) { return S.project === 'all' || t.project === S.project; }); }
function dashboardWorkStatusMatches(ticket, filter) {
  if (!filter || filter === 'all') return true;
  if (filter === 'done') return ticket.status === 'Done';
  if (filter === 'in-flight') return ['In Progress', 'In Verify', 'Human review', 'Ready to merge'].indexOf(ticket.status) >= 0;
  if (filter === 'waiting') return dashboardVisibleWaitingTickets(D().tickets, S.dashboardWaiting.keys).some(function (row) { return row.key === ticket.key; });
  return ticket.status === filter;
}
function projectCloseControl(project) {
  if (!project) return '';
  if (project.closed) return '<span class="pill" data-tone="ok"><span class="dot"></span>Closed · study case v' +
    Number(project.report_version || 0) + '</span>';
  return (project.close_suggested ? '<span class="hint">All project tickets are Done. Ready to close.</span>' : '') +
    '<button class="btn sm" data-act="close-project" data-p="' + esc(project.key) + '">Close project</button>';
}
function renderDHead() {
  var d = D();
  var active = document.activeElement, focusId = active && active.id, focusStart = active && active.selectionStart, focusEnd = active && active.selectionEnd;
  var h = '<h1 class="dash-title">' + (S.view === 'work' ? 'Work board' : 'Delivery') + '</h1><div class="dhead-tools">';
  h += renderProjectPicker();
  h += '<label class="search" for="q" style="flex-direction:row; font-weight:400">' + ICON.search + '<input id="q" type="search" placeholder="Search key or title" value="' + esc(S.search) + '" aria-label="Search tickets"></label>';
  var types = {}; projTickets().forEach(function (t) { if (t.type !== 'Epic') types[t.type] = 1; });
  h += '<div class="seg" role="group" aria-label="Type"><button data-act="type" data-t="all" aria-pressed="' + (S.typeFilter === 'all') + '">All types</button>';
  Object.keys(types).sort().forEach(function (t) { h += '<button data-act="type" data-t="' + esc(t) + '" aria-pressed="' + (S.typeFilter === t) + '">' + esc(t) + '</button>'; });
  h += '</div>';
  h += '<label class="ai-filter" for="ai-filter">AI <select id="ai-filter" aria-label="Filter by AI state"><option value="all"' + (S.aiFilter === 'all' ? ' selected' : '') + '>All AI states</option><option value="ready"' + (S.aiFilter === 'ready' ? ' selected' : '') + '>Ready for AI</option><option value="working"' + (S.aiFilter === 'working' ? ' selected' : '') + '>AI working</option><option value="attention"' + (S.aiFilter === 'attention' ? ' selected' : '') + '>Needs attention</option></select></label>';
  h += '<label class="ai-filter work-status-filter" for="work-status-filter">Tickets <select id="work-status-filter" aria-label="Filter tickets by status">';
  [['all', 'All statuses'], ['done', 'Done'], ['in-flight', 'In flight'], ['waiting', 'Waiting for you']].concat(STATUS_COLS.map(function (c) { return [c[0], c[1]]; })).forEach(function (option) {
    h += '<option value="' + esc(option[0]) + '"' + (S.workStatusFilter === option[0] ? ' selected' : '') + '>' + esc(option[1]) + '</option>';
  });
  h += '</select></label>';
  var epics = sortEpics(projTickets().filter(function (t) { return t.type === 'Epic'; }));
  if (epics.length) {
    h += '<label style="flex-direction:row;align-items:center;gap:8px;font-weight:400">Epic <select id="epicSel" style="width:auto;min-width:200px;max-width:340px"><option value="all">All epics</option>';
    epics.forEach(function (e) { h += '<option value="' + esc(e.key) + '"' + (S.epic === e.key ? ' selected' : '') + '>' + esc(e.key + ' ' + e.summary.slice(0, 50)) + '</option>'; });
    h += '</select></label>';
  }
  h += '<span class="spacer"></span>';
  var p = d.projects.filter(function (x) { return x.key === S.project; })[0];
  if (p) h += '<button class="btn sm" data-act="open-report">Report</button>' + projectCloseControl(p) + '<a class="btn sm" href="https://github.com/' + esc(p.repo) + '/pulls" target="_blank" rel="noopener">Pull requests ' + ICON.ext + '</a><a class="btn sm" href="https://jooservices.atlassian.net/jira/software/c/projects/' + esc(p.key) + '/issues" target="_blank" rel="noopener">Jira ' + ICON.ext + '</a>';
  h += '</div>';
  var head = S.view === 'work' ? $('#work-head') : $('#dhead'); if (head) head.innerHTML = h;
  if (focusId) { var focus = $('#' + focusId); if (focus) { focus.focus(); if (focus.setSelectionRange && focusStart != null) focus.setSelectionRange(focusStart, focusEnd); } }
  if (S.projectPickerOpen) renderProjectPickerOptions();
}
function renderProjectPicker() {
  var d = D(), selected = d.projects.filter(function (p) { return p.key === S.project; })[0];
  var value = S.projectPickerOpen ? S.projectQuery : (S.project === 'all' ? 'All projects' : selected ? selected.key + ' · ' + selected.repo : '');
  return '<div class="project-picker" id="project-picker"><label for="work-project">Choose a project</label>' +
    '<input id="work-project" type="search" role="combobox" aria-autocomplete="list" aria-controls="project-options" aria-expanded="' + S.projectPickerOpen + '" autocomplete="off" placeholder="Search projects by name or key" value="' + esc(value) + '">' +
    (S.project === 'all' ? '' : '<button type="button" class="star-button" data-act="project-star" aria-pressed="' + (S.project && S.starredProjects.indexOf(S.project) >= 0) + '" aria-label="' + (S.starredProjects.indexOf(S.project) >= 0 ? 'Unstar ' : 'Star ') + esc(S.project || 'selected project') + '">' + (S.starredProjects.indexOf(S.project) >= 0 ? '★' : '☆') + '</button>') +
    '<div class="project-options" id="project-options" role="listbox" aria-label="Projects"' + (S.projectPickerOpen ? '' : ' hidden') + '></div></div>';
}
function renderProjectPickerOptions() {
  var box = $('#project-options'), input = $('#work-project');
  if (!box || !input) return;
  var query = String(input.value || '').trim().toLowerCase();
  var matches = D().projects.filter(function (p) { return (p.key + ' ' + p.repo).toLowerCase().indexOf(query) >= 0; });
  matches.sort(function (a, b) {
    var as = S.starredProjects.indexOf(a.key) >= 0, bs = S.starredProjects.indexOf(b.key) >= 0;
    return as !== bs ? (as ? -1 : 1) : a.key.localeCompare(b.key);
  });
  var stars = matches.filter(function (p) { return S.starredProjects.indexOf(p.key) >= 0; }).slice(0, 20);
  var rest = matches.filter(function (p) { return S.starredProjects.indexOf(p.key) < 0; }).slice(0, Math.max(0, 25 - stars.length));
  var h = '';
  if (!query || 'all projects'.indexOf(query) >= 0 || 'all'.indexOf(query) >= 0) {
    h += '<div class="option-group">All projects</div><button type="button" class="project-option" role="option" aria-selected="' + (S.project === 'all') + '" data-act="select-work-project" data-p="all"><span class="key">All projects</span><span class="repo">Tickets across every project visible to you</span></button>';
  }
  if (stars.length) {
    h += '<div class="option-group">Starred projects</div>';
    stars.forEach(function (p) { h += projectOption(p, true); });
  }
  if (rest.length) {
    h += '<div class="option-group">Projects' + (matches.length > stars.length + rest.length ? ' · ' + (matches.length - stars.length - rest.length) + ' more matches; refine your search' : '') + '</div>';
    rest.forEach(function (p) { h += projectOption(p, false); });
  }
  if (!matches.length) h = '<div class="empty">No matching projects.</div>';
  box.innerHTML = h;
  box.hidden = !S.projectPickerOpen;
  input.setAttribute('aria-expanded', String(S.projectPickerOpen));
}
function projectOption(project, starred) {
  return '<button type="button" class="project-option" role="option" aria-selected="' + (project.key === S.project) + '" data-act="select-work-project" data-p="' + esc(project.key) + '"><span class="key">' + esc(project.key) + '</span><span class="repo">' + esc(project.repo) + '</span>' + (starred ? '<span class="starred">Starred</span>' : '') + '</button>';
}
function epicOf(t, byKey) {
  var cur = t, guard = 0;
  while (cur && cur.parent_key && guard++ < 4) { var p = byKey[cur.parent_key]; if (!p) return cur.parent_key; if (p.type === 'Epic') return p.key; cur = p; }
  return null;
}
/* Progress under each epic of a project's tickets; Delivery and Business show the same numbers from this one place. */
function epicProgress(all) {
  var byKey = {}; all.forEach(function (t) { byKey[t.key] = t; });
  var work = all.filter(function (t) { return t.type !== 'Epic'; });
  return sortEpics(all.filter(function (t) { return t.type === 'Epic'; })).map(function (e) {
    var kids = work.filter(function (t) { return epicOf(t, byKey) === e.key; });
    var c = { done: 0, verify: 0, progress: 0, wait: 0, todo: 0 };
    kids.forEach(function (t) { var s = t.status; c[s === 'Done' ? 'done' : (s === 'In Verify' || s === 'Human review') ? 'verify' : s === 'In Progress' ? 'progress' : s === 'Needs owner' ? 'wait' : 'todo']++; });
    return { epic: e, items: kids.length, counts: c, pct: kids.length ? Math.round(100 * c.done / kids.length) : 0 };
  });
}
function epicsPanel(all, act, id) {
  var rows = epicProgress(all);
  if (!rows.length) return '';
  var h = '<section class="panel"' + (id === 'd-epics' ? ' data-block="epics"' : '') + ' id="' + id + '" aria-labelledby="h-' + id + '"><div class="panel-h"><h2 id="h-' + id + '">Epics</h2><span class="hint">Progress of the work under each epic</span><span class="spacer"></span><div class="legend"><span><i class="st-done"></i>Done</span><span><i class="st-verify"></i>In verify or review</span><span><i class="st-progress"></i>In progress</span><span><i class="st-wait"></i>Needs owner</span><span><i class="st-todo"></i>Not started</span></div></div><div class="epics">';
  rows.forEach(function (r) {
    var e = r.epic, c = r.counts;
    var n = r.items || 1, seg = function (k) { return c[k] ? '<span class="st-' + k + '" style="width:' + (100 * c[k] / n).toFixed(2) + '%" title="' + c[k] + '"></span>' : ''; };
    h += '<button class="epic" data-act="' + act + '" data-key="' + esc(e.key) + '"><span><div class="e-t"><span class="mono muted">' + esc(e.key) + '</span> ' + esc(e.summary) + '</div><div class="e-s">' + r.items + ' items · ' + c.done + ' done · ' + esc(e.status) + '</div></span><span class="ebar" role="img" aria-label="' + c.done + ' of ' + r.items + ' done">' + seg('done') + seg('verify') + seg('progress') + seg('wait') + seg('todo') + '</span><span class="mono" style="text-align:right">' + r.pct + '%</span></button>';
  });
  return h + '</div></section>';
}
function renderDBody() {
  if (!window.AIFlowConsole) { window.AIFlowConsole = { state: S, data: D, esc: esc, when: when, projectResponseError: projectResponseError, renderDBody: renderDBody, activeBoard: function () { return S.view === 'work' ? $('#work-body') : $('#dbody'); } }; if (typeof window.dispatchEvent === 'function') window.dispatchEvent(new Event('aiflow-console-ready')); } var all = projTickets();
  var byKey = {}; all.forEach(function (t) { byKey[t.key] = t; });
  var work = all.filter(function (t) { return t.type !== 'Epic'; });
  var q = S.search.trim().toLowerCase();
  var list = work.filter(function (t) {
    var aiMatches = S.aiFilter === 'all' || (S.aiFilter === 'ready' && t.status === 'To Do' && (t.labels || []).indexOf('aiflow-ready') >= 0) ||
      (S.aiFilter === 'working' && !!t.run) || (S.aiFilter === 'attention' &&
        (t.status === 'Needs owner' || t.status === 'Human review' || (!t.run && t.last_run && ['failed', 'lost', 'timeout', 'quota'].indexOf(t.last_run.outcome) >= 0)));
    return aiMatches && dashboardWorkStatusMatches(t, S.workStatusFilter) && (S.typeFilter === 'all' || t.type === S.typeFilter) && (S.epic === 'all' || epicOf(t, byKey) === S.epic) &&
      (!q || t.key.toLowerCase().indexOf(q) >= 0 || t.summary.toLowerCase().indexOf(q) >= 0);
  });
  var cnt = function (s) { return work.filter(function (t) { return t.status === s; }).length; };
  var done = cnt('Done'), people = cnt('Human review') + cnt('Needs owner');
  var runs = D().recent_runs.filter(function (r) { return byKey[r.ticket]; });
  var devDone = runs.filter(function (r) { return r.step === 'dev' && r.outcome === 'done' && r.duration_s; }).map(function (r) { return r.duration_s; }).sort(function (a, b) { return a - b; });
  var med = devDone.length ? devDone[Math.floor(devDone.length / 2)] : null;
  var p = D().projects.filter(function (x) { return x.key === S.project; })[0] || {};
  var wm = p.work_minutes || { target: 15, warn: 30, hard: 45 };

  var boardOnly = S.view === 'work', h = '';
  if (!boardOnly) {
  h = '<section class="kpis" data-block="kpis" aria-label="Project summary">';
  h += kpi('Tickets', String(work.length), all.length - work.length + ' epics', '', 'd-board');
  h += kpi('Done', done + (work.length ? ' · ' + Math.round(100 * done / work.length) + '%' : ''), 'merged or closed', '', 'd-epics');
  h += kpi('Ready for AI', String(work.filter(function (t) { return t.status === 'To Do' && t.labels.indexOf('aiflow-ready') >= 0; }).length), 'To Do with aiflow-ready', '', 'd-board');
  h += kpi('AI working', String(cnt('In Progress')), D().active_runs.filter(function (r) { return byKey[r.ticket]; }).length + ' runs live', '', 'd-board');
  h += kpi('In verify', String(cnt('In Verify')), 'draft PR open, checks next', '', 'd-board');
  h += kpi('Waiting on people', String(people), people ? 'human review or owner answer' : 'nothing waiting', people ? 'warn' : '', 'd-att');
  h += kpi('Median dev run', med == null ? '—' : mmss(med), 'target ' + wm.target + ' min · hard ' + wm.hard, med != null && med > wm.warn * 60 ? 'warn' : '', 'd-runs');
  h += '</section>';

  h += epicsPanel(all, 'epic', 'd-epics');
  }

  h += '<section class="panel" data-block="board"' + (boardOnly ? '' : ' id="d-board"') + ' aria-labelledby="h-board"><div class="panel-h"><h2 id="h-board">Board</h2><span class="hint">Mirrors Jira every 20 s; cards move as workers claim, finish and verify</span></div><div class="board">';
  STATUS_COLS.forEach(function (c) {
    var items = list.filter(function (t) { return inColumn(t, c[0]); });
    if (c[0] === 'Done') items.sort(function (a, b) { return (a.updated_age_s || 0) - (b.updated_age_s || 0); });
    h += '<div class="col"><div class="col-h"><span>' + c[1] + '</span><span class="count">' + items.length + '</span></div>';
    if (!items.length) h += '<div class="col-empty">Nothing here</div>';
    items.slice(0, 40).forEach(function (t) { h += card(t, p); });
    if (items.length > 40) h += '<div class="more">' + (items.length - 40) + ' more · use search</div>';
    h += '</div>';
  });
  h += '</div></section>';

  if (!boardOnly) {
  h += '<section class="panel" data-block="attention" id="d-att" aria-labelledby="h-att"><div class="panel-h"><h2 id="h-att">Needs attention</h2><span class="hint">Waiting on a person, over 30 minutes, or the last run did not finish</span></div><div class="att">';
  var att = [];
  work.forEach(function (t) {
    var lr = t.last_run;
    if (t.status === 'Needs owner') att.push([t, 'crit', 'Needs owner', 'The master role triages the developer question; owner-escalated tickets need an owner decision']);
    else if (t.status === 'Human review') att.push([t, 'info', 'Human review', 'Require human = Yes; checks passed']);
    else if (t.run && (t.run.elapsed_s || 0) >= wm.target * 60) att.push([t, (t.run.elapsed_s >= wm.warn * 60 ? 'crit' : 'warn'), 'Over ' + wm.target + ' min', t.run.account + ' · ' + mmss(t.run.elapsed_s)]);
    else if (!t.run && lr && t.status === 'To Do' && ['failed', 'lost', 'timeout', 'quota'].indexOf(lr.outcome) >= 0) att.push([t, 'warn', 'Retry', lr.account + ': ' + lr.outcome + (lr.error_kind ? ' (' + lr.error_kind + ')' : '')]);
  });
  if (!att.length) h += '<div class="col-empty">Nothing needs attention</div>';
  att.slice(0, 20).forEach(function (x) {
    h += '<button class="att-item" data-act="open-ticket" data-key="' + esc(x[0].key) + '"><span class="flag" data-tone="' + x[1] + '">' + esc(x[2]) + '</span><span style="min-width:0"><div class="t"><span class="mono muted">' + esc(x[0].key) + '</span> ' + esc(x[0].summary) + '</div><div class="d">' + esc(x[3]) + '</div></span><span class="muted">Open</span></button>';
  });
  h += '</div></section>';
  h += '<section class="panel" data-block="runs" id="d-runs" aria-labelledby="h-druns"><div class="panel-h"><h2 id="h-druns">Recent runs</h2></div>' + runsTable(D().active_runs.filter(function (r) { return byKey[r.ticket]; }).map(function (r) { return Object.assign({ outcome: 'running', live: true }, r); }).concat(runs), 10) + '</section>';
  h += analyticsPanel();
  }
  var target = S.view === 'work' ? $('#work-body') : $('#dbody'); if (window.AIFlowConsole && window.AIFlowConsole.runInboxPanel) h += window.AIFlowConsole.runInboxPanel();
  if (target && window.AIFlowConsole && window.AIFlowConsole.renderBoardContent) window.AIFlowConsole.renderBoardContent(target, h); else if (target) target.innerHTML = h;
  if (target && S.view === 'delivery') applyLayout('delivery', target);
}

