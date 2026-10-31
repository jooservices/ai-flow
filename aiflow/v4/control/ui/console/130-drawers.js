/* ---------- drawers ---------- */
function drawerShell(eyebrow, title, statusHtml, body) {
  return '<div class="drawer-h"><div class="d-title"><div class="mono muted" style="font-size:12px">' + eyebrow + '</div><h3 id="drawer-title">' + title + '</h3>' + (statusHtml ? '<div style="margin-top:6px;display:flex;gap:6px;flex-wrap:wrap">' + statusHtml + '</div>' : '') + '</div><button class="iconbtn" data-act="close" aria-label="Close details">' + ICON.close + '</button></div><div class="drawer-b">' + body + '</div>';
}
function fact(l, v) { return '<div class="fact"><span class="fl">' + l + '</span><span class="fv">' + v + '</span></div>'; }
function acLines(text) {
  return String(text || '').split('\n').map(function (l) { return l.trim(); }).filter(function (l) { return /^(-\s*)?AC\s?\d+[:.)]/i.test(l); }).map(function (l) { return l.replace(/^-\s*/, ''); });
}
var TL_KIND = { queued: 'Queued', dev: 'Dev', rework: 'Rework', verify: 'Verify', waiting: 'Waiting on a person', ai: 'Other AI' };
function timeSec(tl) {
  var tt = tl.totals || {}, segs = tl.segments || [];
  var outcomes = Object.keys(tt.runs_by_outcome || {}).map(function (o) { return o + ' ' + tt.runs_by_outcome[o]; }).join(' · ');
  var h = '<div class="dsec"><h4>Where the time went</h4><div class="facts">' + fact('Lead time', mmss(tt.lead_time_s) + (tt.done ? '' : ' so far')) + fact('AI time', mmss(tt.ai_s)) + fact('Queue time', mmss(tt.queue_s)) + fact('Waiting on a person', mmss(tt.waiting_s)) + fact('Rework rounds', String(tt.rework_rounds || 0)) + fact('Runs', (tt.runs || 0) + (outcomes ? ' · ' + esc(outcomes) : '')) + '</div>';
  var total = segs.reduce(function (s, x) { return s + x.seconds; }, 0);
  if (!total) return h + '<div class="empty">No time recorded yet.</div></div>';
  h += '<div class="bd-bar tl-bar" role="img" aria-label="Time segments">';
  segs.forEach(function (s) {
    var label = (TL_KIND[s.kind] || s.kind) + (s.round ? ' round ' + s.round : '') + ' · ' + mmss(s.seconds) + ' · ' + when(s.start) + '–' + when(s.end) + (s.status ? ' · ' + s.status : '') + (s.account ? ' · ' + s.account : '') + (s.outcome ? ' · ' + s.outcome : '');
    h += '<span class="tl-' + esc(s.kind) + '" style="width:' + (100 * s.seconds / total).toFixed(2) + '%" title="' + esc(label) + '"></span>';
  });
  h += '</div><div class="legend">' + Object.keys(TL_KIND).filter(function (k) { return (tt.seconds_by_kind || {})[k] != null; }).map(function (k) { return '<span><i class="tl-' + k + '"></i>' + TL_KIND[k] + '</span>'; }).join('') + '</div></div>';
  return h;
}
function mergeSec(det) {
  var m = det.merge || {}, moves = det.moves || [];
  var link = m.pr_url ? '<a href="' + esc(safeHref(m.pr_url)) + '" target="_blank" rel="noopener">PR #' + esc(String(m.pr_url).split('/').pop()) + ' ' + ICON.ext + '</a>' : '—';
  var h = '<div class="dsec" data-merge-facts><h4>Pull request and merge</h4><div class="facts">' + fact('Pull request', link) + fact('State on GitHub', esc(m.pr_state || '—')) +
    fact('Approved', m.approved_at ? when(m.approved_at) : '—') + fact('Merged', m.merged_at ? when(m.merged_at) + (m.merged_by ? ' · ' + esc(m.merged_by) : '') : '—') +
    fact('Merge commit', m.merge_sha ? '<span class="mono">' + esc(String(m.merge_sha).slice(0, 10)) + '</span>' : '—') +
    fact('Approval to merge', m.approval_to_merge_s != null ? mmss(m.approval_to_merge_s) : '—') + '</div>';
  if (m.note) h += '<div class="small">' + esc(m.note) + '</div>';
  if (moves.length) {
    h += '<h4>Moves and their cause</h4><ul class="checklist">';
    moves.forEach(function (v) { h += '<li><span class="mono small">' + when(v.ts) + '</span> ' + esc((v.from_status || '—') + ' → ' + v.to_status) + ' · <b>' + esc(v.cause) + '</b>' + (v.detail ? ' · ' + esc(v.detail) : '') + '</li>'; });
    h += '</ul>';
  }
  return h + '</div>';
}
function drawerTicket(key) {
  var t = ticketBy(key) || { key: key, summary: key, status: '?', type: '', labels: [], blocked_by: [] };
  var det = S.ticket[key];
  var full = det && det.ticket ? det.ticket : t;
  var pills = pill(stTone(t.status), t.status) + (t.labels || []).map(function (l) { return '<span class="pill">' + esc(l) + '</span>'; }).join('');
  var repository = full.repository || t.repository || (det && det.repository) || '—';
  var b = '<div class="facts">' + fact('Type', esc(t.type)) + fact('Repository', '<span class="mono">' + esc(repository) + '</span>') + fact('Priority', esc(t.priority || '—')) + fact('Parent', t.parent_key ? '<button class="linkbtn mono" data-act="open-ticket" data-key="' + esc(t.parent_key) + '">' + esc(t.parent_key) + '</button>' : '—') + fact('Blocked by', (t.blocked_by || []).length ? esc(t.blocked_by.join(', ')) : '—') + fact('Updated', t.updated_age_s != null ? ago(t.updated_age_s + since()) : '—');
  if (t.run) b += fact('Worker', '<span class="mono">' + esc(t.run.account) + '</span> · ' + esc(t.run.step)) + fact('Work clock', '<span class="mono" data-since="' + (t.run.elapsed_s || 0) + '" data-fmt="clock"></span> / 60:00') + fact('Lease', t.run.lease_ms > 0 ? Math.round(t.run.lease_ms / 1000) + ' s left' : 'expired') + fact('Fence', '#' + t.run.fence);
  b += '</div>';
  b += '<div style="display:flex;gap:8px;flex-wrap:wrap"><a class="btn sm" href="' + esc(safeHref(det && det.jira_url) || 'https://jooservices.atlassian.net/browse/' + key) + '" target="_blank" rel="noopener">Open in Jira ' + ICON.ext + '</a>';
  var prs = det ? det.runs.filter(function (r) { return r.pr_url; }).map(function (r) { return r.pr_url; }) : (t.last_run && t.last_run.pr_url ? [t.last_run.pr_url] : []);
  prs.filter(function (u, i) { return prs.indexOf(u) === i; }).forEach(function (u) { b += '<a class="btn sm" href="' + esc(safeHref(u)) + '" target="_blank" rel="noopener">PR #' + esc(u.split('/').pop()) + ' ' + ICON.ext + '</a>'; });
  b += '</div>';
  if (!det) return drawerShell(esc(key) + ' · ' + esc(t.project || ''), esc(t.summary), pills, b + '<div class="empty">Loading runs and history…</div>');
  var acs = acLines(full.description);
  if (acs.length) {
    var passed = t.status === 'Done';
    b += '<div class="dsec"><h4>Acceptance criteria</h4><ul class="checklist">' + acs.map(function (a) { return '<li><span class="' + (passed ? 'ico-ok' : 'ico-pend') + '">' + (passed ? ICON.ok : ICON.pend) + '</span><span>' + esc(a) + '</span></li>'; }).join('') + '</ul></div>';
  }
  if (det.merge) b += mergeSec(det);
  if (det.timeline) b += timeSec(det.timeline);
  if (det.capacity_decision) {
    var decision = det.capacity_decision, estimate = decision.estimate || {};
    var tokens = estimate.tokens || {}, minutes = estimate.time_minutes || {};
    b += '<div class="dsec"><h4>Why this AI was assigned</h4>';
    b += '<div class="small">' + esc(decision.account) + ' · ' + esc(PROVIDER[decision.provider] || decision.provider || '') +
      ' · ' + esc(when(decision.assigned_at)) + '</div><p>' + esc(decision.reason || 'No assignment explanation was saved.') + '</p>';
    if (tokens.low != null && tokens.high != null && minutes.low != null && minutes.high != null) {
      b += '<div class="small">Saved estimate: ' + tok(tokens.low) + '–' + tok(tokens.high) + ' tokens · ' +
        Number(minutes.low).toFixed(1) + '–' + Number(minutes.high).toFixed(1) + ' minutes</div>';
    }
    b += '</div>';
  }
  if (full.description) b += '<div class="dsec"><h4>Description</h4><div class="prose">' + esc(full.description) + '</div></div>';
  b += '<div class="dsec"><h4>Runs</h4>';
  if (!det.runs.length) b += '<div class="empty">No run yet.</div>';
  else {
    b += '<div class="tablebox"><table style="min-width:520px"><thead><tr><th>Started</th><th>Account</th><th>Step</th><th>Fence</th><th>Time</th><th>Outcome</th><th>Tokens</th></tr></thead><tbody>';
    det.runs.forEach(function (r) {
      var dur = r.ended_at ? (new Date(r.ended_at) - new Date(r.started_at)) / 1000 : (Date.now() - new Date(r.started_at)) / 1000;
      b += '<tr style="cursor:default"><td class="mono small">' + when(r.started_at) + '</td><td class="mono">' + esc(r.account_id) + '</td><td>' + esc(r.step) + '</td><td class="mono">#' + r.fence + '</td><td class="mono">' + mmss(dur) + '</td><td>' + pill(runTone(r.outcome || r.status), r.outcome || r.status) + '</td><td class="mono">' + tok(Number(r.tokens_in) + Number(r.tokens_out)) + '</td></tr>';
      if (r.summary) b += '<tr style="cursor:default"><td colspan="7" class="small" style="white-space:normal">' + esc(r.summary) + '</td></tr>';
    });
    b += '</tbody></table></div>';
  }
  b += '</div>';
  if (det.events.length) {
    b += '<div class="dsec"><h4>Timeline</h4><ul class="timeline">';
    det.events.forEach(function (e) { b += '<li><div class="tl-time">' + when(e.ts) + '</div><b>' + esc(e.type) + '</b> ' + esc([e.account, e.node, e.outcome, e.reason].filter(Boolean).join(' · ')) + '</li>'; });
    b += '</ul></div>';
  }
  if (det.writes.length) {
    b += '<div class="dsec"><h4>Audited writes</h4><ul class="checklist">';
    det.writes.forEach(function (w) { b += '<li><span class="' + (w.accepted ? 'ico-ok' : 'ico-fail') + '">' + (w.accepted ? ICON.ok : ICON.fail) + '</span><span><span class="mono small">' + when(w.ts) + '</span> ' + esc(w.target + ' ' + w.action) + ' · ' + esc(w.actor) + (w.fence != null ? ' · fence #' + w.fence : '') + (w.result ? ' · ' + esc(w.result) : '') + '</span></li>'; });
    b += '</ul></div>';
  }
  return drawerShell(esc(key) + ' · ' + esc(t.project || full.project || ''), esc(t.summary || full.summary), pills, b);
}
function drawerNode(name) {
  var n = nodeBy(name); if (!n) return drawerShell('node', esc(name), '', '<div class="empty">This node is no longer registered.</div>');
  var d = D(), accs = d.accounts.filter(function (a) { return accountAssignedTo(a, n.name); }), m = n.metrics || {};
  var b = (n.purpose || n.notes ? '<div style="font-size:13.5px">' + esc(n.purpose || '') + (n.notes ? '<div class="small" style="margin-top:4px">' + esc(n.notes) + '</div>' : '') + '</div>' : '<div class="small">No additional machine profile details are set.</div>') +
    '<div class="facts">' + fact('IP', '<span class="mono">' + esc(n.ip || '—') + '</span>') + fact('Joined', when(n.joined_at)) + fact('Projects', esc((n.projects || []).join(', ') || '—')) + fact('Capabilities', esc((n.capabilities || []).join(', ') || '—')) + fact('Status', nodeLabel(n)) + fact('Release', '<span class="mono">' + relLabel(n) + '</span>') + fact('Slots', d.active_runs.filter(function (r) { return r.node === n.name; }).length + ' / ' + n.slots_total) + fact('Last heartbeat', nodeLastSeen(n)) + fact('CPUs', String(m.cpus || '—')) + fact('Hostname', '<span class="mono">' + esc(m.hostname || '—') + '</span>') + '</div>';
  if (n.status === 'offline') b += '<div class="offnote">The node stopped sending heartbeats. The watchdog opened an incident and emailed it; runs on this node lose their lease after 90 s and their tickets return to To Do.</div>';
  b += '<div class="dsec"><h4>Resources</h4>' + nodeMetrics(n) + '</div>';
  var missingTools = n.missing_tools || Object.keys(n.tools || {}).filter(function (k) { return n.tools[k] === 'missing'; });
  b += '<div class="dsec"><h4>Tools on this node</h4><div class="ver">';
  Object.keys(n.tools || {}).sort().forEach(function (k) { b += '<span class="vk">' + esc(k) + '</span><span class="vv' + (missingTools.indexOf(k) >= 0 ? ' bad' : '') + '">' + esc(n.tools[k]) + '</span>'; });
  missingTools.forEach(function (k) { if (!(n.tools || {})[k]) b += '<span class="vk">' + esc(k) + '</span><span class="vv bad">missing</span>'; });
  b += '</div>';
  if (missingTools.length) b += '<div class="small" style="color:var(--crit);margin-top:6px">Missing for this node\'s project stacks: ' + esc(missingTools.join(', ')) + '. Install them on the node; the watchdog warns until a heartbeat reports them.</div>';
  b += '</div><div class="dsec"><h4>Test services</h4>';
  var nodeServices = n.services || [];
  if (!nodeServices.length) b += '<div class="empty">No project served by this node declares test services.</div>';
  nodeServices.forEach(function (svc) {
    b += '<div class="node-acc" style="font-size:13px"><span class="sdot" data-tone="' + (svc.healthy ? 'ok' : 'crit') + '"></span><span class="who">' + esc(svc.name) + ' <span class="mono small">' + esc(svc.endpoint) + '</span></span><span class="what">' + esc(svc.healthy ? svc.state : 'unhealthy') + ' · ' + esc(svc.detail || '') + (svc.projects && svc.projects.length ? ' · ' + esc(svc.projects.join(', ')) : '') + '</span></div>';
    if (!svc.healthy) b += '<div class="small" style="color:var(--crit)">This node takes no work for ' + esc((svc.projects || []).join(', ') || 'its projects') + ' until ' + esc(svc.name) + ' is healthy.</div>';
  });
  b += '</div><div class="dsec"><h4>Workers</h4>';
  if (!accs.length) b += '<div class="empty">No accounts assigned.</div>';
  accs.forEach(function (a) { b += '<div class="node-acc" style="font-size:13px"><span class="sdot" data-tone="' + accTone(a) + '"></span><span class="who">aiflow4-worker@' + esc(a.id) + '</span><span class="what">' + esc(doing(a)) + '</span></div>'; });
  b += '</div>';
  if (consoleCan('nodes.manage')) {
    b += '<div class="dsec"><h4>Install and repair</h4>';
    if (n.status === 'online' && n.has_agent) {
      b += '<div class="small">Install only components missing from the required toolchain, or reconcile versions.lock and desired services.</div>';
      b += '<div style="display:flex;gap:8px;flex-wrap:wrap"><button class="btn sm" data-act="node-install" data-node="' + esc(n.name) + '"' + (S.operationBusy[n.name] ? ' disabled' : '') + '>Install missing</button>';
      b += '<button class="btn sm primary" data-act="node-repair" data-node="' + esc(n.name) + '"' + (S.operationBusy[n.name] ? ' disabled' : '') + '>Repair to desired state</button></div>';
    } else {
      b += '<div class="small">This node has no online agent. Generate a one-time join command from Machines and AI.</div>';
    }
    if (n.status !== 'online') b += '<button class="btn sm" data-act="join-command" data-node="' + esc(n.name) + '">Generate join command</button>';
    var join = S.joinCommands[n.name];
    if (join) b += '<div class="small">Expires ' + when(join.expires_at) + ' · one use</div><pre class="report-md">' + esc(join.command) + '</pre><button class="btn sm" data-act="copy-join" data-node="' + esc(n.name) + '">Copy command</button>';
    b += '</div>';
  } else if (S.consoleUser) {
    b += '<div class="dsec"><h4>Install and repair</h4><div class="small">Only an authenticated user with node management permission can install or repair a node.</div></div>';
  }
  var desiredServices = n.desired_services || [];
  b += '<div class="dsec"><h4>Desired services</h4>';
  if (!desiredServices.length) b += '<div class="empty">No node services are declared in the control repo.</div>';
  else desiredServices.forEach(function (svc) { b += '<div class="small">' + esc(svc.name || svc.unit || 'service') + ' · ' + esc(svc.unit || 'unit missing') + (svc.healthcheck || svc.healthcheck_configured ? ' · health check configured' : ' · systemd active check') + '</div>'; });
  b += '</div>';
  if (n.status === 'online' && n.has_agent) b += '<div class="small">AI account authorization is handled separately; installation does not sign accounts in.</div>';
  if (consoleCan('console.read')) {
    var nodeOps = S.nodeOperations[n.name] || [];
    b += '<div class="dsec"><h4>Operation log</h4>';
    if (!nodeOps.length) b += '<div class="empty">No install or repair operations recorded.</div>';
    else {
      b += '<ul class="checklist">';
      nodeOps.forEach(function (op) {
        var toneName = op.status === 'completed' ? 'ico-ok' : op.status === 'failed' ? 'ico-fail' : '';
        b += '<li><span class="' + toneName + '">' + (op.status === 'completed' ? ICON.ok : op.status === 'failed' ? ICON.fail : ICON.pend) + '</span><span><span class="mono small">' + when(op.created_at) + '</span> ' + esc(op.action) + ' · ' + esc(op.status) + ' · ' + esc(op.actor);
        if (op.result && op.result.error) b += '<div class="small">' + esc(op.result.error) + '</div>';
        if (op.result && op.result.services && op.result.services.length) b += '<div class="small">Services: ' + op.result.services.map(function (s) { return esc(s.name) + ' ' + (s.healthy ? 'green' : 'failed'); }).join(', ') + '</div>';
        b += '</span></li>';
      });
      b += '</ul>';
    }
    b += '</div>';
  }
  return drawerShell('node · ' + esc(n.ip), esc(n.name), pill(nodeTone(n), nodeLabel(n)), b);
}
function drawerAcc(id) {
  var a = accBy(id); if (!a) return drawerShell('account', esc(id), '', '<div class="empty">Unknown account.</div>');
  var pr = (a.worker && a.worker.probe) || {};
  var b = '<div class="facts">' + fact('Provider', esc(PROVIDER[a.provider] || a.provider)) + fact('Tier', esc(a.tier)) + fact('Node', '<span class="mono">' + esc(a.node) + '</span>') + fact('Model', '<span class="mono">' + esc(a.model || '—') + '</span>') + fact('Roles', esc((a.roles || []).join(', ') || '—')) + fact('Takes tickets', a.explicit_only ? 'only when a ticket asks for ' + esc(a.provider) : 'any ready ticket') + '</div>';
  b += '<div class="dsec"><h4>Quota</h4>' + qwin(a) + (a.cooldown ? '<div class="small">Cooling down (' + esc(a.cooldown.reason) + '), ' + mmss(a.cooldown.seconds_left) + ' left.</div>' : '') + (a.provider === 'claude' ? '<div class="small">At 90% of a window the watchdog opens a warning; at 100% the account pauses until the reset time and other accounts take its tickets. A Claude limit reset offer (Settings > Usage) restores it at once.</div>' : '') + '</div>';
  b += '<div class="dsec"><h4>Last check</h4><div style="font-size:13px">' + (pr.at ? (pr.ok ? 'Passed ' : 'Failed ') + when(new Date(pr.at * 1000).toISOString()) + (pr.error_kind ? ' · ' + esc(pr.error_kind) : '') + (pr.next ? ' · next ' + when(new Date(pr.next * 1000).toISOString()) : '') : 'Not checked yet') + '</div>' + (a.last_error ? '<div class="small" style="color:var(--crit)">' + esc(a.last_error) + '</div>' : '') + '</div>';
  b += '<div class="dsec"><h4>Now</h4><div style="font-size:13px">' + esc(doing(a)) + '</div></div>';
  var runs = D().recent_runs.filter(function (r) { return r.account === a.id; });
  b += '<div class="dsec"><h4>Recent runs</h4>' + runsTable(runs, 10) + '</div>';
  b += accountMachinesSection(a);
  b += '<div class="dsec"><h4>Config directory</h4><div class="codeline"><code>/srv/aiflow/accounts/' + esc(a.id) + '</code></div></div>';
  return drawerShell('account · ' + esc(PROVIDER[a.provider] || a.provider), esc(a.id), pill(accTone(a), accLabel(a)), b);
}
function drawerInc(id) {
  var i = incBy(id); if (!i) return drawerShell('incident', '#' + esc(id), '', '<div class="empty">Closed and no longer listed.</div>');
  var b = '<div style="font-size:13.5px;white-space:pre-wrap">' + esc(i.detail) + '</div><div class="facts">' + fact('Kind', esc(KIND[i.kind] || i.kind)) + fact('Subject', '<span class="mono">' + esc(i.subject) + '</span>') + fact('Opened', when(i.opened_at)) + fact('Closed', i.closed_at ? when(i.closed_at) : 'open') + '</div>';
  b += '<div class="dsec"><h4>What happens</h4><div style="font-size:13px">' + ({
    node_down: 'Runs on the node keep their lease for 90 s; then the watchdog marks them lost and moves their tickets back to To Do with a comment. The incident closes on the next heartbeat.',
    account_quota: 'The account takes no ticket until it passes its next check; its tickets go to the other accounts.',
    quota_high: 'Informational until 100%; then the account pauses until the reset time.',
    quota_prediction: 'New work is routed to other accounts of this provider because the current burn rate predicts exhaustion before reset.',
    run_slow: 'The run passed the warning clock. It keeps working until the hard limit.',
    role_coverage: 'A role the flow needs (master, intake, verify, dev, release) has fewer than two ready accounts from different providers, so one quota limit or outage could stop it. Resume or add an unpaused account with that role from another provider.',
    role_health: 'A role is switched off for an account only while the pool keeps at least two enabled accounts and no more than half are switched off. If every eligible account scores badly, AI Flow keeps the role on and reports the most frequent failure kind. Completed pull requests and clean no-change runs are successes. A corrected history or installed missing tool restores the role immediately; otherwise successful probation runs can restore it without a day-long wait. Failure counts and involved tickets appear above. Thresholds are on the Command Center, under AI quality by role.',
    node_no_ai: 'The node heartbeats and adds slots, but no AI account is assigned to it, so no worker runs there. Add an account for this machine on Machines and AI, then complete the sign-in handoff.',
    node_tools: 'A project this node serves needs a tool that is missing from the node, so its checks cannot run. Install the tool on the node; the incident closes when a later heartbeat reports it.',
    node_services: 'A test service (PostgreSQL or Redis) that a project declares is missing or unhealthy on this node, so the node takes no work for that project. The node agent recreates missing or stopped services; the incident closes when a later heartbeat reports them healthy.',
    service_undeclared: 'Tickets of a project are parked on a test service (PostgreSQL or Redis) that the project does not declare. The flow doctor declares it itself; this alert appears only when it could not. Add the service to Test services of the project on the Projects screen.',
    ticket_stuck: 'Dev runs of this ticket keep failing. Retries wait longer each time (up to 30 minutes). Read the last run in the ticket drawer, fix the cause or put the ticket on hold.',
    run_hard_limit: 'The run passed the hard limit; the worker stops it and the ticket goes back to To Do.'
  }[i.kind] || 'Handled by the watchdog.') + '</div></div>';
  return drawerShell('incident #' + esc(i.id), esc(i.title), pill(i.closed_at ? 'ok' : i.severity === 'critical' ? 'crit' : 'warn', i.closed_at ? 'Resolved' : i.severity), b);
}
function drawerReport() {
  var r = S.drawer, label = r.epic ? esc(r.epic) : esc(r.id);
  if (r.error) return drawerShell('project report', label, '', '<div class="empty">' + esc(r.error) + '</div>');
  if (!r.text) return drawerShell('project report', label, '', '<div class="empty">Loading Markdown report…</div>');
  var b = '<div style="display:flex;justify-content:flex-end"><button class="btn sm" data-act="copy-report">Copy Markdown</button></div>' +
    '<pre class="report-md">' + esc(r.text) + '</pre>';
  return drawerShell('project report', label, '', b);
}
function renderDrawer() {
  var d = $('#drawer'); if (!S.drawer) return;
  var k = S.drawer.kind, id = S.drawer.id, html = '';
  if (k === 'ticket') html = drawerTicket(id);
  if (k === 'node') html = drawerNode(id);
  if (k === 'acc') html = drawerAcc(id);
  if (k === 'inc') html = drawerInc(id);
  if (k === 'report') html = drawerReport();
  var body = $('.drawer-b', d), scroll = body ? body.scrollTop : 0;
  d.innerHTML = html;
  var nb = $('.drawer-b', d); if (nb) nb.scrollTop = scroll;
  tick();
}
function loadTicket(key) {
  fetch('/v1/tickets/' + encodeURIComponent(key), { cache: 'no-store' }).then(function (r) { return r.ok ? r.json() : null; }).then(function (d) {
    if (d) { S.ticket[key] = d; if (S.drawer && S.drawer.kind === 'ticket' && S.drawer.id === key) renderDrawer(); }
  }).catch(function () {});
}
function loadProjectReport(project, epic) {
  var query = 'format=md' + (epic ? '&epic=' + encodeURIComponent(epic) : '');
  fetch('/v1/projects/' + encodeURIComponent(project) + '/report?' + query, { cache: 'no-store' }).then(function (r) {
    if (!r.ok) throw new Error('Report could not be loaded (' + r.status + ')');
    return r.text();
  }).then(function (text) {
    if (S.drawer && S.drawer.kind === 'report' && S.drawer.id === project && S.drawer.epic === epic) {
      S.drawer.text = text; renderDrawer();
    }
  }).catch(function (e) {
    if (S.drawer && S.drawer.kind === 'report' && S.drawer.id === project && S.drawer.epic === epic) {
      S.drawer.error = String(e.message || e); renderDrawer();
    }
  });
}
function closeProject(key, button) {
  if (button) button.disabled = true;
  fetch('/v1/projects/' + encodeURIComponent(key) + '/close', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ actor: 'owner' })
  }).then(function (r) {
    return r.json().then(function (data) { if (!r.ok) throw new Error(data.detail || 'Project could not be closed'); return data; });
  }).then(function (data) {
    var delivery = (data.sent_channels || []).indexOf('email') >= 0 ? ' and emailed to the owner' : '';
    toast('Project closed', 'Study case v' + data.version + ' saved' + delivery, 'ok');
    poll();
  }).catch(function (e) {
    toast('Project could not be closed', String(e.message || e), 'warn');
    if (button) button.disabled = false;
  });
}
function openProjectReport(opener) {
  lastFocus = opener || document.activeElement;
  var epic = S.epic === 'all' ? null : S.epic;
  S.drawer = { kind: 'report', id: S.project, epic: epic, text: '', error: '' };
  renderDrawer();
  $('#overlay').setAttribute('data-open', ''); $('#drawer').setAttribute('data-open', ''); $('#drawer').setAttribute('aria-hidden', 'false');
  loadProjectReport(S.project, epic);
  setTimeout(function () { var c = $('#drawer [data-act="close"]'); if (c) c.focus(); }, 60);
}
function copyReport() {
  var text = S.drawer && S.drawer.text || '';
  function copied(ok) { toast(ok ? 'Report copied' : 'Could not copy report', ok ? 'Markdown copied to clipboard' : 'Select and copy the report text', ok ? 'ok' : 'warn'); }
  if (window.navigator && window.navigator.clipboard && window.navigator.clipboard.writeText) {
    window.navigator.clipboard.writeText(text).then(function () { copied(true); }).catch(function () { copied(false); });
    return;
  }
  var field = document.createElement('textarea'); field.value = text;
  field.setAttribute('readonly', ''); field.style.position = 'fixed'; field.style.opacity = '0';
  document.body.appendChild(field); field.select();
  var ok = document.execCommand && document.execCommand('copy');
  field.remove(); copied(Boolean(ok));
}
var lastFocus = null;
function openDrawer(kind, id, opener) {
  lastFocus = opener || document.activeElement;
  S.drawer = { kind: kind, id: id };
  if (kind === 'ticket') loadTicket(id);
  if (kind === 'node' && consoleCan('console.read')) loadNodeOperations(id);
  renderDrawer();
  $('#overlay').setAttribute('data-open', ''); $('#drawer').setAttribute('data-open', ''); $('#drawer').setAttribute('aria-hidden', 'false');
  setTimeout(function () { var c = $('#drawer [data-act="close"]'); if (c) c.focus(); }, 60);
}
function closeAll() {
  S.drawer = null;
  $('#overlay').removeAttribute('data-open'); $('#drawer').removeAttribute('data-open'); $('#drawer').setAttribute('aria-hidden', 'true');
  if (lastFocus && lastFocus.focus) lastFocus.focus();
}

