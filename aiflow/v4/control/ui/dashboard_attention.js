(function (global) {
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

  global.AIFlowDashboardAttention = { addForTicket: addForTicket };
})(window);
