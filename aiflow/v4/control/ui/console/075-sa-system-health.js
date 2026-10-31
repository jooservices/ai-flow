/* ---------- SA system health ---------- */
function renderSA() {
  var d = D(), sa = d.sa || {}, slots = sa.slots || {}, limits = sa.thresholds || {};
  var nodes = d.nodes || [], accounts = d.accounts || [], incidents = d.incidents || [];
  var blocked = sa.provider_blocks || [], denials = sa.policy_denials || [], network = sa.network_issues || [];
  var unavailable = accounts.filter(function (a) { return a.provider_status === 'signed_out' || a.check_failed; });
  var utilization = slots.utilization_percent;
  var h = '<section class="kpis" aria-label="System health summary">';
  h += kpi('Node health', nodes.filter(function (n) { return n.status === 'online'; }).length + ' / ' + nodes.length,
    nodes.filter(function (n) { return n.status !== 'online'; }).length + ' offline or joining',
    nodes.some(function (n) { return n.status === 'offline'; }) ? 'crit' : '', 'sa-nodes');
  h += kpi('Fleet slots', slots.used + ' / ' + slots.total,
    utilization == null ? 'no online capacity' : utilization + '% used · add node at ' + (limits.slots_used_percent || 80) + '%',
    utilization >= (limits.slots_used_percent || 80) ? 'warn' : '', 'sa-capacity');
  h += kpi('Blocked accounts', String(blocked.length), unavailable.length + ' signed out or failing checks',
    blocked.length || unavailable.length ? 'warn' : '', 'sa-accounts');
  h += kpi('Open incidents', String(incidents.length), incidents.length ? 'review current incidents' : 'none open',
    incidents.some(function (i) { return i.severity === 'critical'; }) ? 'crit' : incidents.length ? 'warn' : '', 'sa-incidents');
  h += '</section>';

  h += '<section class="panel" id="sa-recommendations"><div class="panel-h"><h2>Capacity recommendations</h2><span class="hint">Thresholds from aiflow-control/sa.yaml</span></div>';
  h += '<div class="small">Slots ' + (limits.slots_used_percent || 80) + '% for ' + (limits.slots_duration_minutes || 30) + ' min · all role accounts cooling ' + (limits.role_cooldown_minutes || 15) + ' min · idle for ' + (limits.idle_days || 7) + ' days</div>';
  if (!(sa.recommendations || []).length) h += '<div class="empty">No node or account changes are recommended right now.</div>';
  (sa.recommendations || []).forEach(function (r) {
    var label = { add_node: 'Add node', add_account: 'Add account · ' + r.subject,
      remove_node: 'Review node · ' + r.subject, remove_account: 'Review account · ' + r.subject }[r.kind] || r.kind;
    h += '<div class="alert"><span class="pill" data-tone="' + (r.kind.indexOf('remove') === 0 ? 'info' : 'warn') + '">' + esc(label) + '</span><span class="a-detail">' + esc(r.reason) + '</span></div>';
  });
  h += '</section>';

  h += '<section class="panel" id="sa-capacity"><div class="panel-h"><h2>Nodes and capacity</h2><span class="hint">Heartbeat health, system load and slot use</span></div>';
  if (!nodes.length) h += '<div class="empty">No nodes are registered.</div>';
  else {
    h += '<div class="tablebox"><table style="min-width:820px"><thead><tr><th>Node</th><th>Health</th><th>Slots used</th><th>CPU</th><th>Memory</th><th>Disk free</th><th>Heartbeat</th></tr></thead><tbody>';
    nodes.forEach(function (n) {
      var m = n.metrics || {}, pct = n.slots_total ? Math.round(100 * n.slots_used / n.slots_total) : 0;
      h += '<tr><td><button class="linkbtn mono" data-act="open-node" data-id="' + esc(n.name) + '">' + esc(n.name) + '</button></td>';
      h += '<td>' + pill(nodeTone(n), nodeLabel(n), n.status !== 'online') + '</td>';
      h += '<td class="mono">' + (n.slots_used || 0) + ' / ' + n.slots_total + ' · ' + pct + '%</td>';
      h += '<td class="mono">' + esc(pctText(m.cpu_pct)) + '</td>';
      h += '<td class="mono">' + esc(m.mem_total ? gib(m.mem_used) + ' / ' + gib(m.mem_total) : '—') + '</td>';
      h += '<td class="mono">' + esc(pctText(m.disk_free_pct)) + '</td>';
      h += '<td class="mono small">' + esc(ago(n.heartbeat_age_s)) + '</td></tr>';
    });
    h += '</tbody></table></div>';
  }
  h += '</section>';

  h += '<section class="panel" id="sa-accounts"><div class="panel-h"><h2>Account access, quota and credentials</h2><span class="hint">Signed-out and failed checks are shown immediately</span></div>';
  if (!accounts.length) h += '<div class="empty">No accounts are configured.</div>';
  else {
    h += '<div class="tablebox"><table style="min-width:900px"><thead><tr><th>Account</th><th>Node</th><th>Status / check</th><th>Quota</th><th>Credential expiry</th></tr></thead><tbody>';
    accounts.forEach(function (a) {
      var status = a.provider_status || a.status;
      var visual = Object.assign({}, a, { status: status });
      var expiry = a.credential_expires_at || 'unknown';
      h += '<tr><td><button class="linkbtn mono" data-act="open-acc" data-id="' + esc(a.id) + '">' + esc(a.id) + '</button></td>';
      h += '<td class="mono">' + esc(a.node) + '</td><td>' + pill(accTone(visual), accLabel(visual)) + (a.check_failed ? ' ' + pill('crit', 'Check failed') : '') + '</td>';
      h += '<td>' + qwin(a) + (status === 'cooldown' ? '<span class="small">' + esc(a.cooldown && a.cooldown.reason || 'cooldown') + '</span>' : '') + '</td>';
      h += '<td class="mono">' + esc(expiry === 'unknown' ? 'unknown' : when(expiry)) + (a.credential_expiry_warning ? ' ' + pill('warn', 'Expires within ' + Number(limits.credential_expiry_days || 7) + ' days') : '') + '</td></tr>';
    });
    h += '</tbody></table></div>';
  }
  h += '</section>';

  h += '<div class="grid-2"><section class="panel" id="sa-blocked"><div class="panel-h"><h2>AI blocked by policy</h2><span class="hint">Denied commands are linked to their account and ticket</span></div>';
  if (!denials.length) h += '<div class="empty">No policy denials recorded.</div>';
  denials.forEach(function (e) {
    var command = e.command || e.reason || 'policy denied this command';
    h += '<div class="alert"><span class="pill" data-tone="crit">Denied</span><span class="a-detail"><button class="linkbtn mono" data-act="open-acc" data-id="' + esc(e.account || '') + '">' + esc(e.account || 'unknown account') + '</button> · <button class="linkbtn mono" data-act="open-ticket" data-key="' + esc(e.ticket || '') + '">' + esc(e.ticket || 'no ticket') + '</button> · ' + esc(command) + '</span><span class="mono small">' + esc(when(e.ts)) + '</span></div>';
  });
  h += '</section><section class="panel"><div class="panel-h"><h2>AI blocked by provider</h2><span class="hint">Cooldown, quota exhaustion or signed-out credentials</span></div>';
  if (!blocked.length) h += '<div class="empty">No provider-blocked accounts.</div>';
  blocked.forEach(function (a) {
    var status = a.provider_status || a.status;
    var visual = Object.assign({}, a, { status: status });
    h += '<div class="alert"><span class="pill" data-tone="' + (status === 'signed_out' ? 'crit' : 'warn') + '">' + esc(accLabel(visual)) + '</span><span class="a-title"><button class="linkbtn mono" data-act="open-acc" data-id="' + esc(a.id) + '">' + esc(a.id) + '</button></span><span class="a-detail">' + esc(a.last_error || (a.cooldown && a.cooldown.reason) || 'provider is temporarily unavailable') + '</span></div>';
  });
  h += '</section></div>';

  h += '<section class="panel" id="sa-network"><div class="panel-h"><h2>Network errors and retries</h2><span class="hint">Read from registry events and runs</span><span class="spacer"></span><a class="btn sm" href="' + esc(safeHref((d.links || {}).o2) || '/o2/') + '" target="_blank" rel="noopener">OpenObserve ' + ICON.ext + '</a></div>';
  if (!network.length) h += '<div class="empty">No network errors or retries recorded.</div>';
  else {
    h += '<div class="tablebox"><table style="min-width:850px"><thead><tr><th>Time</th><th>Source</th><th>Node / account</th><th>Ticket</th><th>Detail</th><th>Retry</th></tr></thead><tbody>';
    network.forEach(function (e) {
      var data = e.data || {};
      var retry;
      if (e.source === 'run') retry = 'attempt ' + Number(e.attempts || 1) + (e.retry ? ' · retried' : ' · not retried yet');
      else retry = data.retry === true ? 'scheduled' :
        (Number(data.attempts) ? Number(data.attempts) + ' attempts' : 'not reported');
      h += '<tr><td class="mono small">' + esc(when(e.ts)) + '</td><td>' + esc(e.source === 'run' ? 'run · ' + (e.error_kind || 'network') : e.type || 'event') + '</td>';
      h += '<td class="mono">' + esc(e.node || '—') + (e.account ? ' · ' + esc(e.account) : '') + '</td>';
      h += '<td>' + (e.ticket ? '<button class="linkbtn mono" data-act="open-ticket" data-key="' + esc(e.ticket) + '">' + esc(e.ticket) + '</button>' : '—') + '</td>';
      h += '<td>' + esc(e.reason || e.detail || e.outcome || 'network failure') + '</td><td>' + esc(retry) + '</td></tr>';
    });
    h += '</tbody></table></div>';
  }
  h += '</section>';

  h += '<section class="panel" id="sa-incidents"><div class="panel-h"><h2>Incidents</h2><span class="hint">Open registry incidents</span></div>';
  if (!incidents.length) h += '<div class="empty">Nothing open.</div>';
  incidents.forEach(function (i) { h += incCard(i, false); });
  h += '</section>';
  $('#view-sa').innerHTML = h;
}

