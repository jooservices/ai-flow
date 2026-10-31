/* ---------- state ---------- */
var S = {
  data: null, fetchedAt: 0, lastOk: 0, error: '', services: [], svcError: '', analytics: [], anError: '', conflicts: null, conflictsError: '',
  dashboardUsage: [], dashboardUsageError: '', capacity: { data: null, error: '', loading: false, loadedAt: 0 },
  dashboardWaiting: { keys: null, error: '', loading: false, loadedAt: 0 },
  dashboardQuota: { rows: null, error: '', loading: false, loadedAt: 0, user: '' },
  dashboardDisagreements: { rows: null, error: '', loading: false, loadedAt: 0 },
  dashboardTab: 'work', workStatusFilter: 'all',
  themePreference: { user: '', loadedUser: '', chain: Promise.resolve() },
  layouts: { person: '', pages: {}, busy: false, mode: '', drag: null, chain: Promise.resolve() },
  consoleUser: null, consoleSessionLoaded: false,
  userAdmin: { users: null, projects: [], roleOptions: [], audit: [],
    loading: false, loadedAt: 0, error: '', editing: '', temporaryPassword: '',
    addForm: { email: '', name: '', roles: [], projects: [], all: true } },
  autonomy: { data: null, error: '', busy: false, saving: false },
  esc: { data: null, error: "", busy: false, saving: false },
  nodeOperations: {}, joinCommands: {}, operationBusy: {},
  projectAdmin: { projects: null, invalidProjects: [], catalog: null, error: '', preflight: null, busy: false, showForm: false,
    repositoryEditor: null, envEditor: null, envMissing: [], envScan: null, envScanBusy: false,
    form: { site: '', project: '', repo: '', base: 'develop', stack: '', services: '', checks: '', containers: false, priority: 5, parallel_limit: 2, merge_method: 'squash' } },
  integrations: { data: null, connections: [], target: '', recommendation: '', error: '', busy: false, pending: null, installations: [], selected: '', notice: '' },
  users: { data: null, error: '', busy: false, loadedAt: 0, initialPassword: '' },
  notifications: { data: null, error: '', notice: '', busy: false, editId: null,
    form: { kind: 'email', name: '', enabled: true }, preferencesDraft: null, preferencesDirty: false },
  runtimeSettings: { data: null, quotaRefresh: null, error: '', notice: '', busy: false, scope: 'secret', project: '', key: '', value: '', changes: [] },
  machines: { addMachine: false, join: {name: '', role: 'worker', project: '', provision_key: '', command: '', expires_at: '', error: '', busy: false},
    addAccount: '', accountEditor: null, signIns: {}, signInLoading: {}, signInRefreshAt: {}, error: '', notice: '' },
  backup: { data: null, form: null, selected: '', error: '', notice: '', loading: false, saving: false, loadedAt: 0 },
  view: viewOf(location.pathname),
  project: null, search: '', typeFilter: 'all', aiFilter: 'all', epic: 'all', accFilter: 'all', starredProjects: [],
  dashboardQuery: '', dashboardSort: 'key', dashboardSortDir: 1, commandSearchOpen: false,
  projectQuery: '', projectPickerOpen: false,
  seenInc: null, seenRuns: null, drawer: null, ticket: {}, first: true,
  leadEpic: null, leadReport: null,
  biz: null, bizError: '', bizLoading: null, reviews: null, reviewsError: '', reviewerToken: '', reviewNoteDrafts: {},
  rh: { window: null, percent: null, busy: false, error: '', sig: '' },
  reviewProject: '', reviewMetrics: null, reviewProjects: [], reviewThreads: [],
  join: { name: '', role: 'worker', project: '', provision_key: '', error: '', busy: false, command: '', expires_at: '', copied: false, sig: '' },
  cr: { project: null, v: { title: '', description: '', epic: '' }, errors: [], sending: false, created: null, sig: '' },
  sh: { project: null, data: null, error: '', loading: null, forms: {}, files: {}, sending: {}, results: {}, renderKey: '' },
  knowledge: { data: null, learning: null, learningProject: '', search: '', error: '', loading: false, filter: 'active', type: 'all', editor: null, history: null, itemEditor: null, owner: '', ownerToken: '', actor: '' },
  integ: { jira: null, error: '', loading: false, busy: false, ownerToken: '', notice: '' },
  licensing: { data: null, status: null, usage: [], error: '', busy: false, statusBusy: false,
    loadedAt: 0, statusAt: 0, limitsPlan: '' }
};
S.support = { ticketKey: '', busy: false, error: '', bundle: null };
var VIEW_PERMISSION = { cc: 'view.dashboard', work: 'view.work_board', delivery: 'view.delivery', sa: 'view.sa',
  lead: 'view.team_lead', business: 'view.business', review: 'view.human_review', stakeholder: 'view.stakeholder',
  machines: 'view.machines_ai', knowledge: 'view.knowledge', integrations: 'view.integrations', projects: 'projects.manage', users: 'console.manage',
  backup: 'backup.read', licensing: 'licensing.manage', notifications: 'notifications.manage', settings: 'console.manage', support: 'console.manage' };
var VIEW_PATH = { cc: '/cc', work: '/work', delivery: '/delivery', sa: '/sa', lead: '/lead', business: '/business',
  review: '/review', stakeholder: '/stakeholder', machines: '/machines', knowledge: '/knowledge', integrations: '/integrations',
  projects: '/projects', users: '/users', backup: '/backup', licensing: '/licensing', notifications: '/notifications', settings: '/settings', support: '/support' };
var VIEW_GROUPS = [['cc', 'work'], ['delivery', 'sa', 'lead', 'business', 'review', 'stakeholder'],
  ['machines', 'users', 'knowledge', 'backup', 'integrations', 'projects', 'licensing', 'notifications', 'settings', 'support']];
function viewOf(path) {
  if (path.indexOf('/licensing') === 0) return 'licensing';
  if (path.indexOf('/notifications') === 0) return 'notifications';
  if (path.indexOf('/settings') === 0) return 'settings';
  if (path.indexOf('/support') === 0) return 'support';
  if (path.indexOf('/work') === 0 || path.indexOf('/board') === 0) return 'work';
  if (path.indexOf('/dashboard') === 0) return 'cc';
  if (path.indexOf('/projects') === 0) return 'projects';
  if (path.indexOf('/integrations') === 0) return 'integrations';
  if (path.indexOf('/machines') === 0) return 'machines';
  if (path.indexOf('/users') === 0) return 'users';
  if (path.indexOf('/sa') === 0) return 'sa';
  if (path.indexOf('/delivery') === 0) return 'delivery';
  if (path.indexOf('/lead') === 0) return 'lead';
  if (path.indexOf('/business') === 0) return 'business';
  if (path.indexOf('/review') === 0) return 'review';
  if (path.indexOf('/stakeholder') === 0) return 'stakeholder';
  if (path.indexOf('/knowledge') === 0) return 'knowledge';
  if (path.indexOf('/backup') === 0) return 'backup';
  if (path.indexOf('/integrations') === 0) return 'integrations';
  return 'cc';
}
var STATUS_COLS = [['Backlog', 'Backlog'], ['Intake', 'Intake'], ['To Do', 'To Do'], ['In Progress', 'In Progress'], ['In Verify', 'In Verify'], ['Human review', 'Human review'], ['Needs owner', 'Needs owner'], ['Done', 'Done']];
function inColumn(ticket, column) { return ticket.status === column || (column === 'Human review' && ticket.status === 'Ready to merge'); }
var KIND = { node_down: 'Node down', node_no_ai: 'Empty node', node_tools: 'Missing tools', node_services: 'Test services', service_undeclared: 'Undeclared services', ticket_stuck: 'Stuck ticket', role_coverage: 'Role coverage', account_quota: 'Account', quota_high: 'Quota', quota_prediction: 'Quota forecast', run_slow: 'Slow run', run_hard_limit: 'Run limit', jira_auth: 'Jira connection', role_health: 'Role health' };
var PROVIDER = { claude: 'Claude', codex: 'Codex', grok: 'Grok', opencode: 'OpenCode' };
var MENU_ITEMS = [
  { view: 'cc', label: 'Dashboard' },
  { view: 'work', label: 'Work board' },
  { view: 'delivery', label: 'Delivery' },
  { view: 'sa', label: 'SA' },
  { view: 'lead', label: 'Team Lead' },
  { view: 'business', label: 'Business' },
  { view: 'review', label: 'Human review' },
  { view: 'stakeholder', label: 'Stakeholder' },
  { view: 'machines', label: 'Machines and AI' },
  { view: 'users', label: 'Users and roles' },
  { view: 'knowledge', label: 'Knowledge' },
  { view: 'backup', label: 'Backup & Restore' },
  { view: 'integrations', label: 'Integrations' },
  { view: 'projects', label: 'Projects' },
  { view: 'licensing', label: 'Licence' },
  { view: 'notifications', label: 'Notifications' },
  { view: 'settings', label: 'Settings', permission: 'console.manage' }
];
MENU_ITEMS.push({ view: 'support', label: 'Support', permission: 'console.manage' });
// Visibility reads the effective permissions sent from ROLE_PERMISSIONS; the server enforces the same grants.
function viewAllowed(view) {
  return !S.consoleUser || (!!VIEW_PERMISSION[view] && consoleCan(VIEW_PERMISSION[view]));
}
function syncMenuVisibility() { applyRoleNavigation(); }

function D() { return S.data || { nodes: [], accounts: [], active_runs: [], recent_runs: [], projects: [], tickets: [], incidents: [], events: [], totals: {}, versions: {} }; }
function accountAssignedTo(a, node) {
  var machines = a && Array.isArray(a.machines) ? a.machines : [];
  if (a && Array.isArray(a.machines)) return machines.some(function (machine) {
    return (typeof machine === 'string' ? machine : machine && machine.node) === node;
  });
  return !!a && a.node === node;
}
function accountPendingOnMachine(a, node) {
  var machines = a && Array.isArray(a.machines) ? a.machines : [];
  var machine = machines.filter(function (item) { return item && typeof item === 'object' && item.node === node; })[0];
  if (a && Array.isArray(a.machines)) return !!(machine && (machine.sign_in_pending || machine.signed_in === false));
  return !!(a && a.sign_in_pending && a.node === node);
}
function since() { return (Date.now() - S.fetchedAt) / 1000; }
function loadPrefs() {
  try {
    var raw = localStorage.getItem(PREFS_KEY);
    if (!raw) return;
    var p = JSON.parse(raw);
    if (!p || typeof p !== 'object') return;
    if (typeof p.project === 'string') S.project = p.project;
    if (typeof p.typeFilter === 'string') S.typeFilter = p.typeFilter;
    if (typeof p.aiFilter === 'string') S.aiFilter = p.aiFilter;
    if (typeof p.epic === 'string') S.epic = p.epic;
    if (typeof p.workStatusFilter === 'string') S.workStatusFilter = p.workStatusFilter;
    if (Array.isArray(p.starredProjects)) S.starredProjects = p.starredProjects.filter(function (x) { return typeof x === 'string'; });
    var urlStatus = new URLSearchParams(location.search || '').get('status');
    if (urlStatus && ['all', 'done', 'in-flight', 'waiting'].concat(STATUS_COLS.map(function (c) { return c[0]; })).indexOf(urlStatus) >= 0) {
      S.workStatusFilter = urlStatus;
    }
  } catch (x) { /* storage off */ }
}
function savePrefs() {
  try {
    localStorage.setItem(PREFS_KEY, JSON.stringify({ project: S.project, typeFilter: S.typeFilter,
      aiFilter: S.aiFilter, epic: S.epic, workStatusFilter: S.workStatusFilter, starredProjects: S.starredProjects }));
  } catch (x) { /* storage off */ }
}
function keyNumber(key) {
  var m = String(key == null ? '' : key).match(/(\d+)(?!.*\d)/);
  return m ? Number(m[1]) : Infinity;
}
function sortEpics(epics) {
  return epics.slice().sort(function (a, b) {
    var an = keyNumber(a.key), bn = keyNumber(b.key);
    return an !== bn ? an - bn : String(a.key).localeCompare(String(b.key));
  });
}
function nodeBy(n) { return D().nodes.filter(function (x) { return x.name === n; })[0]; }
function accBy(id) { return D().accounts.filter(function (x) { return x.id === id; })[0]; }
function ticketBy(k) { return D().tickets.filter(function (x) { return x.key === k; })[0]; }
function incBy(id) { return D().incidents.concat(D().incidents_recent || []).filter(function (x) { return String(x.id) === String(id); })[0]; }
function consoleCan(permission) {
  var permissions = S.consoleUser && S.consoleUser.permissions || [];
  return Array.isArray(permissions) && (permissions.indexOf('*') >= 0 || permissions.indexOf(permission) >= 0);
}
function consoleOwner() { return consoleCan('console.manage') || consoleCan('integrations.manage'); }
function consoleReader() { return consoleCan('console.read'); }
function loadMasterAutonomy(force) {
  if (!consoleReader() || S.autonomy.busy || (S.autonomy.data && !force)) return;
  S.autonomy.busy = true;
  fetch('/v1/master/autonomy', { cache: 'no-store' }).then(function (r) {
    return r.json().then(function (d) { if (!r.ok) throw new Error(d.detail || ('HTTP ' + r.status)); return d; });
  }).then(function (d) { S.autonomy.data = d; S.autonomy.error = ''; })
    .catch(function (err) { S.autonomy.error = String(err.message || err); })
    .then(function () { S.autonomy.busy = false; if (S.view === 'cc') renderDashboard(); });
}
function saveMasterBudget() {
  var input = $('[data-master-budget]'), value = input ? Number(input.value) : NaN;
  if (!Number.isInteger(value) || value < 0 || value > 1440) {
    S.autonomy.error = 'Enter a daily budget from 0 to 1,440 minutes.'; renderDashboard(); return;
  }
  S.autonomy.saving = true; S.autonomy.error = ''; renderDashboard();
  fetch('/v1/master/autonomy/budget', { method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ daily_budget_minutes: value }) }).then(function (r) {
    return r.json().then(function (d) { if (!r.ok) throw new Error(d.detail || ('HTTP ' + r.status)); return d; });
  }).then(function (d) { if (S.autonomy.data) S.autonomy.data.budget = d.budget; })
    .catch(function (err) { S.autonomy.error = String(err.message || err); })
    .then(function () { S.autonomy.saving = false; loadMasterAutonomy(true); });
}

function escCall(method, url, body, onSuccess) {
  S.esc.saving = true; S.esc.error = ''; renderDashboard();
  var options = { method: method, headers: { 'Content-Type': 'application/json' } };
  if (body) options.body = JSON.stringify(body);
  fetch(url, options).then(function (r) {
    return r.json().then(function (d) { if (!r.ok) throw new Error(d.detail || ('HTTP ' + r.status)); return d; });
  }).then(function (data) { if (onSuccess) onSuccess(data); })
    .catch(function (err) { S.esc.error = String(err.message || err); })
    .then(function () { S.esc.saving = false; loadEscalations(true); });
}
function loadEscalations(force) {
  if (!consoleReader() || S.esc.busy || (S.esc.data && !force)) return;
  S.esc.busy = true;
  fetch('/v1/escalations', { cache: 'no-store' }).then(function (r) {
    return r.json().then(function (d) { if (!r.ok) throw new Error(d.detail || ('HTTP ' + r.status)); return d; });
  }).then(function (d) { S.esc.data = d; })
    .catch(function (err) { S.esc.error = String(err.message || err); })
    .then(function () { S.esc.busy = false; if (S.view === 'cc') renderDashboard(); });
}
function answerDecision(id, choice) { escCall('POST', '/v1/decisions/' + encodeURIComponent(id) + '/answer', { choice: choice || 'recommended' }); }
function retireDecision(id) { escCall('POST', '/v1/decisions/' + encodeURIComponent(id) + '/retire'); }
function addDecision() {
  var title = $('[data-esc-title]'), text = $('[data-esc-decision]'), words = $('[data-esc-keywords]');
  if (!title || !text || title.value.trim().length < 3 || text.value.trim().length < 3) {
    S.esc.error = 'Enter a title and the decision.'; renderDashboard(); return;
  }
  var keywords = (words ? words.value : '').split(',').map(function (w) { return w.trim(); }).filter(Boolean);
  escCall('POST', '/v1/decisions', { title: title.value.trim(), decision: text.value.trim(), keywords: keywords });
}
function escalationsPanel() {
  return window.AIFlowOwnerEscalations.render({
    state: S, reader: consoleReader, load: loadEscalations, escape: esc, can: consoleCan, render: renderDashboard
  });
}
function nodeTone(n) { return { online: 'ok', offline: 'crit', draining: 'warn', joined: 'info' }[n.status] || 'muted'; }
function relLabel(n) {
  var p = n.version_policy || {};
  return esc(n.release || '—') + (p.compatible === false ? ' <span title="' + esc(p.reason || '') + '">· incompatible, no new work</span>' : '');
}
function nodeLabel(n) { return { online: 'Online', offline: 'Offline', draining: 'Draining', joined: 'Joining' }[n.status] || n.status; }
function accTone(a) { return { working: 'info', probing: 'info', idle: 'ok', cooldown: 'warn', signed_out: 'crit', error: 'crit', stopped: 'muted', unknown: 'muted', node_offline: 'crit' }[a.status] || 'muted'; }
function accLabel(a) { return { working: 'Working', probing: 'Checking', idle: 'Ready', cooldown: 'No quota', signed_out: 'Signed out', error: 'Failing', stopped: 'Stopped', unknown: 'No worker', node_offline: 'Node offline' }[a.status] || a.status; }
function doing(a) {
  if (a.run) return a.run.ticket + ' · ' + a.run.step + ' · ' + mmss((a.run.elapsed_s || 0) + since());
  if (a.status === 'cooldown') return 'paused until ' + (a.cooldown_until ? when(a.cooldown_until) : 'reset') + (a.last_error ? ' · ' + a.last_error : '');
  if (a.status === 'signed_out') return 'needs sign-in on its node · ' + (a.last_error || '');
  if (a.status === 'error') return a.last_error || 'last check failed';
  if (a.status === 'unknown') return 'no worker reported yet';
  if (a.status === 'stopped') return 'worker stopped';
  if (a.status === 'node_offline') return 'its node ' + a.node + ' is offline';
  return 'waiting for a ready ticket';
}
function runTone(o) { return { done: 'ok', PASS: 'ok', DONE: 'ok', merged: 'ok', answered: 'ok', escalated: 'crit', human: 'info', rework: 'warn', blocked: 'warn', quota: 'warn', failed: 'crit', timeout: 'crit', lost: 'crit', released: 'muted', running: 'info' }[o] || 'muted'; }
function stTone(s) { return { 'Done': 'ok', 'In Progress': 'info', 'In Verify': 'info', 'Human review': 'info', 'Ready to merge': 'info', 'Needs owner': 'crit', 'Intake': 'warn' }[s] || 'muted'; }

