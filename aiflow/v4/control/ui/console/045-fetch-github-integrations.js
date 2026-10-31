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
