/* Account quota refresh settings panel; the main console provides its private state/render helpers. */
(function () {
  var binding = null;

  function panel(state, esc, when, render, responseError) {
    binding = { state: state, esc: esc, when: when, render: render, responseError: responseError };
    if (!state.quotaRefreshLoaded && !state.quotaRefreshLoading) {
      state.quotaRefreshLoading = true;
      fetch('/v1/quota-refresh/settings', { cache: 'no-store' })
        .then(responseError).then(function (data) {
          state.quotaRefresh = data; state.quotaRefreshError = ''; state.quotaRefreshLoaded = true;
        }).catch(function (error) {
          state.quotaRefreshError = String(error.message || error); state.quotaRefreshLoaded = true;
        })
        .then(function () { state.quotaRefreshLoading = false; render(); });
    }
    var quota = state.quotaRefresh || {}, settings = quota.settings || {}, jobs = quota.jobs || [];
    var h = '<section class="panel"><div class="panel-h"><h2>Account quota refresh</h2><span class="spacer"></span>' +
      '<button class="btn sm" data-act="quota-refresh-save"' + (state.busy || state.quotaRefreshLoading ? ' disabled' : '') +
      '>Save interval</button></div><p class="hint">Reads quota through provider CLI status commands without an AI prompt. Run token and cost reporting stays with completed tasks.</p>' +
      '<label>Refresh every (minutes)<input class="in" type="number" min="1" max="1440" step="1" data-quota-refresh-interval value="' +
      esc(settings.interval_minutes || 15) + '"' + (state.busy || state.quotaRefreshLoading ? ' disabled' : '') + '></label>' +
      '<p class="small">Default: 15 minutes. Unsupported providers keep their last snapshot and show the read error.</p>' +
      '<div class="tablewrap"><table><thead><tr><th>Account</th><th>Provider</th><th>Last attempt</th><th>Last successful read</th><th>Next due</th><th>Status</th></tr></thead><tbody>';
    h += jobs.length ? jobs.map(function (job) {
      return '<tr><td>' + esc(job.account_id) + '</td><td>' + esc(job.provider) + '</td><td>' +
        esc(job.last_attempt_at ? when(job.last_attempt_at) : 'Not run') + '</td><td>' +
        esc(job.last_success_at ? when(job.last_success_at) : 'No snapshot yet') + '</td><td>' +
        esc(job.next_due_at ? when(job.next_due_at) : '—') + '</td><td>' +
        esc(job.last_error || (job.last_source ? 'Updated · ' + job.last_source : 'Waiting')) + '</td></tr>';
    }).join('') : '<tr><td colspan="6" class="empty">No quota refresh has been scheduled yet. Workers create schedules when they next poll.</td></tr>';
    h += '</tbody></table></div>' + (state.quotaRefreshError ? '<p class="error" role="alert">' + esc(state.quotaRefreshError) + '</p>' : '') +
      '</section>';
    return h;
  }

  function save() {
    if (!binding || binding.state.busy) return;
    var state = binding.state, field = document.querySelector('[data-quota-refresh-interval]');
    var interval = Number(field && field.value);
    if (!Number.isInteger(interval) || interval < 1 || interval > 1440) {
      state.quotaRefreshError = 'Choose a refresh interval from 1 to 1440 minutes.'; binding.render(); return;
    }
    state.busy = true; state.notice = ''; state.quotaRefreshError = ''; binding.render();
    fetch('/v1/quota-refresh/settings', { method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ interval_minutes: interval }) }).then(binding.responseError)
      .then(function (data) {
        state.quotaRefresh = data; state.quotaRefreshLoaded = true; state.notice = 'Quota refresh interval saved.';
      })
      .catch(function (error) { state.quotaRefreshError = String(error.message || error); })
      .then(function () { state.busy = false; binding.render(); });
  }

  document.addEventListener('click', function (event) {
    var button = event.target.closest && event.target.closest('[data-act="quota-refresh-save"]');
    if (!button) return;
    event.preventDefault(); event.stopImmediatePropagation(); save();
  });
  window.AIFlowQuotaRefresh = { panel: panel };
})();
