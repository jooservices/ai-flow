/* System check and machine readiness extensions for the console template. */
S.systemCheck = null;
S.systemCheckError = '';
S.systemCheckBusy = false;
VIEW_PERMISSION['system-check'] = 'node.operations.read';
VIEW_PATH['system-check'] = '/system-check';
VIEW_GROUPS[2].push('system-check');
MENU_ITEMS.push({ view: 'system-check', label: 'System check' });
HINTS['system-check'] = 'See every condition the flow needs per machine and project, run the check now, and follow the action for any that is not ready.';

var systemCheckTab = $('#tab-system-check');
if (!systemCheckTab) {
  systemCheckTab = document.createElement('button');
  systemCheckTab.className = 'tab';
  systemCheckTab.id = 'tab-system-check';
  systemCheckTab.setAttribute('role', 'tab');
  systemCheckTab.setAttribute('aria-controls', 'view-system-check');
  systemCheckTab.setAttribute('aria-selected', 'false');
  systemCheckTab.setAttribute('data-act', 'view');
  systemCheckTab.setAttribute('data-menu-view', '');
  systemCheckTab.setAttribute('data-view', 'system-check');
  systemCheckTab.textContent = 'System check';
  var licensingTab = $('#tab-licensing');
  licensingTab.parentNode.insertBefore(systemCheckTab, licensingTab);
}
if (!$('#view-system-check')) {
  var systemCheckPanel = document.createElement('section');
  systemCheckPanel.className = 'view';
  systemCheckPanel.id = 'view-system-check';
  systemCheckPanel.setAttribute('role', 'tabpanel');
  systemCheckPanel.setAttribute('aria-labelledby', 'tab-system-check');
  systemCheckPanel.hidden = true;
  $('#view-licensing').parentNode.insertBefore(systemCheckPanel, $('#view-licensing'));
}

var originalSystemCheckViewOf = viewOf;
viewOf = function (path) {
  if (path.indexOf('/system-check') === 0) return 'system-check';
  return originalSystemCheckViewOf(path);
};

function loadSystemCheck() {
  fetch('/v1/system-check', { cache: 'no-store' }).then(function (r) { return projectResponseError(r); })
    .then(function (d) { S.systemCheck = d; S.systemCheckError = ''; if (S.view === 'system-check') renderSystemCheck(); })
    .catch(function (e) { S.systemCheckError = String(e.message || e); if (S.view === 'system-check') renderSystemCheck(); });
}

function runSystemCheck() {
  S.systemCheckBusy = true;
  renderSystemCheck();
  fetch('/v1/system-check/run', { method: 'POST' }).then(function (r) { return projectResponseError(r); })
    .then(function () { return fetch('/v1/system-check', { cache: 'no-store' }).then(function (r) { return projectResponseError(r); }); })
    .then(function (d) { S.systemCheck = d; S.systemCheckError = ''; })
    .catch(function (err) { S.systemCheckError = String(err.message || err); })
    .then(function () { S.systemCheckBusy = false; renderSystemCheck(); });
}

function renderSystemCheck() {
  var root = $('#view-system-check');
  if (!root) return;
  var data = S.systemCheck, h = '<section class="panel"><div class="panel-h"><h2>System check</h2>';
  h += '<span class="hint">Conditions needed before work can start, by machine and project</span><span class="spacer"></span>';
  h += '<a class="btn sm" href="/v1/support-bundle">Download support bundle</a>';
  if (consoleCan('nodes.manage')) h += '<button class="btn sm" data-act="system-check-run"' + (S.systemCheckBusy ? ' disabled' : '') + '>Run now</button>';
  h += '</div>';
  if (S.systemCheckError) h += '<div class="stakeholder-error">' + esc(S.systemCheckError) + '</div>';
  if (!data) h += '<div class="muted">Loading readiness results…</div>';
  else if (!(data.nodes || []).length) h += '<div class="muted">No machine has joined yet. Results will appear after the first machine heartbeat.</div>';
  else (data.nodes || []).forEach(function (node) {
    var readiness = node.readiness || {};
    h += '<section class="project-checks"><div class="panel-h"><strong class="mono">' + esc(node.name) + '</strong>';
    h += '<span class="pill" data-tone="' + (node.status === 'online' ? 'ok' : 'warn') + '">' + esc(node.status) + '</span>';
    h += '<span class="muted">Checked ' + esc(readiness.checked_at ? when(readiness.checked_at) : 'not yet') + ' · trigger ' + esc(readiness.trigger || 'pending') + '</span></div>';
    function conditionRows(scope, conditions) {
      h += '<h3>' + esc(scope) + '</h3>';
      if (!conditions || !conditions.length) { h += '<div class="muted">No conditions reported yet.</div>'; return; }
      h += '<div class="project-table"><table><thead><tr><th>Condition</th><th>Result</th><th>Details and action</th></tr></thead><tbody>';
      conditions.forEach(function (item) {
        var labels = { ready: 'Ready', fixed_automatically: 'Fixed automatically', needs_action: 'Needs an action', not_possible_on_this_machine: 'Not possible on this machine' };
        var tone = item.result === 'ready' || item.result === 'fixed_automatically' ? 'ok' : 'warn';
        var text = item.detail || '';
        if (item.action) text += (text ? ' ' : '') + item.action;
        h += '<tr><td>' + esc(item.name || item.id) + '</td><td><span class="pill" data-tone="' + tone + '">' + esc(labels[item.result] || item.result) + '</span></td><td>' + esc(text || '—') + '</td></tr>';
      });
      h += '</tbody></table></div>';
    }
    conditionRows('Machine', readiness.machine || []);
    Object.keys(readiness.projects || {}).sort().forEach(function (project) {
      conditionRows('Project · ' + project, readiness.projects[project]);
    });
    h += '</section>';
  });
  h += '</section>';
  root.innerHTML = h;
}

var originalSystemCheckRender = render;
render = function () {
  originalSystemCheckRender();
  if (S.view === 'system-check') renderSystemCheck();
};
var originalSystemCheckShowView = showView;
showView = function () {
  originalSystemCheckShowView();
  var panel = $('#view-system-check');
  if (panel) panel.hidden = S.view !== 'system-check';
};
var originalSystemCheckSetView = setView;
setView = function (view) {
  originalSystemCheckSetView(view);
  if (view === 'system-check') loadSystemCheck();
};
var originalSystemCheckRenderProjects = renderProjects;
renderProjects = function () {
  originalSystemCheckRenderProjects();
  renderProjectReadiness();
};

function renderProjectReadiness() {
  var root = $('#view-projects'), projects = (S.projectAdmin || {}).projects || [];
  if (!root || !projects.length) return;
  var h = '<section class="panel system-check-project-readiness"><div class="panel-h"><h2>Machine readiness</h2>';
  h += '<span class="hint">Conditions that affect whether each project can start on a machine</span></div>';
  projects.forEach(function (project) {
    h += '<section class="project-checks"><h3>Readiness for ' + esc(project.key) + '</h3>';
    var nodes = project.node_readiness || [];
    if (!nodes.length) h += '<div class="muted">No assigned machine.</div>';
    nodes.forEach(function (node) {
      if (!node.blocked) {
        h += '<div class="good"><span class="mono">' + esc(node.name) + '</span>: Ready for this project.</div>';
        return;
      }
      h += '<div class="bad"><span class="mono">' + esc(node.name) + '</span> cannot start this project because ';
      h += (node.conditions || []).map(function (item) {
        return esc(item.name || item.id) + ' — ' + esc(item.action || item.detail);
      }).join('; ') + '</div>';
    });
    h += '</section>';
  });
  root.insertAdjacentHTML('beforeend', h + '</section>');
}

document.addEventListener('click', function (event) {
  var button = event.target.closest && event.target.closest('[data-act="system-check-run"]');
  if (button) { event.preventDefault(); runSystemCheck(); }
});
window.addEventListener('popstate', function () {
  if (S.view === 'system-check') loadSystemCheck();
});

if (location.pathname.indexOf('/system-check') === 0) {
  S.view = 'system-check';
  syncMenuVisibility();
  showView();
  render();
  loadSystemCheck();
}
