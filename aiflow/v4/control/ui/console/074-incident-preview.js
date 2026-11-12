/* ---------- Command Center ---------- */
function incidentHandoff(i) {
  var prefix = 'handoff-v1:';
  if (!i || typeof i.detail !== 'string' || i.detail.indexOf(prefix) !== 0) return null;
  try {
    var payload = JSON.parse(i.detail.slice(prefix.length));
    return payload && payload.schema === 'aiflow.ticket-handoff/v1' ? payload : null;
  } catch (_) { return null; }
}
function incidentPreview(i) {
  var payload = incidentHandoff(i);
  if (!payload) return i.detail || '';
  var run = (payload.runs || []).slice(-1)[0] || {}, usage = run.tokens == null ? 'unavailable' : tok(run.tokens);
  var cost = typeof run.cost_usd === 'number' ? '$' + run.cost_usd.toFixed(4) : 'unavailable';
  var note = 'Blocked · ' + payload.streak_count + ' consecutive failures · ' + mmss(payload.elapsed_ms / 1000) +
    ' · ' + (run.node || 'unavailable') + ' · ' + (run.provider || 'unavailable') + '/' + (run.model || 'unavailable') +
    ' · ' + usage + ' tokens · ' + cost;
  var notification = (payload.notification || {}).state || 'unavailable';
  note += ' · notification ' + notification;
  if ((payload.notification || {}).configuration_gap) note += ' · ' + payload.notification.configuration_gap;
  return note;
}
