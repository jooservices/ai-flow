/* Developer-run timeout counts on the Dashboard Work tab. */
S.dashboardTimeouts = S.dashboardTimeouts || { data: null, error: '', loading: false, loadedAt: 0 };

function loadDashboardTimeouts(force) {
  var state = S.dashboardTimeouts;
  if (state.loading || (!force && state.loadedAt && Date.now() - state.loadedAt < ANALYTICS_MS)) return;
  state.loading = true;
  fetch('/v1/dashboard/timeouts?days=30', { cache: 'no-store' }).then(function (r) {
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
    setTimeout(function () { loadDashboardTimeouts(true); }, ANALYTICS_MS);
  });
}

function dashboardTimeoutsPanel() {
  var state = S.dashboardTimeouts, data = state.data || {};
  var h = '<section class="panel" data-block="timeouts" aria-labelledby="dashboard-timeouts-title">';
  h += '<div class="panel-h"><h2 id="dashboard-timeouts-title">Developer run timeouts</h2>';
  h += '<span class="hint">Last 30 UTC days</span></div>';
  if (state.error) {
    return h + '<div class="dashboard-empty">Timeout analytics unavailable: ' + esc(state.error) + '</div></section>';
  }
  if (!state.data) {
    return h + '<div class="dashboard-empty">' + (state.loading ? 'Loading timeout data…' : 'Timeout data is not available yet.') + '</div></section>';
  }
  var share = Number(data.timeout_share_pct || 0), tone = share < 10 ? 'good' : 'warn';
  h += '<div class="facts"><div class="fact"><span class="fl">Developer runs</span><span class="fv mono">' + Number(data.developer_runs || 0) + '</span></div>';
  h += '<div class="fact"><span class="fl">Timed out</span><span class="fv mono">' + Number(data.timeouts || 0) + '</span></div>';
  h += '<div class="fact"><span class="fl">Timeout share</span><span class="fv mono" data-tone="' + tone + '">' + share.toFixed(2) + '%</span><span class="small">Target below 10%</span></div></div>';
  h += '<h3>By day</h3><div class="tablebox"><table><thead><tr><th>UTC day</th><th>Developer runs</th><th>Timeouts</th><th>Share</th></tr></thead><tbody>';
  if (!data.daily || !data.daily.length) h += '<tr><td colspan="4" class="empty">No developer runs in this period.</td></tr>';
  (data.daily || []).forEach(function (row) {
    h += '<tr><td>' + esc(row.day) + '</td><td>' + Number(row.developer_runs) + '</td><td>' + Number(row.timeouts) + '</td><td>' + Number(row.timeout_share_pct).toFixed(2) + '%</td></tr>';
  });
  h += '</tbody></table></div><h3>By ticket</h3><div class="tablebox"><table><thead><tr><th>Ticket</th><th>Project</th><th>Timeouts</th><th>Developer runs</th><th>Latest timeout</th></tr></thead><tbody>';
  if (!data.tickets || !data.tickets.length) h += '<tr><td colspan="5" class="empty">No developer runs timed out in this period.</td></tr>';
  (data.tickets || []).forEach(function (row) {
    h += '<tr><td><button class="linkbtn mono" data-act="open-ticket" data-key="' + esc(row.ticket) + '">' + esc(row.ticket) + '</button></td><td>' + esc(row.project) + '</td><td>' + Number(row.timeouts) + '</td><td>' + Number(row.developer_runs) + '</td><td>' + esc(when(row.latest_timeout)) + '</td></tr>';
  });
  return h + '</tbody></table></div></section>';
}

function injectDashboardTimeoutsPanel() {
  var work = document.querySelector('[data-dashboard-panel="work"]');
  if (!work || work.querySelector('[data-block="timeouts"]')) return;
  var holder = document.createElement('div');
  holder.innerHTML = dashboardTimeoutsPanel();
  var panel = holder.firstElementChild;
  var waiting = work.querySelector('[data-block="waiting"]');
  if (waiting) work.insertBefore(panel, waiting);
  else work.appendChild(panel);
}

var renderDashboardBeforeTimeouts = renderDashboard;
renderDashboard = function () {
  loadDashboardTimeouts(false);
  renderDashboardBeforeTimeouts();
  injectDashboardTimeoutsPanel();
};
