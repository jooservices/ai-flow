/* Canary threshold settings panel; the main console provides its private state/render helpers. */
(function () {
  var binding = null;

  function panel(state, esc, when, render, responseError) {
    binding = { state: state, esc: esc, when: when, render: render, responseError: responseError };
    if (!state.canaryLoaded && !state.canaryLoading && !state.canaryLoadAttempted) {
      state.canaryLoadAttempted = true;
      state.canaryLoading = true;
      fetch('/v1/canary/settings', { cache: 'no-store' })
        .then(responseError).then(function (data) {
          state.canary = data; state.canaryError = ''; state.canaryLoaded = true;
        }).catch(function (error) {
          state.canaryError = String(error.message || error); state.canaryLoaded = false;
        })
        .then(function () { state.canaryLoading = false; render(); });
    }
    var settings = state.canary || {}, defaults = settings.defaults || {};
    var locked = state.busy || state.canaryLoading || !state.canaryLoaded;
    var h = '<section class="panel"><div class="panel-h"><h2>Canary thresholds</h2><span class="spacer"></span>' +
      '<button class="btn sm" data-act="canary-settings-save"' + (locked ? ' disabled' : '') +
      '>Save thresholds</button></div>' +
      '<p class="hint">Minimum time, completed runs and maximum time for the automatic control-plane canary. Defaults fit a repository that merges many times an hour.</p>' +
      '<div class="project-form">' +
      '<label>Minimum time (seconds)<input class="in" type="number" min="60" max="86400" step="1" data-canary-min-age value="' +
      esc(settings.minimum_age_s || defaults.minimum_age_s || 300) + '"' + (locked ? ' disabled' : '') + '></label>' +
      '<label>Minimum completed runs<input class="in" type="number" min="0" max="1000" step="1" data-canary-min-runs value="' +
      esc(settings.minimum_runs != null ? settings.minimum_runs : (defaults.minimum_runs || 3)) + '"' + (locked ? ' disabled' : '') + '></label>' +
      '<label>Maximum time (seconds)<input class="in" type="number" min="60" max="86400" step="1" data-canary-max-age value="' +
      esc(settings.maximum_age_s || defaults.maximum_age_s || 900) + '"' + (locked ? ' disabled' : '') + '></label></div>' +
      '<p class="small">Defaults: ' + esc(defaults.minimum_age_s || 300) + ' s, ' +
      esc(defaults.minimum_runs || 3) + ' runs, ' + esc(defaults.maximum_age_s || 900) +
      ' s. A canary with too few runs because no work is available is judged on health checks.</p>' +
      (settings.updated_at ? '<p class="small">Last changed by ' + esc(settings.updated_by || 'unknown') +
        ' at ' + esc(when(settings.updated_at)) + '.</p>' : '') +
      (state.canaryError ? '<p class="error" role="alert">' + esc(state.canaryError) + '</p>' +
        (!state.canaryLoaded ? '<button class="btn sm" data-act="canary-settings-retry"' +
          (state.canaryLoading ? ' disabled' : '') + '>Retry load</button>' : '') : '') +
      '</section>';
    return h;
  }

  function save() {
    if (!binding || binding.state.busy || !binding.state.canaryLoaded) return;
    var state = binding.state;
    var age = Number(document.querySelector('[data-canary-min-age]') && document.querySelector('[data-canary-min-age]').value);
    var runs = Number(document.querySelector('[data-canary-min-runs]') && document.querySelector('[data-canary-min-runs]').value);
    var maxAge = Number(document.querySelector('[data-canary-max-age]') && document.querySelector('[data-canary-max-age]').value);
    if (![age, runs, maxAge].every(function (n) { return Number.isInteger(n); }) || age < 60 || maxAge < age) {
      state.canaryError = 'Choose a minimum age of at least 60 seconds, runs from 0 to 1000, and a maximum age at least the minimum.';
      binding.render(); return;
    }
    state.busy = true; state.notice = ''; state.canaryError = ''; binding.render();
    fetch('/v1/canary/settings', { method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ minimum_age_s: age, minimum_runs: runs, maximum_age_s: maxAge }) })
      .then(binding.responseError)
      .then(function (data) {
        state.canary = data; state.canaryLoaded = true; state.notice = 'Canary thresholds saved.';
      })
      .catch(function (error) { state.canaryError = String(error.message || error); })
      .then(function () { state.busy = false; binding.render(); });
  }

  function retryLoad() {
    if (!binding || binding.state.busy || binding.state.canaryLoading || binding.state.canaryLoaded) return;
    binding.state.canaryLoadAttempted = false;
    binding.state.canaryError = '';
    binding.render();
  }

  document.addEventListener('click', function (event) {
    var retry = event.target.closest && event.target.closest('[data-act="canary-settings-retry"]');
    if (retry) {
      event.preventDefault(); event.stopImmediatePropagation(); retryLoad(); return;
    }
    var button = event.target.closest && event.target.closest('[data-act="canary-settings-save"]');
    if (!button) return;
    event.preventDefault(); event.stopImmediatePropagation(); save();
  });
  window.AIFlowCanarySettings = { panel: panel };
})();
