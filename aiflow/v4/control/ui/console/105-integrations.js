/* ---------- Integrations: connect Jira (OAuth 2.0 3LO or API token fallback) ----------
   Secrets typed here go straight to the secret store; the API never returns them and the fields are cleared. */
var JIRA_SOURCE = { oauth: 'OAuth 2.0 (3LO)', api_token: 'Email and API token', owner_env: 'owner.env (legacy)', none: 'Not connected' };
function integHeaders() {
  var h = { 'Content-Type': 'application/json' };
  if (S.integ.ownerToken) h.Authorization = 'Bearer ' + S.integ.ownerToken;
  return h;
}
function integFetch(url, opts) {
  return fetch(url, Object.assign({ cache: 'no-store', credentials: 'same-origin', headers: integHeaders() }, opts || {}))
    .then(function (r) { return r.json().then(function (d) { if (!r.ok) throw new Error(d.detail || ('HTTP ' + r.status)); return d; }); });
}
function loadJiraIntegrations() {
  var g = S.integ;
  if (g.loading) return;
  var q = /[?&]jira=([a-z]+)/.exec(location.search || '');
  if (q) {
    g.notice = { connected: 'Jira connected.', denied: 'Consent was not given on Atlassian.', failed: 'Atlassian refused the connection; check the OAuth app and try again.' }[q[1]] || '';
    try { history.replaceState(null, '', '/integrations'); } catch (e) { /* file preview */ }
  }
  g.loading = true; g.error = '';
  integFetch('/v1/integrations/jira').then(function (d) { g.jira = d; g.loading = false; if (S.view === 'integrations') renderIntegrations(); })
    .catch(function (e) { g.error = String(e.message || e); g.loading = false; if (S.view === 'integrations') renderIntegrations(); });
}
function renderJiraIntegrations() {
  var g = S.integ, j = g.jira;
  if (!consoleOwner()) return '<section class="panel"><h1>Integrations</h1><div class="muted">Only a role with integration management permission can manage Jira.</div></section>';
  var h = '<section class="panel"><div class="panel-h"><h1 style="margin:0;font:600 20px var(--f-cond)">Integrations</h1>' +
    '<span class="hint">Connect AI Flow to Jira Cloud. Users with integration management permission can connect or disconnect.</span><span class="spacer"></span>' +
    '<button class="btn" data-act="integ-refresh">Refresh</button></div>' +
    '<div class="knowledge-form"><label class="review-auth">Owner access token (not needed with an owner console session)<input type="password" autocomplete="new-password" data-integ-owner-token value="' + esc(g.ownerToken) + '" aria-label="Owner access token"></label></div>';
  if (g.notice) h += '<div class="small" role="status">' + esc(g.notice) + '</div>';
  if (g.error) h += '<div role="alert" style="color:var(--crit)">' + esc(g.error) + '</div>';
  h += '</section>';
  if (!j) return h + (g.loading ? '<div class="empty">Loading…</div>' : '');
  h += '<div class="knowledge-grid"><section class="panel"><div class="panel-h"><h2 style="margin:0">Jira</h2><span class="pill" data-tone="' +
    (j.needs_reconnect ? 'crit' : j.connected ? 'ok' : 'warn') + '">' + (j.needs_reconnect ? 'Reconnect needed' : j.connected ? 'Connected' : 'Not connected') + '</span></div>';
  if (j.needs_reconnect) h += '<div role="alert" style="color:var(--crit)">The Jira token could not be refreshed' + (j.error ? ' (' + esc(j.error) + ')' : '') + '. Press Connect Jira again.</div>';
  if (j.state === 'disconnected') h += '<div class="small">Disconnected by ' + esc(j.disconnected_by || 'owner') + ' · ' + esc(j.disconnected_at ? when(j.disconnected_at) : '') + '. jira_sync is stopped.</div>';
  var rows = [['Credential', JIRA_SOURCE[j.source] || j.source], ['Site', j.site_name ? j.site_name + (j.site_url ? ' · ' + j.site_url : '') : '—'],
    ['Cloud id', j.cloud_id || '—'], ['Account', j.account || '—'], ['Scopes', (j.scopes || []).join(' ') || '—'],
    ['Expires at', j.expires_at ? when(j.expires_at) + ' (refreshed automatically)' : '—'],
    ['Connected', j.connected_at ? when(j.connected_at) + (j.connected_by ? ' by ' + j.connected_by : '') : '—']];
  h += '<dl class="knowledge-history">' + rows.map(function (r) { return '<div><dt class="small">' + esc(r[0]) + '</dt><dd style="margin:0">' + esc(r[1]) + '</dd></div>'; }).join('') + '</dl>';
  if (j.source === 'owner_env') h += '<div class="small">Running on JIRA_EMAIL / JIRA_API_TOKEN from owner.env. Connect below; the UI credential then takes priority.</div>';
  if (j.source === 'oauth' || j.source === 'api_token') h += '<div class="knowledge-actions"><button class="btn" data-act="integ-disconnect"' + (g.busy ? ' disabled' : '') + '>Disconnect</button></div>';
  h += '</section><section class="panel"><div class="panel-h"><h2 style="margin:0">Connect Jira (OAuth 2.0)</h2></div><div class="knowledge-form">' +
    '<div class="small">Create an OAuth 2.0 (3LO) app at developer.atlassian.com with callback URL <code>' + esc(j.redirect_uri) + '</code> and scopes <code>' + esc((j.requested_scopes || []).join(' ')) + '</code>.</div>' +
    '<label>Client id<input data-integ-field="client_id" value="' + esc((j.oauth_app && j.oauth_app.client_id) || '') + '" autocomplete="off" spellcheck="false"></label>' +
    '<label>Client secret' + (j.oauth_app && j.oauth_app.configured ? ' (stored; leave empty to keep it)' : '') + '<input type="password" data-integ-field="client_secret" autocomplete="new-password" spellcheck="false"></label>' +
    '<div class="knowledge-actions"><button class="btn primary" data-act="integ-connect"' + (g.busy ? ' disabled' : '') + '>Connect Jira</button></div></div>' +
    '<div class="panel-h" style="margin-top:14px"><h2 style="margin:0">Fallback: email and API token</h2></div><div class="knowledge-form">' +
    '<div class="small">For a control plane without a public callback URL. The token is stored in the secret store and never shown again.</div>' +
    '<label>Site<input data-integ-field="site" value="' + esc(j.site_url || '') + '" placeholder="https://your-site.atlassian.net" autocomplete="off"></label>' +
    '<label>Atlassian email<input data-integ-field="email" autocomplete="off" spellcheck="false"></label>' +
    '<label>API token<input type="password" data-integ-field="api_token" autocomplete="new-password" spellcheck="false"></label>' +
    '<div class="knowledge-actions"><button class="btn" data-act="integ-api-token"' + (g.busy ? ' disabled' : '') + '>Save API token</button></div></div></section></div>';
  return h;
}
function loadIntegrations() { loadGitHubIntegrations(); loadJiraIntegrations(); }
function loadNotifications() {
  if (!consoleCan('notifications.manage')) return;
  var state = S.notifications; state.busy = true; state.error = ''; renderNotifications();
  fetch('/v1/notifications', { cache: 'no-store' }).then(function (r) { return projectResponseError(r); })
    .then(function (d) { state.data = d; state.error = ''; if (!state.preferencesDraft || !state.preferencesDirty) state.preferencesDraft = JSON.parse(JSON.stringify(d.preferences)); })
    .catch(function (e) { state.error = String(e.message || e); })
    .then(function () { state.busy = false; renderNotifications(); });
}
function loadRuntimeSettings() {
  if (!consoleCan('console.manage')) return;
  var state = S.runtimeSettings; state.busy = true; state.error = ''; renderRuntimeSettings();
  fetch('/v1/runtime-settings', { cache: 'no-store' }).then(function (r) { return projectResponseError(r); })
    .then(function (d) {
      state.data = d; state.error = '';
      state.changes = d.changes || [];
      if (!state.project && (d.projects || []).length) state.project = d.projects[0];
      state.changes.filter(function (change) { return change.status === 'running' || change.status === 'rolling_back'; })
        .forEach(function (change) { pollRuntimeSettingChange(change.id); });
    }).catch(function (e) { state.error = String(e.message || e); })
    .then(function () { state.busy = false; renderRuntimeSettings(); });
}
function pollRuntimeSettingChange(changeId) {
  fetch('/v1/runtime-settings/changes/' + encodeURIComponent(changeId), { cache: 'no-store' })
    .then(function (r) { return projectResponseError(r); })
    .then(function (change) {
      var state = S.runtimeSettings;
      state.changes = [change].concat((state.changes || []).filter(function (item) { return item.id !== change.id; }));
      if (S.view === 'settings') renderRuntimeSettings();
      if (change.status === 'running' || change.status === 'rolling_back') setTimeout(function () { pollRuntimeSettingChange(change.id); }, 2500);
      else if (change.status === 'completed') {
        state.notice = 'All affected services restarted and passed their health checks.';
        loadRuntimeSettings();
      } else {
        var failed = (change.operations || []).filter(function (item) {
          return item.status === 'failed' && !(item.result && item.result.cancelled);
        })[0];
        var service = failed && failed.result && (failed.result.failed_service || failed.result.error);
        if (change.recovery_status === 'restored') {
          state.error = 'Restart failed' + (service ? ' on ' + failed.node + ': ' + service : '') +
            '. The previous value was restored on all affected machines.';
        } else if (change.recovery_status === 'failed') {
          var recovery = change.recovery_error || 'rollback did not pass its health check';
          state.error = 'Restart failed' + (service ? ' on ' + failed.node + ': ' + service : '') +
            '. Recovery failed on ' + (change.recovery_node || 'an affected machine') + ': ' + recovery +
            '. The previous value could not be confirmed across the fleet, and affected machines remain paused.';
        } else {
          state.error = service ? 'Restart failed on ' + failed.node + ': ' + service + '. Recovery is still pending.' :
            'Restart failed. Recovery is still pending.';
        }
        renderRuntimeSettings();
      }
    }).catch(function () { setTimeout(function () { pollRuntimeSettingChange(changeId); }, 5000); });
}
function saveRuntimeSetting() {
  var state = S.runtimeSettings;
  if (state.busy) return;
  var scope = $('[data-runtime-scope]'), project = $('[data-runtime-project]'), key = $('[data-runtime-key]'), value = $('[data-runtime-value]');
  state.scope = scope ? scope.value : state.scope;
  state.project = project ? project.value : state.project;
  state.key = key ? key.value.trim() : '';
  var secretValue = value ? value.value : '';
  if (value) value.value = '';
  state.value = '';
  if (!state.key || !secretValue) { state.error = 'Enter a key name and value.'; renderRuntimeSettings(); return; }
  var body = { scope: state.scope, project: state.scope === 'project' ? state.project : '', key: state.key, value: secretValue };
  state.busy = true; state.error = ''; state.notice = ''; state.key = '';
  renderRuntimeSettings();
  fetch('/v1/runtime-settings', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
    .then(function (r) { return projectResponseError(r); })
    .then(function (change) {
      state.notice = 'Change queued. New work is paused on affected machines until active work drains and services pass health checks.';
      state.changes = [{ id: change.change_id, status: 'running', operations: (change.operations || []).map(function (op) {
        return { node: op.node, status: 'queued', services: change.services || [], result: {} };
      }) }].concat(state.changes || []);
      pollRuntimeSettingChange(change.change_id);
    }).catch(function (e) { state.error = String(e.message || e); })
    .then(function () { state.busy = false; renderRuntimeSettings(); });
}
function renderRuntimeSettings() {
  var root = $('#view-settings'); if (!root) return;
  var state = S.runtimeSettings, data = state.data || {}, projects = data.projects || [], items = data.settings || [];
  if (!consoleCan('console.manage')) { root.innerHTML = '<section class="panel"><p>Settings are available to owners and administrators.</p></section>'; return; }
  if (!state.project && projects.length) state.project = projects[0];
  var services = state.scope === 'project' ? (data.project_services || {})[state.project] || [] : data.secret_services || [];
  var h = (window.AIFlowQuotaRefresh ? window.AIFlowQuotaRefresh.panel(state, esc, when, renderRuntimeSettings, projectResponseError) : '') + '<section class="panel"><div class="panel-h"><h2>Runtime settings</h2><button class="btn sm" data-act="runtime-settings-refresh"' + (state.busy ? ' disabled' : '') + '>Refresh</button></div>' +
    '<p class="hint">Values are never shown after entry. The control APIs restart one at a time; project workers drain active work before they restart.</p>' +
    '<div class="project-form"><label>Location<select class="in" data-runtime-scope>' +
    '<option value="secret"' + (state.scope === 'secret' ? ' selected' : '') + '>Control runtime secret or setting</option>' +
    '<option value="project"' + (state.scope === 'project' ? ' selected' : '') + '>Project environment</option></select></label>';
  if (state.scope === 'project') h += '<label>Project<select class="in" data-runtime-project>' + projects.map(function (project) {
    return '<option value="' + esc(project) + '"' + (state.project === project ? ' selected' : '') + '>' + esc(project) + '</option>';
  }).join('') + '</select></label>';
  h += '<label>Key name<input class="in" data-runtime-key autocomplete="off" spellcheck="false" placeholder="EXTERNAL_API_TOKEN" value="' + esc(state.key || '') + '"></label>' +
    '<label>Value<input class="in" data-runtime-value type="password" autocomplete="new-password" spellcheck="false" placeholder="Enter value" value="' + esc(state.value || '') + '"></label>' +
    '<div class="wide"><div class="small">Affected services</div><div class="chiprow">' +
    (services.length ? services.map(function (service) { return '<span class="pill">' + esc(service) + '</span>'; }).join('') : '<span class="hint">No online service targets are available for this selection.</span>') +
    '</div></div><div class="wide project-actions"><button class="btn primary" data-act="runtime-setting-save"' +
    (state.busy || !services.length ? ' disabled' : '') + '>Save and restart safely</button><span class="hint">Active work finishes before workers restart. A failed health check restores the previous file value.</span></div></div>';
  if (state.notice) h += '<p class="notice" role="status">' + esc(state.notice) + '</p>';
  if (state.error) h += '<p class="error" role="alert">' + esc(state.error) + '</p>';
  h += '</section><section class="panel"><div class="panel-h"><h2>Stored keys</h2><span class="hint">Names and set state only</span></div><div class="tablewrap"><table><thead><tr><th>Location</th><th>Key</th><th>Type</th><th>State</th><th>Last changed</th><th>Machines</th></tr></thead><tbody>';
  if (!items.length) h += '<tr><td colspan="6" class="empty">No runtime keys are stored yet.</td></tr>';
  else h += items.map(function (item) { return '<tr><td>' + esc(item.scope === 'project' ? 'Project · ' + item.project : 'Control') +
    '</td><td><code>' + esc(item.name) + '</code></td><td>' + (item.is_secret ? 'Secret' : 'Setting') + '</td><td>' +
    (item.is_set ? '<span class="badge ok">Set</span>' : '<span class="badge">Empty</span>') + '</td><td>' +
    esc(item.changed_at ? when(item.changed_at) : '—') + '</td><td>' + esc((item.nodes || []).join(', ') || 'Control') + '</td></tr>'; }).join('');
  h += '</tbody></table></div></section>';
  var changes = state.changes || [];
  if (changes.length) {
    h += '<section class="panel"><div class="panel-h"><h2>Restart results</h2></div><div class="stack">' + changes.slice(0, 8).map(function (change) {
      var status = change.status === 'completed' ? '<span class="badge ok">Succeeded</span>' : change.status === 'failed' ? '<span class="badge danger">Failed</span>' : change.status === 'rolling_back' ? '<span class="badge">Rolling back</span>' : '<span class="badge">Running</span>';
      var operations = (change.operations || []).map(function (op) {
        var detail = op.result && op.result.cancelled ? 'Not applied after a peer failed' :
          op.status === 'failed' ? ((op.result && (op.result.failed_service || op.result.error)) || 'Restart failed') : op.status;
        return '<div class="small">' + esc(op.node) + ' · ' + esc(detail) + '</div>';
      }).join('');
      var rollbacks = (change.rollbacks || []).map(function (op) {
        var detail = op.status === 'failed' ? ((op.result && op.result.error) || 'Rollback failed') : op.status;
        return '<div class="small">' + esc(op.node) + ' · recovery · ' + esc(detail) + '</div>';
      }).join('');
      return '<div class="review-card"><div class="panel-h">' + status + '<code>' + esc(change.id) + '</code></div>' + operations + rollbacks + '</div>';
    }).join('') + '</div></section>';
  }
  root.innerHTML = h;
}
function renderIntegrations() {
  var root = $('#view-integrations'); if (!root) return;
  root.innerHTML = renderGitHubIntegrations() + renderJiraIntegrations();
}
function integValue(name) { var el = $('[data-integ-field="' + name + '"]'); return el ? String(el.value || '').trim() : ''; }
function integClearSecrets() { $$('#view-integrations input[type="password"][data-integ-field]').forEach(function (el) { el.value = ''; }); }
function integConnect() {
  var g = S.integ; g.busy = true;
  var body = JSON.stringify({ client_id: integValue('client_id'), client_secret: integValue('client_secret') });
  integClearSecrets();
  integFetch('/v1/integrations/jira/oauth/start', { method: 'POST', body: body })
    .then(function (d) { location.assign(d.authorize_url); })
    .catch(function (e) { g.busy = false; toast('Connect Jira failed', String(e.message || e), 'crit'); renderIntegrations(); });
}
function integApiToken() {
  var g = S.integ; g.busy = true;
  var body = JSON.stringify({ site: integValue('site'), email: integValue('email'), api_token: integValue('api_token') });
  integClearSecrets();
  integFetch('/v1/integrations/jira/api-token', { method: 'POST', body: body })
    .then(function (d) { g.busy = false; g.jira = d; toast('Jira connected', 'Email and API token stored.', 'ok'); renderIntegrations(); })
    .catch(function (e) { g.busy = false; toast('API token refused', String(e.message || e), 'crit'); renderIntegrations(); });
}
function integDisconnect() {
  var g = S.integ;
  if (!window.confirm('Disconnect Jira? jira_sync and Jira writes stop until Jira is connected again.')) return;
  g.busy = true;
  integFetch('/v1/integrations/jira/disconnect', { method: 'POST', body: '{}' })
    .then(function (d) { g.busy = false; g.jira = d; toast('Jira disconnected', d.revoked ? 'The token was revoked at Atlassian.' : 'The credential was deleted.', 'warn'); renderIntegrations(); })
    .catch(function (e) { g.busy = false; toast('Disconnect failed', String(e.message || e), 'crit'); renderIntegrations(); });
}

