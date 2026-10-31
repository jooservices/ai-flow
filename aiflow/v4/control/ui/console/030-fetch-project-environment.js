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
