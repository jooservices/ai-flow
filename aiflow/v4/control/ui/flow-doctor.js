function flowDoctorPanel(d, esc, when) {
  var doctor = d.flow_doctor || {}, coverage = doctor.role_coverage || [], findings = doctor.findings || [];
  var actions = doctor.actions || [], interventions = d.operator_interventions || {};
  var h = '<section class="dash-section" aria-labelledby="dashboard-doctor-title"><h2 class="dash-section-title" id="dashboard-doctor-title">Flow health</h2>';
  h += '<section class="panel" data-block="flow-doctor" id="p-flow-doctor"><div class="panel-h"><h2>Flow doctor</h2><span class="hint">Minute checks · findings, safe repairs and account coverage</span></div>';
  h += '<div class="facts">' + coverage.map(function (row) {
    return '<div class="fact"><span class="fl">' + esc(row.role) + ' · ready accounts</span><span class="fv mono">' +
      Number(row.ready_count || 0) + ' / 2</span><span class="small">' + esc((row.providers || []).join(', ') || 'no provider ready') +
      (row.healthy ? '' : '<br>' + esc(row.reason || 'Two providers are required.') + '<br>Next: ' + esc(row.next_step || 'The doctor will retry safe repairs.')) + '</span></div>';
  }).join('') + '</div>';
  if (findings.length) {
    h += '<h3>Open findings</h3><div class="att">';
    findings.slice(0, 20).forEach(function (row) {
      var ticket = /^[A-Z][A-Z0-9]+-[0-9]+$/.test(row.subject || '');
      h += '<div class="att-item"><span class="pill" data-tone="warn">' + esc(row.cause) + '</span><span class="t">' +
        (ticket ? '<button class="linkbtn mono" data-act="open-ticket" data-key="' + esc(row.subject) + '">' + esc(row.subject) + '</button>' : '<b>' + esc(row.subject) + '</b>') +
        '<span class="d">' + esc(row.detail) + '</span><span class="d">Next: ' + esc(row.next_step) + '</span></span><span class="small">' + esc(when(row.last_seen)) + '</span></div>';
    });
    h += '</div>';
  } else h += '<div class="empty">No open findings.</div>';
  if (actions.length) {
    h += '<h3>Recent doctor actions</h3><div class="att">';
    actions.slice(0, 12).forEach(function (row) {
      h += '<div class="att-item"><span class="pill" data-tone="ok">' + esc(row.outcome || 'recorded') + '</span><span class="t"><b>' + esc(row.action) + '</b>' +
        (row.ticket_key ? '<span class="d mono">' + esc(row.ticket_key) + '</span>' : '') + '<span class="d">' + esc(row.detail) +
        '</span></span><span class="small">' + esc(when(row.created_at)) + '</span></div>';
    });
    h += '</div>';
  }
  h += '</section><section class="panel" data-block="operator-interventions" id="p-operator-interventions"><div class="panel-h"><h2>Operator interventions</h2><span class="hint">Every recorded manual action creates a product Bug</span></div>';
  h += '<div class="facts"><div class="fact"><span class="fl">Last 24 hours</span><span class="fv mono">' + Number(interventions.last_24h || 0) + '</span></div>' +
    '<div class="fact"><span class="fl">Last 7 days</span><span class="fv mono">' + Number(interventions.last_7d || 0) + '</span></div></div>';
  var recent = interventions.recent || [];
  if (!recent.length) h += '<div class="empty">No operator interventions have been recorded.</div>';
  else {
    h += '<div class="tablebox"><table><thead><tr><th>When</th><th>Who</th><th>What</th><th>Where</th><th>Why</th><th>Bug</th></tr></thead><tbody>';
    recent.slice(0, 12).forEach(function (row) {
      h += '<tr><td title="' + esc(row.happened_at) + '">' + esc(when(row.happened_at)) + '</td><td>' + esc(row.actor) + '</td><td>' + esc(row.action) +
        '</td><td>' + esc(row.source + (row.target ? ' · ' + row.target : '')) + '</td><td>' + esc(row.reason || 'Reason not supplied') +
        '</td><td class="mono">' + (row.bug_ticket_key ? esc(row.bug_ticket_key) : '<span class="muted">creating</span>') + '</td></tr>';
    });
    h += '</tbody></table></div>';
  }
  return h + '</section></section>';
}
