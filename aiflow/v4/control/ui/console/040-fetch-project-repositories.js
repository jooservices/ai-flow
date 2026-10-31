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
