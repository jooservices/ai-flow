function projectRequestBody() {
  var f = S.projectAdmin.form;
  return { site: f.site, project: f.project, repo: f.repo, base: f.base || 'develop', stack: f.stack || '',
    services: servicesFromText(f.services),
    containers: Boolean(f.containers),
    checks: String(f.checks || '').split(/\r?\n/).map(function (x) { return x.trim(); }).filter(Boolean),
    priority: Number(f.priority), parallel_limit: Number(f.parallel_limit), merge_method: f.merge_method };
}
function servicesFromText(text) {
  return String(text || '').split(/[\s,]+/).map(function (x) { return x.trim().toLowerCase(); }).filter(Boolean);
}
function saveProjectServices(key, button) {
  var input = document.querySelector('[data-project-services="' + key + '"]');
  if (!input) return;
  if (button) button.disabled = true;
  fetch('/v1/projects/' + encodeURIComponent(key) + '/services', { method: 'POST',
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ services: servicesFromText(input.value) }) })
    .then(function (r) { return projectResponseError(r); })
    .then(function (d) { toast('Test services saved', d.project + ': ' + ((d.services || []).join(', ') || 'none') + '. Every machine of the project prepares them within two minutes.', 'ok'); loadProjectList(); poll(); })
    .catch(function (e) { toast('Could not save test services', String(e.message || e), 'warn'); if (button) button.disabled = false; });
}
function projectServicesCell(p) {
  var current = (p.services || []).join(', ');
  if (!consoleCan('projects.manage')) return '<span class="mono">' + esc(current || 'none') + '</span>';
  return '<div class="project-actions"><input data-project-services="' + esc(p.key) + '" value="' + esc(current) + '" placeholder="postgres, redis" aria-label="Test services of ' + esc(p.key) + '" size="16">' +
    '<button class="btn sm" data-act="project-services-save" data-key="' + esc(p.key) + '">Save</button></div>';
}
function runProjectPreflight() {
  S.projectAdmin.busy = true; S.projectAdmin.error = ''; S.projectAdmin.preflight = null; renderProjects();
  fetch('/v1/projects/preflight', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(projectRequestBody()) }).then(function (r) { return projectResponseError(r); })
    .then(function (d) { S.projectAdmin.preflight = d; })
    .catch(function (e) { S.projectAdmin.error = String(e.message || e); })
    .then(function () { S.projectAdmin.busy = false; renderProjects(); });
}
function saveProject() {
  S.projectAdmin.busy = true; S.projectAdmin.error = ''; renderProjects();
  fetch('/v1/projects', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(projectRequestBody()) }).then(function (r) { return projectResponseError(r); })
    .then(function (d) {
      toast('Project added', d.project + ' · bot commit ' + (d.commit || 'created'), 'ok');
      S.projectAdmin.showForm = false; S.projectAdmin.preflight = null; S.projectAdmin.catalog = null;
      loadProjectList(); poll();
    }).catch(function (e) { S.projectAdmin.error = String(e.message || e); })
    .then(function () { S.projectAdmin.busy = false; renderProjects(); });
}
function setProjectPaused(key, paused) {
  fetch('/v1/projects/' + encodeURIComponent(key) + '/pause', { method: 'POST',
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ paused: paused }) })
    .then(function (r) { return projectResponseError(r); })
    .then(function (d) { toast(paused ? 'Project paused' : 'Project resumed', d.project, 'ok'); loadProjectList(); poll(); })
    .catch(function (e) { toast('Could not update project', String(e.message || e), 'warn'); });
}
function sharedAccountRequest(method, url, body, title) {
  return fetch(url, { method: method, headers: { 'Content-Type': 'application/json' }, body: body ? JSON.stringify(body) : undefined })
    .then(function (r) { return projectResponseError(r); })
    .then(function (d) { toast(title, 'Done.', 'ok'); poll(); return d; })
    .catch(function (e) { toast('Could not update the account', String(e.message || e), 'warn'); });
}
function createSharedAccount() {
  var id = ($('#sa-new-id') || {}).value || '', provider = ($('#sa-new-provider') || {}).value || 'claude';
  var limit = parseInt(($('#sa-new-limit') || {}).value || '1', 10) || 1;
  sharedAccountRequest('POST', '/v1/shared-accounts', { id: id.trim().toLowerCase(), provider: provider, session_limit: limit }, 'Account created');
}
function enableAccountMachine(account) {
  var node = ($('#sa-machine-' + account) || {}).value;
  if (!node) return;
  sharedAccountRequest('POST', '/v1/shared-accounts/' + encodeURIComponent(account) + '/machines', { node: node }, 'Account enabled on ' + node);
}
function saveAccountLimit(account) {
  var limit = parseInt(($('#sa-limit-' + account) || {}).value || '0', 10);
  if (limit > 0) sharedAccountRequest('PUT', '/v1/shared-accounts/' + encodeURIComponent(account) + '/session-limit', { session_limit: limit }, 'Session limit saved');
}
function removeAccountMachine(account, node, last) {
  var text = last ? node + ' is the last machine using ' + account + '. Removing it removes the account from every machine. Credentials stay on the machine until you sign out there. Continue?'
    : 'Remove ' + account + ' from ' + node + '? Other machines keep using it.';
  if (!window.confirm(text)) return;
  sharedAccountRequest('DELETE', '/v1/shared-accounts/' + encodeURIComponent(account) + '/machines/' + encodeURIComponent(node) + (last ? '?confirm=true' : ''), null, 'Account removed from ' + node);
}
function retryAccountSignin(account, node) {
  sharedAccountRequest('POST', '/v1/shared-accounts/' + encodeURIComponent(account) + '/machines/' + encodeURIComponent(node) + '/signin', null, 'New sign-in started on ' + node);
}
function removeAccountEverywhere(account, count) {
  var text = Number(count) > 0
    ? 'Remove ' + account + ' from all ' + count + ' machines? Running sessions finish; no new ones start. Credentials stay on the machines until you sign out there.'
    : 'Remove ' + account + '? It is not enabled on any machine. This removes the account from Command Center. Continue?';
  if (!window.confirm(text)) return;
  sharedAccountRequest('DELETE', '/v1/shared-accounts/' + encodeURIComponent(account) + '?confirm=true', null, 'Account removed');
}
function accountMachinesSection(a) {
  var manage = consoleCan('nodes.manage'), ms = a.machines || [];
  var b = '<div class="dsec"><h4>Machines · ' + a.runs_active + ' of ' + a.session_limit + ' sessions in use</h4>';
  b += '<div class="small">Quota, sessions and predictions above are for the whole account. Each machine signs in once; credentials stay on the machine.</div>';
  if (!ms.length) b += '<div class="empty">Not enabled on any machine yet.</div>';
  else {
    b += '<div class="tablebox"><table><thead><tr><th scope="col">Machine</th><th scope="col">Status</th><th scope="col">Sessions now</th><th scope="col">Runs · 24 h</th><th scope="col">Tokens · today</th><th scope="col">Cost · today</th><th scope="col"></th></tr></thead><tbody>';
    ms.forEach(function (m) {
      var st = !m.signed_in ? pill('warn', 'Sign-in needed') : m.node_status === 'online' ? pill('ok', 'Online') : pill('crit', 'Offline');
      b += '<tr><td class="mono">' + esc(m.node) + '</td><td>' + st + '</td><td>' + m.runs_active + '</td><td>' + m.runs_24h + '</td><td>' + tok(m.tokens_today || 0) + '</td><td>' + usd(m.cost_today || 0) + '</td><td>';
      if (manage && m.removable) b += '<button class="btn sm" data-act="acc-remove-machine" data-account="' + esc(a.id) + '" data-node="' + esc(m.node) + '" data-last="' + (ms.length === 1 ? '1' : '') + '">Remove</button>';
      b += '</td></tr>';
      if (m.signin) b += '<tr><td colspan="7" class="small">Sign in on <b>' + esc(m.node) + '</b>: open <a href="' + esc(safeHref(m.signin.link)) + '" target="_blank" rel="noopener noreferrer">' + esc(m.signin.link) + '</a> and enter the code <code>' + esc(m.signin.code) + '</code>.</td></tr>';
      else if (!m.signed_in) b += '<tr><td colspan="7" class="small">Waiting for ' + esc(m.node) + ' to show its sign-in link and code.'
        + (manage ? ' <button class="btn sm" data-act="acc-retry-signin" data-account="' + esc(a.id) + '" data-node="' + esc(m.node) + '">Get a new link</button>' : '') + '</td></tr>';
    });
    b += '</tbody></table></div>';
  }
  if (manage) {
    var free = (D().nodes || []).filter(function (n) { return ms.every(function (m) { return m.node !== n.name; }); });
    if (free.length) {
      b += '<div class="flexrow"><label for="sa-machine-' + esc(a.id) + '" class="small">Enable on</label> <select id="sa-machine-' + esc(a.id) + '">' + free.map(function (n) { return '<option>' + esc(n.name) + '</option>'; }).join('') + '</select> <button class="btn sm" data-act="acc-enable" data-account="' + esc(a.id) + '">Enable</button></div>';
    }
    b += '<div class="flexrow"><label for="sa-limit-' + esc(a.id) + '" class="small">Total sessions</label> <input id="sa-limit-' + esc(a.id) + '" type="number" min="1" max="50" value="' + a.session_limit + '" style="width:5em"> <button class="btn sm" data-act="acc-limit" data-account="' + esc(a.id) + '">Save</button>';
    if (!ms.length && a.shared_managed) b += ' <button class="btn sm danger" data-act="acc-remove-all" data-account="' + esc(a.id) + '" data-count="0">Remove account</button>';
    else if (ms.length > 1) b += ' <button class="btn sm" data-act="acc-remove-all" data-account="' + esc(a.id) + '" data-count="' + ms.length + '">Remove from all machines</button>';
    b += '</div>';
  }
  return b + '</div>';
}
function removeManagedProject(key) {
  if (!window.confirm('Remove ' + key + ' from AI Flow? Jira and GitHub data will be kept.')) return;
  fetch('/v1/projects/' + encodeURIComponent(key), { method: 'DELETE' })
    .then(function (r) { return projectResponseError(r); })
    .then(function (d) { toast('Project removed', d.project + ' was removed from AI Flow.', 'ok'); loadProjectList(); poll(); })
    .catch(function (e) { toast('Could not remove project', String(e.message || e), 'warn'); });
}
function loadNodeOperations(name) {
  if (!consoleCan('console.read')) return;
  fetch('/v1/nodes/' + encodeURIComponent(name) + '/operations', { cache: 'no-store' }).then(function (r) {
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  }).then(function (d) {
    S.nodeOperations[name] = d.operations || [];
    if (S.drawer && S.drawer.kind === 'node' && S.drawer.id === name) renderDrawer();
  }).catch(function () { S.nodeOperations[name] = []; });
}
function startNodeOperation(name, action) {
  S.operationBusy[name] = true; renderDrawer();
  fetch('/v1/nodes/' + encodeURIComponent(name) + '/operations', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: action })
  }).then(function (r) {
    return r.json().then(function (d) { if (!r.ok) throw new Error(d.detail || ('HTTP ' + r.status)); return d; });
  }).then(function (d) {
    toast(action === 'repair' ? 'Repair queued' : 'Install queued', name + ' · ' + d.components.join(', '), 'info');
    loadNodeOperations(name);
  }).catch(function (e) { toast('Could not start operation', e.message, 'warn'); })
    .then(function () { S.operationBusy[name] = false; renderDrawer(); });
}
function createJoinCommand(name, requestedRole, requestedProject) {
  var node = nodeBy(name) || {};
  var projects = D().projects || [];
  var project = requestedProject || node.projects && node.projects[0] || (projects[0] && projects[0].key) || '';
  var role = requestedRole || node.role || 'worker';
  if (!project) { toast('Could not create join command', 'Select a project in the Join a node form first.', 'warn'); return; }
  fetch('/v1/nodes/' + encodeURIComponent(name) + '/join-command', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name: name, role: role, project: project })
  }).then(function (r) {
    return r.json().then(function (d) { if (!r.ok) throw new Error(d.detail || ('HTTP ' + r.status)); return d; });
  }).then(function (d) {
    S.joinCommands[name] = d; renderDrawer();
    toast('Join command ready', 'It expires in five minutes and can be used once.', 'ok');
  }).catch(function (e) { toast('Could not create join command', e.message, 'warn'); });
}
function copyJoinCommand(name) {
  var join = S.joinCommands[name]; if (!join) return;
  if (window.navigator && window.navigator.clipboard && window.navigator.clipboard.writeText) {
    window.navigator.clipboard.writeText(join.command).then(function () { toast('Join command copied', '', 'ok'); })
      .catch(function () { toast('Could not copy command', 'Select and copy the command below.', 'warn'); });
  } else toast('Select and copy the join command', '', 'info');
}
