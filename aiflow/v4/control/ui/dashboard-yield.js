/* Daily delivery yield and verify returns on the Dashboard Work tab. */
S.dashboardYield = S.dashboardYield || { data: null, error: '', loading: false, loadedAt: 0, pollTimer: null };

function loadDashboardYield(force) {
  var state = S.dashboardYield;
  if (state.loading || (!force && state.loadedAt && Date.now() - state.loadedAt < ANALYTICS_MS)) return;
  if (state.pollTimer != null) { clearTimeout(state.pollTimer); state.pollTimer = null; }
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
    if (state.pollTimer != null) clearTimeout(state.pollTimer);
    state.pollTimer = setTimeout(function () { state.pollTimer = null; loadDashboardYield(true); }, ANALYTICS_MS);
  });
}

function dashboardYieldPanel() {
  var state = S.dashboardYield, data = state.data || {};
  var h = '<section class="panel" data-block="yield" aria-labelledby="dashboard-yield-title">';
  h += '<div class="panel-h"><h2 id="dashboard-yield-title">Delivery yield</h2><span class="hint">Last 14 UTC days</span></div>';
  if (state.error) return h + '<div class="dashboard-empty">Yield analytics unavailable: ' + esc(state.error) + '</div></section>';
  if (!state.data) return h + '<div class="dashboard-empty">' + (state.loading ? 'Loading yield data…' : 'Yield data is not available yet.') + '</div></section>';
  var per100 = data.merged_per_100_runs == null ? null : Number(data.merged_per_100_runs);
  var target = Number(data.target_merged_per_100 || 40);
  var tone = per100 == null ? 'neutral' : per100 > target ? 'good' : 'warn';
  h += '<div class="facts"><div class="fact"><span class="fl">Completed developer runs</span><span class="fv mono">' + Number(data.developer_runs || 0) + '</span></div>';
  h += '<div class="fact"><span class="fl">Merged tickets</span><span class="fv mono">' + Number(data.merged || 0) + '</span></div>';
  h += '<div class="fact"><span class="fl">Verification returns</span><span class="fv mono">' + Number(data.returns || 0) + '</span></div>';
  h += '<div class="fact"><span class="fl">Merged per 100 runs</span><span class="fv mono" data-tone="' + tone + '">' + (per100 == null ? '—' : per100.toFixed(2)) + '</span><span class="small">Target above ' + target + '</span></div>';
  h += '<div class="fact"><span class="fl">Runs per merged ticket</span><span class="fv mono">' + (data.runs_per_merged == null ? '—' : Number(data.runs_per_merged).toFixed(2)) + '</span></div>';
  h += '<div class="fact"><span class="fl">Tokens per merged ticket</span><span class="fv mono">' + (data.tokens_per_merged == null ? '—' : Number(data.tokens_per_merged).toLocaleString()) + '</span></div></div>';
  h += '<h3>By day</h3><div class="tablebox"><table><thead><tr><th>UTC day</th><th>Completed developer runs</th><th>Merged</th><th>Returns</th><th>Merged / 100 runs</th><th>Runs / merged</th><th>Tokens / merged</th></tr></thead><tbody>';
  if (!data.daily || !data.daily.length) h += '<tr><td colspan="7" class="empty">No developer runs in this period.</td></tr>';
  (data.daily || []).forEach(function (row) {
    h += '<tr><td>' + esc(row.day) + '</td><td>' + Number(row.developer_runs) + '</td><td>' + Number(row.merged) + '</td><td>' + Number(row.returns) + '</td>';
    h += '<td>' + (row.merged_per_100_runs == null ? '—' : Number(row.merged_per_100_runs).toFixed(2)) + '</td><td>' + (row.runs_per_merged == null ? '—' : Number(row.runs_per_merged).toFixed(2)) + '</td>';
    h += '<td>' + (row.tokens_per_merged == null ? '—' : Number(row.tokens_per_merged).toLocaleString()) + '</td></tr>';
  });
  return h + '</tbody></table></div><p class="hint">Returns count ended verification runs sent back to development. Token totals include ended runs started that day.</p></section>';
}

function injectDashboardYieldPanel() {
  var work = document.querySelector('[data-dashboard-panel="work"]');
  if (!work || work.querySelector('[data-block="yield"]')) return;
  var holder = document.createElement('div');
  holder.innerHTML = dashboardYieldPanel();
  var panel = holder.firstElementChild;
  var timeouts = work.querySelector('[data-block="timeouts"]');
  if (timeouts && timeouts.nextSibling) work.insertBefore(panel, timeouts.nextSibling);
  else if (timeouts) work.appendChild(panel);
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
