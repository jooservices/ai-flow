/* ---------- Analytics (/v1/analytics/daily, per UTC day, project and account) ---------- */
function utcDays(n) {
  var out = [], t = Date.now();
  for (var i = n - 1; i >= 0; i--) out.push(new Date(t - i * 86400000).toISOString().slice(0, 10));
  return out;
}
function analyticsChart(rows) {
  var days = utcDays(ANALYTICS_DAYS), by = {};
  days.forEach(function (d) { by[d] = { ok: 0, rework: 0, failed: 0, other: 0 }; });
  rows.forEach(function (r) {
    var b = by[r.day]; if (!b) return;
    b.ok += r.done + r.merged; b.rework += r.rework; b.failed += r.failed;
    b.other += Math.max(0, r.runs - r.done - r.merged - r.rework - r.failed);
  });
  var max = 1; days.forEach(function (d) { var b = by[d]; max = Math.max(max, b.ok + b.rework + b.failed + b.other); });
  var W = 28, G = 8, H = 120, LB = 16, svg = '<svg class="an-chart" viewBox="0 0 ' + (days.length * (W + G)) + ' ' + (H + LB) + '" role="img" aria-label="Runs per day by outcome, last ' + days.length + ' days">';
  days.forEach(function (d, i) {
    var b = by[d], x = i * (W + G) + G / 2, y = H;
    [['ok', 'Done or merged'], ['rework', 'Rework'], ['failed', 'Failed'], ['other', 'Other']].forEach(function (k) {
      if (!b[k[0]]) return;
      var hh = H * b[k[0]] / max; y -= hh;
      svg += '<rect class="an-' + k[0] + '" x="' + x + '" y="' + y.toFixed(1) + '" width="' + W + '" height="' + hh.toFixed(1) + '"><title>' + esc(d) + ' · ' + k[1] + ': ' + b[k[0]] + '</title></rect>';
    });
    svg += '<text x="' + (x + W / 2) + '" y="' + (H + LB - 3) + '" text-anchor="middle">' + esc(d.slice(8)) + '</text>';
  });
  return svg + '</svg>';
}
function conflictLossBlock() {
  /* Work lost to merge conflicts (/v1/analytics/conflicts): share of verifications, time spent, trend */
  var c = S.conflicts;
  var h = '<div id="d-conflicts" class="conflict-loss" aria-label="Work lost to merge conflicts"><h3>Lost to merge conflicts</h3>';
  if (S.conflictsError) return h + '<div class="empty">Conflict numbers unavailable: ' + esc(S.conflictsError) + '</div></div>';
  if (!c) return h + '<div class="empty">Loading…</div></div>';
  var cur = c.current, prev = c.previous, ch = c.change;
  var dir = function (v, unit) { return v === 0 ? 'no change' : (v > 0 ? '+' : '') + v + unit; };
  h += '<div class="legend"><span>Share lost: <b class="mono">' + cur.share_percent + '%</b> (' + cur.conflicts + ' of ' + cur.verifications + ' verifications)</span>';
  h += '<span>Time spent: <b class="mono">' + mmss(cur.seconds) + '</b>, average <span class="mono">' + mmss(cur.average_seconds) + '</span> per conflict</span>';
  h += '<span>Previous ' + c.days + ' days: <span class="mono">' + prev.share_percent + '%</span>, <span class="mono">' + mmss(prev.seconds) + '</span></span>';
  h += '<span>Change: <span class="mono">' + dir(ch.share_points, ' points') + '</span></span>';
  h += '<span>Updated automatically, not counted: <span class="mono">' + cur.clean_updates + '</span></span></div>';
  var max = 1; c.trend.forEach(function (t) { max = Math.max(max, t.share_percent); });
  var W = 28, G = 8, H = 60, LB = 16, svg = '<svg class="an-chart" viewBox="0 0 ' + (c.trend.length * (W + G)) + ' ' + (H + LB) + '" role="img" aria-label="Share of verifications lost to conflicts per day">';
  c.trend.forEach(function (t, i) {
    var x = i * (W + G) + G / 2, hh = H * t.share_percent / max;
    svg += '<rect class="an-rework" x="' + x + '" y="' + (H - hh).toFixed(1) + '" width="' + W + '" height="' + hh.toFixed(1) + '"><title>' + esc(t.period) + ' · ' + t.share_percent + '% (' + t.conflicts + ' of ' + t.verifications + ') · ' + mmss(t.seconds) + '</title></rect>';
    svg += '<text x="' + (x + W / 2) + '" y="' + (H + LB - 3) + '" text-anchor="middle">' + esc(t.period.slice(8)) + '</text>';
  });
  return h + svg + '</svg><p class="hint">' + esc(c.definition) + '</p></div>';
}
function analyticsPanel() {
  var rows = S.analytics.filter(function (r) { return r.project === S.project; });
  var h = '<section class="panel" data-block="analytics" id="d-analytics" aria-labelledby="h-an"><div class="panel-h"><h2 id="h-an">Analytics</h2><span class="hint">Last ' + ANALYTICS_DAYS + ' days (UTC) per account; refreshed every minute</span><span class="spacer"></span><div class="legend"><span><i class="an-ok"></i>Done or merged</span><span><i class="an-rework"></i>Rework</span><span><i class="an-failed"></i>Failed</span><span><i class="an-other"></i>Other</span></div></div>';
  if (S.anError) h += '<div class="empty">Analytics unavailable: ' + esc(S.anError) + '</div>';
  h += conflictLossBlock();
  h += analyticsChart(rows);
  if (!rows.length) return h + '<div class="empty">No runs for this project in the last ' + ANALYTICS_DAYS + ' days.</div></section>';
  h += '<div class="tablebox"><table class="an-table" style="min-width:880px"><thead><tr><th>Day</th><th>Account</th><th>Runs</th><th>Done</th><th>Merged</th><th>Rework</th><th>Failed</th><th>Tokens in</th><th>Tokens out</th><th>Cost</th><th>Median run</th></tr></thead><tbody>';
  rows.forEach(function (r) {
    h += '<tr><td class="mono">' + esc(r.day) + '</td><td class="mono">' + esc(r.account) + '</td><td class="mono">' + r.runs + '</td><td class="mono">' + r.done + '</td><td class="mono">' + r.merged + '</td><td class="mono">' + r.rework + '</td><td class="mono">' + r.failed + '</td>';
    h += '<td class="mono">' + tok(r.tokens_in) + '</td><td class="mono">' + tok(r.tokens_out) + '</td><td class="mono">' + usd(r.cost_usd) + '</td><td class="mono">' + mmss(r.median_run_s) + '</td></tr>';
  });
  return h + '</tbody></table></div></section>';
}
function card(t, p) {
  var flags = '';
  if (t.labels.indexOf('aiflow-hold') >= 0) flags += '<span class="flag" data-tone="muted">on hold</span>';
  if (t.labels.indexOf('aiflow-ready') >= 0 && t.status === 'To Do') flags += '<span class="flag" data-tone="ok">ready</span>';
  if (t.labels.indexOf('require-human') >= 0) flags += '<span class="flag" data-tone="info">require human</span>';
  if (t.labels.indexOf('tier-senior') >= 0) flags += '<span class="flag" data-tone="muted">senior</span>';
  if (t.status === 'In Verify' && t.already_done) flags += '<span class="flag" data-tone="ok">Already done · sent to verification</span>';
  var blocked = (t.blocked_by || []).filter(function (k) { var b = ticketBy(k); return !b || b.status !== 'Done'; });
  if (blocked.length && t.status !== 'Done') flags += '<span class="flag" data-tone="warn">blocked by ' + esc(blocked.join(', ')) + '</span>';
  var h = '<button class="card" data-act="open-ticket" data-key="' + esc(t.key) + '">';
  h += '<span class="c-top"><span class="c-key">' + esc(t.key) + '</span><span class="c-type">' + esc(t.type) + '</span>' + (t.priority && t.priority !== 'Medium' ? '<span class="c-type">' + esc(t.priority) + '</span>' : '') + '</span>';
  h += '<span class="c-title">' + esc(t.summary) + '</span>';
  if (t.repository) h += '<span class="c-run">Repository · <span class="mono">' + esc(t.repository) + '</span></span>';
  if (flags) h += '<span class="c-top">' + flags + '</span>';
  if (t.run) {
    var wm = p.work_minutes || {};
    var hard = (wm.hard || 45) * 60;
    h += '<span class="c-run"><span class="sdot" data-tone="info"></span><span class="mono">' + esc(t.run.account) + '</span> · ' + esc(t.run.step) + ' · fence #' + t.run.fence + '</span>';
    h += '<span class="c-clock"><span class="track"><i data-clock="' + (t.run.elapsed_s || 0) + '" data-hard="' + hard + '"></i><b style="left:50%"></b><b style="left:75%"></b></span><span class="mono" data-since="' + (t.run.elapsed_s || 0) + '" data-fmt="clock"></span></span>';
  } else if (t.last_run) {
    var lr = t.last_run;
    h += '<span class="c-run">' + pill(runTone(lr.outcome), lr.outcome) + ' <span class="mono">' + esc(lr.account) + '</span> · ' + mmss(lr.duration_s) + (lr.pr_url ? ' · PR #' + esc(lr.pr_url.split('/').pop()) : '') + '</span>';
  }
  h += '</button>';
  if (t.status === 'In Verify' && t.already_done && t.already_done.pr_url) {
    var prMatch = t.already_done.pr_url.match(/\/pull\/(\d+)(?:\/|$)/), prLabel = prMatch ? 'Pull request #' + prMatch[1] : 'Open pull request';
    return '<div class="card-wrap">' + h + '<a class="card-pr-link" href="' + esc(safeHref(t.already_done.pr_url)) + '" target="_blank" rel="noopener" aria-label="' + esc(prLabel + ' for ' + t.key) + '">' + esc(prLabel) + ' ' + ICON.ext + '</a></div>';
  }
  return h;
}

