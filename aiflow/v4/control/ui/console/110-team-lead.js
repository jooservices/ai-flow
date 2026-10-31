/* ---------- Team Lead epic coverage and portable reports ---------- */
function loadLeadReport(project, epic) {
  var current = S.leadReport;
  if (current && current.project === project && current.epic === epic &&
      (current.loading || Date.now() - current.loadedAt < 30000)) return;
  var entry = { project: project, epic: epic, loading: true, loadedAt: 0, data: null, error: '' };
  S.leadReport = entry;
  fetch('/v1/projects/' + encodeURIComponent(project) + '/report?epic=' + encodeURIComponent(epic), { cache: 'no-store' })
    .then(function (r) { if (!r.ok) throw new Error('Epic report could not be loaded (' + r.status + ')'); return r.json(); })
    .then(function (data) {
      if (S.leadReport !== entry) return;
      entry.data = data; entry.loading = false; entry.loadedAt = Date.now();
      if (S.view === 'lead') renderLead();
    }).catch(function (e) {
      if (S.leadReport !== entry) return;
      entry.error = String(e.message || e); entry.loading = false; entry.loadedAt = Date.now();
      if (S.view === 'lead') renderLead();
    });
}
function leadReportHref(format) {
  return '/v1/projects/' + encodeURIComponent(S.project) + '/report?epic=' + encodeURIComponent(S.leadEpic) + '&format=' + format;
}
function leadDuration(stats) {
  if (!stats || !stats.sample_size) return 'n/a';
  return 'median ' + mmss(stats.median_seconds) + ' · p90 ' + mmss(stats.p90_seconds) + ' · n=' + stats.sample_size;
}
function renderLead() {
  var root = $('#view-lead'), projects = D().projects, project = projects.filter(function (p) { return p.key === S.project; })[0];
  var epics = sortEpics(projTickets().filter(function (t) { return t.type === 'Epic'; }));
  if (!project && projects.length) { S.project = projects[0].key; project = projects[0]; }
  if (!epics.some(function (e) { return e.key === S.leadEpic; })) S.leadEpic = epics.length ? epics[0].key : null;
  var h = '<section class="panel"><div class="panel-h"><h1 style="margin:0;font:600 20px var(--f-cond)">Team Lead · Requirement coverage and progress</h1><span class="spacer"></span></div>';
  h += '<div class="lead-controls"><label>Project<select id="leadProject">' + projects.map(function (p) {
    return '<option value="' + esc(p.key) + '"' + (p.key === S.project ? ' selected' : '') + '>' + esc(p.key) + '</option>';
  }).join('') + '</select></label><label>Epic<select id="leadEpic"' + (epics.length ? '' : ' disabled') + '>' +
    epics.map(function (e) { return '<option value="' + esc(e.key) + '"' + (e.key === S.leadEpic ? ' selected' : '') + '>' + esc(e.key + ' · ' + e.summary) + '</option>'; }).join('') +
    '</select></label><div class="lead-tools">' + (S.leadEpic ?
      '<a class="btn sm" download href="' + esc(leadReportHref('md')) + '">Markdown</a>' +
      '<a class="btn sm" download href="' + esc(leadReportHref('csv')) + '">CSV</a>' +
      '<a class="btn sm" target="_blank" rel="noopener" href="' + esc(leadReportHref('html')) + '">Printable HTML</a>' : '') +
      '</div></div></section>';
  if (!epics.length) { root.innerHTML = h + '<section class="panel"><div class="empty">This project has no epics.</div></section>'; return; }
  var report = S.leadReport;
  if (!report || report.project !== S.project || report.epic !== S.leadEpic) {
    root.innerHTML = h + '<section class="panel"><div class="empty">Loading epic coverage…</div></section>';
    loadLeadReport(S.project, S.leadEpic); return;
  }
  if (report.loading) { root.innerHTML = h + '<section class="panel"><div class="empty">Loading epic coverage…</div></section>'; return; }
  if (report.error) { root.innerHTML = h + '<section class="panel"><div class="empty">' + esc(report.error) + '</div></section>'; return; }
  if (Date.now() - report.loadedAt >= 30000) loadLeadReport(S.project, S.leadEpic);
  var data = report.data, t = data.tickets, usage = data.usage, coverage = data.requirement_coverage;
  h += '<section class="kpis" aria-label="Epic progress">' +
    kpi('Done / total', t.done + ' / ' + t.total, 'tickets completed', '', 'lead-coverage') +
    kpi('In progress', String(t.in_progress), 'In Progress, In Verify, Human review', '', 'lead-coverage') +
    kpi('Needs owner', String(t.needs_owner), 'currently waiting for an owner', t.needs_owner ? 'warn' : '', 'lead-coverage') +
    kpi('Rework rounds', String(data.rework_rounds.count), 'verify requested changes', '', 'lead-coverage') +
    kpi('Lead time', leadDuration(data.lead_time), 'completed tickets', '', 'lead-coverage') +
    kpi('Tokens', tok(usage.tokens_total), usage.tokens_total + ' recorded', '', 'lead-coverage') +
    kpi('Estimated cost', '$' + Number(usage.cost_usd).toFixed(6), 'USD from recorded runs', '', 'lead-coverage') + '</section>';
  h += '<section class="panel" id="lead-coverage"><div class="panel-h"><h2>Requirement coverage</h2><span class="pill" data-tone="' + (coverage.insufficient_tasks ? 'crit' : 'ok') + '">' +
    (coverage.insufficient_tasks ? 'Insufficient tasks' : 'No coverage gaps') + '</span><span class="hint">AC status reflects the latest completed verify verdict; PASS verifies each listed AC.</span></div>';
  if (coverage.gaps.length) h += '<div class="lead-gap">' + coverage.gaps.map(function (g) {
    return '<span class="pill" data-tone="crit">' + esc(g.ticket + ': ' + g.reason.replace(/_/g, ' ')) + '</span>';
  }).join(' ') + '</div>';
  if (!coverage.tickets.length) h += '<div class="empty">No child tickets under this epic.</div>';
  else {
    h += '<div class="lead-tablebox"><table class="lead-table"><thead><tr><th>Ticket / summary</th><th>Parent</th><th>Status</th><th>Latest verify</th><th>Acceptance criteria</th><th>Rework</th></tr></thead><tbody>';
    coverage.tickets.forEach(function (ticket) {
      var verify = ticket.verify.verdict || ticket.verify.outcome || 'No verify result';
      var acHtml = ticket.acceptance_criteria.length ? '<ul>' + ticket.acceptance_criteria.map(function (ac) {
        var label = ac.status === 'verified' ? 'Verified' : ac.status === 'not_verified' ? 'No verify result' : 'Not confirmed';
        return '<li><span class="pill" data-tone="' + (ac.status === 'verified' ? 'ok' : 'warn') + '">' + label + '</span> <b>' + esc(ac.id) + ':</b> ' + esc(ac.text) + '</li>';
      }).join('') + '</ul>' : '<span class="pill" data-tone="crit">No AC lines</span>';
      h += '<tr><td><span class="mono">' + esc(ticket.key) + '</span><br>' + esc(ticket.summary) + '</td>' +
        '<td class="mono">' + esc(ticket.parent_key || '—') + '</td><td>' + esc(ticket.status) + '</td>' +
        '<td>' + esc(verify) + (ticket.verify.ended_at ? '<br><span class="small">' + esc(when(ticket.verify.ended_at)) + '</span>' : '') + '</td>' +
        '<td class="lead-ac">' + acHtml + '</td><td>' + ticket.rework_rounds + '</td></tr>';
    });
    h += '</tbody></table></div>';
  }
  h += '</section>';
  root.innerHTML = h;
}

