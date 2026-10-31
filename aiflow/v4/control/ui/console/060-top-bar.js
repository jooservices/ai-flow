/* ---------- top bar ---------- */
function renderLive() {
  var el = $('#live'); if (!el) return;
  var age = S.lastOk ? (Date.now() - S.lastOk) / 1000 : null;
  var stale = age == null || age > STALE_S;
  el.innerHTML = (stale ? pill('crit', S.lastOk ? 'Control API unreachable' : 'Connecting', true) : pill('ok', 'Live', true)) +
    '<span class="ago">' + (S.lastOk ? 'updated ' + ago(age) : (S.error ? esc(S.error) : '')) + '</span>';
  var env = $('#envtag'); if (env && S.data) env.textContent = (S.data.env || 'lab') + ' · ' + (S.data.control_repo || '').replace('ref: refs/heads/', 'control ');
}

