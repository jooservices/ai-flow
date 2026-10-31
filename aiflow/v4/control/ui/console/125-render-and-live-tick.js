/* ---------- render and live tick ---------- */
/* ---------- footer ---------- */
function footerVersion(d) { return ((d.versions || {}).ai_flow || {}).ref || d.version || '—'; }
function renderFooter() {
  var d = D(), links = d.links || {};
  $('#f-year').textContent = String(new Date().getFullYear());
  if (!S.data) return;
  $('#f-version').textContent = footerVersion(d);
  $('#f-env').textContent = d.env || '—';
  $('#f-control').textContent = String(d.control_repo || '—').replace('ref: refs/heads/', '');
  [['#f-o2', links.o2], ['#f-mail', links.mail], ['#f-gatus', links.gatus]].forEach(function (l) { if (l[1]) $(l[0]).setAttribute('href', l[1]); });
}

function machineRequest(url, options) {
  return fetch(url, options || {}).then(function (r) {
    return r.json().then(function (d) { if (!r.ok) throw new Error(d.detail || ('HTTP ' + r.status)); return d; });
  });
}
function openMachineForm() { S.machines.addMachine = !S.machines.addMachine; S.machines.error = ''; renderMachines(); }
function submitMachineJoin() {
  var form = $('#machines-join-form'); if (!form || S.machines.join.busy) return;
  var j = S.machines.join;
  j.name = $('#machine-name', form).value.trim(); j.role = $('#machine-role', form).value;
  j.project = $('#machine-project', form).value; j.error = ''; j.busy = true; j.command = '';
  renderMachines();
  machineRequest('/v1/nodes/' + encodeURIComponent(j.name) + '/join-command', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({name: j.name, role: j.role, project: j.project})
  }).then(function (d) { j.command = d.command; j.expires_at = d.expires_at; j.error = ''; })
    .catch(function (e) { j.error = String(e.message || e); })
    .then(function () { j.busy = false; renderMachines(); });
}
function copyMachineCommand() {
  var command = S.machines.join.command; if (!command) return;
  if (new Date(S.machines.join.expires_at).getTime() <= Date.now()) { toast('Join command expired', 'Create a new command to add this machine.', 'warn'); return; }
  if (window.navigator && window.navigator.clipboard && window.navigator.clipboard.writeText) {
    window.navigator.clipboard.writeText(command).then(function () { toast('Join command copied', '', 'ok'); })
      .catch(function () { toast('Select and copy the command', '', 'info'); });
  } else toast('Select and copy the join command', '', 'info');
}
function addMachineAccount(name) {
  var form = $('#account-add-' + CSS.escape(name)); if (!form) return;
  var jobs = $$('[data-new-job]:checked', form).map(function (item) { return item.value; });
  var sessions = Number($('[name="sessions"]', form).value || 0), provider = $('[name="provider"]', form).value;
  machineRequest('/v1/machines/' + encodeURIComponent(name) + '/accounts', {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({provider: provider, name: ($('[name="name"]', form).value || '').trim(), sessions: sessions, jobs: jobs})}).then(function (d) { S.machines.signIns[d.account] = d; S.machines.signInRefreshAt[d.account] = Date.now(); S.machines.addAccount = ''; S.machines.error = ''; poll(); renderMachines(); })
    .catch(function (e) { S.machines.error = String(e.message || e); renderMachines(); });
}
function startAccountSignIn(id) {
  machineRequest('/v1/accounts/' + encodeURIComponent(id) + '/sign-in', {method: 'POST'})
    .then(function (d) { S.machines.signIns[id] = d; S.machines.signInRefreshAt[id] = Date.now(); S.machines.error = ''; poll(); renderMachines(); })
    .catch(function (e) { S.machines.error = String(e.message || e); renderMachines(); });
}
function completeAccountSignIn(id) {
  machineRequest('/v1/accounts/' + encodeURIComponent(id) + '/sign-in/complete', {method: 'POST'})
    .then(function () { delete S.machines.signIns[id]; S.machines.error = ''; poll(); renderMachines(); })
    .catch(function (e) { S.machines.error = String(e.message || e); renderMachines(); });
}
function loadAccountSignIn(id) {
  if (S.machines.signInLoading[id] || Date.now() - (S.machines.signInRefreshAt[id] || 0) < 2000) return;
  S.machines.signInLoading[id] = true;
  machineRequest('/v1/accounts/' + encodeURIComponent(id) + '/sign-in')
    .then(function (d) { S.machines.signIns[id] = d; })
    .catch(function (e) { S.machines.signIns[id] = {account: id, error: String(e.message || e)}; })
    .then(function () { S.machines.signInRefreshAt[id] = Date.now(); delete S.machines.signInLoading[id]; if (S.view === 'machines') renderMachines(); });
}
function saveAccountSettings(id) {
  var form = $('#account-edit-' + CSS.escape(id)); if (!form) return;
  var jobs = $$('[data-edit-job]:checked', form).map(function (item) { return item.value; });
  var field = $('[name="sessions"]', form), payload = {jobs: jobs}; if (field) payload.sessions = Number(field.value || 0);  /* shared accounts keep one total limit */
  machineRequest('/v1/accounts/' + encodeURIComponent(id), {method: 'PATCH', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)})
    .then(function () { S.machines.accountEditor = null; S.machines.error = ''; poll(); renderMachines(); })
    .catch(function (e) { S.machines.error = String(e.message || e); renderMachines(); });
}
function setAccountPaused(id, paused) {
  machineRequest('/v1/accounts/' + encodeURIComponent(id), {method: 'PATCH', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({action: paused ? 'pause' : 'resume'})})
    .then(function () { poll(); renderMachines(); })
    .catch(function (e) { S.machines.error = String(e.message || e); renderMachines(); });
}
function removeMachineAccount(id) {
  if (!window.confirm('Remove AI account ' + id + '? Its provider credentials remain on the machine.')) return;
  machineRequest('/v1/accounts/' + encodeURIComponent(id), {method: 'DELETE', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({confirm: true})})
    .then(function () { poll(); renderMachines(); })
    .catch(function (e) { S.machines.error = String(e.message || e); renderMachines(); });
}
function toggleMachineWork(name, accepting) {
  if (!accepting && !window.confirm('Stop ' + name + ' from taking new work? Running sessions can finish.')) return;
  machineRequest('/v1/nodes/' + encodeURIComponent(name) + '/work-acceptance?accepting_work=' + String(accepting), {
    method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({confirm: !accepting})
  }).then(function () { poll(); renderMachines(); })
    .catch(function (e) { S.machines.error = String(e.message || e); renderMachines(); });
}
function removeMachine(name) {
  if (!window.confirm('Remove machine ' + name + '? Its node credential will stop working and its AI accounts will be removed from AI Flow.')) return;
  machineRequest('/v1/nodes/' + encodeURIComponent(name), {method: 'DELETE', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({confirm: true})})
    .then(function () { poll(); renderMachines(); })
    .catch(function (e) { S.machines.error = String(e.message || e); renderMachines(); });
}
function jobChecks(selected, attr) {
  return ['develop', 'verify', 'intake', 'master'].map(function (job) {
    return '<label><input type="checkbox" ' + attr + ' value="' + job + '"' + (selected.indexOf(job) >= 0 ? ' checked' : '') + '> ' + job + '</label>';
  }).join('');
}
function renderMachines() {
  var root = $('#view-machines'); if (!root) return;
  var state = S.machines, data = D(), nodes = (data.nodes || []).filter(function (n) { return n.has_agent || n.status !== 'joined'; });
  var h = '<section class="panel" data-block="machines"><div class="panel-h"><h2>Machines and AI</h2><span class="hint">Add machines and manage AI accounts, sessions, jobs and quota.</span><span class="spacer"></span>';
  if (consoleCan('nodes.manage')) h += '<button class="btn primary" data-act="machine-add-toggle">Add a machine</button>';
  h += '</div>';
  if (state.error) h += '<div class="stakeholder-error" role="alert">' + esc(state.error) + '</div>';
  h += '<div class="machine-list">';
  if (!nodes.length) h += '<div class="machine-empty">No machine has joined yet.</div>';
  nodes.forEach(function (n) {
    var machineAccounts = (data.accounts || []).filter(function (a) { return accountAssignedTo(a, n.name); });
    var pendingAccounts = machineAccounts.filter(function (a) { return accountPendingOnMachine(a, n.name); });
    var accounts = machineAccounts.filter(function (a) { return !accountPendingOnMachine(a, n.name); });
    var assignedSessions = machineAccounts.reduce(function (total, a) { return total + Number(a.parallel || 1); }, 0);
    var availableSessions = Math.max(0, 8 - assignedSessions);
    var metrics = n.metrics || {}, editorId = state.accountEditor;
    h += '<article class="machine-card"><div class="machine-head"><h3>' + esc(n.name) + '</h3>' + pill(nodeTone(n), nodeLabel(n)) +
      (!n.accepting_work ? pill('warn', 'Not taking new work') : '') + '<span class="spacer"></span>';
    h += '<button class="btn sm" data-act="open-node" data-id="' + esc(n.name) + '">Machine details</button>';
    if (consoleCan('nodes.manage')) h += '<button class="btn sm" data-act="machine-add-account" data-name="' + esc(n.name) + '">Add AI account</button>';
    if (consoleCan('nodes.manage')) h += '<button class="btn sm" data-act="machine-work" data-name="' + esc(n.name) + '" data-accept="' + String(!n.accepting_work) + '">' + (n.accepting_work ? 'Stop new work' : 'Resume work') + '</button>';
    if (consoleCan('nodes.manage')) h += '<button class="btn sm danger" data-act="machine-remove" data-name="' + esc(n.name) + '">Remove machine</button>';
    h += '</div><div class="machine-facts"><span>Memory <strong>' + gib(metrics.mem_used) + ' / ' + gib(metrics.mem_total) + '</strong></span>';
    h += '<span>Processors <strong>' + esc(metrics.cpus == null ? '—' : metrics.cpus) + '</strong></span>';
    h += '<span>Sessions busy <strong>' + esc(n.sessions_busy || 0) + ' / ' + esc(n.slots_total || 0) + '</strong></span>';
    h += '<span>Configured AI sessions <strong>' + esc(assignedSessions) + ' / 8</strong></span>';
    h += '<span>Last heartbeat <strong>' + esc(nodeLastSeen(n)) + '</strong></span></div>';
    if (state.addAccount === n.name && consoleCan('nodes.manage')) {
      h += '<form class="project-form machine-form" id="account-add-' + esc(n.name) + '" onsubmit="return false"><label>AI<select name="provider" aria-label="AI provider"><option value="codex">Codex</option><option value="claude">Claude</option><option value="opencode">OpenCode</option><option value="grok">Grok</option></select></label><label>Name<input name="name" type="text" maxlength="40" placeholder="Optional, e.g. Team plan" aria-label="Account name"></label>';
      h += '<label>Sessions at once<input name="sessions" type="number" min="1" max="' + availableSessions + '" value="1"' + (availableSessions < 1 ? ' disabled' : '') + '></label>';
      h += '<div class="small">' + availableSessions + ' of the machine’s eight AI sessions are available.</div><div class="wide job-options">' + jobChecks(['develop', 'verify'], 'data-new-job') + '</div>';
      h += '<div class="project-actions wide"><button class="btn primary" data-act="machine-account-save" data-name="' + esc(n.name) + '"' + (availableSessions < 1 ? ' disabled' : '') + '>Create sign-in link and code</button><button class="btn" data-act="machine-account-cancel">Cancel</button></div></form>';
    }
    h += '<div class="machine-accounts">';
    pendingAccounts.forEach(function (a) {
      var machineUsage = (a.machines || []).filter(function (m) { return m && m.node === n.name; })[0] || {};
      var sharedAssignment = !!(a.shared || a.shared_managed || machineUsage.shared_assignment);
      var signIn = sharedAssignment ? machineUsage.signin : state.signIns[a.id];
      var removableAssignment = (a.machines || []).some(function (m) { return m.node === n.name && m.removable; });
      if (!sharedAssignment && consoleCan('nodes.manage') &&
          (!signIn || (state.signInRefreshAt[a.id] && Date.now() - state.signInRefreshAt[a.id] >= 2000))) loadAccountSignIn(a.id);
      h += '<section class="managed-account"><div class="machine-head"><h4>' + esc(a.id) + '</h4>' + pill('warn', 'Sign-in pending') +
        '<span class="muted">' + esc(PROVIDER[a.provider] || a.provider) + '</span><span class="spacer"></span>';
      if (consoleCan('nodes.manage') && sharedAssignment) h += '<button class="btn sm" data-act="acc-retry-signin" data-account="' + esc(a.id) + '" data-node="' + esc(n.name) + '">Retry sign-in</button>';
      else if (consoleCan('nodes.manage')) h += '<button class="btn sm" data-act="account-signin" data-id="' + esc(a.id) + '">Start sign-in again</button>';
      var signInReady = !sharedAssignment && signIn && signIn.status === 'completed' && (a.status === 'idle' || a.status === 'working');
      if (!sharedAssignment && consoleCan('nodes.manage')) h += '<button class="btn sm primary" data-act="account-signin-complete" data-id="' + esc(a.id) + '"' + (signInReady ? '' : ' disabled') + '>Finish sign-in</button>';
      if (consoleCan('nodes.manage') && removableAssignment) h += '<button class="btn sm danger" data-act="acc-remove-machine" data-account="' + esc(a.id) + '" data-node="' + esc(n.name) + '" data-last="' + ((a.machines || []).length === 1 ? '1' : '') + '">Remove</button>';
      else if (consoleCan('nodes.manage') && !sharedAssignment) h += '<button class="btn sm danger" data-act="account-remove" data-id="' + esc(a.id) + '">Remove</button>';
      h += '</div>';
      if (sharedAssignment && signIn && signIn.link) h += '<div class="signin-result"><a href="' + esc(safeHref(signIn.link)) + '" target="_blank" rel="noopener noreferrer">Open provider authorization</a>' +
        (signIn.code ? ' · provider code <span class="mono">' + esc(signIn.code) + '</span>' : '') +
        (signIn.expires_at ? ' · expires ' + esc(when(signIn.expires_at)) : '') +
        '<p class="small">Complete authorization for ' + esc(n.name) + '. Its worker will check the credentials after the provider login finishes.</p></div>';
      else if (sharedAssignment) h += '<div class="small">Waiting for ' + esc(n.name) + ' to show its sign-in link and code.' +
        (consoleCan('nodes.manage') ? ' Use Retry sign-in to start a fresh attempt.' : '') + '</div>';
      else if (signIn && signIn.error) h += '<div class="small">' + esc(signIn.error) + ' · Start sign-in again to retry.</div>';
      else if (signIn && signIn.status === 'failed') h += '<div class="small">' + esc(signIn.error || 'Provider sign-in failed.') + ' · Start sign-in again to retry.</div>';
      else if (signIn && signIn.sign_in_url) h += '<div class="signin-result"><a href="' + esc(safeUrl(signIn.sign_in_url)) + '" target="_blank" rel="noopener">Open provider authorization</a>' +
        (signIn.code ? ' · provider code <span class="mono">' + esc(signIn.code) + '</span>' : '') +
        (signIn.expires_at ? ' · expires ' + esc(when(signIn.expires_at)) : '') +
        '<p class="small">Complete the provider authorization for this account on ' + esc(n.name) + '. The machine stores the resulting credentials in this account’s private home. After its check succeeds, choose Finish sign-in.</p></div>';
      else if (signIn && signIn.status === 'completed') h += '<div class="small">Provider authorization completed. Waiting for the machine to check the account.</div>';
      else h += '<div class="small">' + (consoleCan('nodes.manage') ? 'Starting provider sign-in on ' + esc(n.name) + '…' : 'Sign-in is pending owner action.') + '</div>';
      h += '</section>';
    });
    if (!accounts.length && !pendingAccounts.length) h += '<div class="machine-empty">No AI accounts on this machine.</div>';
    accounts.forEach(function (a) {
      var machineUsage = (a.machines || []).find(function (m) { return m.node === n.name; }) || {};
      var sharedAssignment = !!(a.shared || a.shared_managed || machineUsage.shared_assignment);
      var removableAssignment = (a.machines || []).some(function (m) { return m.node === n.name && m.removable; });
      h += '<section class="managed-account"><div class="machine-head"><h4>' + esc(a.name || a.id) + '</h4>' + pill(accTone(a), a.paused ? 'Paused' : accLabel(a)) +
        '<span class="muted">' + esc(PROVIDER[a.provider] || a.provider) + (sharedAssignment
          ? ' · total limit ' + esc(a.session_limit || a.parallel) + ' sessions'
          : ' · ' + esc(a.parallel) + ' session' + (a.parallel === 1 ? '' : 's') +
            ' · ' + esc((a.jobs || []).join(', ') || 'no jobs')) + '</span><span class="spacer"></span>';
      if (!sharedAssignment && consoleCan('nodes.manage')) h += '<button class="btn sm" data-act="account-pause" data-id="' + esc(a.id) + '" data-paused="' + String(!a.paused) + '">' + (a.paused ? 'Resume' : 'Pause') + '</button>';
      if (consoleCan('nodes.manage')) h += '<button class="btn sm" data-act="account-edit" data-id="' + esc(a.id) + '">' + (sharedAssignment ? 'Jobs' : 'Sessions and jobs') + '</button>';
      if (!sharedAssignment && consoleCan('nodes.manage')) h += '<button class="btn sm" data-act="account-signin" data-id="' + esc(a.id) + '">' + (a.status === 'signed_out' ? 'Sign in' : 'Sign in again') + '</button>';
      if (consoleCan('nodes.manage') && removableAssignment) h += '<button class="btn sm danger" data-act="acc-remove-machine" data-account="' + esc(a.id) + '" data-node="' + esc(n.name) + '" data-last="' + ((a.machines || []).length === 1 ? '1' : '') + '">Remove</button>';
      else if (consoleCan('nodes.manage') && !sharedAssignment) h += '<button class="btn sm danger" data-act="account-remove" data-id="' + esc(a.id) + '">Remove</button>';
      h += '</div><div class="machine-facts"><span>Tokens today <strong>' + esc(tok(machineUsage.tokens_today || 0)) + '</strong></span>';
      h += '<span>Cost today <strong>' + esc(usd(machineUsage.cost_today || 0)) + '</strong></span><span>State <strong>' + esc(accLabel(a)) + '</strong></span></div>';
      if (state.accountEditor === a.id && consoleCan('nodes.manage')) {
        h += '<form class="project-form machine-form" id="account-edit-' + esc(a.id) + '" onsubmit="return false">' + (sharedAssignment ? '' : '<label>Sessions at once<input name="sessions" type="number" min="1" max="8" value="' + esc(a.parallel || 1) + '"></label>') + '<div class="wide job-options">' + jobChecks(a.jobs || [], 'data-edit-job') + '</div>';
        h += '<div class="project-actions wide"><button class="btn primary" data-act="account-edit-save" data-id="' + esc(a.id) + '">Save changes</button><button class="btn" data-act="account-edit-cancel">Cancel</button></div></form>';
      }
      h += '</section>';
    });
    h += '</div></article>';
  });
  h += '</div></section>';
  if (state.addMachine && consoleCan('nodes.manage')) {
    var j = state.join, projects = data.projects || [];
    h += '<section class="panel" data-block="add-machine"><div class="panel-h"><h2>Add a machine</h2><span class="hint">The one-time command expires after five minutes.</span></div>';
    h += '<form class="project-form" id="machines-join-form" onsubmit="return false"><label>Machine name<input id="machine-name" required maxlength="63" value="' + esc(j.name) + '"></label>';
    h += '<label>Role<select id="machine-role" aria-label="Machine role"><option value="worker"' + (j.role === 'worker' ? ' selected' : '') + '>worker</option><option value="control"' + (j.role === 'control' ? ' selected' : '') + '>control</option></select></label>';
    h += '<label>Project<select id="machine-project" aria-label="Machine project">' + projects.map(function (p) { return '<option value="' + esc(p.key) + '"' + (j.project === p.key ? ' selected' : '') + '>' + esc(p.key) + '</option>'; }).join('') + '</select></label>';
    h += '<div><button class="btn primary" data-act="machine-join-create"' + (j.busy || !projects.length ? ' disabled' : '') + '>' + (j.busy ? 'Creating…' : 'Show one-time command') + '</button></div>';
    if (j.error) h += '<div class="err wide" role="alert">' + esc(j.error) + '</div>';
    if (j.command) h += '<label class="wide">Command expires in <span class="mono" data-countdown="' + esc(j.expires_at) + '"></span><textarea rows="2" readonly onclick="this.select()">' + esc(j.command) + '</textarea></label><div><button class="btn" data-act="machine-copy-join">Copy command</button></div>';
    h += '<div class="small wide">This screen checks for the machine through live heartbeats. It will appear in the machine list after its agent joins.</div></form></section>';
  }
  root.innerHTML = h;
  applyLayout('machines', root);
}
/* Result of the project's check commands run once on a clean checkout of each machine (docs/v4/missing-tools.md). */
function projectChecksCell(c) {
  if (!c || !c.has_checks) return '<span class="muted">no checks</span>';
  var current = (c.machines || []).filter(function (m) { return m.current; });
  if (!current.length) return pill('info', 'not checked yet');
  var blocked = [];
  current.forEach(function (m) { (m.cannot_run || []).forEach(function (x) { blocked.push({ node: m.node, x: x }); }); });
  if (!blocked.length) return pill('ok', 'all can run · ' + current.length + ' machine' + (current.length > 1 ? 's' : ''));
  var h = pill('crit', blocked.length + ' cannot run');
  blocked.forEach(function (b) { h += '<div class="small" style="color:var(--crit)"><span class="mono">' + esc(b.x.command) + '</span> on ' + esc(b.node) + ': ' + esc(b.x.detail || ('missing ' + (b.x.tool || 'tool'))) + '</div>'; });
  return h;
}
function toolWaitsPanel(d) {
  var waits = d.tool_waits || [];
  if (!waits.length) return '';
  var h = '<section class="panel" data-block="tool-waits" id="p-tool-waits" aria-labelledby="h-tool-waits"><div class="panel-h"><h2 id="h-tool-waits">Parked tickets</h2><span class="hint">Requirement, time parked, and automatic next action</span></div>';
  h += '<table><thead><tr><th>Requirement</th><th>Ticket</th><th>Parked since</th><th>Missing on</th><th>System action</th><th>Maintenance ticket</th></tr></thead><tbody>';
  waits.forEach(function (w) {
    (w.tickets || []).forEach(function (t) {
      h += '<tr><td class="mono">' + esc(t.requirement || w.requirement) + '</td>';
      h += '<td class="mono" title="' + esc(t.summary || t.detail || '') + '">' + esc(t.key) + '</td>';
      h += '<td title="' + esc(t.since || w.since) + '">' + esc(when(t.since || w.since)) + '</td>';
      h += '<td>' + esc(t.machine || 'unknown') + '</td><td>' + esc(t.action || w.action || 'Waiting for a capable machine') + '</td>';
      h += '<td class="mono">' + (w.maintenance_ticket ? esc(w.maintenance_ticket) : '<span class="muted">after 24 h without a capable machine</span>') + '</td></tr>';
    });
  });
  return h + '</tbody></table></section>';
}
function renderProjects() {
  var root = $('#view-projects'); if (!root) return;
  var state = S.projectAdmin, rows = state.projects || [], invalid = state.invalidProjects || [], h = '<section class="panel"><div class="panel-h"><h2>Projects</h2>';
  h += '<span class="hint">Projects configured in aiflow-control and mirrored from Jira</span><span class="spacer"></span>';
  if (consoleCan('projects.manage')) h += '<button class="btn primary" data-act="project-add-open">Add project</button>';
  h += '</div>';
  if (state.error && !state.showForm) h += '<div class="stakeholder-error">' + esc(state.error) + '</div>';
  if (invalid.length) {
    h += '<div class="stakeholder-error" role="alert"><strong>Project files could not be loaded.</strong> These projects are unavailable to machines until their files are corrected in aiflow-control.';
    h += '<ul>' + invalid.map(function (item) { return '<li><span class="mono">' + esc(item.file) + '</span>: ' + esc(item.reason) + '</li>'; }).join('') + '</ul></div>';
  }
  if (state.projects === null) h += '<div class="muted">Loading projects…</div>';
  else if (!rows.length && !invalid.length) h += '<div class="muted">No projects are configured.</div>';
  else if (rows.length) {
    h += '<div class="project-table"><table><thead><tr><th>Key</th><th>Repository</th><th>Status</th><th>Priority</th><th>Tickets</th><th>Checks</th><th>Test services</th><th>Actions</th></tr></thead><tbody>';
    rows.forEach(function (p) {
      h += '<tr><td class="mono">' + esc(p.key) + '</td><td>' + esc((p.repositories || [p.repo]).join(', ')) + '</td><td><span class="pill" data-tone="' + (p.status === 'active' ? 'ok' : 'warn') + '">' + esc(p.status) + '</span></td>';
      h += '<td class="mono">' + esc(p.priority) + '</td><td class="mono">' + esc(p.ticket_count) + '</td><td class="project-check-state">' + projectChecksCell(p.checks) + '</td><td>' + projectServicesCell(p) + '</td><td><div class="project-actions">';
      if (consoleCan('projects.manage')) {
        h += '<button class="btn sm" data-act="project-repositories" data-key="' + esc(p.key) + '">Repositories</button>';
        h += '<button class="btn sm" data-act="project-environment" data-key="' + esc(p.key) + '">Environment</button>';
        h += '<button class="btn sm" data-act="project-pause" data-key="' + esc(p.key) + '" data-paused="' + (p.status !== 'paused') + '">' + (p.status === 'paused' ? 'Resume' : 'Pause') + '</button>';
        h += '<button class="btn sm danger" data-act="project-remove" data-key="' + esc(p.key) + '">Remove</button>';
      }
      h += '</div></td></tr>';
    });
    h += '</tbody></table></div>';
  }
  if (!consoleCan('projects.manage')) h += '<div class="muted">Only an authenticated user with project management permission can add, pause, resume or remove projects.</div>';
  h += '</section>';
  if (state.showForm && consoleCan('projects.manage')) {
    var form = state.form, catalog = state.catalog || {}, jiraProjects = catalog.jira_projects || [], repos = catalog.github_repos || [];
    h += '<section class="panel"><div class="panel-h"><h2>Add project</h2><span class="hint">Preflight runs before any control repo change.</span></div>';
    if (state.error) h += '<div class="stakeholder-error">' + esc(state.error) + '</div>';
    if (state.busy && !catalog.jira_sites) h += '<div class="muted">Loading connected Jira projects and GitHub repositories…</div>';
    if (catalog.jira_sites) {
      h += '<form class="project-form" onsubmit="return false">';
      h += '<label>Connected Jira site<select data-project-field="site">' + catalog.jira_sites.map(function (site) { return '<option value="' + esc(site) + '"' + (form.site === site ? ' selected' : '') + '>' + esc(site) + '</option>'; }).join('') + '</select></label>';
      h += '<label>Jira project key or URL<input data-project-field="project" list="jira-project-options" value="' + esc(form.project) + '" placeholder="ABC or Jira project URL"><datalist id="jira-project-options">' + jiraProjects.map(function (p) { return '<option value="' + esc(p.key) + '" label="' + esc(p.name) + '"></option>'; }).join('') + '</datalist></label>';
      h += '<label>GitHub repository<select data-project-field="repo">' + repos.map(function (r) { return '<option value="' + esc(r.full_name) + '"' + (form.repo === r.full_name ? ' selected' : '') + '>' + esc(r.full_name) + '</option>'; }).join('') + '</select></label>';
      h += '<div class="wide project-actions"><button class="btn sm" data-act="project-catalog-refresh"' + (state.busy ? ' disabled' : '') + '>Refresh repositories</button>';
      h += '<a class="btn sm" href="/v1/integrations/github/installation-settings" target="_blank" rel="noopener">Add repository on GitHub</a></div>';
      h += '<label>Base branch<input data-project-field="base" value="' + esc(form.base || 'develop') + '"></label>';
      h += '<label>Stack<select data-project-field="stack"><option value="">No stack</option>' + (catalog.stacks || []).map(function (stack) { return '<option value="' + esc(stack) + '"' + (form.stack === stack ? ' selected' : '') + '>' + esc(stack) + '</option>'; }).join('') + '</select></label>';
      h += '<label>Test services (postgres, redis)<input data-project-field="services" value="' + esc(form.services) + '" placeholder="postgres, redis"></label>';
      h += '<label>Checks need Docker containers<select data-project-field="containers"><option value="false"' + (!form.containers ? ' selected' : '') + '>No</option><option value="true"' + (form.containers ? ' selected' : '') + '>Yes · require the safe rootless runtime</option></select></label>';
      h += '<label>Priority (lower runs first)<input type="number" min="0" max="100" data-project-field="priority" value="' + esc(form.priority) + '"></label>';
      h += '<label>Parallel limit<input type="number" min="1" max="100" data-project-field="parallel_limit" value="' + esc(form.parallel_limit) + '"></label>';
      h += '<label>Merge method<select data-project-field="merge_method">' + ['squash', 'merge', 'rebase'].map(function (method) { return '<option value="' + method + '"' + (form.merge_method === method ? ' selected' : '') + '>' + method + '</option>'; }).join('') + '</select></label>';
      h += '<label class="wide">Checks, one command per line<textarea data-project-field="checks" placeholder="composer install&#10;vendor/bin/phpunit">' + esc(form.checks) + '</textarea></label></form>';
      if (state.preflight) {
        var pf = state.preflight;
        h += '<div class="project-checks"><strong>Preflight result</strong>';
        h += pf.missing_statuses.length ? '<div class="bad">Missing Jira workflow statuses:</div><ul class="bad">' + pf.missing_statuses.map(function (status) { return '<li>' + esc(status) + '</li>'; }).join('') + '</ul>' : '<div class="good">Jira workflow has all required statuses.</div>';
        h += pf.branch_exists ? '<div class="good">Base branch exists in the selected repository.</div>' : '<div class="bad">Base branch is missing from the selected repository.</div>';
        h += '<strong>Node toolchains</strong>';
        if (!pf.nodes.length) h += '<div class="muted">No nodes have reported a heartbeat yet.</div>';
        pf.nodes.forEach(function (n) { h += '<div class="' + (n.ready ? 'good' : 'bad') + '">' + esc(n.name) + ': ' + (n.ready ? 'ready' : 'missing ' + esc(n.missing_tools.join(', '))) + '</div>'; });
        h += '</div>';
      }
      h += '<div class="project-actions"><button class="btn" data-act="project-preflight"' + (state.busy ? ' disabled' : '') + '>Run preflight</button>';
      h += '<button class="btn primary" data-act="project-save"' + (!state.preflight || !state.preflight.ok || state.busy ? ' disabled' : '') + '>Save project</button>';
      h += '<button class="btn" data-act="project-add-cancel">Cancel</button></div>';
    }
    h += '</section>';
  }
  var editor = state.repositoryEditor;
  if (editor) {
    h += '<section class="panel"><div class="panel-h"><h2>Repositories · ' + esc(editor.key) + '</h2><span class="hint">Map Jira epics to repositories reachable through GitHub connections.</span><span class="spacer"></span><button class="btn sm" data-act="project-repositories-close">Close</button></div>';
    if (editor.error) h += '<div class="stakeholder-error">' + esc(editor.error) + '</div>';
    if (editor.busy || !editor.data) h += '<div class="muted">Loading Jira epics and GitHub repositories…</div>';
    else {
      var data = editor.data, options = data.github_repos || [], configured = data.repositories || [], defaults = data.project_defaults || {};
      h += '<p class="muted">Project default: <span class="mono">' + esc(data.default_repo) + '</span>. Unmapped tickets use this repository.</p>';
      h += '<div class="project-table"><table><thead><tr><th>Epic</th><th>Jira status</th><th>Repository</th></tr></thead><tbody>';
      (data.epics || []).forEach(function (epic) {
        var selected = (data.epic_repositories || {})[epic.key] || '';
        h += '<tr><td><span class="mono">' + esc(epic.key) + '</span> ' + esc(epic.summary) + '</td><td>' + esc(epic.status || '—') + '</td><td><select data-epic-repository="' + esc(epic.key) + '"><option value="">Use project default · ' + esc(data.default_repo) + '</option>';
        options.forEach(function (r) { h += '<option value="' + esc(r.full_name) + '"' + (selected === r.full_name ? ' selected' : '') + '>' + esc(r.full_name) + '</option>'; });
        h += '</select></td></tr>';
      });
      if (!(data.epics || []).length) h += '<tr><td colspan="3" class="muted">No Jira epics found in this project.</td></tr>';
      h += '</tbody></table></div>';
      h += '<h3>Repository settings</h3><p class="muted">Blank values inherit the project defaults: base ' + esc(defaults.base || 'develop') + ', stack ' + esc(defaults.stack || 'none') + ', containers ' + (defaults.containers ? 'enabled' : 'disabled') + ', checks ' + esc((defaults.checks || []).join(', ') || 'not configured') + '.</p>';
      configured.forEach(function (r) {
        h += '<section class="project-checks"><strong class="mono">' + esc(r.repo) + (r.repo === data.default_repo ? ' · default' : '') + '</strong>';
        var reach = r.connections || [], conns = data.connections || [];
        if (r.connection_stale) h += '<div class="bad">Previously assigned to <span class="mono">' + esc(r.assigned_connection) + '</span>, which can no longer reach this repository. The available connection is selected below.</div>';
        h += '<div class="project-form"><label>GitHub connection<select data-repository-config="connection" data-repo="' + esc(r.repo) + '">';
        if (!reach.length) h += '<option value="">No connection can reach this repository</option>';
        conns.filter(function (c) { return reach.indexOf(c.id) >= 0; }).forEach(function (c) {
          h += '<option value="' + esc(c.id) + '"' + (r.connection === c.id ? ' selected' : '') + '>' + esc(c.label) + ' · ' + esc(c.account) + ' · ' + (c.kind === 'app' ? 'GitHub App' : 'access token') + '</option>';
        });
        h += '</select></label>';
        h += '<label>Base branch<input data-repository-config="base" data-repo="' + esc(r.repo) + '" value="' + esc(r.base || '') + '" placeholder="Inherit ' + esc(defaults.base || 'develop') + '"></label>';
        h += '<label>Stack<select data-repository-config="stack" data-repo="' + esc(r.repo) + '"><option value="">Inherit project stack</option>' + (data.stacks || []).map(function (s) { return '<option value="' + esc(s) + '"' + (r.stack === s ? ' selected' : '') + '>' + esc(s) + '</option>'; }).join('') + '</select></label>';
        h += '<label>Container runtime<select data-repository-config="containers" data-repo="' + esc(r.repo) + '"><option value="inherit"' + (r.containers === null || r.containers === undefined ? ' selected' : '') + '>Inherit project setting</option><option value="true"' + (r.containers === true ? ' selected' : '') + '>Require safe runtime</option><option value="false"' + (r.containers === false ? ' selected' : '') + '>Not required</option></select></label>';
        h += '<label class="wide">Checks, one command per line<textarea data-repository-config="checks" data-repo="' + esc(r.repo) + '" placeholder="Inherit project checks">' + esc((r.checks || []).join('\n')) + '</textarea></label></div></section>';
      });
      h += '<div class="project-actions"><select data-repository-add><option value="">Add a repository…</option>' + options.filter(function (r) { return !configured.some(function (c) { return c.repo === r.full_name; }); }).map(function (r) { return '<option value="' + esc(r.full_name) + '">' + esc(r.full_name) + '</option>'; }).join('') + '</select><button class="btn sm" data-act="project-repository-add">Add repository</button><span class="spacer"></span><button class="btn primary" data-act="project-repositories-save"' + (editor.saving ? ' disabled' : '') + '>Save repository settings</button></div>';
    }
    h += '</section>';
  }
  if (consoleCan('projects.manage')) h += projectEnvironmentPanels(state);
  root.innerHTML = h;
}

function renderSupport() {
  var root = $('#view-support'); if (!root) return;
  if (!consoleCan('console.manage')) {
    root.innerHTML = '<section class="panel"><div class="panel-h"><h2>Support</h2></div><p class="hint">Owner or Super Administrator access is required to build a support bundle.</p></section>';
    return;
  }
  var support = S.support, tickets = D().tickets || [];
  var h = '<div class="dhead"><div><div class="eyebrow">Owner tools</div><h1>Support</h1><p class="hint">Build a private diagnostic bundle, review its inventory, then download it to inspect before sharing.</p></div></div>';
  h += '<section class="panel"><div class="panel-h"><h2>Build a bundle</h2><span class="hint">Logs are capped at 5 MB per file.</span></div>';
  h += '<div class="project-form"><label>Ticket (optional)<select id="support-ticket"><option value="">All recent incidents and control logs</option>';
  h += tickets.filter(function (ticket) { return ticket.type !== 'Epic'; }).map(function (ticket) {
    return '<option value="' + esc(ticket.key) + '"' + (support.ticketKey === ticket.key ? ' selected' : '') + '>' + esc(ticket.key + ' · ' + ticket.summary) + '</option>';
  }).join('');
  h += '</select></label><div class="wide project-actions"><button class="btn primary" data-act="support-build"' + (support.busy ? ' disabled' : '') + '>' + (support.busy ? 'Scanning bundle…' : 'Build support bundle') + '</button>';
  h += '<span class="hint">Includes versions, safe settings, machine and account states, up to 100 incidents from 7 days, and control service logs from 24 hours.</span></div></div>';
  if (support.error) h += '<p class="error" role="alert">' + esc(support.error) + '</p>';
  h += '</section>';
  if (support.bundle) {
    var inv = support.bundle.inventory || {};
    h += '<section class="panel" aria-labelledby="support-inventory"><div class="panel-h"><h2 id="support-inventory">Review bundle inventory</h2><span class="badge ok">Sensitive data scan passed</span></div>';
    h += '<p class="hint">Created ' + esc(when(inv.created_at)) + '. Review these files before you share the downloaded bundle.</p><ul class="plain-list">';
    h += (inv.files || []).map(function (file) { return '<li><span class="mono">' + esc(file.name) + '</span><span class="hint">' + Number(file.bytes || 0).toLocaleString() + ' bytes</span></li>'; }).join('');
    h += '</ul><div class="project-actions"><a class="btn primary" href="' + esc(safeHref(support.bundle.download_url)) + '">Download reviewed bundle</a><button class="btn" data-act="support-clear">Build another</button><span class="hint">The download stays available for 10 minutes.</span></div></section>';
  }
  root.innerHTML = h;
}

function buildSupportBundle() {
  if (S.support.busy || !consoleCan('console.manage')) return;
  var select = $('#support-ticket');
  S.support.ticketKey = select ? select.value : S.support.ticketKey;
  S.support.busy = true; S.support.error = ''; S.support.bundle = null; renderSupport();
  fetch('/v1/support/bundles', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ticket_key: S.support.ticketKey }) })
    .then(function (r) { return r.json().then(function (d) { if (!r.ok) throw new Error(d.detail || ('HTTP ' + r.status)); return d; }); })
    .then(function (d) { S.support.bundle = d; S.support.error = ''; })
    .catch(function (e) { S.support.error = String(e.message || e); })
    .then(function () { S.support.busy = false; renderSupport(); });
}

function licensingResponse(response) {
  return response.json().then(function (data) {
    if (response.ok) return data;
    var detail = data.detail || ('HTTP ' + response.status);
    throw new Error(typeof detail === 'string' ? detail : (detail.message || JSON.stringify(detail)));
  });
}
function loadLicensing() {
  if (S.licensing.busy || !consoleCan('licensing.manage')) return;
  S.licensing.busy = true;
  Promise.all([
    fetch('/v1/licensing', { cache: 'no-store' }).then(licensingResponse),
    fetch('/v1/licensing/usage', { cache: 'no-store' }).then(licensingResponse)
  ]).then(function (results) {
    S.licensing.data = results[0]; S.licensing.usage = results[1].rows || [];
    S.licensing.status = results[0]; S.licensing.statusAt = Date.now();
    S.licensing.limitsPlan = S.licensing.limitsPlan || results[0].plan || 'free';
    S.licensing.loadedAt = Date.now(); S.licensing.error = '';
  }).catch(function (error) { S.licensing.error = String(error.message || error); })
    .then(function () { S.licensing.busy = false; render(); });
}
function loadLicenseStatus() {
  if (S.licensing.statusBusy || !consoleReader()) return;
  S.licensing.statusBusy = true;
  fetch('/v1/licensing/status', { cache: 'no-store' }).then(licensingResponse)
    .then(function (state) { S.licensing.status = state; S.licensing.statusAt = Date.now(); })
    .catch(function () { /* the page can still render without a status warning */ })
    .then(function () { S.licensing.statusBusy = false; renderLicenseNotice(); });
}
function renderLicenseNotice() {
  var el = $('#license-warning'), state = S.licensing.data || S.licensing.status;
  if (!el || !state || !consoleReader() || state.status === 'active') {
    if (el) el.hidden = true;
    return;
  }
  el.hidden = false;
  el.setAttribute('data-tone', state.read_only ? 'crit' : 'warn');
  if (state.read_only) el.textContent = 'AI Flow is read-only because the licence is ' + state.reason + '. Activate or renew a licence on the Licence screen to restore normal operation.';
  else el.textContent = 'Licence ' + state.reason + '. AI Flow remains available during the 14-day grace period until ' + (state.grace_until ? new Date(state.grace_until).toLocaleString() : 'the grace period ends') + '.';
}
function renderLicensing() {
  var root = $('#view-licensing');
  if (!root) return;
  if (!consoleCan('licensing.manage')) { root.innerHTML = '<div class="empty">The Licence screen is available to owners and super administrators.</div>'; return; }
  var state = S.licensing.data, h = '<div class="dhead"><div><h1>Licence</h1><p>Activate a plan, set resource limits and review monthly AI usage.</p></div><button class="btn sm" data-act="license-refresh">Refresh</button></div>';
  if (S.licensing.error) h += '<div class="alert" data-tone="crit"><span class="a-detail">' + esc(S.licensing.error) + '</span></div>';
  if (!state) {
    h += '<section class="panel"><div class="muted">' + (S.licensing.busy ? 'Loading licence…' : 'Licence status is unavailable.') + '</div></section>';
    root.innerHTML = h; return;
  }
  var statusLabel = state.licence_required === false ? 'No licence required' : (state.status === 'active' ? 'Active' : (state.status === 'grace' ? 'Grace period' : 'Read-only')); h += licensingNotice(state, esc);
  var reasonLabel = state.reason === 'expired' ? 'Expired' : (state.reason === 'invalid' ? 'Invalid signature or claims' : '');
  h += '<section class="panel"><div class="panel-h"><h2>' + esc(state.plan.charAt(0).toUpperCase() + state.plan.slice(1)) + ' plan</h2><span class="pill" data-tone="' + (state.status === 'active' ? 'ok' : (state.read_only ? 'crit' : 'warn')) + '">' + esc(statusLabel) + '</span><span class="spacer"></span><span class="muted">' + esc(reasonLabel) + '</span></div>';
  h += '<p class="muted">Expiry: <strong>' + esc(state.expires_at ? new Date(state.expires_at).toLocaleString() : 'No expiry · Free plan') + '</strong>';
  if (state.grace_until) h += ' · Grace period ends ' + esc(new Date(state.grace_until).toLocaleString());
  h += '</p><p class="small muted">Installation ID: <span class="mono">' + esc(state.installation_id) + '</span> · Share this ID with the licence issuer for an offline licence file.</p>';
  h += '<div class="project-table"><table><thead><tr><th>Included resource</th><th>Plan limit</th><th>In use</th></tr></thead><tbody>';
  [['machines', 'Machines'], ['ai_accounts', 'AI accounts'], ['projects', 'Projects'], ['users', 'Users']].forEach(function (item) {
    h += '<tr><td>' + item[1] + '</td><td>' + state.limits[item[0]] + '</td><td>' + ((state.usage || {})[item[0]] || 0) + '</td></tr>';
  });
  h += '</tbody></table></div></section>';
  (state.sync_warnings || []).forEach(function (warning) {
    h += '<div class="alert" data-tone="warn"><span class="a-detail">' + esc(warning) + '</span></div>';
  });
  h += '<section class="panel"><div class="panel-h"><h2>Activate or renew</h2><span class="hint">The key is sent only to the configured licence service.</span></div>';
  h += '<div class="project-form"><label>Licence key<input type="password" autocomplete="new-password" data-license-key placeholder="Enter licence key"></label><div><button class="btn primary" data-act="license-activate"' + (S.licensing.busy ? ' disabled' : '') + '>Activate online</button></div>';
  h += '<label class="wide">Signed licence file<input type="file" data-license-file accept=".license,.licence,.jwt,text/plain,application/jwt"></label><div><button class="btn" data-act="license-offline"' + (S.licensing.busy ? ' disabled' : '') + '>Activate offline</button></div></div></section>';
  var plan = S.licensing.limitsPlan || state.plan, selectedLimits = state.plans[plan] || state.limits;
  h += '<section class="panel"><div class="panel-h"><h2>Plan limits</h2><span class="hint">Adjust the limits used by this installation.</span></div><div class="project-form"><label>Plan<select data-license-plan><option value="free"' + (plan === 'free' ? ' selected' : '') + '>Free</option><option value="team"' + (plan === 'team' ? ' selected' : '') + '>Team</option><option value="business"' + (plan === 'business' ? ' selected' : '') + '>Business</option></select></label>';
  [['machines', 'Machines'], ['ai_accounts', 'AI accounts'], ['projects', 'Projects'], ['users', 'Users']].forEach(function (item) {
    h += '<label>' + item[1] + '<input type="number" min="1" max="1000000" data-license-limit="' + item[0] + '" value="' + selectedLimits[item[0]] + '"></label>';
  });
  h += '<div><button class="btn primary" data-act="license-save-limits"' + (S.licensing.busy ? ' disabled' : '') + '>Save plan limits</button></div></div></section>';
  h += '<section class="panel"><div class="panel-h"><h2>Usage report</h2><span class="hint">Tickets use their UTC Done month; tokens and cost use UTC run start month.</span><span class="spacer"></span><a class="btn sm" href="/v1/licensing/usage.csv">Export spreadsheet</a></div><div class="project-table"><table><thead><tr><th>Project</th><th>Month</th><th>Tickets finished</th><th>Input tokens</th><th>Output tokens</th><th>Cost (USD)</th></tr></thead><tbody>';
  if (!S.licensing.usage.length) h += '<tr><td colspan="6" class="muted">No usage has been recorded yet.</td></tr>';
  S.licensing.usage.forEach(function (row) { h += '<tr><td>' + esc(row.project) + '</td><td>' + esc(row.month) + '</td><td>' + row.tickets_finished + '</td><td>' + Number(row.tokens_in).toLocaleString() + '</td><td>' + Number(row.tokens_out).toLocaleString() + '</td><td>$' + Number(row.cost_usd).toFixed(6) + '</td></tr>'; });
  h += '</tbody></table></div></section>';
  root.innerHTML = h;
}
function submitLicense(path, payload, successText) {
  S.licensing.busy = true; S.licensing.error = ''; renderLicensing();
  fetch(path, { method: path === '/v1/licensing/limits' ? 'PUT' : 'POST',
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) })
    .then(licensingResponse).then(function (data) {
      S.licensing.data = data; toast(successText, 'Licence settings saved.', 'ok');
    }).catch(function (error) { S.licensing.error = String(error.message || error); })
    .then(function () { S.licensing.busy = false; if (!S.licensing.error) loadLicensing(); else renderLicensing(); });
}

function notificationField(field, label, type, placeholder) {
  var value = S.notifications.form[field];
  return '<label>' + esc(label) + '<input type="' + esc(type || 'text') + '" data-notify-field="' + esc(field) + '" value="' +
    esc(value || '') + '" placeholder="' + esc(placeholder || '') + '" autocomplete="' +
    (type === 'password' ? 'new-password' : 'off') + '"></label>';
}
function renderNotifications() {
  var root = $('#view-notifications'); if (!root) return;
  var state = S.notifications, data = state.data || {}, channels = data.channels || [], prefs = data.preferences || { routes: {} };
  prefs = state.preferencesDraft || prefs;
  if (!consoleCan('notifications.manage')) { root.innerHTML = '<section class="panel"><p>Notifications are available to administrators.</p></section>'; return; }
  var kindLabels = { email: 'Email', telegram: 'Telegram', slack: 'Slack', teams: 'Microsoft Teams', webhook: 'Generic webhook' };
  var h = '<section class="panel"><div class="panel-h"><h2>Notifications</h2><button class="btn sm" data-act="notifications-refresh"' +
    (state.busy ? ' disabled' : '') + '>Refresh</button></div><p class="muted">Configure stored delivery channels, choose which alerts use them, and review recent results. Saved credentials stay hidden.</p>';
  if (state.error) h += '<div class="alert"><span class="a-detail">' + esc(state.error) + '</span></div>';
  if (state.notice) h += '<div class="notify-state">' + esc(state.notice) + '</div>';
  h += '<h3>Channels</h3><div class="notify-grid">';
  channels.forEach(function (c) {
    var last = c.last_delivery || {}, destination = '';
    if (c.kind === 'email') destination = (c.config.from || '') + ' → ' + (c.config.to || '') + ' · ' + (c.config.host || '') + ':' + (c.config.port || '');
    else if (c.kind === 'telegram') destination = 'Chat ' + (c.config.chat_id || '');
    else destination = 'Endpoint saved and hidden';
    var secretCount = Object.keys(c.secrets || {}).filter(function (k) { return c.secrets[k]; }).length;
    h += '<article class="notify-channel"><div class="panel-h"><h3>' + esc(c.name) + '</h3><span class="pill" data-tone="' +
      (c.enabled ? 'ok' : 'muted') + '">' + (c.enabled ? 'Enabled' : 'Disabled') + '</span></div>' +
      '<div class="notify-meta">' + esc(kindLabels[c.kind] || c.kind) + ' · ' + esc(destination) + '<br>Credentials: ' +
      (secretCount ? 'saved, hidden' : 'not set') + '<br>Last delivery: ' + esc(last.status || 'never') +
      (last.at ? ' · ' + esc(when(last.at)) : '') + (last.error ? '<br>Result: ' + esc(last.error) : '') + '</div>' +
      '<div class="notify-actions"><button class="btn sm" data-act="notification-test" data-id="' + esc(c.id) + '">Send test message</button>' +
      '<button class="btn sm" data-act="notification-edit" data-id="' + esc(c.id) + '">Edit</button>' +
      '<button class="btn sm" data-act="notification-toggle" data-id="' + esc(c.id) + '">' + (c.enabled ? 'Disable' : 'Enable') + '</button>' +
      '<button class="btn sm" data-act="notification-delete" data-id="' + esc(c.id) + '">Remove</button></div></article>';
  });
  if (!channels.length) h += '<div class="empty">No alert channels configured yet.</div>';
  h += '</div><h3>' + (state.editId ? 'Edit channel' : 'Add a channel') + '</h3><form id="notification-channel-form" class="panel">' +
    '<div class="notify-form-grid">' + notificationField('name', 'Channel name', 'text', 'Operations email') +
    '<label>Channel kind<select id="notification-kind" data-notify-kind-select' + (state.editId ? ' disabled' : '') + '>' + Object.keys(kindLabels).map(function (k) {
      return '<option value="' + k + '"' + (state.form.kind === k ? ' selected' : '') + '>' + esc(kindLabels[k]) + '</option>';
    }).join('') + '</select></label>';
  var selectedKind = state.form.kind || 'email';
  var fieldHtml = {
    email: notificationField('host', 'SMTP or hosted sender host', 'text', 'smtp.example.com') +
      notificationField('port', 'Port', 'number', '587') + notificationField('username', 'Username (optional)', 'text', '') +
      notificationField('password', 'Password (leave blank to keep saved password)', 'password', 'Hidden after saving') +
      notificationField('from', 'From address', 'email', 'aiflow@example.com') + notificationField('to', 'To address', 'email', 'owner@example.com') +
      '<label>Connection security<select data-notify-field="tls_mode"><option value="starttls"' + (state.form.tls_mode !== 'ssl' && state.form.tls_mode !== 'none' ? ' selected' : '') + '>STARTTLS</option><option value="ssl"' + (state.form.tls_mode === 'ssl' ? ' selected' : '') + '>TLS (SSL)</option><option value="none"' + (state.form.tls_mode === 'none' ? ' selected' : '') + '>None</option></select></label>',
    telegram: notificationField('bot_token', 'Bot token', 'password', 'Hidden after saving') + notificationField('chat_id', 'Private Telegram chat ID', 'text', 'Your numeric chat ID') +
      '<label><span>Jira project for brainstorms</span><select data-notify-field="project_key"><option value="">Choose a configured project</option>' +
      (data.projects || []).map(function (p) { return '<option value="' + esc(p.key) + '"' + (state.form.project_key === p.key ? ' selected' : '') + '>' + esc(p.key) + '</option>'; }).join('') +
      '</select></label><label class="checkline"><input type="checkbox" data-notify-checkbox="ba_chat_enabled"' +
      (state.form.ba_chat_enabled ? ' checked' : '') + '> Enable BA brainstorming chat</label>' +
      '<div class="notify-meta">Use the bot in a private chat. The BA AI asks questions, sends a draft proposal, and creates Jira tickets only after approval in chat.</div>',
    slack: notificationField('webhook_url', 'Slack incoming webhook URL', 'password', 'Hidden after saving'),
    teams: notificationField('webhook_url', 'Microsoft Teams webhook URL', 'password', 'Hidden after saving'),
    webhook: notificationField('url', 'Webhook URL', 'password', 'Hidden after saving') + notificationField('auth_token', 'Bearer token (optional)', 'password', 'Hidden after saving')
  };
  h += fieldHtml[selectedKind] || fieldHtml.email;
  h += '</div><div class="notify-actions"><button class="btn primary" type="submit"' + (state.busy ? ' disabled' : '') + '>' +
    (state.editId ? 'Save channel' : 'Add channel') + '</button>' + (state.editId ? '<button class="btn" type="button" data-act="notification-cancel">Cancel</button>' : '') + '</div></form>';
  h += '<h3>Alert routing</h3><p class="muted">Quiet hours use UTC. Critical incidents still send during quiet hours so urgent failures reach an administrator.</p>';
  h += '<div class="notify-routing"><strong>Alert kind</strong>' + channels.map(function (c) { return '<strong>' + esc(c.name) + '</strong>'; }).join('') + '</div>';
  Object.keys(data.alert_kinds || {}).forEach(function (kind) {
    h += '<div class="notify-routing"><span>' + esc(data.alert_kinds[kind]) + '</span>' + channels.map(function (c) {
      var checked = ((prefs.routes || {})[kind] || []).indexOf(c.id) >= 0;
      return '<label><input type="checkbox" data-route-kind="' + esc(kind) + '" data-route-channel="' + esc(c.id) + '"' +
        (checked ? ' checked' : '') + (!c.enabled ? ' disabled' : '') + '></label>';
    }).join('') + '</div>';
  });
  if (!channels.length) h += '<div class="empty">Add a channel before setting alert routes.</div>';
  h += '<div class="notify-actions"><label>Quiet hours start (UTC) <input type="time" id="notification-quiet-start" value="' + esc(prefs.quiet_start || '') + '"></label>' +
    '<label>Quiet hours end (UTC) <input type="time" id="notification-quiet-end" value="' + esc(prefs.quiet_end || '') + '"></label>' +
    '<button class="btn primary" data-act="notification-preferences-save">Save routing</button></div></section>';
  h += '<section class="panel"><div class="panel-h"><h2>Recent deliveries</h2></div>';
  var deliveries = data.deliveries || [];
  if (!deliveries.length) h += '<div class="empty">No deliveries recorded.</div>';
  deliveries.slice(0, 15).forEach(function (d) {
    h += '<div class="alert"><span class="pill" data-tone="' + (d.status === 'sent' ? 'ok' : d.status === 'failed' ? 'crit' : 'warn') + '">' + esc(d.status) +
      '</span><span class="a-title">' + esc(d.channel_name) + ' · ' + esc(d.title) + '</span><span class="a-detail">' +
      esc(d.attempts + '/' + d.max_attempts + (d.last_error ? ' · ' + d.last_error : '')) + '</span></div>';
  });
  h += '</section><section class="panel"><div class="panel-h"><h2>Open delivery incidents</h2></div>';
  var incidents = data.incidents || [];
  if (!incidents.length) h += '<div class="empty">No open delivery incidents.</div>';
  incidents.forEach(function (i) { h += '<div class="alert"><span class="pill" data-tone="crit">Critical</span><span class="a-title">' +
    esc(i.title) + '</span><span class="a-detail">' + esc(i.detail) + '</span></div>'; });
  root.innerHTML = h + '</section>';
}
function notificationConfigFromForm(form) {
  var keys = { email: ['host', 'port', 'username', 'password', 'from', 'to', 'tls_mode'],
    telegram: ['bot_token', 'chat_id', 'ba_chat_enabled', 'project_key'], slack: ['webhook_url'], teams: ['webhook_url'], webhook: ['url', 'auth_token'] }[form.kind] || [];
  var config = {}; keys.forEach(function (key) { if (form[key] !== undefined) config[key] = form[key]; }); return config;
}
function notificationFormSubmit() {
  var state = S.notifications, form = state.form;
  if (!consoleCan('notifications.manage') || state.busy) return;
  state.busy = true; state.error = ''; renderNotifications();
  var payload = { name: form.name || '', kind: form.kind || 'email', enabled: form.enabled !== false,
    config: notificationConfigFromForm(form) };
  if (state.editId) payload.id = state.editId;
  fetch('/v1/notifications/channels', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) })
    .then(function (r) { return projectResponseError(r); })
    .then(function () { state.editId = null; state.notice = 'Channel saved. Credentials remain hidden after saving.';
      state.form = { kind: 'email', name: '', enabled: true }; loadNotifications(); })
    .catch(function (e) { state.error = String(e.message || e); })
    .then(function () { state.busy = false; renderNotifications(); });
}
function notificationPreferencesSubmit() {
  var state = S.notifications, data = state.data || {}, routes = {};
  Object.keys(data.alert_kinds || {}).forEach(function (kind) { routes[kind] = (state.preferencesDraft.routes || {})[kind] || []; });
  var start = state.preferencesDraft.quiet_start || '', end = state.preferencesDraft.quiet_end || '';
  fetch('/v1/notifications/preferences', { method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ routes: routes, quiet_start: start, quiet_end: end }) })
    .then(function (r) { return projectResponseError(r); })
    .then(function (d) { state.preferencesDraft = d.preferences; state.preferencesDirty = false; state.notice = 'Alert routing and quiet hours saved.'; state.error = ''; loadNotifications(); })
    .catch(function (e) { state.error = String(e.message || e); renderNotifications(); });
}
function notificationTest(id) {
  var state = S.notifications; state.error = ''; state.notice = 'Sending test message…'; renderNotifications();
  fetch('/v1/notifications/channels/' + encodeURIComponent(id) + '/test', { method: 'POST' })
    .then(function (r) { return projectResponseError(r); })
    .then(function (d) { state.notice = 'Test result: ' + d.result.status + (d.result.error ? ' (' + d.result.error + ')' : ''); loadNotifications(); })
    .catch(function (e) { state.error = String(e.message || e); state.notice = ''; renderNotifications(); });
}
function notificationDelete(id) {
  if (!window.confirm('Remove this channel and its saved credentials?')) return;
  var state = S.notifications;
  fetch('/v1/notifications/channels/' + encodeURIComponent(id), { method: 'DELETE' })
    .then(function (r) { return projectResponseError(r); })
    .then(function () { state.notice = 'Channel removed.'; loadNotifications(); })
    .catch(function (e) { state.error = String(e.message || e); renderNotifications(); });
}
function captureNotificationPreferences() {
  var state = S.notifications, data = state.data || {}, routes = {};
  Object.keys(data.alert_kinds || {}).forEach(function (kind) {
    routes[kind] = $$('[data-route-kind="' + kind + '"]:checked').map(function (box) { return Number(box.getAttribute('data-route-channel')); });
  });
  var start = $('#notification-quiet-start'), end = $('#notification-quiet-end');
  state.preferencesDraft = { routes: routes, quiet_start: start ? start.value : '', quiet_end: end ? end.value : '', timezone: 'UTC' };
  state.preferencesDirty = true;
}


function quickSearchResults(query) {
  var q = String(query || '').trim().toLowerCase(), role = S.consoleUser && S.consoleUser.role;
  var views = MENU_ITEMS.filter(function (item) { return viewAllowed(item.view, role) && (!q || item.label.toLowerCase().indexOf(q) >= 0); })
    .slice(0, 8).map(function (item) { return { kind: 'view', key: item.view, label: item.label, detail: 'View' }; });
  var projects = D().projects.filter(function (p) { return !q || (p.key + ' ' + p.repo).toLowerCase().indexOf(q) >= 0; });
  projects.sort(function (a, b) {
    var as = S.starredProjects.indexOf(a.key) >= 0, bs = S.starredProjects.indexOf(b.key) >= 0;
    return as !== bs ? (as ? -1 : 1) : a.key.localeCompare(b.key);
  });
  projects = projects.slice(0, q ? 6 : 4).map(function (p) { return { kind: 'project', key: p.key, label: p.key, detail: p.repo }; });
  var tickets = q ? D().tickets.filter(function (t) { return (t.key + ' ' + t.summary + ' ' + t.project).toLowerCase().indexOf(q) >= 0; })
    .slice(0, 8).map(function (t) { return { kind: 'ticket', key: t.key, label: t.key, detail: t.summary }; }) : [];
  return views.concat(projects, tickets);
}
function renderCommandSearchResults(query) {
  var root = $('#command-results'); if (!root) return;
  var rows = quickSearchResults(query);
  root.innerHTML = rows.length ? rows.map(function (row) {
    return '<button class="command-result" type="button" role="option" data-act="search-result" data-kind="' + esc(row.kind) + '" data-key="' + esc(row.key) + '"><strong>' + esc(row.label) + '</strong><span>' + esc(row.detail) + '</span><span class="kind">' + esc(row.kind) + '</span></button>';
  }).join('') : '<div class="empty">No matching views, projects, or tickets.</div>';
}
function openCommandSearch() {
  var root = $('#command-search'); if (!root) return;
  S.commandSearchOpen = true; root.hidden = false;
  var input = $('#command-query'); if (input) { input.value = ''; renderCommandSearchResults(''); input.focus(); }
}
function closeCommandSearch() {
  var root = $('#command-search'); if (!root) return;
  S.commandSearchOpen = false; root.hidden = true;
  var opener = $('[data-act="search-open"]'); if (opener) opener.focus();
}
function openEpicOnWorkBoard(key) {
  S.epic = key; S.search = ''; S.aiFilter = 'all'; savePrefs(); setView('work');
}
function chooseSearchResult(kind, key, source) {
  closeCommandSearch();
  if (kind === 'view') { setView(key); return; }
  if (kind === 'project') {
    var alreadyWork = S.view === 'work';
    S.project = key; S.epic = 'all'; S.search = ''; S.projectQuery = ''; S.projectPickerOpen = false; savePrefs(); setView('work');
    if (alreadyWork) { render(); var picker = $('#work-project'); if (picker) picker.focus(); }
    return;
  }
  if (kind === 'ticket') {
    setView('work'); openDrawer('ticket', key, source); return;
  }
}

function render() {
  if (S.layouts.drag) return;
  renderLive();
  renderFooter();
  renderLicenseNotice();
  if (S.view === 'cc') renderDashboard();
  else if (S.view === 'work' || S.view === 'delivery') { renderDHead(); renderDBody(); }
  else if (S.view === 'sa') renderSA();
  else if (S.view === 'users') renderUsers();
  else if (S.view === 'security') renderSecurity();
  else if (S.view === 'lead') renderLead();
  else if (S.view === 'business') renderBusiness();
  else if (S.view === 'review') renderReviews();
  else if (S.view === 'stakeholder') renderStakeholder();
  else if (S.view === 'machines') renderMachines();
  else if (S.view === 'knowledge') renderKnowledge();
  else if (S.view === 'backup') renderBackup();
  else if (S.view === 'integrations') renderIntegrations();
  else if (S.view === 'projects') renderProjects();
  else if (S.view === 'licensing') renderLicensing();
  else if (S.view === 'notifications') renderNotifications();
  else if (S.view === 'settings') renderRuntimeSettings();
  else if (S.view === 'support') renderSupport();
  else { renderDHead(); renderDBody(); }
  if (S.drawer) { if (S.drawer.kind === 'ticket' && S.data) loadTicket(S.drawer.id); renderDrawer(); }
  tick();
}
function tick() {
  var s = since();
  $$('[data-since]').forEach(function (el) {
    var v = el.getAttribute('data-since'); if (v === '') { el.textContent = 'never'; return; }
    var sec = Number(v) + s;
    el.textContent = el.getAttribute('data-fmt') === 'ago' ? ago(sec) : mmss(sec);
  });
  $$('[data-clock]').forEach(function (el) {
    var sec = Number(el.getAttribute('data-clock')) + s, hard = Number(el.getAttribute('data-hard')) || 3600, p = Math.min(100, 100 * sec / hard);
    el.style.width = p.toFixed(1) + '%'; el.setAttribute('data-tone', p >= 75 ? 'crit' : p >= 50 ? 'warn' : 'ok');
  });
  $$('[data-countdown]').forEach(function (el) {
    var left = Math.max(0, Math.ceil((new Date(el.getAttribute('data-countdown')).getTime() - Date.now()) / 1000));
    el.textContent = left ? mmss(left) + ' remaining' : 'Expired';
  });
  renderLive();
}
/* Help: every screen has a page in the documentation site (docs/guide/screens/<view>.md, served at /help/) and a
   short hint for first use that stays away once dismissed. */
var HELP_BASE = '/help/screens/';
var HINTS = {
  cc: 'Start here: nodes, AI accounts, runs and incidents. Open an incident for its detail and a runbook.',
  work: 'Pick a project to see its tickets by state. Open a ticket for its runs, questions and links.',
  machines: 'Machines that run AI work and the AI accounts on them: heartbeat, slots and sign-in state.',
  users: 'See the role and screen permissions of each person. Add people, change roles and project limits, or disable a person here; every change is audited.',
  delivery: 'Pick a project to see its board. "Needs attention" lists tickets that wait on a person or did not finish.',
  sa: 'Read-only health of nodes and accounts, with advice on capacity. Nothing here changes the fleet.',
  lead: 'Choose a project to check that each epic has enough tasks for its acceptance criteria, then export a report.',
  business: 'Epics, questions waiting for your answer and change requests. Answer questions in Jira.',
  review: 'Tickets waiting for a person before merge. Enter the reviewer access token to approve or send back.',
  stakeholder: 'Open the test environment of a project, try the change, then record acceptance or feedback.',
  knowledge: 'Study cases, skills, guides and policies the AI workers read. Every save creates a new version.',
  backup: 'Review backup and weekly restore status, set a schedule and destination, and save the encryption key separately.',
  integrations: 'Connect Jira and GitHub first. Secrets are stored on the control machine and never shown.',
  projects: 'Add a Jira project, then check the preflight. Pause or remove a project here.',
  licensing: 'Activate a licence key online or from a signed file, check plan limits and usage, and export the usage report.',
  support: 'Build a diagnostic bundle with one button, read its inventory, then download it to inspect before you send it. Secrets and personal data are removed.',
  notifications: 'Set up alert channels and routing, then review recent delivery results. Saved credentials stay hidden.',
  settings: 'Review write-only runtime keys, see affected services, and save a value with a safe restart.'
};
function helpUrl(view) { return HELP_BASE + view + '.html'; }
function hintSeen(view) { try { return localStorage.getItem('aiflow4-hint-' + view) === '1'; } catch (e) { return false; } }
function renderHelp() {
  var link = document.getElementById('help-link');
  if (link) link.setAttribute('href', helpUrl(S.view));
  var box = document.getElementById('firstuse');
  if (!box) return;
  box.innerHTML = (HINTS[S.view] && !hintSeen(S.view))
    ? '<div class="firstuse" role="note"><span>' + esc(HINTS[S.view]) + ' <a href="' + esc(helpUrl(S.view)) + '" target="_blank" rel="noopener">Read more in Help</a></span><button class="btn sm" data-act="hint-dismiss">Got it</button></div>'
    : '';
}
function showView() {
  $$('.tab').forEach(function (t) { t.setAttribute('aria-selected', String(t.getAttribute('data-view') === S.view)); });
  ['cc', 'work', 'delivery', 'sa', 'lead', 'business', 'review', 'stakeholder', 'machines', 'users', 'knowledge', 'backup', 'integrations', 'projects', 'licensing', 'notifications', 'settings', 'support'].forEach(function (v) {
    var panel = $('#view-' + v); if (panel) panel.hidden = S.view !== v;
  });
  renderHelp();
}
function setView(v) {
  if (S.consoleUser && !viewAllowed(v)) {
    toast('View unavailable', 'This view is not available for your role.', 'warn'); return;
  }
  if (S.view === v) return;
  S.view = v;
  S.layouts.mode = '';
  $$('.tab').forEach(function (t) { t.setAttribute('aria-selected', String(t.getAttribute('data-view') === v)); });
  showView();
  var viewPath = VIEW_PATH[v] || '/cc';
  if (v === 'work' && S.workStatusFilter && S.workStatusFilter !== 'all') viewPath += '?status=' + encodeURIComponent(S.workStatusFilter);
  try { history.pushState(null, '', viewPath); } catch (e) { /* file preview */ }
  render();
  if (v === 'cc' || v === 'work' || v === 'delivery') loadDashboardWaiting(true);
  if (v === 'support') renderSupport();
  if (v === 'review') loadReviews();
  if (v === 'knowledge') loadKnowledge(true);
  if (v === 'backup') loadBackups(true);
  if (v === 'integrations') loadIntegrations();
  if (v === 'projects') loadProjectList();
  if (v === 'security') loadSecurity();
  if (v === 'licensing') loadLicensing();
  if (v === 'users') loadUsers();
  if (v === 'machines') pollServices();
  if (v === 'notifications') loadNotifications();
  if (v === 'settings') loadRuntimeSettings();
}
function flash(id) { var el = document.getElementById(id); if (!el) return; el.scrollIntoView({ behavior: reduceMotion ? 'auto' : 'smooth', block: 'start' }); el.classList.remove('flash-target'); void el.offsetWidth; el.classList.add('flash-target'); }

