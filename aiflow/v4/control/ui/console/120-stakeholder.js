/* ---------- Stakeholder review and Jira verdicts ---------- */
function stakeholderEnvLink(name, label, url) {
  return url ? '<a class="btn" href="' + esc(safeHref(url)) + '" target="_blank" rel="noopener">' + label + ' ' + ICON.ext + '</a>' :
    '<span class="btn" aria-label="' + label + ': chưa deploy">' + label + ': chưa deploy</span>';
}
function renderStakeholderHead() {
  var d = D(), h = '<div class="projtabs" role="group" aria-label="Project">';
  d.projects.forEach(function (p) { h += '<button class="projtab" data-act="project" data-p="' + esc(p.key) + '" aria-pressed="' + (S.project === p.key) + '">' + esc(p.key) + '<span class="sub">' + esc(p.repo) + '</span></button>'; });
  if (!d.projects.length) h += '<span class="muted">No project in aiflow-control/projects</span>';
  h += '</div><span class="spacer"></span><button class="btn sm" data-act="stakeholder-refresh">Refresh</button>';
  var urls = S.sh.data && S.sh.project === S.project ? S.sh.data.urls || {} : {};
  h += '<div class="stakeholder-links" aria-label="Deployment environments">' +
    stakeholderEnvLink('develop', 'Develop (UAT)', urls.develop) + stakeholderEnvLink('production', 'Production', urls.production) + '</div>';
  $('#shead').innerHTML = h;
}
function loadStakeholder(project, refresh) {
  if (!project || (S.sh.loading === project)) return;
  if (!refresh && S.sh.project === project && S.sh.data) return;
  S.sh.loading = project; S.sh.error = '';
  fetch('/v1/stakeholder/' + encodeURIComponent(project), { cache: 'no-store' }).then(function (r) {
    return r.json().then(function (d) { if (!r.ok) throw new Error(typeof d.detail === 'string' ? d.detail : 'HTTP ' + r.status); return d; });
  }).then(function (d) {
    S.sh.project = project; S.sh.data = d; S.sh.error = '';
    (d.changes || []).forEach(function (c) {
      if (!S.sh.forms[c.key]) S.sh.forms[c.key] = { environment: 'develop', target_key: c.key, sender: '',
        release_commit: c.release_commit || '', expected: '', actual: '' };
    });
  }).catch(function (e) { S.sh.project = project; S.sh.data = null; S.sh.error = String(e.message || e); })
    .then(function () { S.sh.loading = null; S.sh.renderKey = ''; if (S.view === 'stakeholder') renderStakeholder(); });
}
function renderStakeholderBody() {
  var host = $('#sbody'), d = S.sh.data && S.sh.project === S.project ? S.sh.data : null;
  if (S.sh.error) { host.innerHTML = '<section class="panel"><div class="empty">Không tải được danh sách thay đổi: ' + esc(S.sh.error) + '</div></section>'; return; }
  if (!d) { host.innerHTML = '<section class="panel"><div class="empty">Đang tải thay đổi mới nhất…</div></section>'; return; }
  if (!d.changes.length) { host.innerHTML = '<section class="panel"><div class="panel-h"><h2>Thay đổi mới nhất</h2></div><div class="empty">Chưa có thay đổi đã hoàn tất để nghiệm thu.</div></section>'; return; }
  var h = '<section class="panel"><div class="panel-h"><h2>Thay đổi mới nhất</h2><span class="hint">Chọn môi trường, xem ticket và release hoặc commit, rồi ghi nhận kết quả.</span></div><div class="view">';
  d.changes.forEach(function (c) {
    var f = S.sh.forms[c.key] || { environment: 'develop', target_key: c.key, sender: '', release_commit: c.release_commit || '', expected: '', actual: '' };
    S.sh.forms[c.key] = f;
    var result = S.sh.results[c.key], jira = jiraUrl(c.key), busy = Boolean(S.sh.sending[c.key]);
    h += '<article class="stakeholder-change"><div><h3><a class="mono" href="' + esc(jira) + '" target="_blank" rel="noopener">' + esc(c.key) + '</a> · ' + esc(c.summary) + '</h3>';
    h += '<div class="small">' + esc(c.type) + ' · ' + esc(c.status) + (c.epic ? ' · Epic ' + esc(c.epic) : '') + ' · ' + esc(c.changed_at) + '</div>';
    if (c.change_summary) h += '<div style="margin-top:6px">' + esc(c.change_summary) + '</div>';
    h += '</div><form class="stakeholder-form" data-sh-form="' + esc(c.key) + '">';
    h += '<label>Môi trường<select data-sh-key="' + esc(c.key) + '" data-sh-field="environment"><option value="develop"' + (f.environment === 'develop' ? ' selected' : '') + '>develop (UAT)</option><option value="production"' + (f.environment === 'production' ? ' selected' : '') + '>production</option></select></label>';
    h += '<label>Ghi nhận nghiệm thu trên<select data-sh-key="' + esc(c.key) + '" data-sh-field="target_key"><option value="' + esc(c.key) + '"' + (f.target_key === c.key ? ' selected' : '') + '>' + esc(c.key) + ' (ticket)</option>';
    if (c.epic && c.epic !== c.key) h += '<option value="' + esc(c.epic) + '"' + (f.target_key === c.epic ? ' selected' : '') + '>' + esc(c.epic) + ' (epic)</option>';
    h += '</select></label>';
    h += '<label>Release hoặc commit<input type="text" data-sh-key="' + esc(c.key) + '" data-sh-field="release_commit" value="' + esc(f.release_commit || '') + '"></label>';
    h += '<label>Người gửi<input type="text" data-sh-key="' + esc(c.key) + '" data-sh-field="sender" value="' + esc(f.sender || '') + '" maxlength="200"></label>';
    h += '<label class="wide">Mong đợi<textarea rows="3" data-sh-key="' + esc(c.key) + '" data-sh-field="expected">' + esc(f.expected || '') + '</textarea></label>';
    h += '<label class="wide">Thực tế<textarea rows="3" data-sh-key="' + esc(c.key) + '" data-sh-field="actual">' + esc(f.actual || '') + '</textarea></label>';
    h += '<label class="wide">Ảnh chụp (tuỳ chọn)<input type="file" accept="image/png,image/jpeg,image/webp" data-sh-key="' + esc(c.key) + '" data-sh-field="screenshot">' +
      (S.sh.files[c.key] ? '<span class="small">Đã chọn: ' + esc(S.sh.files[c.key].name) + '</span>' : '') + '</label>';
    h += '<div class="stakeholder-actions wide"><button class="btn primary" type="button" data-act="stakeholder-submit" data-key="' + esc(c.key) + '" data-verdict="expected"' + (busy ? ' disabled' : '') + '>Đúng mong đợi</button>';
    h += '<button class="btn" type="button" data-act="stakeholder-submit" data-key="' + esc(c.key) + '" data-verdict="not_expected"' + (busy ? ' disabled' : '') + '>Chưa đúng mong đợi</button>';
    h += busy ? '<span class="muted">Đang ghi nhận…</span>' : '';
    if (result && result.ok) h += '<span class="stakeholder-result" role="status">' + (result.key ? 'Đã tạo ticket <a class="mono" href="' + esc(jiraUrl(result.key)) + '" target="_blank" rel="noopener">' + esc(result.key) + '</a> ở Intake.' : 'Đã ghi nhận comment nghiệm thu trên ' + esc(result.ticket) + '.') + '</span>';
    if (result && result.error) h += '<span class="stakeholder-error" role="alert">' + esc(result.error) + '</span>';
    h += '</div></form></article>';
  });
  host.innerHTML = h + '</div></section>';
}
function renderStakeholder() {
  renderStakeholderHead();
  if (!S.project) { $('#sbody').innerHTML = '<section class="panel"><div class="empty">Chưa có project được cấu hình.</div></section>'; return; }
  if (S.sh.project !== S.project && S.sh.loading !== S.project) loadStakeholder(S.project);
  if (S.sh.renderKey !== S.project || (!S.sh.data && S.sh.loading !== S.project)) {
    renderStakeholderBody(); S.sh.renderKey = S.project;
  }
}
function readStakeholderFile(file) {
  return new Promise(function (resolve, reject) {
    var reader = new FileReader(); reader.onload = function () { resolve(String(reader.result || '')); };
    reader.onerror = function () { reject(new Error('Could not read screenshot')); }; reader.readAsDataURL(file);
  });
}
function submitStakeholder(changeKey, verdict) {
  var f = S.sh.forms[changeKey] || {}, errors = [];
  if (!String(f.sender || '').trim()) errors.push('Nhập người gửi.');
  if (verdict === 'not_expected' && !String(f.expected || '').trim()) errors.push('Nhập mong đợi.');
  if (verdict === 'not_expected' && !String(f.actual || '').trim()) errors.push('Nhập điều thực tế đã xảy ra.');
  if (errors.length) { S.sh.results[changeKey] = { error: errors.join(' ') }; S.sh.renderKey = ''; renderStakeholder(); return; }
  if (S.sh.sending[changeKey]) return;
  S.sh.sending[changeKey] = true; delete S.sh.results[changeKey]; S.sh.renderKey = ''; renderStakeholder();
  var screenshot = S.sh.files[changeKey];
  var data = Promise.resolve('');
  if (verdict === 'not_expected' && screenshot) data = readStakeholderFile(screenshot);
  data.then(function (screenshotData) {
    var body = { change_key: changeKey, target_key: f.target_key || changeKey, environment: f.environment || 'develop',
      verdict: verdict, sender: f.sender, release_commit: f.release_commit || '', expected: f.expected || '', actual: f.actual || '',
      screenshot_data: screenshotData, screenshot_filename: screenshot ? screenshot.name : 'screenshot.png' };
    return fetch('/v1/stakeholder/' + encodeURIComponent(S.project) + '/verdict', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
    }).then(function (r) {
      return r.json().then(function (d) { if (!r.ok) throw new Error(typeof d.detail === 'string' ? d.detail : 'Jira could not record the verdict (' + r.status + ')'); return d; });
    });
  }).then(function (d) {
    S.sh.results[changeKey] = { ok: true, key: d.key, ticket: d.ticket };
    if (verdict === 'not_expected') { f.expected = ''; f.actual = ''; delete S.sh.files[changeKey]; }
  }).catch(function (e) { S.sh.results[changeKey] = { error: 'Không ghi nhận được: ' + String(e.message || e) }; })
    .then(function () { delete S.sh.sending[changeKey]; S.sh.renderKey = ''; renderStakeholder(); });
}

