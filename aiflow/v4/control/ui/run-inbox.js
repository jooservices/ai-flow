/* Run-inbox controls for Work Board and Delivery. Loaded by the console's Work view. */
(function () {
  window.addEventListener('aiflow-console-ready', function () {
    var app = window.AIFlowConsole, S = app.state;
    S.runInbox = { runId: '', runLabel: '', messages: [], loadedAt: 0, loading: false, sending: false,
      requestId: 0, requestRunId: '', draft: '', kind: 'context', error: '', notice: '' };
    try {
      var saved = JSON.parse(sessionStorage.getItem('aiflow4-run-inbox-selection') || 'null');
      if (saved && typeof saved.id === 'string') {
        S.runInbox.runId = saved.id;
        S.runInbox.runLabel = typeof saved.label === 'string' ? saved.label : '';
      }
    } catch (x) { /* session storage can be disabled */ }

    function rememberSelection() {
      try { sessionStorage.setItem('aiflow4-run-inbox-selection', JSON.stringify({ id: S.runInbox.runId, label: S.runInbox.runLabel })); }
      catch (x) { /* session storage can be disabled */ }
    }
    function load(runId, force) {
      var state = S.runInbox;
      if (!runId || (state.loading && state.requestRunId === runId) ||
          (!force && state.runId === runId && Date.now() - state.loadedAt < 12000)) return;
      var requestId = ++state.requestId;
      state.loading = true; state.requestRunId = runId; state.error = '';
      fetch('/v1/runs/' + encodeURIComponent(runId) + '/inbox', { cache: 'no-store' })
        .then(app.projectResponseError)
        .then(function (data) { if (state.requestId === requestId && state.runId === runId) { state.messages = data.messages || []; state.loadedAt = Date.now(); state.error = ''; } })
        .catch(function (error) { if (state.requestId === requestId && state.runId === runId) { state.error = String(error.message || error); state.loadedAt = Date.now(); } })
        .then(function () { if (state.requestId === requestId) { state.loading = false; if (state.runId !== runId) load(state.runId, true); else if (S.view === 'work' || S.view === 'delivery') app.renderDBody(); } });
    }
    function panel() {
      var state = S.runInbox;
      if (S.view !== 'work' && S.view !== 'delivery') return '';
      var data = app.data(), tickets = data.tickets || [];
      var active = (data.active_runs || []).filter(function (run) {
        return tickets.some(function (ticket) { return ticket.key === run.ticket; }) &&
          ['dev', 'verify', 'triage', 'intake', 'impact', 'master'].indexOf(run.step) >= 0;
      });
      var selected = active.filter(function (run) { return run.id === state.runId; })[0] || null;
      var historical = !selected && !!state.runId;
      if (!selected && !historical) selected = active[0] || null;
      if (!selected && !historical) return '';
      if (selected && state.runId !== selected.id) {
        state.runId = selected.id; state.runLabel = selected.ticket + ' · ' + selected.step + ' · ' + selected.node;
        state.messages = []; state.loadedAt = 0; rememberSelection();
      }
      if (selected) state.runLabel = selected.ticket + ' · ' + selected.step + ' · ' + selected.node;
      var permissions = (S.consoleUser && S.consoleUser.permissions) || [];
      var canSend = !S.consoleUser || permissions.indexOf('*') >= 0 || permissions.indexOf('review.actions') >= 0;
      if (!canSend) return '';
      load(state.runId, false);
      var loadingDisabled = state.loading ? ' disabled' : '';
      var esc = app.esc;
      var options = active.map(function (run) {
        var label = run.ticket + ' · ' + run.step + ' · ' + run.node;
        return '<option value="' + esc(run.id) + '"' + (selected && run.id === selected.id ? ' selected' : '') + '>' + esc(label) + '</option>';
      }).join('');
      if (historical) options = '<option value="' + esc(state.runId) + '" selected>' + esc(state.runLabel || 'Previous run') + ' · ended</option>' + options;
      var messages = state.messages.length ? state.messages.map(function (message) {
        var tone = message.status === 'included' ? 'ok' : message.status === 'undelivered' ? 'warn' : 'info';
        var label = message.status === 'included' ? 'Included in provider prompt' : message.status === 'received' ? 'Received by worker' :
          message.status === 'undelivered' ? (message.terminal_reason || 'Run ended before checkpoint') : 'Waiting for checkpoint';
        return '<article class="alert"><span class="pill" data-tone="' + tone + '">' + esc(message.kind) + ' · ' + label + '</span>' +
          '<span class="a-detail"><strong>' + esc(message.author) + '</strong> · ' + esc(app.when(message.created_at)) + '<br><span style="white-space:pre-wrap">' + esc(message.body) + '</span></span></article>';
      }).join('') : '<div class="empty">No guidance sent to this run yet.</div>';
      var html = '<section class="panel" id="run-inbox"><div class="panel-h"><h2>Guide a live run</h2><span class="hint">Delivered after the current AI attempt completes; active tools are never interrupted.</span></div>';
      html += '<div class="form-grid"><label class="field"><span>Active run</span><select id="run-inbox-run">' + options + '</select></label>';
      html += '<div class="dsec"><h4>Inbox</h4>' + (state.loading ? '<div class="muted">Refreshing…</div>' : '') + messages + '</div>';
      if (state.error) html += '<div class="alert"><span class="a-detail">' + esc(state.error) + '</span></div>';
      if (state.notice) html += '<div class="alert"><span class="a-detail">' + esc(state.notice) + '</span></div>';
      if (canSend && selected) {
        html += '<label class="field"><span>Message type</span><select id="run-inbox-kind"' + loadingDisabled + '><option value="context"' + (state.kind === 'context' ? ' selected' : '') + '>Context</option>' +
          '<option value="request"' + (state.kind === 'request' ? ' selected' : '') + '>Request</option><option value="idea"' + (state.kind === 'idea' ? ' selected' : '') + '>Idea</option></select></label>';
        html += '<label class="field"><span>Message for the worker</span><textarea id="run-inbox-draft" maxlength="8000" rows="3"' + loadingDisabled + ' placeholder="Add context or a request for the current run…">' + esc(state.draft) + '</textarea></label>';
        html += '<button class="btn primary" type="button" data-act="run-inbox-send"' + (state.loading || state.sending || !state.draft.trim() ? ' disabled' : '') + '>Send to worker</button>';
      } else if (!selected) html += '<div class="muted">This run has ended. Any message not included at the next checkpoint is marked undelivered.</div>';
      return html + '</div></section>';
    }
    function send() {
      var state = S.runInbox, runId = state.runId, draft = state.draft, text = draft.trim(), kind = state.kind;
      if (!runId || !text || state.sending) return;
      state.sending = true; state.error = ''; state.notice = ''; app.renderDBody();
      var clientId = (window.crypto && window.crypto.randomUUID) ? window.crypto.randomUUID() :
        'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function (c) { var r = Math.random() * 16 | 0; return (c === 'x' ? r : (r & 3 | 8)).toString(16); });
      fetch('/v1/runs/' + encodeURIComponent(runId) + '/inbox', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ client_id: clientId, kind: kind, body: text }) }).then(app.projectResponseError)
        .then(function () { if (state.runId === runId) { if (state.draft === draft) state.draft = ''; state.notice = 'Message queued for the next AI attempt checkpoint.'; state.loadedAt = 0; load(runId, true); } })
        .catch(function (error) { if (state.runId === runId) state.error = String(error.message || error); })
        .then(function () { state.sending = false; app.renderDBody(); });
    }

    app.runInboxPanel = panel;
    app.renderBoardContent = function (target, html) {
      var focused = target && typeof target.contains === 'function' && target.contains(document.activeElement) ? document.activeElement : null;
      var focusedId = focused && /^(run-inbox-draft|run-inbox-kind|run-inbox-run)$/.test(focused.id) ? focused.id : '';
      var selection = focusedId === 'run-inbox-draft' ? [focused.selectionStart, focused.selectionEnd] : null;
      var focusedValue = focusedId === 'run-inbox-kind' ? focused.value : null;
      if (target) target.innerHTML = html;
      if (focusedId) {
        var restore = target && target.querySelector('#' + focusedId);
        if (restore) {
          restore.focus();
          if (focusedId === 'run-inbox-kind' && focusedValue) {
            restore.value = focusedValue; S.runInbox.kind = focusedValue;
          }
          if (selection) restore.setSelectionRange(selection[0], selection[1]);
        }
      }
    };
    document.addEventListener('click', function (event) {
      var button = event.target.closest && event.target.closest('[data-act="run-inbox-send"]');
      if (button) send();
    });
    document.addEventListener('input', function (event) {
      if (event.target.id !== 'run-inbox-draft') return;
      S.runInbox.draft = event.target.value;
      var board = app.activeBoard(), button = board && board.querySelector('[data-act="run-inbox-send"]');
      if (button) button.disabled = !event.target.value.trim() || S.runInbox.sending;
    });
    document.addEventListener('change', function (event) {
      if (event.target.id === 'run-inbox-run') {
        var state = S.runInbox, option = event.target.options[event.target.selectedIndex];
        state.runId = event.target.value; state.messages = []; state.loadedAt = 0; state.error = ''; state.notice = '';
        state.runLabel = option ? option.textContent.replace(/ · ended$/, '') : '';
        rememberSelection(); load(state.runId, true); app.renderDBody();
      } else if (event.target.id === 'run-inbox-kind') S.runInbox.kind = event.target.value;
    });
  });
})();
