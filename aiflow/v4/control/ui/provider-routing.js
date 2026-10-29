/* Provider quota preferences and assignment explanations for Machines and AI. */
(function () {
  var state = { data: null, order: null, ceilings: {}, busy: false, error: '', loadedAt: 0,
    loading: false, signature: '', log: { ticket: '', items: null, total: 0, error: '' } };
  var names = { codex: 'Codex', grok: 'Grok', claude: 'Claude', opencode: 'OpenCode' };

  function esc(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, function (char) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char];
    });
  }
  function providerName(provider) { return names[provider] || provider; }
  function percent(value) {
    var amount = Number(value);
    return Number.isFinite(amount) ? amount.toFixed(1) : esc(value);
  }
  function dateTime(value) {
    var date = new Date(value);
    return Number.isNaN(date.getTime()) ? 'unknown' : date.toLocaleString();
  }
  function panel(root) {
    if (!root || root.querySelector('#p-routing')) return;
    root.insertAdjacentHTML('beforeend', '<section class="panel" data-block="routing" id="p-routing" aria-labelledby="h-routing">' +
      '<div class="panel-h"><h2 id="h-routing">AI provider routing</h2><span class="hint">Tickets go first to the provider with the most quota left; a provider above its ceiling is held back</span></div>' +
      '<div id="routing-host"></div></section>');
  }
  function load() {
    if (state.loading) return;
    state.loading = true;
    fetch('/v1/provider-routing', { cache: 'no-store' }).then(function (response) {
      if (!response.ok) throw new Error('HTTP ' + response.status);
      return response.json();
    }).then(function (data) { apply(data, false); state.error = ''; })
      .catch(function (error) { state.error = String(error.message || error); })
      .then(function () { state.loading = false; state.loadedAt = Date.now(); render(); });
  }
  function apply(data, resetDraft) {
    state.data = data;
    if (resetDraft || !state.order) {
      state.order = data.order.slice();
      state.ceilings = {};
      data.providers.forEach(function (provider) { state.ceilings[provider.provider] = String(provider.ceiling_pct); });
    }
  }
  function loadDecisions(more) {
    var log = state.log, base = more ? (log.items || state.data.decisions) : [], offset = base.length;
    fetch('/v1/provider-routing/decisions?limit=30&offset=' + offset + '&ticket=' + encodeURIComponent(log.ticket),
      { cache: 'no-store' }).then(function (response) { return response.json(); }).then(function (data) {
      if (more) log.items = base.concat(data.decisions);
      else { log.items = data.decisions; log.total = data.total; }
      log.error = '';
    }).catch(function (error) { log.error = String(error.message || error); }).then(render);
  }
  function move(provider, direction) {
    var index = state.order.indexOf(provider), next = index + direction;
    if (index < 0 || next < 0 || next >= state.order.length) return;
    state.order.splice(index, 1); state.order.splice(next, 0, provider); render();
  }
  function save() {
    if (state.busy) return;
    state.busy = true; state.error = ''; render();
    var ceilings = {};
    Object.keys(state.ceilings).forEach(function (provider) { ceilings[provider] = Number(state.ceilings[provider]); });
    fetch('/v1/provider-routing/settings', { method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ order: state.order, ceilings: ceilings }) }).then(function (response) {
      return response.json().then(function (data) {
        if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'HTTP ' + response.status);
        return data;
      });
    }).then(function (data) {
      apply(data, true);
      if (window.toast) window.toast('Provider routing saved', 'The new order and ceilings apply to the next offers.', 'ok');
    }).catch(function (error) { state.error = String(error.message || error); })
      .then(function () { state.busy = false; render(); });
  }
  function renderWindow(item) {
    return esc(item.name.replace(/_/g, ' ')) + (item.account ? ' · ' + esc(item.account) : '') + ' ' +
      (item.reset ? 'reset' : percent(item.used_pct) + '% used' +
        (item.reset_at ? ' · resets ' + esc(dateTime(item.reset_at)) : ''));
  }
  function renderProviderRow(provider, index, canEdit) {
    var name = provider.provider;
    var tone = provider.held_back ? 'crit' : provider.preference === 'spend first' ? 'ok' :
      provider.preference === 'keep for need' ? 'warn' : 'info';
    var h = '<tr><td class="mono">';
    if (canEdit) h += '<button class="btn sm" data-routing-action="move" data-provider="' + esc(name) +
      '" data-dir="-1" aria-label="Move ' + esc(name) + ' earlier"' + (index === 0 ? ' disabled' : '') + '>↑</button> ';
    h += (index + 1);
    if (canEdit) h += ' <button class="btn sm" data-routing-action="move" data-provider="' + esc(name) +
      '" data-dir="1" aria-label="Move ' + esc(name) + ' later"' +
      (index === state.order.length - 1 ? ' disabled' : '') + '>↓</button>';
    h += '</td><td>' + esc(providerName(name)) + ' <span class="muted">· ' + provider.accounts + ' account' +
      (provider.accounts === 1 ? '' : 's') + '</span></td>';
    h += '<td>' + (window.pill ? window.pill(tone, provider.preference) : esc(provider.preference)) + '</td>';
    h += '<td class="mono">' + (provider.unknown ? '—' : esc(provider.headroom_pct) + '%') + '</td>';
    h += '<td class="small">' + (provider.windows.length ? provider.windows.map(renderWindow).join('<br>') : '—') + '</td>';
    h += '<td><input type="number" min="1" max="100" step="1" style="width:5em" aria-label="Ceiling for ' +
      esc(name) + '" data-routing-ceiling="' + esc(name) + '" value="' + esc(state.ceilings[name]) + '"' +
      (canEdit ? '' : ' disabled') + '></td>';
    h += '<td class="small">' + (provider.held_back ? '<span style="color:var(--crit)">Held back: ' +
      esc(provider.held_reason) + '</span>' : 'In normal use') + (provider.unknown ?
        '<div>Quota unknown: this provider reports no quota, so it is treated as having headroom.</div>' : '') +
      '</td></tr>';
    return h;
  }
  function renderProviderRows(data, canEdit) {
    var h = '<div class="tablebox"><table><thead><tr><th scope="col">Preferred order</th>' +
      '<th scope="col">Provider</th><th scope="col">Spend preference</th><th scope="col">Quota left</th>' +
      '<th scope="col">Windows</th><th scope="col">Ceiling (% of window)</th><th scope="col">Status</th>' +
      '</tr></thead><tbody>';
    state.order.forEach(function (name, index) {
      var provider = data.providers.find(function (item) { return item.provider === name; });
      if (provider) h += renderProviderRow(provider, index, canEdit);
    });
    return h + '</tbody></table></div>';
  }
  function renderRoutingSettings(data, canEdit) {
    var h = '<div class="small" style="padding:8px 12px">Default order when providers are otherwise equal: Codex, Grok, Claude. ' +
      'The ceiling applies to every window a provider reports, each with its own reset time. A held-back provider ' +
      'keeps master, triage, tickets labelled for it and work no other provider can do; it returns after its window ' +
      'resets or when usage is below 60%.</div>';
    h += canEdit ? '<div style="padding:0 12px 8px"><button class="btn primary" data-routing-action="save"' +
      (state.busy ? ' disabled' : '') + '>' + (state.busy ? 'Saving…' : 'Save order and ceilings') + '</button></div>' :
      '<div class="small" style="padding:0 12px 8px">Only an owner can change the order and ceilings.</div>';
    if (data.settings && data.settings.updated_by) h += '<div class="small" style="padding:0 12px 8px">Last changed by ' +
      esc(data.settings.updated_by) + ' at ' + esc(dateTime(data.settings.updated_at)) + '.</div>';
    if (state.error) h += '<div class="err" role="alert" style="padding:0 12px 8px">' + esc(state.error) + '</div>';
    return h;
  }
  function renderAssignmentHistory(data) {
    var log = state.log, rows = log.items || data.decisions;
    var total = log.items ? log.total : data.decisions.length;
    var h = '<div class="panel-h" style="padding:6px 12px 0"><h2 style="font-size:14px">Why each ticket went to its account</h2>' +
      '<span class="hint">Latest assignments</span></div>' +
      '<div style="padding:4px 12px"><input type="search" id="routing-ticket" aria-label="Find the assignments of a ticket" ' +
      'placeholder="Ticket key, e.g. JAIF-264" value="' + esc(log.ticket) + '"> ' +
      '<button class="btn sm" data-routing-action="find">Find</button></div>';
    if (log.error) h += '<div class="err" role="alert" style="padding:0 12px">' + esc(log.error) + '</div>';
    if (!rows.length) return h + '<div class="empty">' + (log.ticket ?
      'No assignment recorded for ' + esc(log.ticket) + '.' : 'No ticket has been assigned yet.') + '</div>';
    h += '<div class="tablebox"><table><thead><tr><th scope="col">When</th><th scope="col">Ticket</th>' +
      '<th scope="col">Step</th><th scope="col">Account</th><th scope="col">Reason</th></tr></thead><tbody>';
    rows.forEach(function (item) {
      h += '<tr><td>' + esc(dateTime(item.at)) + '</td><td class="mono">' + esc(item.ticket) + '</td><td>' +
        esc(item.step) + '</td><td class="mono">' + esc(item.account) + '</td><td class="small">' +
        esc(item.reason) + '</td></tr>';
    });
    h += '</tbody></table></div>';
    if ((!log.items && total >= 30) || (log.items && log.items.length < total)) {
      h += '<div style="padding:4px 12px"><button class="btn sm" data-routing-action="more">Show older assignments</button></div>';
    }
    return h;
  }
  function render() {
    var host = document.getElementById('routing-host');
    if (!host) return;
    var data = state.data, canEdit = window.consoleCan && window.consoleCan('console.manage');
    var signature = JSON.stringify([data, state.order, state.ceilings, state.busy, state.error, canEdit, state.log]);
    if (signature === state.signature && host.firstChild) return;
    state.signature = signature;
    if (!data) { host.innerHTML = '<div class="empty">' + esc(state.error || 'Loading…') + '</div>'; return; }
    host.innerHTML = renderProviderRows(data, canEdit) + renderRoutingSettings(data, canEdit) +
      renderAssignmentHistory(data);
  }
  document.addEventListener('click', function (event) {
    var control = event.target.closest && event.target.closest('[data-routing-action]');
    if (!control) return;
    var action = control.getAttribute('data-routing-action');
    if (action === 'move') move(control.getAttribute('data-provider'), Number(control.getAttribute('data-dir')));
    if (action === 'save') save();
    if (action === 'find') { state.log.ticket = (document.getElementById('routing-ticket').value || '').trim(); state.log.items = null; loadDecisions(false); }
    if (action === 'more') loadDecisions(true);
  });
  document.addEventListener('input', function (event) {
    var input = event.target, provider = input.getAttribute && input.getAttribute('data-routing-ceiling');
    if (provider && state.ceilings) state.ceilings[provider] = input.value;
  });
  window.AIFlowProviderRouting = {
    mount: function (root) {
      panel(root);
      render();
      if (!state.loading && Date.now() - state.loadedAt > 15000) load();
    }
  };
  function watchMachines() {
    var root = document.getElementById('view-machines');
    if (!root) return;
    new MutationObserver(function () {
      if (root.childElementCount) window.AIFlowProviderRouting.mount(root);
    }).observe(root, { childList: true });
    if (root.childElementCount) window.AIFlowProviderRouting.mount(root);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', watchMachines);
  else watchMachines();
}());
