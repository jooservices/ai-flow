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
