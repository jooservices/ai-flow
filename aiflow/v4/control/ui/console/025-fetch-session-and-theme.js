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
