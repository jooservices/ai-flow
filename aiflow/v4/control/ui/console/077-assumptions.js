function assumptionsPanel() {
  var state = S.dashboardAssumptions, data = state.data;
  var h = '<section class="panel" data-block="assumptions">' +
    '<div class="panel-h"><h2>AI assumptions</h2>' +
    '<span class="hint">Conservative choices on structure, naming, scope and tools · UTC days</span></div>';
  if (state.error) return h + '<div class="dashboard-empty">Assumptions unavailable: ' + esc(state.error) + '</div></section>';
  if (!data) return h + '<div class="dashboard-empty">' + (state.loading ? 'Loading assumptions…' : 'No assumptions recorded yet.') + '</div></section>';
  var total = Number(data.total_assumptions || 0);
  var contradicted = Number(data.total_contradicted || 0);
  var active = total - contradicted;
  h += '<div class="facts">' +
    '<div class="fact"><span class="fl">Assumptions made</span><span class="fv mono">' + total + '</span></div>' +
    '<div class="fact"><span class="fl">Held</span><span class="fv mono">' + active + '</span></div>' +
    '<div class="fact"><span class="fl">Contradicted</span><span class="fv mono' + (contradicted ? ' warn' : '') + '">' + contradicted + '</span></div>' +
    '</div>';
  var days = data.days || [];
  if (days.length) {
    h += '<div class="tablebox"><table style="min-width:480px"><thead><tr><th>Day</th><th>Assumptions</th><th>Contradicted</th></tr></thead><tbody>';
    days.forEach(function (row) {
      h += '<tr><td class="mono">' + esc(row.day) + '</td><td class="mono">' + Number(row.total || 0) + '</td><td class="mono' + (row.contradicted ? ' warn' : '') + '">' + Number(row.contradicted || 0) + '</td></tr>';
    });
    h += '</tbody></table></div>';
  }
  var recent = data.recent || [];
  if (recent.length) {
    h += '<div class="dsec"><h4>Recent assumptions</h4><ul class="feed">';
    recent.slice(0, 8).forEach(function (row) {
      var badge = row.contradicted ? '<span class="pill" data-tone="crit">Contradicted</span>' : '<span class="pill" data-tone="ok">Active</span>';
      h += '<li><span class="f-time">' + esc(String(row.created_at || '').slice(0, 10)) + '</span>' +
        '<span class="f-type mono"><button class="linkbtn mono" data-act="open-ticket" data-key="' + esc(row.ticket_key) + '">' + esc(row.ticket_key) + '</button></span>' +
        '<span class="f-text">' + esc(row.assumption) + (row.why ? ' · <span class="muted">' + esc(row.why) + '</span>' : '') + ' ' + badge + '</span></li>';
    });
    h += '</ul></div>';
  } else if (!days.length) {
    h += '<div class="dashboard-empty">No assumptions made by the flow yet.</div>';
  }
  return h + '</section>';
}
