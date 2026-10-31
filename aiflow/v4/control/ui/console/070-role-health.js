/* ---------- role health: quality by AI account and role ---------- */
var RH_ROLE = { dev: 'Developer', verify: 'Verifier', master: 'Master', intake_refiner: 'Intake' };
function roleHealthPanel(rh) {
  var s = rh.settings || {}, rows = rh.roles || [];
  var h = '<section class="panel" id="p-role-health" aria-labelledby="h-rh"><div class="panel-h"><h2 id="h-rh">AI quality by role</h2><span class="hint">Success rate over the last ' + esc(s.window_runs || 10) + ' scored runs · cost and rework over ' + esc(rh.metrics_days || 30) + ' days · a role is switched off above ' + esc(s.failure_percent == null ? 60 : s.failure_percent) + '% failure score</span></div>';
  if (!rows.length) return h + '<div class="empty">No AI account has a role yet.</div><div id="rh-settings"></div></section>';
  h += '<div class="tablebox"><table style="min-width:900px"><thead><tr><th scope="col">Account</th><th scope="col">Role</th><th scope="col">State</th><th scope="col">Success rate</th><th scope="col">Cost per finished ticket</th><th scope="col">Rework rounds</th><th scope="col">Note</th></tr></thead><tbody>';
  rows.forEach(function (r) {
    var tone = r.mode === 'on' ? 'ok' : r.mode === 'probation' ? 'warn' : 'crit';
    var label = r.mode === 'on' ? 'On' : r.mode === 'probation' ? 'Off · probation ticket available' : 'Off · probation running';
    var note = r.note || '';
    if (r.probation) note += (note ? ' ' : '') + 'Probation: ' + r.probation.streak + ' of ' + r.probation.needed + ' in a row succeeded' + (r.probation.open_ticket ? ' · working on ' + r.probation.open_ticket : '') + '.';
    h += '<tr><td class="mono">' + esc(r.account) + '</td><td>' + esc(RH_ROLE[r.role] || r.role) + '</td><td>' + pill(tone, label) + '</td>';
    h += '<td class="mono" data-rh="rate">' + (r.success_rate == null ? '—' : esc(r.success_rate) + '% <span class="muted">(' + r.scored_runs + ' runs)</span>') + '</td>';
    h += '<td class="mono" data-rh="cost">' + (r.cost_per_finished_ticket == null ? '—' : esc(usd(r.cost_per_finished_ticket))) + '</td>';
    h += '<td class="mono" data-rh="rework">' + esc(r.rework_rounds) + '</td><td class="small">' + esc(note) + '</td></tr>';
  });
  return h + '</tbody></table></div><div id="rh-settings"></div></section>';
}
function renderRoleHealthSettings() {
  var host = $('#rh-settings'); if (!host) return;
  var s = (D().role_health || {}).settings || {};
  if (S.rh.window === null) { S.rh.window = String(s.window_runs == null ? 10 : s.window_runs); S.rh.percent = String(s.failure_percent == null ? 60 : s.failure_percent); }
  var canEdit = consoleCan('console.manage');
  var sig = [S.rh.window, S.rh.percent, S.rh.busy, S.rh.error, canEdit, s.window_runs, s.failure_percent].join('|');
  if (sig === S.rh.sig && host.firstChild) return;
  S.rh.sig = sig;
  var h = '<form class="crform" id="rh-form" novalidate style="padding:12px 0 0"><div class="small">Thresholds: when the latest runs of one account and role reach the failure score below, that role is switched off for that account. Defaults: 10 runs, above 60%.</div>';
  h += '<label for="rh-window">Runs in the window<input id="rh-window" type="number" min="1" max="100" step="1" value="' + esc(S.rh.window) + '"' + (canEdit ? '' : ' disabled') + '></label>';
  h += '<label for="rh-percent">Switch the role off above this failure score (%)<input id="rh-percent" type="number" min="0" max="99" step="1" value="' + esc(S.rh.percent) + '"' + (canEdit ? '' : ' disabled') + '></label>';
  if (canEdit) h += '<div><button class="btn primary" type="submit"' + (S.rh.busy ? ' disabled' : '') + '>' + (S.rh.busy ? 'Saving…' : 'Save thresholds') + '</button></div>';
  else h += '<div class="small">Only an owner can change these thresholds.</div>';
  if (s.updated_by) h += '<div class="small">Last changed by ' + esc(s.updated_by) + ' at ' + esc(when(s.updated_at)) + '.</div>';
  if (S.rh.error) h += '<div class="err" role="alert">' + esc(S.rh.error) + '</div>';
  host.innerHTML = h + '</form>';
}
function saveRoleHealth() {
  if (S.rh.busy) return;
  S.rh.busy = true; S.rh.error = ''; renderRoleHealthSettings();
  fetch('/v1/role-health/settings', { method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ window_runs: Number(S.rh.window), failure_percent: Number(S.rh.percent) }) })
    .then(function (r) { return r.json().then(function (d) { if (!r.ok) throw new Error(typeof d.detail === 'string' ? d.detail : 'HTTP ' + r.status); return d; }); })
    .then(function (d) {
      D().role_health = d; S.rh.window = null; toast('Thresholds saved', 'Window ' + d.settings.window_runs + ' runs, failure score above ' + d.settings.failure_percent + '%.', 'ok');
    }).catch(function (e) { S.rh.error = String(e.message || e); })
    .then(function () { S.rh.busy = false; if (S.view === 'cc') renderCC(); });
}

function renderNodeJoin(force) {
  var host = $('#node-onboarding'); if (!host) return;
  var projects = D().projects || [];
  if (!S.join.project && projects.length) S.join.project = projects[0].key;
  var sig = [projects.map(function (p) { return p.key; }).join(','), S.join.busy, S.join.error,
    S.join.command, S.join.expires_at, S.join.copied].join('|');
  if (!force && sig === S.join.sig) return;
  S.join.sig = sig;
  var h = '<section class="panel" id="p-node-join"><div class="panel-h"><h2 id="h-node-join">Join a node</h2><span class="hint">Create a one-use token for one node and project. It expires in five minutes.</span></div>';
  h += '<form class="crform" id="node-join-form" novalidate><label for="join-name">Node name<input id="join-name" required maxlength="63" autocomplete="off" value="' + esc(S.join.name) + '"></label>';
  h += '<label for="join-role">Role<select id="join-role"><option value="worker"' + (S.join.role === 'worker' ? ' selected' : '') + '>worker</option><option value="control"' + (S.join.role === 'control' ? ' selected' : '') + '>control</option></select></label>';
  h += '<label for="join-project">Project<select id="join-project">';
  projects.forEach(function (p) { h += '<option value="' + esc(p.key) + '"' + (S.join.project === p.key ? ' selected' : '') + '>' + esc(p.key) + '</option>'; });
  h += '</select></label>';
  h += '<div><button class="btn primary" type="submit"' + (S.join.busy || !projects.length ? ' disabled' : '') + '>' + (S.join.busy ? 'Creating…' : 'Show one-time command') + '</button></div>';
  if (S.join.error) h += '<div class="err" role="alert">' + esc(S.join.error) + '</div>';
  if (S.join.command) h += '<label class="wide" for="join-command">Run on the new Ubuntu node before ' + esc(when(S.join.expires_at)) + '<textarea id="join-command" rows="2" readonly onclick="this.select()">' + esc(S.join.command) + '</textarea></label><div><button class="btn" type="button" data-act="copy-join">' + (S.join.copied ? 'Copied' : 'Copy command') + '</button></div>';
  h += '<div class="small wide">This one-time command expires in five minutes. Run it on the new machine to install and join it; AI account credentials are added separately on this screen.</div></form></section>';
  host.innerHTML = h;
}

function submitNodeJoin() {
  if (S.join.busy) return;
  S.join.error = ''; S.join.command = ''; S.join.copied = false; S.join.busy = true; renderNodeJoin(true);
  fetch('/v1/nodes/' + encodeURIComponent(S.join.name) + '/join-command', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name: S.join.name, role: S.join.role, project: S.join.project })
  }).then(function (r) {
    return r.json().then(function (d) { if (!r.ok) throw new Error(typeof d.detail === 'string' ? d.detail : 'Join command request failed (' + r.status + ')'); return d; });
  }).then(function (d) {
    S.join.expires_at = d.expires_at;
    S.join.command = d.command;
  }).catch(function (e) { S.join.error = String(e.message || e); })
    .then(function () { S.join.busy = false; renderNodeJoin(true); });
}
function accMatch(a, f) {
  if (f === 'all') return true;
  if (f === 'working') return a.status === 'working' || a.status === 'probing';
  if (f === 'bad') return a.status === 'signed_out' || a.status === 'error' || a.status === 'unknown' || a.status === 'stopped' || a.status === 'node_offline';
  return a.status === f;
}
function incCard(i, closed) {
  var tn = closed ? 'ok' : i.severity === 'critical' ? 'crit' : i.severity === 'warning' ? 'warn' : 'info';
  return '<button class="inc" data-act="open-inc" data-id="' + esc(i.id) + '"' + (closed ? ' data-closed' : '') + '><span class="inc-top"><span class="mono muted" style="font-size:12px">#' + i.id + ' · ' + esc(when(i.opened_at)) + '</span><span style="font-weight:600">' + esc(i.title) + '</span><span class="spacer"></span>' + pill(tn, closed ? 'Resolved ' + when(i.closed_at) : (i.severity === 'critical' ? 'Critical' : 'Warning'), !closed && i.severity === 'critical') + '</span><span class="muted" style="font-size:12.5px">' + esc(i.detail) + '</span></button>';
}
function versionsBox() {
  var d = D(), pin = (d.versions && d.versions.tools) || {};
  var map = { claude_code: 'claude', codex: 'codex', opencode: 'opencode', grok: 'grok', gh: 'gh', git: 'git', uv: 'uv', node: 'node', docker: 'docker' };
  var h = '<div class="ver"><span class="vk">ai-flow</span><span class="vv">' + esc(((d.versions || {}).ai_flow || {}).ref || '—') + ' · nodes run ' + esc(d.nodes.map(function (n) { return n.name + ' ' + (n.release || '?'); }).join(', ')) + '</span>';
  Object.keys(pin).forEach(function (k) {
    var want = String(pin[k]);
    var parts = d.nodes.map(function (n) {
      var have = ((n.tools || {})[map[k] || k] || 'missing');
      var ok = have.indexOf(want) >= 0;
      return '<span class="' + (ok ? '' : 'bad') + '" title="' + esc(have) + '">' + esc(n.name) + (ok ? ' ✓' : ' ✗ ' + esc(have.slice(0, 30))) + '</span>';
    });
    h += '<span class="vk">' + esc(k) + '</span><span class="vv">' + esc(want) + ' · ' + parts.join(' · ') + '</span>';
  });
  return h + '</div>';
}
function runsTable(rows, limit) {
  if (!rows.length) return '<div class="empty">No runs yet. Workers start as soon as a ticket in To Do carries the aiflow-ready label.</div>';
  var h = '<div class="tablebox"><table class="runs" style="min-width:880px"><thead><tr><th>Started</th><th>Ticket</th><th>Step</th><th>Account</th><th>Model</th><th>Node</th><th>Outcome</th><th>Time</th><th>Tokens</th><th>Cost</th><th>PR</th></tr></thead><tbody>';
  rows.slice(0, limit || 30).forEach(function (r) {
    var started = r.live ? '<span class="mono" data-since="' + (r.elapsed_s || 0) + '" data-fmt="ago"></span>' : (r.ended_age_s != null ? ago(r.ended_age_s + (r.duration_s || 0) + since()) : '—');
    h += '<tr tabindex="0" data-act="open-ticket" data-key="' + esc(r.ticket) + '"><td class="mono small">' + started + '</td><td class="mono">' + esc(r.ticket) + '</td><td>' + esc(r.step) + '</td><td class="mono">' + esc(r.account) + '</td><td class="mono small">' + esc(r.model || '—') + '</td><td class="mono">' + esc(r.node) + '</td>';
    h += '<td>' + pill(runTone(r.outcome), r.outcome + (r.error_kind && r.error_kind !== r.outcome ? ' · ' + r.error_kind : ''), r.live) + '</td>';
    h += '<td class="mono">' + (r.live ? '<span data-since="' + (r.elapsed_s || 0) + '" data-fmt="clock"></span>' : mmss(r.duration_s)) + '</td><td class="mono">' + (r.live ? '—' : tok((r.tokens_in || 0) + (r.tokens_out || 0))) + '</td><td class="mono">' + (r.live ? '—' : usd(r.cost_usd)) + '</td>';
    h += '<td>' + (r.pr_url ? '<a href="' + esc(safeHref(r.pr_url)) + '" target="_blank" rel="noopener" data-stop>#' + esc(r.pr_url.split('/').pop()) + ' ' + ICON.ext + '</a>' : '—') + '</td></tr>';
  });
  return h + '</tbody></table></div>';
}

