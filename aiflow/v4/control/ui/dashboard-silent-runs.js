/* Silent developer run counts on the Dashboard Work tab. */
S.dashboardSilentRuns = S.dashboardSilentRuns || { data: null, error: '', loading: false, loadedAt: 0 };
var silentRunsRefreshTimer = 0;

function loadDashboardSilentRuns(force) {
  var state = S.dashboardSilentRuns;
  if (state.loading || (!force && state.loadedAt && Date.now() - state.loadedAt < ANALYTICS_MS)) return;
  clearTimeout(silentRunsRefreshTimer);
  state.loading = true;
  fetch('/v1/dashboard/silent-runs?days=30', { cache: 'no-store' }).then(function (r) {
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
    silentRunsRefreshTimer = setTimeout(function () { loadDashboardSilentRuns(true); }, ANALYTICS_MS);
  });
}

function dashboardSilentRunsPanel() {
  var state = S.dashboardSilentRuns, data = state.data || {};
  var h = '<section class="panel" data-block="silent-runs" aria-labelledby="dashboard-silent-runs-title">';
  h += '<div class="panel-h"><h2 id="dashboard-silent-runs-title">Silent developer runs</h2>';
  h += '<span class="hint">Last 30 UTC days</span></div>';
  if (state.error) {
    return h + '<div class="dashboard-empty">Silent-run analytics unavailable: ' + esc(state.error) + '</div></section>';
  }
  if (!state.data) {
    return h + '<div class="dashboard-empty">' + (state.loading ? 'Loading silent-run data…' : 'Silent-run data is not available yet.') + '</div></section>';
  }
  var share = Number(data.silent_share_pct || 0), tone = share < 2 ? 'good' : 'warn';
  h += '<div class="facts"><div class="fact"><span class="fl">Developer runs</span><span class="fv mono">' + Number(data.developer_runs || 0) + '</span></div>';
  h += '<div class="fact"><span class="fl">Silent runs</span><span class="fv mono">' + Number(data.silent_runs || 0) + '</span></div>';
  h += '<div class="fact"><span class="fl">Silent share</span><span class="fv mono" data-tone="' + tone + '">' + share.toFixed(2) + '%</span><span class="small">Target below 2%</span></div></div>';
  h += '<h3>By day</h3><div class="tablebox"><table><thead><tr><th>UTC day</th><th>Developer runs</th><th>Silent runs</th><th>Share</th></tr></thead><tbody>';
  if (!data.daily || !data.daily.length) h += '<tr><td colspan="4" class="empty">No developer runs in this period.</td></tr>';
  (data.daily || []).forEach(function (row) {
    h += '<tr><td>' + esc(row.day) + '</td><td>' + Number(row.developer_runs) + '</td><td>' + Number(row.silent_runs) + '</td><td>' + Number(row.silent_share_pct).toFixed(2) + '%</td></tr>';
  });
  h += '</tbody></table></div><h3>By ticket</h3><div class="tablebox"><table><thead><tr><th>Ticket</th><th>Project</th><th>Silent runs</th><th>Developer runs</th><th>Latest silent run</th></tr></thead><tbody>';
  if (!data.tickets || !data.tickets.length) h += '<tr><td colspan="5" class="empty">No developer runs went silent in this period.</td></tr>';
  (data.tickets || []).forEach(function (row) {
    h += '<tr><td><button class="linkbtn mono" data-act="open-ticket" data-key="' + esc(row.ticket) + '">' + esc(row.ticket) + '</button></td><td>' + esc(row.project) + '</td><td>' + Number(row.silent_runs) + '</td><td>' + Number(row.developer_runs) + '</td><td>' + esc(when(row.latest_silent)) + '</td></tr>';
  });
  return h + '</tbody></table></div></section>';
}

function injectDashboardSilentRunsPanel() {
  var work = document.querySelector('[data-dashboard-panel="work"]');
  if (!work || work.querySelector('[data-block="silent-runs"]')) return;
  var holder = document.createElement('div');
  holder.innerHTML = dashboardSilentRunsPanel();
  var panel = holder.firstElementChild;
  var timeouts = work.querySelector('[data-block="timeouts"]');
  if (timeouts && timeouts.nextSibling) work.insertBefore(panel, timeouts.nextSibling);
  else {
    var waiting = work.querySelector('[data-block="waiting"]');
    if (waiting) work.insertBefore(panel, waiting);
    else work.appendChild(panel);
  }
}

var renderDashboardBeforeSilentRuns = renderDashboard;
renderDashboard = function () {
  loadDashboardSilentRuns(false);
  renderDashboardBeforeSilentRuns();
  injectDashboardSilentRunsPanel();
};
