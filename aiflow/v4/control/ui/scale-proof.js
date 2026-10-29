/* Scale proof (/v1/state scale_proof): budgets, runs attached to releases, measured values and findings. */
function scaleEsc(value) {
  return String(value == null ? '' : value).replace(/[&<>"']/g, function (c) {
    return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
  });
}
function scaleWhen(iso) {
  if (!iso) return '—';
  var d = new Date(iso), today = new Date(), same = d.toDateString() === today.toDateString();
  return (same ? '' : (d.getMonth() + 1) + '/' + d.getDate() + ' ') + String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
}
function scalePill(tone, text) {
  return '<span class="pill" data-tone="' + tone + '"><span class="dot"></span>' + scaleEsc(text) + '</span>';
}
function scaleJiraUrl(key, site) {
  return String(site || 'https://jooservices.atlassian.net').replace(/\/+$/, '') + '/browse/' + encodeURIComponent(key);
}
function scaleProofPanel(sp, jiraSite) {
  sp = sp || { runs: [], budgets: [], findings: [] };
  var h = '<section class="panel" id="p-scale" aria-labelledby="h-p-scale"><div class="panel-h"><h2 id="h-p-scale">Scale proof</h2><span class="hint">100 projects, 50 machines, 200 AI sessions: measured against the performance budgets on every release; a regression blocks the release</span></div>';
  if (!sp.runs.length) return h + '<div class="empty">No scale proof run is attached to a release yet.</div></section>';
  sp.runs.forEach(function (r) {
    var sc = r.scenario || {};
    h += '<div class="att-item" data-scale-run="' + scaleEsc(r.id) + '" data-passed="' + (r.passed ? 'yes' : 'no') + '"><span class="flag" data-tone="' + (r.passed ? 'ok' : 'crit') + '">' + (r.passed ? 'Within budget' : 'Regression') + '</span>';
    h += '<span style="min-width:0"><div class="t">Release <span class="mono">' + scaleEsc(r.release || '(none)') + '</span> · run #' + scaleEsc(r.id) + ' · ' + scaleEsc(scaleWhen(r.created_at)) + ' · ' + scaleEsc(sc.projects || '?') + ' projects, ' + scaleEsc(sc.machines || '?') + ' machines, ' + scaleEsc(sc.sessions || '?') + ' sessions</div>';
    h += '<table class="runs" style="min-width:0"><thead><tr><th>Budget</th><th>Statistic</th><th>Limit</th><th>Measured</th><th></th></tr></thead><tbody>';
    (r.budgets || []).forEach(function (b) {
      h += '<tr data-budget="' + scaleEsc(b.key) + '" data-state="' + (b.passed ? 'ok' : 'exceeded') + '"><td>' + scaleEsc(b.title) + '</td><td class="muted">' + scaleEsc(b.statistic) + '</td><td class="mono">' + scaleEsc(b.limit) + ' ' + scaleEsc(b.unit) + '</td><td class="mono">' + (b.measured == null ? '—' : scaleEsc(b.measured) + ' ' + scaleEsc(b.unit)) + '</td><td>' + scalePill(b.passed ? 'ok' : 'crit', b.passed ? 'ok' : 'exceeded') + '</td></tr>';
    });
    h += '</tbody></table>';
    if ((r.failures || []).length) h += '<ul class="d reasons">' + r.failures.map(function (f) { return '<li>' + scaleEsc(f) + '</li>'; }).join('') + '</ul>';
    var made = (r.tickets || []).filter(function (t) { return t.ticket; });
    if (made.length) h += '<div class="d scale-tickets">Tickets: ' + made.map(function (t) { return '<a class="mono" data-ticket="' + scaleEsc(t.ticket) + '" href="' + scaleEsc(scaleJiraUrl(t.ticket, jiraSite)) + '" target="_blank" rel="noopener">' + scaleEsc(t.ticket) + '</a>'; }).join(', ') + '</div>';
    h += '</span></div>';
  });
  return h + '</section>';
}
