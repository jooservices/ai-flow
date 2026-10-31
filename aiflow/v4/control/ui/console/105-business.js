/* ---------- Business: progress by epic, tickets waiting on the owner, change requests ----------
   Read-only on tickets: the only write is the change request form (a new Story in Backlog). The owner moves an
   assessed change request to Intake in Jira; nothing here changes a ticket's status. */
function jiraUrl(key) { return String(safeHref(S.biz && S.biz.jira_site) || 'https://jooservices.atlassian.net').replace(/\/+$/, '') + '/browse/' + encodeURIComponent(key); }
function needsOwner(all) {
  var byKey = {}; all.forEach(function (t) { byKey[t.key] = t; });
  return all.filter(function (t) { return t.status === 'Needs owner'; }).map(function (t) {
    return { key: t.key, summary: t.summary, epic: t.type === 'Epic' ? t.key : epicOf(t, byKey), escalated: (t.labels || []).indexOf('owner-escalated') >= 0 };
  });
}
function projTabs() {
  var d = D(), h = '<div class="projtabs" role="group" aria-label="Project">';
  d.projects.forEach(function (p) { h += '<button class="projtab" data-act="project" data-p="' + esc(p.key) + '" aria-pressed="' + (S.project === p.key) + '">' + esc(p.key) + '<span class="sub">' + esc(p.repo) + '</span></button>'; });
  if (!d.projects.length) h += '<span class="muted">No project in aiflow-control/projects</span>';
  return h + '</div>';
}
var READY_TEXT = window.AIFlowReadyText;
/* Release progress per Jira version (/v1/state release_progress): x/y Done, open tickets, tickets added late with
   the date, why the version is not ready, and the release outcome. Command Center (detail) also lists the release
   steps with run ID and node; Business (compact) keeps x/y, late tickets, reasons, tag and GitHub Release link. */
var PROG_TEXT = {
  en: { title: 'Release progress', hint: 'Jira Fix versions: Done of total, open tickets, tickets added late, why not ready, release steps',
        none: 'No Jira version.', ready: 'Ready', notReady: 'Not ready', released: 'Released', running: 'Release running',
        failed: 'Release failed', blocked: 'Blocked', done: 'Done', open: 'Open', late: 'added late', on: 'on',
        tag: 'Tag', link: 'GitHub Release', run: 'run', node: 'node', noSteps: 'No release run yet',
        steps: { check: 'Check', changelog: 'Changelog', pr: 'PR', merge: 'Merge', tag: 'Tag', release: 'GitHub Release' } },
  vi: { title: 'Tiến độ release', hint: 'Jira Fix version: số ticket Done trên tổng, ticket thêm sau, lý do chưa sẵn sàng, tag và GitHub Release',
        none: 'Chưa có Jira version nào.', ready: 'Sẵn sàng', notReady: 'Chưa sẵn sàng', released: 'Đã release', running: 'Đang release',
        failed: 'Release lỗi', blocked: 'Bị chặn', done: 'Done', open: 'Còn mở', late: 'thêm sau', on: 'ngày',
        tag: 'Tag', link: 'GitHub Release', run: 'run', node: 'node', noSteps: 'Chưa có lần release nào',
        steps: { check: 'Kiểm tra', changelog: 'Changelog', pr: 'PR', merge: 'Merge', tag: 'Tag', release: 'GitHub Release' } }
};
function safeUrl(u) { return /^https?:\/\//.test(u || '') ? u : ''; }
function safeHref(u) { u = String(u == null ? '' : u); return /^https?:\/\//i.test(u) || /^\/(?![\/\\])/.test(u) ? u : ''; }
function releaseState(v) {
  var s = v.release && v.release.status;
  return v.released ? 'released' : s === 'checking' ? 'running' : s === 'failed' ? 'failed' : v.ready ? 'ready' : 'notReady';
}
function releaseLink(v, t) {
  var rel = v.release || {}, h = '';
  if (rel.tag) h += '<span class="mono">' + esc(t.tag) + ' ' + esc(rel.tag) + '</span>';
  if (safeUrl(rel.github_release_url)) h += ' <a href="' + esc(safeHref(rel.github_release_url)) + '" target="_blank" rel="noopener">' + esc(t.link) + '</a>';
  return h;
}
function releaseSteps(v, t) {
  var steps = (v.release && v.release.steps) || [];
  if (!steps.length) return '<div class="muted">' + esc(t.noSteps) + '</div>';
  return '<ol class="d release-steps">' + steps.map(function (s) {
    var who = (s.run_id ? ' · ' + t.run + ' <span class="mono">' + esc(String(s.run_id).slice(0, 8)) + '</span>' : '') + (s.node ? ' · ' + t.node + ' <span class="mono">' + esc(s.node) + '</span>' : '');
    var link = safeUrl(s.url) ? ' <a href="' + esc(safeHref(s.url)) + '" target="_blank" rel="noopener">' + esc(s.detail || '') + '</a>' : (s.detail ? ' ' + esc(s.detail) : '');
    return '<li data-step="' + esc(s.step) + '" data-state="' + esc(s.state) + '"><b>' + esc(t.steps[s.step] || s.step) + '</b> · ' + esc(s.state) + link + who + '</li>';
  }).join('') + '</ol>';
}
function progressPanel(versions, lang, id, detail) {
  var t = PROG_TEXT[lang] || PROG_TEXT.en, rt = READY_TEXT[lang] || READY_TEXT.en;
  var h = '<section class="panel" id="' + id + '" aria-labelledby="h-' + id + '"><div class="panel-h"><h2 id="h-' + id + '">' + esc(t.title) + '</h2><span class="hint">' + esc(t.hint) + '</span></div>';
  if (!versions.length) return h + '<div class="empty">' + esc(t.none) + '</div></section>';
  versions.forEach(function (v) {
    var st = releaseState(v), tone = st === 'released' || st === 'ready' ? 'ok' : st === 'failed' ? 'crit' : 'warn';
    h += '<div class="att-item" data-version="' + esc(v.version_id) + '" data-state="' + st + '"><span class="flag" data-tone="' + tone + '">' + esc(t[st]) + '</span>';
    h += '<span style="min-width:0"><div class="t"><span class="mono muted">' + esc(v.project) + '</span> ' + esc(v.version) + ' · ' + v.done_count + ' / ' + v.ticket_count + ' ' + esc(t.done) + '</div>';
    if (v.released) h += '<div class="d">' + releaseLink(v, t) + '</div>';
    if (v.late_tickets && v.late_tickets.length) h += '<ul class="d late">' + v.late_tickets.map(function (x) { return '<li data-late="' + esc(x.key) + '">' + esc(x.key) + ' · ' + esc(t.late) + (x.added_at ? ' ' + esc(t.on) + ' ' + esc(String(x.added_at).slice(0, 10)) : '') + '</li>'; }).join('') + '</ul>';
    if (v.open_tickets && v.open_tickets.length) h += '<div class="d open-tickets">' + esc(t.open) + ': ' + v.open_tickets.map(function (x) { return '<span class="mono" data-open="' + esc(x.key) + '">' + esc(x.key) + '</span>' + (detail ? ' ' + esc(x.status) : ''); }).join(', ') + '</div>';
    if (v.reasons && v.reasons.length) h += '<ul class="d reasons">' + v.reasons.map(function (r) { return '<li data-cause="' + esc(r.cause) + '">' + esc(readinessReason(r, lang)) + '</li>'; }).join('') + '</ul>';
    if (detail) h += releaseSteps(v, t);
    h += '</span></div>';
  });
  return h + '</section>';
}
function readinessReason(r, lang) { var t = READY_TEXT[lang] || READY_TEXT.en; return (t[r.cause] || function () { return (r.ticket || '') + ': ' + r.cause; })(r); }
function readinessPanel(versions, lang, id) {
  var t = READY_TEXT[lang] || READY_TEXT.en;
  var h = '<section class="panel" id="' + id + '" aria-labelledby="h-' + id + '"><div class="panel-h"><h2 id="h-' + id + '">' + esc(t.title) + '</h2><span class="hint">' + esc(t.hint) + '</span></div>';
  if (!versions.length) return h + '<div class="empty">' + esc(t.none) + '</div></section>';
  versions.forEach(function (v) {
    h += '<div class="att-item" data-version="' + esc(v.version_id) + '" data-ready="' + (v.ready ? 'yes' : 'no') + '"><span class="flag" data-tone="' + (v.ready ? 'ok' : 'warn') + '">' + esc(v.ready ? t.ready : t.notReady) + '</span>';
    h += '<span style="min-width:0"><div class="t"><span class="mono muted">' + esc(v.project) + '</span> ' + esc(v.version) + ' · ' + v.done_count + ' / ' + v.ticket_count + ' ' + esc(t.done) + '</div>';
    if (v.reasons.length) h += '<ul class="d">' + v.reasons.map(function (r) { return '<li data-cause="' + esc(r.cause) + '">' + esc(readinessReason(r, lang)) + '</li>'; }).join('') + '</ul>';
    h += '</span></div>';
  });
  return h + '</section>';
}
function renderBHead() { $('#bhead').innerHTML = projTabs(); }
function renderBBody() {
  var all = projTickets();
  var h = epicsPanel(all, 'open-ticket', 'b-epics') || '<section class="panel" id="b-epics"><div class="panel-h"><h2>Epics</h2></div><div class="empty">Project này chưa có epic.</div></section>';
  var wait = needsOwner(all);
  h += progressPanel((D().release_progress || []).filter(function (v) { return v.project === S.project; }), 'vi', 'b-ready', false);
  h += '<section class="panel" id="b-answer" aria-labelledby="h-b-answer"><div class="panel-h"><h2 id="h-b-answer">Cần bạn trả lời</h2><span class="hint">Ticket ở Needs owner: câu hỏi của intake và việc triage chuyển lên owner. Trả lời trong Jira.</span></div><div class="att">';
  if (!wait.length) h += '<div class="col-empty">Không có ticket nào chờ bạn trả lời</div>';
  wait.forEach(function (w) {
    h += '<div class="att-item" data-wait="' + esc(w.key) + '"><span class="flag" data-tone="crit">' + (w.escalated ? 'Triage escalate' : 'Câu hỏi') + '</span><span style="min-width:0"><div class="t"><span class="mono muted">' + esc(w.key) + '</span> ' + esc(w.summary) + '</div><div class="d">Epic: ' + esc(w.epic || '—') + '</div></span><a class="btn sm" href="' + esc(jiraUrl(w.key)) + '" target="_blank" rel="noopener">Jira ' + ICON.ext + '</a></div>';
  });
  $('#bbody').innerHTML = h + '</div></section>';
}
function crErrors(v) {
  var out = [];
  if (!String(v.title || '').trim()) out.push('Thiếu tiêu đề');
  if (!String(v.description || '').trim()) out.push('Thiếu mô tả');
  return out;
}
function renderBForm(force) {
  var epics = sortEpics(projTickets().filter(function (t) { return t.type === 'Epic'; }));
  if (S.cr.project !== S.project) { S.cr.project = S.project; S.cr.v = { title: '', description: '', epic: '' }; S.cr.errors = []; S.cr.created = null; }
  if (S.cr.v.epic && !epics.some(function (e) { return e.key === S.cr.v.epic; })) S.cr.v.epic = '';
  var sig = [S.project, epics.map(function (e) { return e.key; }).join(','), S.cr.sending, S.cr.errors.join(';'), S.cr.created ? S.cr.created.key : ''].join('|');
  if (!force && sig === S.cr.sig) return;  // keep what the user is typing: redraw only when something else changed
  S.cr.sig = sig;
  var v = S.cr.v;
  var h = '<section class="panel" id="b-cr-form" aria-labelledby="h-b-cr-form"><div class="panel-h"><h2 id="h-b-cr-form">Gửi change request</h2><span class="hint">Tạo một Story trong ' + esc(S.project || '') + ' (label change-request, trạng thái Backlog). AI đánh giá tác động; owner quyết định chuyển sang Intake.</span></div>';
  h += '<form class="crform" id="cr-form" novalidate><label for="cr-title">Tiêu đề<input id="cr-title" type="text" required value="' + esc(v.title) + '"></label>';
  h += '<label for="cr-desc">Mô tả<textarea id="cr-desc" rows="5" required>' + esc(v.description) + '</textarea></label>';
  h += '<label for="cr-epic">Epic<select id="cr-epic"><option value="">Không gắn epic</option>';
  epics.forEach(function (e) { h += '<option value="' + esc(e.key) + '"' + (v.epic === e.key ? ' selected' : '') + '>' + esc(e.key + ' ' + e.summary.slice(0, 60)) + '</option>'; });
  h += '</select></label>';
  if (S.cr.errors.length) h += '<div class="err" role="alert">' + S.cr.errors.map(esc).join('<br>') + '</div>';
  if (S.cr.created) h += '<div role="status">Đã tạo <a class="mono" href="' + esc(safeHref(S.cr.created.url)) + '" target="_blank" rel="noopener">' + esc(S.cr.created.key) + ' ' + ICON.ext + '</a> ở Backlog; AI đang đánh giá tác động.</div>';
  h += '<div><button class="btn primary" type="submit"' + (S.cr.sending ? ' disabled' : '') + '>' + (S.cr.sending ? 'Đang gửi…' : 'Gửi change request') + '</button></div></form></section>';
  $('#bform').innerHTML = h;
}
function submitCR() {
  var v = S.cr.v, project = S.project;
  S.cr.errors = crErrors(v); S.cr.created = null;
  if (S.cr.errors.length || S.cr.sending || !project) { renderBForm(); return false; }
  S.cr.sending = true; renderBForm();
  fetch('/v1/business/' + encodeURIComponent(project) + '/change-requests', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title: v.title, description: v.description, epic: v.epic })
  }).then(function (r) {
    return r.json().then(function (d) { if (!r.ok) throw new Error(typeof d.detail === 'string' ? d.detail : 'Gửi không được (' + r.status + ')'); return d; });
  }).then(function (d) {
    S.cr.created = d; S.cr.v = { title: '', description: '', epic: v.epic }; loadBiz();
  }).catch(function (e) { S.cr.errors = [String(e.message || e)]; })
    .then(function () { S.cr.sending = false; renderBForm(true); });
  return true;
}
function list(items) { return items && items.length ? '<ul>' + items.map(function (i) { return '<li>' + esc(i) + '</li>'; }).join('') + '</ul>' : '<span class="muted">không có</span>'; }
function crCard(cr) {
  var ticket = ticketBy(cr.key), repository = ticket && ticket.repository;
  var h = '<div class="cr-item" data-cr="' + esc(cr.key) + '"><div><a class="mono" href="' + esc(safeHref(cr.url) || jiraUrl(cr.key)) + '" target="_blank" rel="noopener">' + esc(cr.key) + '</a> ' + esc(cr.summary) + '</div>';
  h += '<div class="small">Epic: ' + esc(cr.epic || '—') + ' · Repository: ' + esc(repository || '—') + ' · Trạng thái: ' + esc(cr.status) + '</div>';
  var i = cr.impact;
  if (!i) return h + '<div><span class="flag" data-tone="info">Đang đánh giá</span></div></div>';
  h += '<div><span class="flag" data-tone="' + ({ S: 'ok', M: 'warn', L: 'crit' }[i.size] || 'muted') + '">Độ lớn ' + esc(i.size) + '</span></div>';
  h += '<div class="small">Epic bị ảnh hưởng: ' + esc((i.epics || []).join(', ') || 'không có') + ' · Ticket bị ảnh hưởng: ' + esc((i.tickets || []).join(', ') || 'không có') + '</div>';
  h += '<div><b>Rủi ro</b>' + list(i.risks) + '</div><div><b>Xung đột với việc đang làm</b>' + list(i.conflicts) + '</div>';
  return h + '</div>';
}
function renderBCR() {
  var b = S.biz && S.biz.project === S.project ? S.biz : null;
  var h = '<section class="panel" id="b-crs" aria-labelledby="h-b-crs"><div class="panel-h"><h2 id="h-b-crs">Change request</h2><span class="hint">Ticket có label change-request và kết quả đánh giá tác động; chờ owner duyệt ở Backlog</span></div>';
  if (S.bizError) h += '<div class="empty">Không tải được change request: ' + esc(S.bizError) + '</div>';
  if (!b) h += '<div class="empty">Đang tải…</div>';
  else if (!b.change_requests.length) h += '<div class="empty">Chưa có change request.</div>';
  else b.change_requests.forEach(function (cr) { h += crCard(cr); });
  $('#bcr').innerHTML = h + '</section>';
}
function loadBiz() {
  var project = S.project; if (!project) return;
  S.bizLoading = project;
  fetch('/v1/business/' + encodeURIComponent(project) + '/change-requests', { cache: 'no-store' }).then(function (r) {
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  }).then(function (d) { S.biz = d; S.bizError = ''; })
    .catch(function (e) { S.bizError = String(e.message || e); })
    .then(function () { S.bizLoading = null; if (S.view === 'business' && S.project === project) renderBCR(); });
}
function renderBusiness() {
  renderBHead(); renderBBody(); renderBForm(); renderBCR();
  if ((!S.biz || S.biz.project !== S.project) && S.bizLoading !== S.project) loadBiz();
}

