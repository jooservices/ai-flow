/* Security console extension: keep the auth controls outside the legacy console template. */
VIEW_PERMISSION.security = 'console.read';
VIEW_PATH.security = '/security';
VIEW_GROUPS[2].push('security');
var securityMenuIndex = MENU_ITEMS.findIndex(function (item) { return item.view === 'notifications'; });
MENU_ITEMS.splice(securityMenuIndex, 0, { view: 'security', label: 'Security' });
HINTS.security = 'Set up two-factor sign-in and recovery codes, set the session length, sign sessions out and read the audit log.';
var originalViewOfWithSecurity = viewOf;
viewOf = function (path) { return path.indexOf('/security') === 0 ? 'security' : originalViewOfWithSecurity(path); };
S.view = viewOf(location.pathname);
var originalShowViewWithSecurity = showView;
showView = function () {
  originalShowViewWithSecurity();
  var panel = $('#view-security'); if (panel) panel.hidden = S.view !== 'security';
};
/* ---------- security: two-factor, sessions, people, audit log (docs/v4/security.md) ---------- */
var SEC = { mfa: null, settings: null, users: null, sessions: null, audit: null, category: '', setup: null, codes: null, code: '', msg: '', err: '', busy: false };
function secApi(url, method, body) {
  var o = { cache: 'no-store', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' } };
  if (method) o.method = method;
  if (body !== undefined) o.body = JSON.stringify(body);
  return fetch(url, o).then(function (r) {
    return r.json().catch(function () { return {}; }).then(function (d) { if (!r.ok) throw new Error(d.detail || ('HTTP ' + r.status)); return d; });
  });
}
function secFail(e) { SEC.err = e && e.message || 'Request failed'; SEC.busy = false; renderSecurity(); }
function loadSecurity() {
  secApi('/v1/security/mfa').then(function (d) {
    SEC.mfa = d; renderSecurity();
    if (d.can_manage_security) {
      secApi('/v1/security/settings').then(function (x) { SEC.settings = x; renderSecurity(); }).catch(secFail);
      secApi('/v1/security/users').then(function (x) { SEC.users = x.users; renderSecurity(); }).catch(secFail);
      secApi('/v1/security/sessions').then(function (x) { SEC.sessions = x.sessions; renderSecurity(); }).catch(secFail);
    }
    loadAuditLog();
  }).catch(secFail);
}
function loadAuditLog() {
  secApi('/v1/security/audit?limit=100' + (SEC.category ? '&category=' + encodeURIComponent(SEC.category) : ''))
    .then(function (x) { SEC.audit = x; renderSecurity(); }).catch(function () { SEC.audit = null; renderSecurity(); });
}
function renderSecurity() {
  var root = $('#view-security'); if (!root) return;
  var m = SEC.mfa, h = '<h1 class="dash-title">Security</h1><p class="lead">Two-factor sign-in, session length, forced sign-out and the audit log.</p>';
  if (SEC.err) h += '<div class="notice err" role="alert">' + esc(SEC.err) + '</div>';
  if (SEC.msg) h += '<div class="notice" role="status">' + esc(SEC.msg) + '</div>';
  if (m && m.setup_required) h += '<div class="notice err" role="alert">Your role must use two-factor sign-in. Set it up below to continue.</div>';
  h += '<section class="panel"><div class="panel-h"><h2>Two-factor sign-in</h2><span class="hint">Your account</span></div>';
  if (!m) h += '<div class="empty">Loading…</div>';
  else if (!m.enabled) {
    h += '<p>Two-factor sign-in is <b>off</b>. Use an authenticator app (any app that supports TOTP).</p>';
    if (SEC.setup) {
      h += '<p>Add this key to your authenticator app, then enter the 6-digit code it shows.</p>';
      h += '<p><code data-sec-secret>' + esc(SEC.setup.secret) + '</code></p>';
      h += '<p class="small">Or open this link on your phone: <code>' + esc(SEC.setup.otpauth_uri) + '</code></p>';
      h += '<form data-sec-form="enable"><label>Code <input data-sec-code value="' + esc(SEC.code) + '" inputmode="numeric" autocomplete="one-time-code" maxlength="8"></label> ';
      h += '<button class="btn primary" type="submit"' + (SEC.busy ? ' disabled' : '') + '>Turn on two-factor</button></form>';
    } else h += '<button class="btn primary" data-sec="setup"' + (SEC.busy ? ' disabled' : '') + '>Set up two-factor sign-in</button>';
  } else {
    h += '<p>Two-factor sign-in is <b>on</b>. Recovery codes left: <b>' + Number(m.recovery_codes_left) + '</b>.</p>';
    h += '<form data-sec-form="codes"><label>Current code <input data-sec-code value="' + esc(SEC.code) + '" autocomplete="one-time-code" maxlength="32"></label> ';
    h += '<button class="btn" type="submit"' + (SEC.busy ? ' disabled' : '') + '>Issue new recovery codes</button> ';
    h += m.required ? '' : '<button class="btn" type="button" data-sec="disable"' + (SEC.busy ? ' disabled' : '') + '>Turn off</button>';
    h += '</form>';
  }
  if (SEC.codes) {
    h += '<div class="notice" role="status"><b>Recovery codes</b> — each works once if you lose your authenticator. They are shown only now; store them safely.</div><pre data-sec-codes>';
    SEC.codes.forEach(function (c) { h += esc(c) + '\n'; });
    h += '</pre>';
  }
  h += '</section>';
  if (m && m.can_manage_security) {
    var s = SEC.settings;
    h += '<section class="panel"><div class="panel-h"><h2>Session length and sign-in rules</h2><span class="hint">Applies to the next sign-in</span></div>';
    if (!s) h += '<div class="empty">Loading…</div>';
    else {
      h += '<form data-sec-form="settings"><label>Session length (hours) <input data-sec-ttl type="number" min="' + Number(s.session_ttl_min_s / 3600) + '" max="' + Number(s.session_ttl_max_s / 3600) + '" step="0.5" value="' + Number(s.session_ttl_s / 3600) + '"></label> ';
      h += '<label><input data-sec-require type="checkbox" checked disabled> Require two-factor sign-in for administrators (' + esc((s.mfa_roles || []).join(', ')) + ')</label> ';
      h += '<button class="btn primary" type="submit"' + (SEC.busy ? ' disabled' : '') + '>Save</button></form>';
      h += '<p class="small">Secret encryption key: ' + (s.secret_key_configured ? 'configured' : 'not configured (set AIFLOW4_SECRET_KEY)') + ' · rate limits: ' + Number(s.rate_limit_per_min) + ' requests/min per address, ' + Number(s.rate_limit_public_per_min) + '/min on public endpoints.</p>';
    }
    h += '</section><section class="panel"><div class="panel-h"><h2>Signed-in sessions</h2><span class="hint">Forced sign-out ends the session at once</span></div>';
    if (!SEC.sessions) h += '<div class="empty">Loading…</div>';
    else if (!SEC.sessions.length) h += '<div class="empty">No sessions with tracking yet.</div>';
    else {
      h += '<div class="tablebox"><table><thead><tr><th>Person</th><th>Role</th><th>From</th><th>Started</th><th>Expires</th><th></th></tr></thead><tbody>';
      SEC.sessions.forEach(function (x) {
        h += '<tr><td>' + esc(x.email) + (x.current ? ' (this session)' : '') + '</td><td>' + esc(x.role) + '</td><td>' + esc(x.ip) + '</td><td>' + esc(x.created_at) + '</td><td>' + esc(x.expires_at) + '</td>';
        h += '<td><button class="btn" data-sec="revoke" data-sid="' + esc(x.sid) + '">Sign out</button></td></tr>';
      });
      h += '</tbody></table></div>';
    }
    h += '</section><section class="panel"><div class="panel-h"><h2>People</h2><span class="hint">Two-factor status and recovery</span></div>';
    if (!SEC.users) h += '<div class="empty">Loading…</div>';
    else {
      h += '<div class="tablebox"><table><thead><tr><th>Person</th><th>Role</th><th>Two-factor</th><th>Recovery codes</th><th>Sessions</th><th>State</th><th></th></tr></thead><tbody>';
      SEC.users.forEach(function (u) {
        h += '<tr><td>' + esc(u.email) + '</td><td>' + esc(u.role) + '</td><td>' + (u.totp_enabled ? 'on' : 'off') + '</td><td>' + Number(u.recovery_codes_left) + '</td><td>' + Number(u.sessions) + '</td>';
        h += '<td>' + (u.disabled ? 'disabled' : u.locked ? 'locked' : 'active') + '</td><td>';
        h += '<button class="btn" data-sec="user-signout" data-email="' + esc(u.email) + '">Sign out everywhere</button> ';
        h += '<button class="btn" data-sec="user-mfa-reset" data-email="' + esc(u.email) + '">Reset two-factor</button> ';
        h += '<button class="btn" data-sec="user-unlock" data-email="' + esc(u.email) + '">Unlock</button></td></tr>';
      });
      h += '</tbody></table></div>';
    }
    h += '</section>';
  }
  var a = SEC.audit;
  if (a) {
    h += '<section class="panel"><div class="panel-h"><h2>Audit log</h2><span class="hint">Read only · changes to settings, people, roles and credentials</span></div>';
    h += '<label>Show <select data-sec-category><option value="">everything</option>';
    (a.categories || []).forEach(function (c) { h += '<option value="' + esc(c) + '"' + (c === SEC.category ? ' selected' : '') + '>' + esc(c) + '</option>'; });
    h += '</select></label>';
    if (!a.entries.length) h += '<div class="empty">Nothing recorded yet.</div>';
    else {
      h += '<div class="tablebox"><table><thead><tr><th>When</th><th>Who</th><th>Area</th><th>What</th><th>Subject</th><th>From</th></tr></thead><tbody>';
      a.entries.forEach(function (e) {
        h += '<tr><td>' + esc(e.ts) + '</td><td>' + esc(e.actor) + '</td><td>' + esc(e.category) + '</td><td>' + esc(e.action) + '</td><td>' + esc(e.target) + '</td><td>' + esc(e.ip) + '</td></tr>';
      });
      h += '</tbody></table></div>';
    }
    h += '</section>';
  }
  root.innerHTML = h;
}
function secDo(promise, done) {
  SEC.busy = true; SEC.err = ''; SEC.msg = ''; renderSecurity();
  promise.then(function (d) { SEC.busy = false; done(d); renderSecurity(); }).catch(secFail);
}
document.addEventListener('click', function (e) {
  var el = e.target.closest && e.target.closest('[data-sec]'); if (!el) return;
  var act = el.getAttribute('data-sec'), email = el.getAttribute('data-email'), sid = el.getAttribute('data-sid');
  if (act === 'setup') secDo(secApi('/v1/security/mfa/setup', 'POST'), function (d) { SEC.setup = d; SEC.code = ''; });
  else if (act === 'disable') {
    if (!SEC.code) { SEC.err = 'Enter a current code first.'; renderSecurity(); return; }
    secDo(secApi('/v1/security/mfa/disable', 'POST', { code: SEC.code }), function () { SEC.code = ''; SEC.msg = 'Two-factor sign-in is off.'; loadSecurity(); });
  }
  else if (act === 'revoke') secDo(secApi('/v1/security/sessions/' + encodeURIComponent(sid) + '/revoke', 'POST'), function () { SEC.msg = 'Session ended.'; loadSecurity(); });
  else if (act === 'user-signout') secDo(secApi('/v1/security/users/' + encodeURIComponent(email) + '/sign-out', 'POST'), function () { SEC.msg = 'Signed out everywhere.'; loadSecurity(); });
  else if (act === 'user-mfa-reset') {
    if (!window.confirm('Reset two-factor sign-in for ' + email + '? They are signed out and must set it up again.')) return;
    secDo(secApi('/v1/security/users/' + encodeURIComponent(email) + '/mfa/reset', 'POST'), function () { SEC.msg = 'Two-factor sign-in reset.'; loadSecurity(); });
  }
  else if (act === 'user-unlock') secDo(secApi('/v1/security/users/' + encodeURIComponent(email) + '/unlock', 'POST'), function () { SEC.msg = 'Account unlocked.'; loadSecurity(); });
});
document.addEventListener('input', function (e) {
  if (e.target.matches && e.target.matches('[data-sec-code]')) SEC.code = e.target.value;
});
document.addEventListener('change', function (e) {
  if (e.target.matches && e.target.matches('[data-sec-category]')) { SEC.category = e.target.value; loadAuditLog(); }
});
document.addEventListener('submit', function (e) {
  var kind = e.target.getAttribute && e.target.getAttribute('data-sec-form'); if (!kind) return;
  e.preventDefault();
  if (kind === 'enable') secDo(secApi('/v1/security/mfa/enable', 'POST', { code: SEC.code }), function (d) {
    SEC.codes = d.recovery_codes; SEC.setup = null; SEC.code = ''; SEC.msg = 'Two-factor sign-in is on.'; loadSecurity();
  });
  else if (kind === 'codes') secDo(secApi('/v1/security/mfa/recovery-codes', 'POST', { code: SEC.code }), function (d) {
    SEC.codes = d.recovery_codes; SEC.code = ''; loadSecurity();
  });
  else if (kind === 'settings') {
    var hours = Number($('[data-sec-ttl]').value), body = { session_ttl_s: Math.round(hours * 3600), require_mfa: $('[data-sec-require]').checked };
    secDo(secApi('/v1/security/settings', 'PUT', body), function (d) { SEC.settings = d; SEC.msg = 'Saved.'; loadSecurity(); });
  }
});
