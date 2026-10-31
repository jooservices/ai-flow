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

