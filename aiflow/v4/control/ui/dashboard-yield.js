/* Developer-run yield figures on the Dashboard Work tab. */
S.dashboardYield = S.dashboardYield || { data: null, error: '', loading: false, loadedAt: 0 };

function loadDashboardYield(force) {
  var state = S.dashboardYield;
  if (state.loading || (!force && state.loadedAt && Date.now() - state.loadedAt < ANALYTICS_MS)) return;
  state.loading = true;
  fetch('/v1/dashboard/yield?days=14', { cache: 'no-store' }).then(function (r) {
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
    setTimeout(function () { loadDashboardYield(true); }, ANALYTICS_MS);
  });
}

function dashboardYieldPanel() {
  var state = S.dashboardYield, data = state.data || {};
  var h = '<section class="panel" data-block="yield" aria-labelledby="dashboard-yield-title">';
  h += '<div class="panel-h"><h2 id="dashboard-yield-title">Merged tickets per developer run</h2>';
  h += '<span class="hint">Last 14 UTC days</span></div>';
  if (state.error) {
    return h + '<div class="dashboard-empty">Yield analytics unavailable: ' + esc(state.error) + '</div></section>';
  }
  if (!state.data) {
    return h + '<div class="dashboard-empty">' + (state.loading ? 'Loading yield data…' : 'Yield data is not available yet.') + '</div></section>';
  }
  var per100 = Number(data.merged_per_100_runs || 0), target = Number(data.target_merged_per_100 || 40);
  var tone = per100 >= target ? 'good' : 'warn';
  h += '<div class="facts"><div class="fact"><span class="fl">Developer runs</span><span class="fv mono">' + Number(data.developer_runs || 0) + '</span></div>';
  h += '<div class="fact"><span class="fl">Merged</span><span class="fv mono">' + Number(data.merged || 0) + '</span></div>';
  h += '<div class="fact"><span class="fl">Merged per 100 runs</span><span class="fv mono" data-tone="' + tone + '">' + per100.toFixed(2) + '</span><span class="small">Target above ' + target + '</span></div>';
  h += '<div class="fact"><span class="fl">Runs per merged ticket</span><span class="fv mono">' + (data.runs_per_merged == null ? '—' : Number(data.runs_per_merged).toFixed(2)) + '</span></div>';
  h += '<div class="fact"><span class="fl">Tokens per merged ticket</span><span class="fv mono">' + (data.tokens_per_merged == null ? '—' : tok(data.tokens_per_merged)) + '</span></div></div>';
  h += '<h3>By day</h3><div class="tablebox"><table><thead><tr><th>UTC day</th><th>Developer runs</th><th>Merged</th><th>Per 100 runs</th><th>Runs / merged</th><th>Tokens / merged</th></tr></thead><tbody>';
  if (!data.daily || !data.daily.length) h += '<tr><td colspan="6" class="empty">No developer runs in this period.</td></tr>';
  (data.daily || []).forEach(function (row) {
    h += '<tr><td>' + esc(row.day) + '</td><td>' + Number(row.developer_runs) + '</td><td>' + Number(row.merged) + '</td>';
    h += '<td>' + Number(row.merged_per_100_runs).toFixed(2) + '</td>';
    h += '<td>' + (row.runs_per_merged == null ? '—' : Number(row.runs_per_merged).toFixed(2)) + '</td>';
    h += '<td>' + (row.tokens_per_merged == null ? '—' : tok(row.tokens_per_merged)) + '</td></tr>';
  });
  return h + '</tbody></table></div><p class="hint">Completed developer runs are ended step=dev runs. Merged counts verify outcomes that merged a pull request. Target: above 40 merged per 100 completed developer runs.</p></section>';
}

function injectDashboardYieldPanel() {
  var work = document.querySelector('[data-dashboard-panel="work"]');
  if (!work || work.querySelector('[data-block="yield"]')) return;
  var holder = document.createElement('div');
  holder.innerHTML = dashboardYieldPanel();
  var panel = holder.firstElementChild;
  var conflict = work.querySelector('[data-block="conflict-loss"]');
  if (conflict && conflict.nextSibling) work.insertBefore(panel, conflict.nextSibling);
  else if (conflict) work.appendChild(panel);
  else {
    var waiting = work.querySelector('[data-block="waiting"]');
    if (waiting) work.insertBefore(panel, waiting);
    else work.appendChild(panel);
  }
}

var renderDashboardBeforeYield = renderDashboard;
renderDashboard = function () {
  loadDashboardYield(false);
  renderDashboardBeforeYield();
  injectDashboardYieldPanel();
};
