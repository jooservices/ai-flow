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

