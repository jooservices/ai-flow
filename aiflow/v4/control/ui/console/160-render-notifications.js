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


