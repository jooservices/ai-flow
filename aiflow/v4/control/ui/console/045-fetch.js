/* ---------- fetch ---------- */
function poll() {
  fetch('/v1/state', { cache: 'no-store' }).then(function (r) {
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  }).then(function (d) {
    var prevInc = S.seenInc, prevRuns = S.seenRuns;
    S.data = d; S.fetchedAt = Date.now(); S.lastOk = Date.now(); S.error = '';
    var prefsChanged = false;
    if (d.projects.length && !(S.view === 'work' && S.project === 'all') && !d.projects.some(function (p) { return p.key === S.project; })) {
      S.project = d.projects[0].key; S.epic = 'all'; prefsChanged = true;
    }
    var tickets = projTickets();
    if (S.epic !== 'all' && !tickets.some(function (t) { return t.type === 'Epic' && t.key === S.epic; })) {
      S.epic = 'all'; prefsChanged = true;
    }
    if (S.typeFilter !== 'all' && !tickets.some(function (t) { return t.type !== 'Epic' && t.type === S.typeFilter; })) {
      S.typeFilter = 'all'; prefsChanged = true;
    }
    if (prefsChanged) savePrefs();
    notify(prevInc, prevRuns);
    render();
    if (S.view === 'knowledge' && consoleCan('knowledge.read')) loadKnowledge();
    if (S.view === 'business' && !S.bizLoading) loadBiz();  // assessments arrive while the page is open
    if (S.view === 'backup' && consoleCan('backup.read') && Date.now() - S.backup.loadedAt > 30000) loadBackups(true);
  }).catch(function (e) { S.error = String(e.message || e); renderLive(); })
    .then(function () { setTimeout(poll, POLL_MS); });
}
function pollConsoleSession() {
  fetch('/v1/console/session', { cache: 'no-store' }).then(function (r) {
    if (!r.ok) { var error = new Error('HTTP ' + r.status); error.status = r.status; throw error; }
    return r.json();
  }).then(function (d) {
    S.consoleUser = d.authenticated ? d : null; S.consoleSessionLoaded = true;
    loadConsoleThemePreference();
    document.documentElement.classList.remove('role-loading');
    applyRoleNavigation();
    loadLayouts();
    if (S.consoleUser && !consoleCan(VIEW_PERMISSION[S.view] || 'view.dashboard')) {
      location.replace(S.consoleUser.landing_page || '/cc'); return;
    }
    render(); if (window.AIFlowSetup) AIFlowSetup.poll(); else if (!S.setupScript) { S.setupScript = document.head.appendChild(Object.assign(document.createElement('script'), { src: '/ui/setup-progress.js' })); }
    if (S.view === 'cc' || S.view === 'work' || S.view === 'delivery') loadDashboardWaiting(true);
    if (consoleReader() && (!S.licensing.status || Date.now() - S.licensing.statusAt > 60000) && !S.licensing.statusBusy) loadLicenseStatus();
    if (consoleCan('licensing.manage') && (!S.licensing.data || Date.now() - S.licensing.loadedAt > 60000) && !S.licensing.busy) loadLicensing();
    if (consoleReader() && !S.autonomy.data && !S.autonomy.busy) loadMasterAutonomy(false);
    if (S.drawer && S.drawer.kind === 'node' && consoleCan('console.read')) loadNodeOperations(S.drawer.id);
    if (S.view === 'integrations' && consoleCan('integrations.manage') && !S.integrations.data && !S.integrations.busy) loadIntegrations();
    if (S.view === 'users' && consoleCan('console.manage') && Date.now() - S.userAdmin.loadedAt > 30000 && !S.userAdmin.loading) loadUsers(true);
    if (S.view === 'notifications' && consoleCan('notifications.manage') && !S.notifications.data && !S.notifications.busy) loadNotifications();
    if (S.view === 'settings' && consoleCan('console.manage') && !S.runtimeSettings.data && !S.runtimeSettings.busy) loadRuntimeSettings();
    if (S.view === 'knowledge' && consoleCan('knowledge.read')) loadKnowledge();
    if (consoleCan('view.machines_ai')) pollServices();
    if (S.view === 'backup' && consoleCan('backup.read')) loadBackups();
  }).catch(function (error) {
    if (error && error.status === 401) { location.replace('/login'); return; }
    S.consoleUser = null; S.consoleSessionLoaded = true; loadConsoleThemePreference();
    document.documentElement.classList.remove('role-loading'); applyRoleNavigation(); render(); pollServices(); })
    .then(function () { setTimeout(pollConsoleSession, 15000); });
}
function themeStorageKey(user) { return 'aiflow4-theme:' + encodeURIComponent(user || 'anonymous').toLowerCase(); }
function applyTheme(theme) {
  var value = theme === 'light' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', value);
  return value;
}
function loadConsoleThemePreference() {
  var user = S.consoleUser && S.consoleUser.user || 'anonymous';
  if (S.themePreference.loadedUser === user) return;
  var key = themeStorageKey(user), local = '';
  try { local = localStorage.getItem(key) || ''; } catch (x) { /* storage off */ }
  S.themePreference.user = user;
  fetch('/v1/console/preferences', { cache: 'no-store' }).then(function (r) {
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  }).then(function (d) {
    var theme = applyTheme(d.theme);
    try { localStorage.setItem(key, theme); } catch (x) { /* storage off */ }
  }).catch(function () { if (local) applyTheme(local); })
    .then(function () { S.themePreference.loadedUser = user; });
}
function saveConsoleTheme(theme) {
  var value = applyTheme(theme), user = S.consoleUser && S.consoleUser.user || 'anonymous';
  S.themePreference.user = user;
  try { localStorage.setItem(themeStorageKey(user), value); } catch (x) { /* storage off */ }
  if (!S.consoleUser) return;
  var request = S.themePreference.chain.then(function () {
    return fetch('/v1/console/preferences', { method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ theme: value }) }).then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); });
  });
  S.themePreference.chain = request.catch(function () {});
}
function applyRoleNavigation() {
  var tabs = $$('.tab[data-view]');
  tabs.forEach(function (tab) {
    var view = tab.getAttribute('data-view'), permission = VIEW_PERMISSION[view] || 'view.dashboard';
    tab.hidden = !!S.consoleUser && !consoleCan(permission);
    tab.setAttribute('aria-hidden', String(tab.hidden));
  });
  var nav = $('.tabs');
  var logLink = $('#logs-nav'); if (logLink) logLink.hidden = !S.consoleUser || !consoleCan('view.logs');
  if (nav) {
    $$('.tab-divider', nav).forEach(function (divider) { divider.remove(); });
    VIEW_GROUPS.forEach(function (group, index) {
      if (index) { var divider = document.createElement('span'); divider.className = 'tab-divider'; divider.setAttribute('aria-hidden', 'true'); nav.appendChild(divider); }
      group.forEach(function (view) { var tab = $('#tab-' + view); if (tab) nav.appendChild(tab); });
    });
  }
  var as = $('#view-as-switch'), select = $('#view-as-role'), user = S.consoleUser;
  if (user && !consoleCan(VIEW_PERMISSION[S.view] || 'view.dashboard')) {
    location.replace(user.landing_page || '/cc'); return;
  }
  if (!as || !select) return;
  as.hidden = !(user && user.can_view_as);
  if (!user || !user.can_view_as) return;
  var choices = user.role_options || [];
  select.innerHTML = '<option value="">' + (user.viewing_as ? 'Viewing as ' + esc(user.role_label) : 'My roles') + '</option>' +
    (user.viewing_as ? '<option value="__stop">Return to my role</option>' : '') + choices.map(function (r) {
      return '<option value="' + esc(r.id) + '">' + esc(r.label) + '</option>';
    }).join('');
  select.value = '';
  select.onchange = function () {
    var value = select.value; if (!value) return;
    var endpoint = value === '__stop' ? '/v1/console/view-as/stop' : '/v1/console/view-as';
    fetch(endpoint, { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: value === '__stop' ? '{}' : JSON.stringify({ role: value }) })
      .then(function (r) { return r.json().then(function (d) { if (!r.ok) throw new Error(d.detail || ('HTTP ' + r.status)); return d; }); })
      .then(function () { location.href = value === '__stop' ? '/cc' : (choices.filter(function (r) { return r.id === value; })[0] || {}).landing_page || '/cc'; })
      .catch(function (e) { toast('View as failed', String(e.message || e), 'crit'); select.value = ''; });
  };
}
function projectResponseError(response) {
  return response.json().then(function (data) {
    if (response.ok) return data;
    var detail = data.detail || ('HTTP ' + response.status);
    if (typeof detail === 'object') {
      if (detail.missing_statuses && detail.missing_statuses.length) return Promise.reject(new Error('Missing Jira statuses: ' + detail.missing_statuses.join(', ')));
      return Promise.reject(new Error(detail.message || JSON.stringify(detail)));
    }
    return Promise.reject(new Error(String(detail)));
  });
}
function loadProjectList() {
  fetch('/v1/projects', { cache: 'no-store' }).then(function (r) { return projectResponseError(r); })
    .then(function (d) { S.projectAdmin.projects = d.projects || []; S.projectAdmin.invalidProjects = d.invalid_projects || []; S.projectAdmin.error = ''; if (S.view === 'projects') renderProjects(); })
    .catch(function (e) { S.projectAdmin.error = String(e.message || e); if (S.view === 'projects') renderProjects(); });
  loadEnvironmentOverview();
}
function loadEnvironmentOverview() {
  if (!consoleCan('projects.manage')) return;
  fetch('/v1/environment/missing', { cache: 'no-store' }).then(function (r) { return projectResponseError(r); })
    .then(function (d) { S.projectAdmin.envMissing = d.missing || []; if (S.view === 'projects') renderProjects(); }).catch(function () {});
  fetch('/v1/environment/scan', { cache: 'no-store' }).then(function (r) { return projectResponseError(r); })
    .then(function (d) { S.projectAdmin.envScan = d.scan || null; if (S.view === 'projects') renderProjects(); }).catch(function () {});
}
function loadProjectEnvironment(key) {
  S.projectAdmin.envEditor = { key: key, busy: true, saving: false, error: '', data: null };
  renderProjects();
  fetch('/v1/projects/' + encodeURIComponent(key) + '/environment', { cache: 'no-store' })
    .then(function (r) { return projectResponseError(r); })
    .then(function (data) { var ed = S.projectAdmin.envEditor; if (ed && ed.key === key) { ed.data = data; ed.error = ''; } })
    .catch(function (e) { var ed = S.projectAdmin.envEditor; if (ed && ed.key === key) ed.error = String(e.message || e); })
    .then(function () { var ed = S.projectAdmin.envEditor; if (ed && ed.key === key) { ed.busy = false; renderProjects(); } });
}
function saveProjectEnvironment() {
  var ed = S.projectAdmin.envEditor; if (!ed || !ed.data || ed.saving) return;
  ed.saving = true; ed.error = ''; renderProjects();
  var items = ed.data.items.map(function (i) { return { name: i.name, kind: i.kind, value: i.kind === 'secret' ? '' : i.value, runs: i.runs, description: i.description || '' }; });
  fetch('/v1/projects/' + encodeURIComponent(ed.key) + '/environment', { method: 'PUT',
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ items: items }) })
    .then(function (r) { return projectResponseError(r); })
    .then(function (d) { toast('Environment saved', d.commit || 'Control repository updated', 'ok');
      S.projectAdmin.envEditor = null; loadProjectList(); })
    .catch(function (e) { ed.error = String(e.message || e); ed.saving = false; renderProjects(); });
}
function runEnvironmentScan() {
  var state = S.projectAdmin; if (state.envScanBusy) return;
  state.envScanBusy = true; renderProjects();
  fetch('/v1/environment/scan', { method: 'POST' }).then(function (r) { return projectResponseError(r); })
    .then(function (d) { state.envScan = d.scan; toast('Scan finished', d.scan.offenders.length + ' of ' + d.scan.scanned + ' open tickets name server wording', 'ok'); })
    .catch(function (e) { toast('Scan failed', String(e.message || e), 'crit'); })
    .then(function () { state.envScanBusy = false; renderProjects(); });
}
function projectEnvironmentPanels(state) {
  var h = '', editor = state.envEditor;
  if (editor) {
    h += '<section class="panel" data-panel="project-environment"><div class="panel-h"><h2>Environment · ' + esc(editor.key) + '</h2><span class="hint">Test services, base URLs, databases, browsers and secrets by name. Runs get them as environment variables.</span><span class="spacer"></span><button class="btn sm" data-act="project-environment-close">Close</button></div>';
    if (editor.error) h += '<div class="stakeholder-error">' + esc(editor.error) + '</div>';
    if (editor.busy || !editor.data) h += '<div class="muted">Loading the environment…</div>';
    else {
      var data = editor.data, items = data.items || [];
      (data.problems || []).forEach(function (p) { h += '<div class="bad">' + esc(p) + '</div>'; });
      h += '<p class="muted">Stored in <span class="mono">projects/' + esc(editor.key) + '.yaml</span> of the control repository. A secret is listed by name only: its value is set on the machine and is never shown or stored here.</p>';
      h += '<div class="project-table"><table><thead><tr><th>Variable</th><th>Kind</th><th>Value</th><th>Runs that may use it</th><th></th></tr></thead><tbody>';
      items.forEach(function (it, index) {
        var secret = it.kind === 'secret';
        h += '<tr><td><input data-env-field="name" data-index="' + index + '" value="' + esc(it.name) + '" placeholder="WP_BASE_URL" aria-label="Variable name"></td>';
        h += '<td><select data-env-field="kind" data-index="' + index + '" aria-label="Kind">' + (data.kinds || []).map(function (k) { return '<option value="' + esc(k) + '"' + (it.kind === k ? ' selected' : '') + '>' + esc(k.replace('_', ' ')) + '</option>'; }).join('') + '</select></td>';
        h += '<td><input data-env-field="value" data-index="' + index + '" value="' + esc(secret ? '' : it.value) + '"' + (secret ? ' disabled placeholder="set on the machine"' : '') + ' aria-label="Value"></td>';
        h += '<td>' + (data.runs || []).map(function (r) { return '<label class="chip2" style="cursor:pointer"><input type="checkbox" data-env-run="' + esc(r) + '" data-index="' + index + '"' + ((it.runs || []).indexOf(r) >= 0 ? ' checked' : '') + '> ' + esc(r) + '</label>'; }).join(' ') + '</td>';
        h += '<td><button class="btn sm danger" data-act="project-environment-remove" data-index="' + index + '">Remove</button></td></tr>';
      });
      if (!items.length) h += '<tr><td colspan="5" class="muted">No environment items. Runs of this project get none.</td></tr>';
      h += '</tbody></table></div>';
      h += '<div class="project-actions"><button class="btn sm" data-act="project-environment-add">Add item</button><span class="spacer"></span><button class="btn primary" data-act="project-environment-save"' + (editor.saving ? ' disabled' : '') + '>Save environment</button></div>';
    }
    h += '</section>';
  }
  if (editor && editor.data) {
    var postMerge = editor.data.post_merge || [];
    h += '<section class="panel" data-panel="post-merge-environment"><div class="panel-h"><h2>Post-merge environment checks</h2><span class="hint">Deferred criteria are checked by the configured project release checks using items allowed for post_merge.</span></div>';
    if (!postMerge.length) h += '<div class="muted">No deferred criteria are waiting for or have completed a post-merge check.</div>';
    else {
      h += '<div class="project-table"><table><thead><tr><th>Ticket</th><th>Deferred criteria</th><th>Result</th><th>Release checks</th><th>Checked</th></tr></thead><tbody>';
      postMerge.forEach(function (item) {
        var result = item.result || {}, outcomes = result.criteria || [];
        var criteria = (item.criteria || []).map(function (c) {
          var criterion = typeof c === 'string' ? { text: c } : c;
          return '<div><strong>' + esc(criterion.id || '') + '</strong> ' + esc(criterion.text || '') +
            '<div class="hint">Configured check: ' + esc(criterion.check || 'unmapped') + '</div></div>';
        });
        var checks = outcomes.map(function (c) {
          var check = c.check_result || {}, command = check.command || c.check || 'unmapped';
          var status = check.passed === true ? 'passed' : check.passed === false ? 'failed' : (c.status || 'pending');
          var exit = Number.isInteger(check.exit_code) ? ' (exit ' + check.exit_code + ')' : '';
          return (c.id ? c.id + ': ' : '') + command + ': ' + status + exit;
        });
        h += '<tr><td class="mono">' + esc(item.ticket) + '</td><td>' + criteria.join('') + '</td>';
        h += '<td><span class="pill" data-tone="' + (item.status === 'passed' ? 'ok' : item.status === 'failed' ? 'crit' : 'warn') + '">' + esc(item.status) + '</span></td>';
        h += '<td>' + esc(checks.join(', ') || result.error || '—') + (item.release_ticket ? ' · ' + esc(item.release_ticket) : '') + '</td><td>' + esc(item.checked_at ? when(item.checked_at) : 'waiting') + '</td></tr>';
      });
      h += '</tbody></table></div>';
    }
    h += '</section>';
  }
  var missing = state.envMissing || [];
  h += '<section class="panel" data-panel="environment-missing"><div class="panel-h"><h2>Missing environment items</h2><span class="hint">Reported by a developer run by name; AI Flow provides the item or raises one environment ticket. The owner is never asked.</span></div>';
  if (!missing.length) h += '<div class="muted">No run is waiting for an environment item.</div>';
  else {
    h += '<div class="project-table"><table><thead><tr><th>Item</th><th>Project</th><th>Ticket</th><th>State</th><th>Environment ticket</th><th>Since</th></tr></thead><tbody>';
    missing.forEach(function (m) {
      h += '<tr><td class="mono">' + esc(m.item) + '</td><td class="mono">' + esc(m.project) + '</td><td><span class="mono">' + esc(m.ticket) + '</span> ' + esc(m.summary) + '</td><td><span class="pill" data-tone="' + (m.state === 'waiting' ? 'warn' : 'ok') + '">' + esc(m.state) + '</span>' + (m.resolution ? ' <span class="hint">' + esc(m.resolution) + '</span>' : '') + '</td><td class="mono">' + esc(m.environment_ticket || '—') + '</td><td>' + esc(when(m.since)) + '</td></tr>';
    });
    h += '</tbody></table></div>';
  }
  h += '</section>';
  var scan = state.envScan;
  h += '<section class="panel" data-panel="environment-scan"><div class="panel-h"><h2>Tickets that name servers</h2><span class="hint">Open tickets of every project that name a host, an address, a machine, a network or an access instruction. Tickets nobody works on go back to intake, where the wording is removed and the requirement kept.</span><span class="spacer"></span><button class="btn sm" data-act="environment-scan"' + (state.envScanBusy ? ' disabled' : '') + '>' + (state.envScanBusy ? 'Scanning…' : 'Scan now') + '</button></div>';
  if (!scan) h += '<div class="muted">No scan has run yet. The scan runs once a day.</div>';
  else {
    h += '<p class="hint">Scanned ' + esc(when(scan.created_at)) + ' · ' + esc(scan.scanned) + ' open tickets · ' + esc((scan.offenders || []).length) + ' break the rule</p>';
    if ((scan.offenders || []).length) {
      h += '<div class="project-table"><table><thead><tr><th>Ticket</th><th>Project</th><th>Status</th><th>Names</th><th>Action</th></tr></thead><tbody>';
      scan.offenders.forEach(function (o) {
        h += '<tr><td class="mono">' + esc(o.ticket) + '</td><td class="mono">' + esc(o.project) + '</td><td>' + esc(o.status) + '</td><td>' + esc((o.kinds || []).join(', ')) + '</td><td>' + esc(o.action) + '</td></tr>';
      });
      h += '</tbody></table></div>';
    }
  }
  h += '</section>';
  return h;
}
function adminRequest(url, options) {
  return fetch(url, Object.assign({ cache: 'no-store' }, options || {})).then(function (r) {
    return r.json().then(function (d) { if (!r.ok) throw new Error(d.detail || ('HTTP ' + r.status)); return d; });
  });
}
function loadUsers(force) {
  var u = S.userAdmin;
  if (!consoleCan('console.manage') || u.loading || (u.users && !force)) return;
  u.loading = true; u.error = ''; renderUsers();
  adminRequest('/v1/console/users').then(function (d) {
    u.users = d.users || []; u.projects = d.projects || []; u.roleOptions = d.role_options || []; u.audit = d.audit || [];
    u.error = ''; u.loadedAt = Date.now(); u.loading = false; renderUsers();
  }).catch(function (e) { u.error = String(e.message || e); u.loading = false; renderUsers(); });
}
var ROLE_VIEW_LABELS = { 'view.dashboard': 'Dashboard', 'view.work_board': 'Work board', 'view.delivery': 'Delivery',
  'view.sa': 'SA', 'view.team_lead': 'Team Lead', 'view.business': 'Business', 'view.human_review': 'Human review',
  'view.stakeholder': 'Stakeholder', 'view.machines_ai': 'Machines and AI', 'view.knowledge': 'Knowledge',
  'view.integrations': 'Integrations', 'projects.manage': 'Projects', 'console.manage': 'Users and roles' };
function roleChecks(root, className, selected) {
  return S.userAdmin.roleOptions.map(function (r) {
    return '<label class="chip2" style="cursor:pointer"><input type="checkbox" class="' + className + '" value="' + esc(r.id) + '"' +
      (selected.indexOf(r.id) >= 0 ? ' checked' : '') + '> ' + esc(r.label) + '</label>';
  }).join('');
}
function projectChoices(selected, all) {
  return '<label class="fld">Projects <select class="in" data-user-projects multiple size="' + Math.min(5, Math.max(2, S.userAdmin.projects.length)) + '"' + (all ? ' disabled' : '') + '>' +
    S.userAdmin.projects.map(function (key) { return '<option value="' + esc(key) + '"' + (selected.indexOf(key) >= 0 ? ' selected' : '') + '>' + esc(key) + '</option>'; }).join('') +
    '</select></label><label class="chip2" style="cursor:pointer"><input type="checkbox" data-user-all' + (all ? ' checked' : '') + '> All projects</label>';
}
function renderUsers() {
  var root = $('#view-users'); if (!root) return;
  if (!consoleCan('console.manage')) { root.innerHTML = '<section class="panel"><h2>View unavailable</h2><p>This view is not available for your role.</p></section>'; return; }
  var u = S.userAdmin, h = '<section class="panel" data-block="users"><div class="panel-h"><h2>People and roles</h2><span class="hint">Role changes, project access and account status take effect immediately.</span><span class="spacer"></span><button class="btn sm" data-user-refresh>Refresh</button></div>';
  if (u.error) h += '<div class="stakeholder-error">' + esc(u.error) + '</div>';
  if (u.temporaryPassword) h += '<div class="project-checks"><strong>One-time password for ' + esc(u.temporaryPassword.email) + '</strong><p>Copy this password and share it with the person. It will not be shown again.</p><code>' + esc(u.temporaryPassword.value) + '</code><button class="btn sm" data-user-password-dismiss>Dismiss</button></div>';
  if (u.loading && !u.users) h += '<div class="muted">Loading people…</div>';
  else {
    h += '<div class="project-table"><table><thead><tr><th>Person</th><th>Roles</th><th>Projects</th><th>Last sign-in</th><th>Status</th><th></th></tr></thead><tbody>';
    (u.users || []).forEach(function (person) {
      var labels = (person.roles || []).map(function (role) { return (u.roleOptions.filter(function (r) { return r.id === role; })[0] || {}).label || role; });
      var projects = person.project_keys == null ? 'All projects' : person.project_keys.length ? person.project_keys.join(', ') : 'No projects';
      h += '<tr><td><b>' + esc(person.display_name || person.email) + '</b><div class="small muted">' + esc(person.email) + '</div></td><td>' +
        labels.map(function (label) { return '<span class="pill">' + esc(label) + '</span>'; }).join(' ') + '</td><td>' + esc(projects) + '</td><td>' +
        esc(person.last_login_at ? new Date(person.last_login_at).toLocaleString() : 'Never') + '</td><td>' +
        (person.disabled ? '<span class="pill" data-tone="crit">Disabled</span>' : '<span class="pill" data-tone="ok">Active</span>') +
        '</td><td><div class="project-actions"><button class="btn sm" data-user-edit="' + esc(person.email) + '">Edit</button><button class="btn sm' +
        (person.disabled ? '' : ' danger') + '" data-user-toggle="' + esc(person.email) + '" data-disabled="' + (!person.disabled) + '">' +
        (person.disabled ? 'Enable' : 'Disable') + '</button></div></td></tr>';
      if (u.editing === person.email) {
        h += '<tr><td colspan="6"><div class="project-form"><div class="wide"><strong>Roles</strong><div>' +
          roleChecks(root, 'edit-role', person.roles || []) + '</div></div><div>' + projectChoices(person.project_keys || [], person.project_keys == null) +
          '</div><div class="project-actions"><button class="btn primary sm" data-user-save="' + esc(person.email) + '">Save changes</button>' +
          '<button class="btn sm" data-user-cancel>Cancel</button></div></div></td></tr>';
      }
    });
    if (!(u.users || []).length) h += '<tr><td colspan="6" class="muted">No people have been added.</td></tr>';
    h += '</tbody></table></div>';
  }
  var addForm = u.addForm;
  h += '</section><div class="users-layout-secondary" data-block="access"><section class="panel"><div class="panel-h"><h2>Add a person</h2><span class="hint">AI Flow creates a one-time password for the new person.</span></div>' +
    '<div class="project-form"><label>Email<input class="in" type="email" data-user-email autocomplete="off" required value="' + esc(addForm.email) + '"></label>' +
    '<label>Name<input class="in" data-user-name autocomplete="off" value="' + esc(addForm.name) + '"></label><div><strong>Roles</strong><div>' + roleChecks(root, 'new-role', addForm.roles || []) +
    '</div></div><div>' + projectChoices(addForm.projects || [], addForm.all !== false) + '</div><div class="project-actions"><button class="btn primary" data-user-add>Add person</button></div></div></section>';
  h += '<section class="panel"><div class="panel-h"><h2>Views by role</h2><span class="hint">A person with multiple roles sees the union of their views.</span></div>' +
    '<div class="project-table"><table><thead><tr><th>View</th>' + u.roleOptions.map(function (r) { return '<th>' + esc(r.label) + '</th>'; }).join('') +
    '</tr></thead><tbody>' + Object.keys(ROLE_VIEW_LABELS).map(function (permission) {
      return '<tr><td>' + esc(ROLE_VIEW_LABELS[permission]) + '</td>' + u.roleOptions.map(function (r) {
        var on = r.permissions.indexOf('*') >= 0 || r.permissions.indexOf(permission) >= 0;
        return '<td>' + (on ? '✓' : '—') + '</td>';
      }).join('') + '</tr>';
    }).join('') + '</tbody></table></div></section>';
  h += '<section class="panel"><div class="panel-h"><h2>Change history</h2><span class="hint">Who made each account or role change</span></div>' +
    '<div class="project-table"><table><thead><tr><th>When</th><th>Who</th><th>Change</th><th>Details</th></tr></thead><tbody>' +
    (u.audit || []).map(function (item) { return '<tr><td>' + esc(item.ts ? new Date(item.ts).toLocaleString() : '') + '</td><td>' + esc(item.actor) +
      '</td><td>' + esc(item.action.replace(/_/g, ' ')) + '</td><td>' + esc(item.result) + '<div class="small muted">' +
      esc(JSON.stringify(item.payload || {})) + '</div></td></tr>'; }).join('') +
    '</tbody></table></div></section></div>';
  root.innerHTML = h;
  applyLayout('users', root);
  $$('[data-user-refresh]', root).forEach(function (b) { b.onclick = function () { loadUsers(true); }; });
  $$('[data-user-password-dismiss]', root).forEach(function (b) { b.onclick = function () { u.temporaryPassword = ''; renderUsers(); }; });
  $$('[data-user-edit]', root).forEach(function (b) { b.onclick = function () { u.editing = b.dataset.userEdit; renderUsers(); }; });
  $$('[data-user-cancel]', root).forEach(function (b) { b.onclick = function () { u.editing = ''; renderUsers(); }; });
  $$('[data-user-all]', root).forEach(function (box) { box.onchange = function () { var select = $('[data-user-projects]', box.closest('.project-form')); if (select) select.disabled = box.checked; }; });
  $$('[data-user-toggle]', root).forEach(function (b) { b.onclick = function () {
    adminRequest('/v1/console/users/' + encodeURIComponent(b.dataset.userToggle) + '/disabled', {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ disabled: b.dataset.disabled === 'true' })
    }).then(function () { loadUsers(true); }).catch(function (e) { toast('Account update failed', String(e.message || e), 'crit'); });
  }; });
  $$('[data-user-save]', root).forEach(function (b) { b.onclick = function () {
    var edit = b.closest('.project-form'), roles = $$('.edit-role:checked', edit).map(function (x) { return x.value; });
    var all = $('[data-user-all]', edit).checked, projects = all ? null : $$('[data-user-projects] option:checked', edit).map(function (x) { return x.value; });
    adminRequest('/v1/console/users/' + encodeURIComponent(b.dataset.userSave), { method: 'PUT',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ roles: roles, project_keys: projects })
    }).then(function () { u.editing = ''; loadUsers(true); }).catch(function (e) { toast('Person update failed', String(e.message || e), 'crit'); });
  }; });
  $$('[data-user-add]', root).forEach(function (b) { b.onclick = function () {
    var form = b.closest('.project-form'), email = $('[data-user-email]', form).value, name = $('[data-user-name]', form).value;
    var roles = $$('.new-role:checked', form).map(function (x) { return x.value; }), all = $('[data-user-all]', form).checked;
    var projects = all ? null : $$('[data-user-projects] option:checked', form).map(function (x) { return x.value; });
    adminRequest('/v1/console/users', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email: email, display_name: name, roles: roles, project_keys: projects })
    }).then(function (d) {
      u.addForm = { email: '', name: '', roles: [], projects: [], all: true };
      u.temporaryPassword = { email: d.email, value: d.temporary_password }; loadUsers(true);
    })
      .catch(function (e) { toast('Person could not be added', String(e.message || e), 'crit'); });
  }; });
}
function loadProjectRepositories(key) {
  S.projectAdmin.repositoryEditor = { key: key, busy: true, saving: false, error: '', data: null };
  renderProjects();
  fetch('/v1/projects/' + encodeURIComponent(key) + '/repositories', { cache: 'no-store' })
    .then(function (r) { return projectResponseError(r); })
    .then(function (data) { var ed = S.projectAdmin.repositoryEditor; if (ed && ed.key === key) { ed.data = data; ed.error = ''; } })
    .catch(function (e) { var ed = S.projectAdmin.repositoryEditor; if (ed && ed.key === key) ed.error = String(e.message || e); })
    .then(function () { var ed = S.projectAdmin.repositoryEditor; if (ed && ed.key === key) { ed.busy = false; renderProjects(); } });
}
function addProjectRepository(name) {
  var ed = S.projectAdmin.repositoryEditor, data = ed && ed.data;
  if (!data || !name || (data.repositories || []).some(function (r) { return r.repo === name; })) return;
  var known = (data.github_repos || []).filter(function (g) { return g.full_name === name; })[0] || {};
  data.repositories.push({ repo: name, base: '', checks: [], stack: '', containers: null, connection: known.connection || '', connections: known.connections || [], effective: { base: data.project_defaults.base,
    checks: data.project_defaults.checks, stack: data.project_defaults.stack, containers: data.project_defaults.containers } });
  renderProjects();
}
function saveProjectRepositories() {
  var ed = S.projectAdmin.repositoryEditor; if (!ed || !ed.data || ed.saving) return;
  ed.saving = true; ed.error = ''; renderProjects();
  var body = { repositories: ed.data.repositories.map(function (r) { return { repo: r.repo, base: r.base || '',
    checks: r.checks || [], stack: r.stack || '', containers: r.containers,
    connection: (r.connection_picked || r.connection_stale) ? (r.connection || '') : '' }; }), epic_repositories: ed.data.epic_repositories || {} };
  fetch('/v1/projects/' + encodeURIComponent(ed.key) + '/repositories', { method: 'PUT',
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
    .then(function (r) { return projectResponseError(r); })
    .then(function (d) { toast('Repository settings saved', d.commit || 'Control repository updated', 'ok');
      S.projectAdmin.repositoryEditor = null; loadProjectList(); poll(); })
    .catch(function (e) { ed.error = String(e.message || e); ed.saving = false; renderProjects(); });
}
function loadProjectCatalog(refresh) {
  if (refresh) S.projectAdmin.catalog = null;
  if (S.projectAdmin.catalog || S.projectAdmin.busy) return;
  S.projectAdmin.busy = true; S.projectAdmin.error = ''; renderProjects();
  fetch('/v1/projects/catalog', { cache: 'no-store' }).then(function (r) { return projectResponseError(r); })
    .then(function (d) {
      S.projectAdmin.catalog = d;
      var form = S.projectAdmin.form;
      if (!form.site && d.jira_sites && d.jira_sites.length) form.site = d.jira_sites[0];
      if (!form.repo && d.github_repos && d.github_repos.length) form.repo = d.github_repos[0].full_name;
      S.projectAdmin.error = '';
    }).catch(function (e) { S.projectAdmin.error = String(e.message || e); })
    .then(function () { S.projectAdmin.busy = false; renderProjects(); });
}
function githubResponseError(response) {
  return response.json().then(function (data) {
    if (response.ok) return data;
    var detail = data.detail || ('HTTP ' + response.status);
    return Promise.reject(new Error(typeof detail === 'string' ? detail : 'GitHub connection could not be completed'));
  });
}
function githubAppTarget() { return S.integrations.target || ((S.integrations.connections || []).length ? 'new' : 'default'); }
function addGitHubToken() {
  var tokenInput = $('[data-github-token]'), token = tokenInput ? tokenInput.value.trim() : '';
  var account = ($('[data-github-token-account]') || {}).value || '', label = ($('[data-github-token-label]') || {}).value || '';
  if (!token) { S.integrations.error = 'Paste the access token.'; renderIntegrations(); return; }
  S.integrations.busy = true; S.integrations.error = ''; renderIntegrations();
  fetch('/v1/integrations/github/connections/token', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ token: token, account: account.trim(), label: label.trim() }) })
    .then(function (r) { return githubResponseError(r); })
    .then(function () { S.integrations.notice = 'Access token verified with GitHub and stored. It will not be shown again.'; loadGitHubIntegrations(); })
    .catch(function (e) { S.integrations.error = String(e.message || e); S.integrations.busy = false; renderIntegrations(); });
}
function githubConnectionAction(id, method, suffix, body, notice) {
  S.integrations.busy = true; S.integrations.error = ''; renderIntegrations();
  var options = { method: method, headers: { 'Content-Type': 'application/json' } };
  if (body) options.body = JSON.stringify(body);
  fetch('/v1/integrations/github/connections/' + encodeURIComponent(id) + suffix, options)
    .then(function (r) { return githubResponseError(r); })
    .then(function () { S.integrations.notice = notice; loadGitHubIntegrations(); })
    .catch(function (e) { S.integrations.error = String(e.message || e); S.integrations.busy = false; renderIntegrations(); });
}
function githubManifestStart() {
  var orgField = $('[data-github-org]'), org = orgField ? orgField.value.trim() : '';
  if (!org) { S.integrations.error = 'Enter the GitHub organization that will own the App.'; renderIntegrations(); return; }
  window.open('/v1/integrations/github/manifest/start?organization=' + encodeURIComponent(org) + '&connection=' + encodeURIComponent(githubAppTarget()), '_blank', 'noopener');
}
function replaceGitHubToken(id) {
  var field = $('[data-github-replace-token="' + id + '"]'), pasted = field ? field.value.trim() : '';
  if (!pasted) { S.integrations.error = 'Paste the new token first.'; renderIntegrations(); return; }
  githubConnectionAction(id, 'PUT', '/token', { token: pasted }, 'Token replaced.');
}
function removeGitHubConnection(id) {
  if (window.confirm('Remove this GitHub connection? Repositories using it must be reassigned first.')) githubConnectionAction(id, 'DELETE', '', null, 'Connection removed.');
}
function loadGitHubIntegrations() {
  S.integrations.busy = true; S.integrations.error = ''; renderIntegrations();
  fetch('/v1/integrations/github/connections', { cache: 'no-store' }).then(function (r) { return githubResponseError(r); })
    .then(function (d) {
      S.integrations.connections = d.connections || []; S.integrations.recommendation = d.recommendation || '';
      if (!S.integrations.connections.length) S.integrations.target = '';
      S.integrations.error = '';
    })
    .catch(function (e) { S.integrations.error = String(e.message || e); })
    .then(function () { S.integrations.busy = false; renderIntegrations(); });
}
function loadImportedInstallations() {
  var pending = S.integrations.pending; if (!pending) return;
  S.integrations.busy = true; S.integrations.error = ''; renderIntegrations();
  fetch('/v1/integrations/github/imports/' + encodeURIComponent(pending.pending_id) + '/installations', { cache: 'no-store' })
    .then(function (r) { return githubResponseError(r); })
    .then(function (d) {
      S.integrations.pending.app = d.app; S.integrations.pending.install_url = d.install_url;
      S.integrations.installations = d.installations || [];
      if (!S.integrations.installations.some(function (i) { return String(i.id) === String(S.integrations.selected); })) {
        S.integrations.selected = S.integrations.installations.length ? String(S.integrations.installations[0].id) : '';
      }
    }).catch(function (e) { S.integrations.error = String(e.message || e); })
    .then(function () { S.integrations.busy = false; renderIntegrations(); });
}
function importGitHubApp() {
  var idInput = $('[data-github-app-id]'), keyInput = $('[data-github-key-file]');
  var keyFile = keyInput && keyInput.files && keyInput.files[0], appId = Number(idInput && idInput.value);
  if (!appId || !keyFile) { S.integrations.error = 'Enter the App ID and select its private key file.'; renderIntegrations(); return; }
  S.integrations.busy = true; S.integrations.error = ''; renderIntegrations();
  keyFile.text().then(function (privateKey) {
    return fetch('/v1/integrations/github/imports', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ app_id: appId, private_key: privateKey, connection: githubAppTarget() }) });
  }).then(function (r) { return githubResponseError(r); })
    .then(function (d) {
      S.integrations.pending = { pending_id: d.pending_id, app: d.app, install_url: d.install_url };
      S.integrations.installations = []; S.integrations.selected = '';
      if (keyInput) keyInput.value = '';
      loadImportedInstallations();
    }).catch(function (e) { S.integrations.error = String(e.message || e); if (keyInput) keyInput.value = ''; })
    .then(function () { S.integrations.busy = false; renderIntegrations(); });
}
function connectImportedGitHubApp() {
  var pending = S.integrations.pending;
  if (!pending || !S.integrations.selected) return;
  S.integrations.busy = true; S.integrations.error = ''; renderIntegrations();
  fetch('/v1/integrations/github/imports/' + encodeURIComponent(pending.pending_id) + '/connect', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ installation_id: Number(S.integrations.selected) })
  }).then(function (r) { return githubResponseError(r); })
    .then(function () { S.integrations.pending = null; S.integrations.target = ''; S.integrations.notice = 'GitHub is connected.'; loadGitHubIntegrations(); })
    .catch(function (e) { S.integrations.error = String(e.message || e); })
    .then(function () { S.integrations.busy = false; renderIntegrations(); });
}
function renderGitHubIntegrations() {
  var state = S.integrations, data = state.data || {}, h = '<section class="panel"><div class="panel-h"><h2>Integrations</h2>';
  h += '<span class="hint">Manage GitHub connections used by workers and project setup.</span><span class="spacer"></span>';
  if (consoleCan('integrations.manage')) h += '<button class="btn sm" data-act="github-refresh">Refresh</button>';
  h += '</div>';
  if (!consoleCan('integrations.manage')) h += '<div class="muted">Only an authenticated user with integration management permission can manage integrations.</div>';
  else {
    if (state.notice) h += '<div class="good" role="status">' + esc(state.notice) + '</div>';
    if (state.error) h += '<div class="stakeholder-error" role="alert">' + esc(state.error) + '</div>';
    if (state.busy) h += '<div class="muted">Loading GitHub connection…</div>';
    var list = state.connections || [];
    list.forEach(function (c) {
      h += '<div class="project-checks" data-github-connection="' + esc(c.id) + '"><div><strong>' + esc(c.label) + '</strong> · <span class="mono">' + esc(c.account) + '</span> (' + esc(c.account_type || 'account') + ')';
      h += ' · ' + (c.kind === 'app' ? 'GitHub App' : 'Access token') + ' · <span class="' + (c.status === 'connected' ? 'good' : 'stakeholder-error') + '">' + esc(c.status) + '</span></div>';
      if (c.warning) h += '<div class="stakeholder-error" role="note">' + esc(c.warning) + '</div>';
      h += '<div class="muted">' + esc((c.repositories || []).length) + ' reachable repositories' + ((c.repositories || []).length ? ': <span class="mono">' + esc((c.repositories || []).join(', ')) + '</span>' : '') + '</div>';
      h += '<div class="project-actions"><button class="btn sm" data-act="github-test" data-id="' + esc(c.id) + '">Test</button>';
      if (c.kind === 'token') h += '<input type="password" data-github-replace-token="' + esc(c.id) + '" placeholder="New token" autocomplete="off"><button class="btn sm" data-act="github-replace-token" data-id="' + esc(c.id) + '">Replace token</button>';
      else {
        if (c.settings_url) h += '<a class="btn sm" href="' + esc(safeHref(c.settings_url)) + '" target="_blank" rel="noopener">Add repositories on GitHub</a>';
        h += '<button class="btn sm" data-act="github-replace-app" data-id="' + esc(c.id) + '">Replace App</button>';
      }
      h += '<button class="btn sm" data-act="github-remove" data-id="' + esc(c.id) + '">Remove</button></div></div>';
    });
    if (!list.length && !state.busy) h += '<div class="muted">No GitHub connection yet.</div>';
    if (state.target && state.target !== 'new') h += '<div class="muted">The next GitHub App you add replaces connection ' + esc(state.target) + '.</div>';
    if (!state.pending) {
      h += '<h3>Add an access token</h3><p class="muted">Paste a personal or fine-grained token. AI Flow verifies it with GitHub, keeps it in the secret store and never shows it again. A GitHub App is recommended: a token reaches more, has no bot identity, and is never handed to a node.</p>';
      h += '<label>Access token<input type="password" data-github-token autocomplete="off"></label>';
      h += '<label>Account or organization (blank: the token owner)<input data-github-token-account autocomplete="off"></label>';
      h += '<label>Label<input data-github-token-label autocomplete="off"></label>';
      h += '<div class="project-actions"><button class="btn" data-act="github-add-token"' + (state.busy ? ' disabled' : '') + '>Verify and add token</button></div>';
      h += '<h3>Create a GitHub App</h3><p class="muted">GitHub will create an App in the organization you choose, then let you select the installation and repositories.</p>';
      h += '<label>GitHub organization<input data-github-org value="" placeholder="organization-name" autocomplete="off"></label>';
      h += '<div class="project-actions"><button class="btn primary" data-act="github-manifest"' + (state.busy ? ' disabled' : '') + '>Connect GitHub</button></div>';
      h += '<details><summary>Use an existing GitHub App</summary><p class="muted">Choose its PEM private key file. The file content is sent directly to the control plane secret store and is not displayed here.</p>';
      h += '<label>GitHub App ID<input data-github-app-id inputmode="numeric" autocomplete="off"></label>';
      h += '<label>Private key file<input type="file" accept=".pem,.key,application/x-pem-file" data-github-key-file></label>';
      h += '<div class="project-actions"><button class="btn primary" data-act="github-import"' + (state.busy ? ' disabled' : '') + '>Verify existing App</button></div></details>';
    } else {
      var pending = state.pending;
      h += '<h3>Choose the App installation</h3><p class="muted">' + esc(pending.app && pending.app.name || 'GitHub App') + ' has been verified. Install it on GitHub and select the repositories AI Flow can access, then refresh this list.</p>';
      h += '<div class="project-actions"><a class="btn" href="/v1/integrations/github/imports/' + encodeURIComponent(pending.pending_id) + '/install" target="_blank" rel="noopener">Install or configure on GitHub</a>';
      h += '<button class="btn" data-act="github-import-refresh"' + (state.busy ? ' disabled' : '') + '>Refresh installations</button></div>';
      if (state.installations.length) {
        h += '<label>Account or organization<select data-github-installation>' + state.installations.map(function (item) {
          var selected = String(item.id) === String(state.selected) ? ' selected' : '';
          return '<option value="' + esc(item.id) + '"' + selected + '>' + esc(item.login) + ' (' + esc(item.type || 'account') + ') · ' + esc(item.repository_selection) + '</option>';
        }).join('') + '</select></label>';
        h += '<div class="project-actions"><button class="btn primary" data-act="github-import-connect"' + (state.busy ? ' disabled' : '') + '>Connect selected installation</button></div>';
      } else h += '<div class="muted">No installation found yet. Install the App on GitHub, choose repositories, return here and refresh.</div>';
    }
    h += '<p class="small">Private keys and short lived access tokens are never shown in this page. GitHub connection changes are audited.</p>';
  }
  h += '</section>'; return h;
}
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

