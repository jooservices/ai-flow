/* ---------- shared bits ---------- */
function meter(label, pct, text) {
  var p = pct == null ? 0 : Math.max(0, Math.min(100, pct));
  return '<div class="meter"><span class="m-label">' + label + '</span><span class="track"><i style="width:' + p.toFixed(1) + '%" data-tone="' + tone(p) + '"></i></span><span class="m-val">' + esc(text) + '</span></div>';
}
function kpi(label, value, sub, toneName, target) {
  return '<button class="kpi" data-tone="' + (toneName || '') + '" data-act="jump" data-target="' + target + '"><span class="k-label">' + label + '</span><span class="k-value">' + value + '</span><span class="k-sub">' + sub + '</span></button>';
}
function pill(toneName, text, pulse) { return '<span class="pill" data-tone="' + toneName + '"' + (pulse ? ' data-pulse' : '') + '><span class="dot"></span>' + esc(text) + '</span>'; }
function nodeMetrics(n) {
  var m = n.metrics || {};
  var mem = m.mem_total ? 100 * m.mem_used / m.mem_total : null;
  var disk = m.disk_free_pct != null ? 100 - m.disk_free_pct : null;
  return meter('CPU', m.cpu_pct, m.cpu_pct == null ? '—' : m.cpu_pct.toFixed(0) + '% · load ' + (m.load1 != null ? m.load1 : '—')) +
    meter('RAM', mem, mem == null ? '—' : gib(m.mem_used) + ' / ' + gib(m.mem_total)) +
    meter('Disk', disk, disk == null ? '—' : gib(m.disk_free) + ' free');
}
function qwin(a) {
  var w = (a.quota && a.quota.windows) || null;
  if (!w || !Object.keys(w).length) {
    var note = (a.quota_awareness && a.quota_awareness.is_estimate) ? 'Provider quota not reported; usage estimate below' : a.provider === 'codex' ? 'Codex quota not reported' : a.provider === 'grok' ? 'Grok reports only an empty balance' : a.provider === 'opencode' ? 'free model, no usage reported' : 'not reported yet';
    return '<div class="qcell"><span class="qnote">' + note + '</span>' + quotaAwareness(a) + '</div>';
  }
  var h = '<div class="qcell">';
  [['five_hour', '5 hours'], ['seven_day', 'Weekly'], ['seven_day_opus', 'Weekly opus'], ['seven_day_sonnet', 'Weekly sonnet']].forEach(function (x) {
    var v = w[x[0]]; if (!v || v.utilization == null) return;
    var p = 100 * (v.utilization > 1 ? v.utilization / 100 : v.utilization);
    h += '<div class="qrow" title="resets ' + esc(quotaWhen(v.resets_at)) + '"><span class="ql">' + x[1] + '</span><span class="track"><i style="width:' + Math.min(100, p).toFixed(1) + '%" data-tone="' + tone(p) + '"></i></span><span class="qv">' + Math.round(p) + '%</span></div>';
  });
  var r5 = w.five_hour && w.five_hour.resets_at, r7 = w.seven_day && w.seven_day.resets_at;
  h += '<span class="qnote">resets ' + (r5 ? quotaWhen(r5) : '—') + (r7 ? ' · weekly ' + quotaWhen(r7) : '') + '</span>' + quotaAwareness(a) + '</div>';
  return h;
}
function quotaHistoryChart(history, account) {
  history = history || [];
  var max = 1;
  history.forEach(function (day) { max = Math.max(max, Number(day.tokens || 0)); });
  var bars = history.map(function (day, index) {
    var height = Math.max(1, Math.round(22 * Number(day.tokens || 0) / max));
    var x = 2 + index * 9;
    return '<rect x="' + x + '" y="' + (24 - height) + '" width="5" height="' + height + '" rx="1" fill="var(--accent)" opacity=".8"><title>' + esc(day.day) + ': ' + tok(day.tokens || 0) + ' tokens · ' + usd(day.cost_usd || 0) + '</title></rect>';
  }).join('');
  return '<svg class="quota-history" viewBox="0 0 68 26" role="img" aria-label="Seven day token history for ' + esc(account) + '">' + bars + '</svg>';
}
function quotaAwareness(account) {
  var awareness = account && account.quota_awareness;
  if (!awareness || !awareness.windows || !awareness.windows.length) return '';
  var history = awareness.history || [], usage = awareness.usage_7d || {};
  var h = '<div class="quota-aware"><div class="quota-history-row"><strong>' + (awareness.is_estimate ? 'Usage estimate' : 'Quota forecast') + '</strong>' + quotaHistoryChart(history, account.id || 'account') + '<span class="qnote">7 days · ' + tok(usage.tokens || 0) + ' tokens · ' + usd(usage.cost_usd || 0) + '</span></div>';
  awareness.windows.forEach(function (window) {
    var prediction = window.prediction || {}, rates = window.burn_rates || {};
    var left = window.left_pct == null ? 'unknown' : window.left_pct + '%';
    if (window.left_tokens != null) left += ' · ' + tok(window.left_tokens) + ' tokens';
    var used = window.used_pct == null ? tok(window.used_tokens || 0) + ' tokens' : window.used_pct + '% quota';
    if (window.used_tokens != null) used += ' · ' + tok(window.used_tokens) + ' tokens';
    var reset = window.reset_at ? when(window.reset_at) : 'unknown';
    h += '<div class="quota-forecast"><span><b>' + esc(window.label) + (window.source === 'estimate' ? ' · estimate' : '') + '</b> used ' + esc(used) + ' · left ' + esc(left) + ' · reset ' + esc(reset) + (window.reset_source && window.reset_source !== 'reported' ? ' (' + esc(window.reset_source) + ')' : '') + '</span>';
    h += '<span class="qnote">Tokens ' + tok(window.tokens || 0) + ' · Cost ' + usd(window.cost_usd || 0) + ' · Burn tokens/hour: 1h ' + tok((rates.last_hour || {}).tokens_per_hour || 0) + ', 24h ' + tok((rates.last_day || {}).tokens_per_hour || 0) + ', window ' + tok((rates.whole_window || {}).tokens_per_hour || 0) + '</span>';
    h += '<span class="qnote">Cost/hour: 1h ' + usd((rates.last_hour || {}).cost_usd_per_hour || 0) + ', 24h ' + usd((rates.last_day || {}).cost_usd_per_hour || 0) + ', window ' + usd((rates.whole_window || {}).cost_usd_per_hour || 0) + '</span>';
    h += '<span class="qnote">' + esc(prediction.status || 'unknown') + ' · ' + esc(prediction.confidence || awareness.confidence || 'low') + ' confidence · ' + awareness.runs_24h + ' runs / 24h' + (prediction.exhaustion_at ? ' · run out ' + esc(when(prediction.exhaustion_at)) : '') + '</span></div>';
  });
  if (awareness.largest_run) h += '<span class="qnote">Largest context run: ' + esc(awareness.largest_run.ticket || 'unknown ticket') + ' · ' + tok(awareness.largest_run.context_tokens == null ? awareness.largest_run.tokens : awareness.largest_run.context_tokens) + ' input tokens · ' + tok(awareness.largest_run.tokens) + ' total · ' + usd(awareness.largest_run.cost_usd) + '</span>';
  return h + '</div>';
}
function maxUtil(a) {
  var w = (a.quota && a.quota.windows) || {}, m = null;
  Object.keys(w).forEach(function (k) { if (w[k] && w[k].utilization != null) m = Math.max(m || 0, w[k].utilization > 1 ? w[k].utilization / 100 : w[k].utilization); });
  return m;
}

