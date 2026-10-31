function renderSupport() {
  var root = $('#view-support'); if (!root) return;
  if (!consoleCan('console.manage')) {
    root.innerHTML = '<section class="panel"><div class="panel-h"><h2>Support</h2></div><p class="hint">Owner or Super Administrator access is required to build a support bundle.</p></section>';
    return;
  }
  var support = S.support, tickets = D().tickets || [];
  var h = '<div class="dhead"><div><div class="eyebrow">Owner tools</div><h1>Support</h1><p class="hint">Build a private diagnostic bundle, review its inventory, then download it to inspect before sharing.</p></div></div>';
  h += '<section class="panel"><div class="panel-h"><h2>Build a bundle</h2><span class="hint">Logs are capped at 5 MB per file.</span></div>';
  h += '<div class="project-form"><label>Ticket (optional)<select id="support-ticket"><option value="">All recent incidents and control logs</option>';
  h += tickets.filter(function (ticket) { return ticket.type !== 'Epic'; }).map(function (ticket) {
    return '<option value="' + esc(ticket.key) + '"' + (support.ticketKey === ticket.key ? ' selected' : '') + '>' + esc(ticket.key + ' · ' + ticket.summary) + '</option>';
  }).join('');
  h += '</select></label><div class="wide project-actions"><button class="btn primary" data-act="support-build"' + (support.busy ? ' disabled' : '') + '>' + (support.busy ? 'Scanning bundle…' : 'Build support bundle') + '</button>';
  h += '<span class="hint">Includes versions, safe settings, machine and account states, up to 100 incidents from 7 days, and control service logs from 24 hours.</span></div></div>';
  if (support.error) h += '<p class="error" role="alert">' + esc(support.error) + '</p>';
  h += '</section>';
  if (support.bundle) {
    var inv = support.bundle.inventory || {};
    h += '<section class="panel" aria-labelledby="support-inventory"><div class="panel-h"><h2 id="support-inventory">Review bundle inventory</h2><span class="badge ok">Sensitive data scan passed</span></div>';
    h += '<p class="hint">Created ' + esc(when(inv.created_at)) + '. Review these files before you share the downloaded bundle.</p><ul class="plain-list">';
    h += (inv.files || []).map(function (file) { return '<li><span class="mono">' + esc(file.name) + '</span><span class="hint">' + Number(file.bytes || 0).toLocaleString() + ' bytes</span></li>'; }).join('');
    h += '</ul><div class="project-actions"><a class="btn primary" href="' + esc(safeHref(support.bundle.download_url)) + '">Download reviewed bundle</a><button class="btn" data-act="support-clear">Build another</button><span class="hint">The download stays available for 10 minutes.</span></div></section>';
  }
  root.innerHTML = h;
}

function buildSupportBundle() {
  if (S.support.busy || !consoleCan('console.manage')) return;
  var select = $('#support-ticket');
  S.support.ticketKey = select ? select.value : S.support.ticketKey;
  S.support.busy = true; S.support.error = ''; S.support.bundle = null; renderSupport();
  fetch('/v1/support/bundles', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ticket_key: S.support.ticketKey }) })
    .then(function (r) { return r.json().then(function (d) { if (!r.ok) throw new Error(d.detail || ('HTTP ' + r.status)); return d; }); })
    .then(function (d) { S.support.bundle = d; S.support.error = ''; })
    .catch(function (e) { S.support.error = String(e.message || e); })
    .then(function () { S.support.busy = false; renderSupport(); });
}

