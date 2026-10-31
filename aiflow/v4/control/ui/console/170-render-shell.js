function render() {
  if (S.layouts.drag) return;
  renderLive();
  renderFooter();
  renderLicenseNotice();
  if (S.view === 'cc') renderDashboard();
  else if (S.view === 'work' || S.view === 'delivery') { renderDHead(); renderDBody(); }
  else if (S.view === 'sa') renderSA();
  else if (S.view === 'users') renderUsers();
  else if (S.view === 'security') renderSecurity();
  else if (S.view === 'lead') renderLead();
  else if (S.view === 'business') renderBusiness();
  else if (S.view === 'review') renderReviews();
  else if (S.view === 'stakeholder') renderStakeholder();
  else if (S.view === 'machines') renderMachines();
  else if (S.view === 'knowledge') renderKnowledge();
  else if (S.view === 'backup') renderBackup();
  else if (S.view === 'integrations') renderIntegrations();
  else if (S.view === 'projects') renderProjects();
  else if (S.view === 'licensing') renderLicensing();
  else if (S.view === 'notifications') renderNotifications();
  else if (S.view === 'settings') renderRuntimeSettings();
  else if (S.view === 'support') renderSupport();
  else { renderDHead(); renderDBody(); }
  if (S.drawer) { if (S.drawer.kind === 'ticket' && S.data) loadTicket(S.drawer.id); renderDrawer(); }
  tick();
}
function tick() {
  var s = since();
  $$('[data-since]').forEach(function (el) {
    var v = el.getAttribute('data-since'); if (v === '') { el.textContent = 'never'; return; }
    var sec = Number(v) + s;
    el.textContent = el.getAttribute('data-fmt') === 'ago' ? ago(sec) : mmss(sec);
  });
  $$('[data-clock]').forEach(function (el) {
    var sec = Number(el.getAttribute('data-clock')) + s, hard = Number(el.getAttribute('data-hard')) || 3600, p = Math.min(100, 100 * sec / hard);
    el.style.width = p.toFixed(1) + '%'; el.setAttribute('data-tone', p >= 75 ? 'crit' : p >= 50 ? 'warn' : 'ok');
  });
  $$('[data-countdown]').forEach(function (el) {
    var left = Math.max(0, Math.ceil((new Date(el.getAttribute('data-countdown')).getTime() - Date.now()) / 1000));
    el.textContent = left ? mmss(left) + ' remaining' : 'Expired';
  });
  renderLive();
}
/* Help: every screen has a page in the documentation site (docs/guide/screens/<view>.md, served at /help/) and a
   short hint for first use that stays away once dismissed. */
var HELP_BASE = '/help/screens/';
var HINTS = {
  cc: 'Start here: nodes, AI accounts, runs and incidents. Open an incident for its detail and a runbook.',
  work: 'Pick a project to see its tickets by state. Open a ticket for its runs, questions and links.',
  machines: 'Machines that run AI work and the AI accounts on them: heartbeat, slots and sign-in state.',
  users: 'See the role and screen permissions of each person. Add people, change roles and project limits, or disable a person here; every change is audited.',
  delivery: 'Pick a project to see its board. "Needs attention" lists tickets that wait on a person or did not finish.',
  sa: 'Read-only health of nodes and accounts, with advice on capacity. Nothing here changes the fleet.',
  lead: 'Choose a project to check that each epic has enough tasks for its acceptance criteria, then export a report.',
  business: 'Epics, questions waiting for your answer and change requests. Answer questions in Jira.',
  review: 'Tickets waiting for a person before merge. Enter the reviewer access token to approve or send back.',
  stakeholder: 'Open the test environment of a project, try the change, then record acceptance or feedback.',
  knowledge: 'Study cases, skills, guides and policies the AI workers read. Every save creates a new version.',
  backup: 'Review backup and weekly restore status, set a schedule and destination, and save the encryption key separately.',
  integrations: 'Connect Jira and GitHub first. Secrets are stored on the control machine and never shown.',
  projects: 'Add a Jira project, then check the preflight. Pause or remove a project here.',
  licensing: 'Activate a licence key online or from a signed file, check plan limits and usage, and export the usage report.',
  support: 'Build a diagnostic bundle with one button, read its inventory, then download it to inspect before you send it. Secrets and personal data are removed.',
  notifications: 'Set up alert channels and routing, then review recent delivery results. Saved credentials stay hidden.',
  settings: 'Review write-only runtime keys, see affected services, and save a value with a safe restart.'
};
function helpUrl(view) { return HELP_BASE + view + '.html'; }
function hintSeen(view) { try { return localStorage.getItem('aiflow4-hint-' + view) === '1'; } catch (e) { return false; } }
function renderHelp() {
  var link = document.getElementById('help-link');
  if (link) link.setAttribute('href', helpUrl(S.view));
  var box = document.getElementById('firstuse');
  if (!box) return;
  box.innerHTML = (HINTS[S.view] && !hintSeen(S.view))
    ? '<div class="firstuse" role="note"><span>' + esc(HINTS[S.view]) + ' <a href="' + esc(helpUrl(S.view)) + '" target="_blank" rel="noopener">Read more in Help</a></span><button class="btn sm" data-act="hint-dismiss">Got it</button></div>'
    : '';
}
function showView() {
  $$('.tab').forEach(function (t) { t.setAttribute('aria-selected', String(t.getAttribute('data-view') === S.view)); });
  ['cc', 'work', 'delivery', 'sa', 'lead', 'business', 'review', 'stakeholder', 'machines', 'users', 'knowledge', 'backup', 'integrations', 'projects', 'licensing', 'notifications', 'settings', 'support'].forEach(function (v) {
    var panel = $('#view-' + v); if (panel) panel.hidden = S.view !== v;
  });
  renderHelp();
}
function setView(v) {
  if (S.consoleUser && !viewAllowed(v)) {
    toast('View unavailable', 'This view is not available for your role.', 'warn'); return;
  }
  if (S.view === v) return;
  S.view = v;
  S.layouts.mode = '';
  $$('.tab').forEach(function (t) { t.setAttribute('aria-selected', String(t.getAttribute('data-view') === v)); });
  showView();
  var viewPath = VIEW_PATH[v] || '/cc';
  if (v === 'work' && S.workStatusFilter && S.workStatusFilter !== 'all') viewPath += '?status=' + encodeURIComponent(S.workStatusFilter);
  try { history.pushState(null, '', viewPath); } catch (e) { /* file preview */ }
  render();
  if (v === 'cc' || v === 'work' || v === 'delivery') loadDashboardWaiting(true);
  if (v === 'support') renderSupport();
  if (v === 'review') loadReviews();
  if (v === 'knowledge') loadKnowledge(true);
  if (v === 'backup') loadBackups(true);
  if (v === 'integrations') loadIntegrations();
  if (v === 'projects') loadProjectList();
  if (v === 'security') loadSecurity();
  if (v === 'licensing') loadLicensing();
  if (v === 'users') loadUsers();
  if (v === 'machines') pollServices();
  if (v === 'notifications') loadNotifications();
  if (v === 'settings') loadRuntimeSettings();
}
function flash(id) { var el = document.getElementById(id); if (!el) return; el.scrollIntoView({ behavior: reduceMotion ? 'auto' : 'smooth', block: 'start' }); el.classList.remove('flash-target'); void el.offsetWidth; el.classList.add('flash-target'); }

