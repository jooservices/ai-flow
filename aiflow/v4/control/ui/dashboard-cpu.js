/* CPU use of each machine on the Dashboard AI and machines tab: AI tools, tests, browsers and databases. */
S.dashboardCpu = S.dashboardCpu || { data: null, error: '', loading: false, loadedAt: 0 };
var CPU_REFRESH_MS = 15000;
var cpuRefreshTimer = 0;
var CPU_GROUPS = [['ai_tools', 'AI tools'], ['tests', 'Tests'], ['browsers', 'Browsers'], ['databases', 'Databases']];

function loadDashboardCpu(force) {
  var state = S.dashboardCpu;
  if (state.loading || (!force && state.loadedAt && Date.now() - state.loadedAt < CPU_REFRESH_MS)) return;
  clearTimeout(cpuRefreshTimer);
  state.loading = true;
  fetch('/v1/cpu-overview', { cache: 'no-store' }).then(function (r) {
    if (!r.ok) throw new Error('Request failed (' + r.status + ')');
    return r.json();
  }).then(function (data) {
    state.data = data; state.error = ''; state.loadedAt = Date.now();
    if (S.view === 'cc' && S.data) renderDashboard();
  }).catch(function (e) {
    state.error = String(e.message || e); state.loadedAt = Date.now();
    if (S.view === 'cc' && S.data) renderDashboard();
  }).then(function () {
    state.loading = false;
    cpuRefreshTimer = setTimeout(function () { loadDashboardCpu(true); }, CPU_REFRESH_MS);
  });
}

function cpuShare(value) {
  return value === null || value === undefined ? '—' : Number(value).toFixed(1) + '%';
}

function dashboardCpuRow(node) {
  var breakdown = node.breakdown || {};
  var h = '<tr><td class="mono">' + esc(node.node) + '<div class="small">' + (node.online ? 'online' : 'no recent heartbeat') + '</div></td>';
  h += '<td class="mono">' + cpuShare(node.cpu_pct) + '</td>';
  CPU_GROUPS.forEach(function (group) { h += '<td class="mono" data-cpu-group="' + group[0] + '">' + cpuShare(breakdown[group[0]]) + '</td>'; });
  return h + '</tr>';
}

function dashboardCpuPanel() {
  var state = S.dashboardCpu, nodes = (state.data && state.data.nodes) || [];
  var h = '<section class="panel" data-block="cpu-use" aria-labelledby="dashboard-cpu-title">';
  h += '<div class="panel-h"><h2 id="dashboard-cpu-title">CPU use by machine</h2>';
  h += '<span class="hint">Share of each machine\'s CPU · visible without signing in</span></div>';
  if (state.error) return h + '<div class="dashboard-empty">CPU use unavailable: ' + esc(state.error) + '</div></section>';
  if (!state.data) return h + '<div class="dashboard-empty">' + (state.loading ? 'Loading CPU use…' : 'CPU use is not available yet.') + '</div></section>';
  if (!nodes.length) return h + '<div class="dashboard-empty">No machines are registered.</div></section>';
  h += '<div class="tablebox"><table><thead><tr><th>Machine</th><th>Total</th>';
  CPU_GROUPS.forEach(function (group) { h += '<th>' + group[1] + '</th>'; });
  h += '</tr></thead><tbody>' + nodes.map(dashboardCpuRow).join('') + '</tbody></table></div>';
  return h + '</section>';
}

function injectDashboardCpuPanel() {
  var ai = document.querySelector('[data-dashboard-panel="ai"]');
  if (!ai || ai.querySelector('[data-block="cpu-use"]')) return;
  var holder = document.createElement('div');
  holder.innerHTML = dashboardCpuPanel();
  var panel = holder.firstElementChild;
  var usage = ai.querySelector('[data-block="usage"]');
  if (usage) ai.insertBefore(panel, usage);
  else ai.appendChild(panel);
}

var renderDashboardBeforeCpu = renderDashboard;
renderDashboard = function () {
  loadDashboardCpu(false);
  renderDashboardBeforeCpu();
  injectDashboardCpuPanel();
};
