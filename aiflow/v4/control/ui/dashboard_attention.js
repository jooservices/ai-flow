(function (global) {
  function slowVerificationsContent(items, escapeHtml) {
    var h = '<div class="panel-h"><h2 id="dashboard-slow-verifications-title">Slow verifications</h2><span class="hint">Waiting more than 30 minutes</span></div>';
    if (!items.length) return h + '<div class="empty">No slow verifications.</div>';
    h += '<div class="att">';
    items.forEach(function (item) {
      var accounts = (item.accounts || []).map(function (account) {
        var label = account.account || 'Unknown verifier';
        if (account.no_verdict) label += ' (no verdict x' + account.no_verdict
          + (account.barred ? ', not offered again' : '') + ')';
        return label;
      });
      h += '<button class="att-item" data-act="open-ticket" data-key="' + escapeHtml(item.key)
        + '"><span class="pill" data-tone="warn">' + escapeHtml(item.waiting_minutes)
        + ' min</span><span class="t"><span class="mono muted">' + escapeHtml(item.key) + '</span> '
        + escapeHtml(item.summary) + '<span class="d">' + escapeHtml(item.project) + ' · tried: '
        + escapeHtml(accounts.join(', ') || 'no account has tried yet')
        + '</span></span><span class="muted">Open</span></button>';
    });
    return h + '</div>';
  }

  function addForTicket(attention, ticket, workerMinutes, formatDuration) {
    var lastRun = ticket.last_run;
    if (ticket.status === 'Needs owner') attention.push([ticket, 'crit', 'Needs owner',
      'The master role triages the developer question; owner-escalated tickets need an owner decision']);
    else if (ticket.status === 'Human review') attention.push([ticket, 'info', 'Human review',
      'Require human = Yes; checks passed']);
    else if (ticket.run && (ticket.run.elapsed_s || 0) >= workerMinutes.target * 60) {
      attention.push([ticket, ticket.run.elapsed_s >= workerMinutes.warn * 60 ? 'crit' : 'warn',
        'Over ' + workerMinutes.target + ' min', ticket.run.account + ' · ' + formatDuration(ticket.run.elapsed_s)]);
    } else if (ticket.slow_verify) attention.push([ticket, 'warn', 'Waiting for verification', ticket.slow_verify]);
    else if (!ticket.run && lastRun && ticket.status === 'To Do'
      && ['failed', 'lost', 'timeout', 'quota'].indexOf(lastRun.outcome) >= 0) {
      attention.push([ticket, 'warn', 'Retry', lastRun.account + ': ' + lastRun.outcome
        + (lastRun.error_kind ? ' (' + lastRun.error_kind + ')' : '')]);
    }
  }

  global.AIFlowDashboardAttention = {
    addForTicket: addForTicket,
    slowVerificationsContent: slowVerificationsContent
  };
})(window);
