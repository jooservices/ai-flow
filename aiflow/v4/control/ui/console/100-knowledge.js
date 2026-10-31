/* ---------- Knowledge: versioned resources in aiflow-control ---------- */
var KNOWLEDGE_TYPES = { study_case: 'Study case', knowledge: 'Knowledge', skill: 'Skill', agent: 'Agent', guide: 'Guide', policy: 'Policy' };
function loadKnowledge(force) {
  var k = S.knowledge;
  if (!consoleCan('knowledge.read')) return;
  var project = S.project || '';
  if (k.loading || (!force && k.data && k.learning && k.learningProject === project && Date.now() - k.loadedAt < 30000)) return;
  k.loading = true; k.error = '';
  var resources = fetch('/v1/knowledge/resources?include_archived=true', { cache: 'no-store' }).then(function (r) {
    return r.json().then(function (d) { if (!r.ok) throw new Error(d.detail || ('HTTP ' + r.status)); return d; });
  });
  var learning = fetch('/v1/knowledge/items?project=' + encodeURIComponent(project) + '&q=' + encodeURIComponent(k.search || '') + '&include_retired=true', { cache: 'no-store' }).then(function (r) {
    return r.json().then(function (d) { if (!r.ok) throw new Error(d.detail || ('HTTP ' + r.status)); return d; });
  });
  Promise.all([resources, learning]).then(function (data) {
    k.data = data[0]; k.learning = data[1]; k.learningProject = project; k.loadedAt = Date.now(); k.loading = false;
    if (S.view === 'knowledge') renderKnowledge();
  }).catch(function (e) {
    k.error = String(e.message || e); k.loading = false;
    if (S.view === 'knowledge') renderKnowledge();
  });
}
function knowledgeNew() {
  S.knowledge.editor = { type: 'knowledge', id: '', title: '', content: '', archived: false, version: null };
  S.knowledge.history = null; renderKnowledge();
}
function knowledgeEdit(type, id) {
  var rows = (S.knowledge.data && S.knowledge.data.resources) || [];
  var row = rows.filter(function (r) { return r.type === type && r.id === id; })[0];
  if (!row) return;
  S.knowledge.editor = { type: row.type, id: row.id, title: row.title, content: row.content,
    archived: row.archived, version: row.version };
  S.knowledge.history = null; renderKnowledge();
}
function knowledgeSlug(value) {
  return String(value || '').trim().toLowerCase().replace(/[^a-z0-9._-]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 80);
}
function knowledgeDate(value) { return value ? when(value) : '—'; }
function renderLearningKnowledge(k, canManage) {
  var d = k.learning || { counts: [], newest: [], most_used: [], items: [], effectiveness: {} };
  var stats = d.effectiveness || {}, items = d.items || [];
  var h = '<section class="panel" data-block="learning"><div class="panel-h"><h2 style="margin:0">Learning and study cases</h2><span class="hint">Automatic run, ticket, rework and incident records for ' + esc(S.project || 'general knowledge') + '.</span></div>';
  h += '<div class="lead-controls"><label>Search this project and general knowledge<input data-knowledge-search value="' + esc(k.search || '') + '" placeholder="Search by meaning or words"></label><button class="btn" data-act="knowledge-search">Search</button>';
  if (canManage) h += '<button class="btn primary" data-act="knowledge-item-new">Add knowledge item</button>';
  h += '</div><div class="knowledge-actions" style="margin:12px 0">' +
    '<span class="pill">Runs with context pack: ' + Number(stats.runs_with_context_pack || 0) + '</span>' +
    '<span class="pill">Known mistakes repeated: ' + Number(stats.known_mistakes_repeated || 0) + '</span>' +
    '</div><div class="knowledge-grid"><div><h3>Counts by project and kind</h3><div class="knowledge-list">';
  (d.counts || []).forEach(function (row) { h += '<div class="small"><span class="mono">' + esc(row.project) + '</span> · ' + esc(KNOWLEDGE_TYPES[row.kind] || row.kind) + ': ' + Number(row.count) + '</div>'; });
  if (!(d.counts || []).length) h += '<div class="empty">No captured knowledge yet.</div>';
  h += '</div><h3 style="margin-top:14px">Rework rounds per week</h3><div class="knowledge-list">';
  (stats.rework_by_week || []).forEach(function (row) { h += '<div class="small">' + esc(row.week) + ' · ' + Number(row.rounds) + ' rounds across ' + Number(row.tickets) + ' tickets · ' + Number(row.rounds_per_ticket || 0) + ' rounds per ticket</div>'; });
  if (!(stats.rework_by_week || []).length) h += '<div class="small">No verification rework recorded.</div>';
  h += '</div><h3 style="margin-top:14px">Newest</h3><div class="knowledge-list">';
  (d.newest || []).forEach(function (item) { h += '<div class="small">' + esc(item.title) + ' · ' + esc(item.kind) + '</div>'; });
  h += '</div><h3 style="margin-top:14px">Most used</h3><div class="knowledge-list">';
  (d.most_used || []).forEach(function (item) { h += '<div class="small">' + esc(item.title) + ' · handed to ' + Number(item.handed_count || 0) + ' runs</div>'; });
  h += '</div></div><div><h3>Knowledge items · ' + items.length + '</h3><div class="knowledge-list">';
  if (!items.length) h += '<div class="empty">No matching items in this project or general scope.</div>';
  items.forEach(function (item) {
    var scope = item.scope === 'general' ? 'General' : item.project + (item.epic ? ' · epic ' + item.epic : '');
    var source = esc(item.source_type || 'manual') + (item.source_ref ? ' · ' + esc(item.source_ref) : '');
    var statusTone = item.status === 'active' ? 'ok' : (item.status === 'stale' ? 'warn' : 'muted');
    h += '<article class="knowledge-item"><div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap"><h3>' + esc(item.title) + '</h3>' +
      '<span class="pill" data-tone="' + statusTone + '">' + esc(item.status) + '</span><span class="pill">' + esc(KNOWLEDGE_TYPES[item.kind] || item.kind) + ' · ' + esc(scope) + '</span></div>' +
      '<div class="small">' + esc(item.content) + '</div><div class="small">Source: ' + source + (item.source_url ? ' · <a href="' + esc(safeHref(item.source_url)) + '" target="_blank" rel="noopener">ticket</a>' : '') +
      (item.run_id ? ' · <a href="/v1/tickets/' + encodeURIComponent(item.ticket_key) + '/timeline" target="_blank" rel="noopener">run ' + esc(item.run_id) + '</a>' : '') +
      (item.pr_url ? ' · <a href="' + esc(safeHref(item.pr_url)) + '" target="_blank" rel="noopener">pull request</a>' : '') + '</div>' +
      '<div class="small">Created by ' + esc(item.created_by || 'unknown') + ' · approved by ' + esc(item.approved_by || 'pending') + ' · handed to ' + Number(item.handed_count || 0) + ' runs</div>';
    if (canManage) h += '<div class="knowledge-actions">' +
      '<button class="btn sm" data-act="knowledge-item-edit" data-id="' + esc(item.id) + '">Correct</button>' +
      ((!item.approved || item.status === 'stale') ? '<button class="btn sm" data-act="knowledge-item-approve" data-id="' + esc(item.id) + '">Approve / review</button>' : '') +
      (item.status === 'active' ? '<button class="btn sm" data-act="knowledge-item-stale" data-id="' + esc(item.id) + '">Flag for review</button>' : '') +
      (item.status !== 'retired' ? '<button class="btn sm" data-act="knowledge-item-retire" data-id="' + esc(item.id) + '">Retire</button>' : '') +
      '<button class="btn sm" data-act="knowledge-item-merge" data-id="' + esc(item.id) + '">Merge duplicate…</button></div>';
    h += '</article>';
  });
  h += '</div>';
  var editor = k.itemEditor;
  if (canManage && editor) {
    h += '<div class="knowledge-form" style="margin-top:14px"><h3>' + (editor.id ? 'Correct item' : 'Add knowledge item') + '</h3>' +
      '<label>Kind<select data-learning-field="kind">' + ['study_case','decision','convention','skill','guide'].map(function (kind) { return '<option value="' + kind + '"' + (editor.kind === kind ? ' selected' : '') + '>' + esc(KNOWLEDGE_TYPES[kind]) + '</option>'; }).join('') + '</select></label>' +
      '<label>Scope<select data-learning-field="scope"><option value="project"' + (editor.scope !== 'general' ? ' selected' : '') + '>This project</option><option value="general"' + (editor.scope === 'general' ? ' selected' : '') + '>General, shared across projects</option></select></label>' +
      '<label>Epic key (optional)<input data-learning-field="epic" value="' + esc(editor.epic || '') + '"></label>' +
      '<label>Title<input data-learning-field="title" value="' + esc(editor.title || '') + '"></label>' +
      '<label>Content<textarea data-learning-field="content">' + esc(editor.content || '') + '</textarea></label>' +
      '<div class="knowledge-actions"><button class="btn primary" data-act="knowledge-item-save">Save item</button><button class="btn" data-act="knowledge-item-cancel">Cancel</button></div></div>';
  }
  return h + '</div></section>';
}
function renderKnowledge() {
  var root = $('#view-knowledge'); if (!root) return;
  if (!consoleCan('knowledge.read')) {
    root.innerHTML = '<section class="panel"><h1>Knowledge and Skills</h1><div class="muted">Sign in with a role that can view knowledge resources.</div></section>';
    applyLayout('knowledge', root);
    return;
  }
  var canManage = consoleCan('knowledge.manage'), canApprove = consoleCan('knowledge.approve');
  var k = S.knowledge, d = k.data || { resources: [], proposals: [] }, rows = d.resources || [];
  var h = '<section class="panel" data-block="header"><div class="panel-h"><h1 style="margin:0;font:600 20px var(--f-cond)">Knowledge and Skills</h1>' +
    '<span class="hint">Study cases, knowledge, skills, agents, guides and policies are versioned in aiflow-control.</span><span class="spacer"></span>' +
    (canManage ? '<button class="btn primary" data-act="knowledge-new">New resource</button>' : '') +
    '<button class="btn" data-act="knowledge-refresh">Refresh</button></div>';
  h += '<div class="lead-controls"><label>Editor name<input data-knowledge-actor value="' + esc(k.actor || '') + '" placeholder="Your name" autocomplete="name"></label>' +
    '<label>Type<select id="knowledge-kind"><option value="all">All types</option>' + Object.keys(KNOWLEDGE_TYPES).map(function (type) {
      return '<option value="' + type + '"' + (k.type === type ? ' selected' : '') + '>' + KNOWLEDGE_TYPES[type] + '</option>';
    }).join('') + '</select></label><div class="seg">' + [['active','Active'],['archived','Archived'],['all','All']].map(function (f) {
      return '<button data-act="knowledge-filter" data-filter="' + f[0] + '" aria-pressed="' + (k.filter === f[0]) + '">' + f[1] + '</button>';
    }).join('') + '</div></div>';
  if (k.error) h += '<div class="knowledge-error" style="color:var(--crit)">' + esc(k.error) + '</div>';
  if (k.loading && !k.data) h += '<div class="empty">Loading resources…</div>';
  h += '</section>';
  var visible = rows.filter(function (r) {
    return (k.type === 'all' || r.type === k.type) && (k.filter === 'all' || (k.filter === 'archived') === Boolean(r.archived));
  });
  h += '<section class="panel" data-block="resources"><div class="panel-h"><h2 style="margin:0">Resources</h2><span class="pill">' + visible.length + '</span></div><div class="knowledge-list">';
  if (!visible.length) h += '<div class="empty">No resources in this view yet.</div>';
  visible.forEach(function (r) {
    h += '<article class="knowledge-item"><div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap"><h3>' + esc(r.title) + '</h3>' +
      '<span class="pill" data-tone="' + (r.archived ? 'muted' : 'ok') + '">' + (r.archived ? 'Archived' : 'Active') + '</span></div>' +
      '<div class="small"><span class="mono">' + esc(r.id) + '</span> · ' + esc(KNOWLEDGE_TYPES[r.type] || r.type) + ' · v' + r.version + '</div>' +
      '<div class="small">Edited by ' + esc(r.updated_by) + ' · ' + esc(knowledgeDate(r.updated_at)) + '</div><div class="knowledge-actions">' +
      (canManage ? '<button class="btn sm" data-act="knowledge-edit" data-kind="' + esc(r.type) + '" data-id="' + esc(r.id) + '">Edit</button>' : '') +
      '<button class="btn sm" data-act="knowledge-history" data-kind="' + esc(r.type) + '" data-id="' + esc(r.id) + '">History</button>' +
      (canManage ? '<button class="btn sm" data-act="knowledge-archive" data-kind="' + esc(r.type) + '" data-id="' + esc(r.id) + '" data-archived="' + (!r.archived) + '">' + (r.archived ? 'Restore active' : 'Archive') + '</button>' : '') + '</div></article>';
  });
  h += '</div></section>';
  var e = k.editor;
  if (e && canManage) {
    h += '<section class="panel" data-block="editor"><div class="panel-h"><h2 style="margin:0">' + (e.version == null ? 'Create resource' : 'Edit resource') + '</h2><span class="spacer"></span><button class="btn sm" data-act="knowledge-cancel">Cancel</button></div>' +
      '<div class="knowledge-form"><label>Type<select data-knowledge-field="type">' + Object.keys(KNOWLEDGE_TYPES).map(function (type) {
        return '<option value="' + type + '"' + (e.type === type ? ' selected' : '') + '>' + KNOWLEDGE_TYPES[type] + '</option>';
      }).join('') + '</select></label><label>Id<input data-knowledge-field="id" value="' + esc(e.id) + '" placeholder="lowercase-slug or project key"></label>' +
      '<label>Title<input data-knowledge-field="title" value="' + esc(e.title) + '" maxlength="200"></label>' +
      '<label>Content<textarea data-knowledge-field="content" spellcheck="false">' + esc(e.content) + '</textarea></label>' +
      (e.type === 'policy' ? '<div class="small">Policy YAML is proposed as a pull request. It becomes active only after owner approval and merge.</div>' : '') +
      '<div class="knowledge-actions"><button class="btn primary" data-act="knowledge-save">Save</button></div></div></section>';
  }
  if (k.history) {
    var versions = k.history.history || [], pendingHistory = k.history.proposals || [];
    h += '<section class="panel" data-block="history"><div class="panel-h"><h2 style="margin:0">History · ' + esc(k.history.id) + '</h2><span class="spacer"></span><button class="btn sm" data-act="knowledge-history-close">Close</button></div><div class="knowledge-history">';
    if (!versions.length && !pendingHistory.length) h += '<div class="empty">No history is available.</div>';
    versions.forEach(function (v) {
      var older = versions.filter(function (x) { return x.version === v.version - 1; })[0];
      h += '<article class="knowledge-version"><div><strong>Version ' + v.version + '</strong> · ' + esc(v.action) + ' · edited by <strong>' + esc(v.actor) + '</strong> · ' + esc(knowledgeDate(v.created_at)) +
        (v.approved_by ? ' · approved by ' + esc(v.approved_by) : '') + '</div><div class="knowledge-grid" style="margin-top:8px"><div><div class="small">Before' + (older ? ' · v' + older.version : '') + '</div><pre>' + esc(older ? older.content : '(no earlier version)') + '</pre></div>' +
        '<div><div class="small">After · ' + esc(v.title) + (v.archived ? ' · archived' : '') + '</div><pre>' + esc(v.content) + '</pre>' + (canManage ? '<button class="btn sm" data-act="knowledge-restore" data-kind="' + esc(k.history.type) + '" data-id="' + esc(k.history.id) + '" data-version="' + esc(v.version) + '">Restore this version</button>' : '') + '</div></div></article>';
    });
    pendingHistory.forEach(function (p) {
      h += '<article class="knowledge-version"><div><strong>Pending PR #' + p.pr_number + '</strong> · proposed by <strong>' + esc(p.actor) + '</strong> · ' + esc(knowledgeDate(p.created_at)) +
        ' · before v' + p.base_version + ' · not active</div><div class="knowledge-grid" style="margin-top:8px"><div><div class="small">Before</div><pre>' + esc(p.base_content || '(new policy)') + '</pre></div>' +
        '<div><div class="small">Proposed after · ' + esc(p.title) + (p.archived ? ' · archived after merge' : '') + '</div><pre>' + esc(p.content) + '</pre><a href="' + esc(safeHref(p.pr_url)) + '" target="_blank" rel="noopener">Review pull request ' + ICON.ext + '</a></div></div></article>';
    });
    h += '</div></section>';
  }
  var pending = d.proposals || [];
  h += '<section class="panel" data-block="approvals"><div class="panel-h"><h2 style="margin:0">Policy approvals</h2><span class="pill" data-tone="' + (pending.length ? 'warn' : 'ok') + '">' + pending.length + ' pending</span></div>';
  if (canApprove) h += '<div class="knowledge-form"><label>Approver name<input data-knowledge-owner value="' + esc(k.owner || (S.consoleUser && S.consoleUser.user) || '') + '" placeholder="Approver name" autocomplete="name"></label>' +
    '<label class="review-auth">Owner access token (not needed with an authorized console session)<input type="password" autocomplete="new-password" data-owner-token value="' + esc(k.ownerToken) + '" aria-label="Owner access token"></label></div>';
  h += '<div class="small">Policy proposals do not affect running decisions until an authorized administrator merges the pull request.</div>';
  if (!pending.length) h += '<div class="empty">No policy pull requests are waiting for approval.</div>';
  pending.forEach(function (p) {
    h += '<article class="knowledge-item"><h3>' + esc(p.title) + '</h3><div class="small">' + esc(p.id) + ' · proposed by ' + esc(p.actor) + ' · ' + esc(knowledgeDate(p.created_at)) + '</div>' +
      '<details><summary>View proposed change</summary><div class="knowledge-grid" style="margin-top:8px"><div><div class="small">Before · v' + p.base_version + '</div><pre>' + esc(p.base_content || '(new policy)') + '</pre></div><div><div class="small">After' + (p.archived ? ' · archived after merge' : '') + '</div><pre>' + esc(p.content) + '</pre></div></div></details>' +
      '<a href="' + esc(safeHref(p.pr_url)) + '" target="_blank" rel="noopener">Review PR #' + esc(p.pr_number) + ' ' + ICON.ext + '</a>' + (canApprove ? '<div class="knowledge-actions"><button class="btn primary" data-act="knowledge-approve" data-proposal="' + esc(p.proposal_id) + '">Approve and merge policy</button></div>' : '') + '</article>';
  });
  h += '</section>';
  h += renderLearningKnowledge(k, canManage);
  root.innerHTML = h;
  applyLayout('knowledge', root);
}
function loadBackups(force) {
  if (!consoleCan('backup.read')) return;
  var b = S.backup;
  if (b.loading || (!force && b.data)) return;
  b.loading = true; b.error = '';
  fetch('/v1/backups', { cache: 'no-store' }).then(function (r) {
    return r.json().then(function (d) { if (!r.ok) throw new Error(d.detail || ('HTTP ' + r.status)); return d; });
  }).then(function (d) {
    b.data = d; b.form = { schedule: d.schedule, destination_kind: d.destination_kind,
      destination_path: d.destination_path, s3_endpoint: d.s3_endpoint, s3_bucket: d.s3_bucket,
      s3_prefix: d.s3_prefix, s3_region: d.s3_region, retention_days: d.retention_days };
    var list = d.backups || [];
    if (!list.some(function (x) { return x.source === b.selected; })) b.selected = list.length ? list[0].source : '';
    b.loading = false; b.loadedAt = Date.now(); if (S.view === 'backup') renderBackup();
  }).catch(function (e) {
    b.error = String(e.message || e); b.loading = false; b.loadedAt = Date.now(); if (S.view === 'backup') renderBackup();
  });
}
function backupResultTone(status) { return status === 'success' ? 'ok' : status === 'failed' ? 'crit' : status === 'running' ? 'info' : 'muted'; }
function backupSourceCommand(source, config) {
  if (!source) return '';
  var endpoint = config.destination_kind === 's3' ? config.s3_endpoint || '' : '';
  var region = config.s3_region || 'us-east-1';
  function arg(v) { return "'" + String(v || '').replace(/'/g, "'\\''") + "'"; }
  var script = [
    'set +e',
    'IMAGE=$(docker inspect -f "{{.Config.Image}}" aiflow4-control-api-a)',
    'VERSION=$(docker inspect -f "{{ index .Config.Labels \\"org.opencontainers.image.version\\" }}" aiflow4-control-api-a)',
    'AIFLOW4_IMAGE_REPOSITORY="${IMAGE%:*}" AIFLOW4_IMAGE_TAG="${IMAGE##*:}" AIFLOW4_VERSION="$VERSION"',
    'COMPOSE="/srv/aiflow/build/$VERSION/deploy/v4/control/compose.yaml"',
    'docker compose -f "$COMPOSE" stop control-api-a control-api-b control-loops',
    'stop_status=$?',
    'if [ "$stop_status" -ne 0 ]; then AIFLOW4_IMAGE_REPOSITORY="${IMAGE%:*}" AIFLOW4_IMAGE_TAG="${IMAGE##*:}" AIFLOW4_VERSION="$VERSION" docker compose -f "$COMPOSE" up -d --no-deps --force-recreate control-api-a control-api-b control-loops; exit "$stop_status"; fi',
    'AIFLOW4_IMAGE_REPOSITORY="${IMAGE%:*}" AIFLOW4_IMAGE_TAG="${IMAGE##*:}" AIFLOW4_VERSION="$VERSION" docker compose -f "$COMPOSE" run --rm --no-deps -v "$1:/run/aiflow4-backup-key:ro" -v /srv/aiflow/control/repos/aiflow-control:/srv/aiflow/control/repos/aiflow-control-restore -e AIFLOW4_CONTROL_REPO=/srv/aiflow/control/repos/aiflow-control-restore control-restore backup restore --from "$2" --key-file /run/aiflow4-backup-key --s3-endpoint "$3" --s3-region "$4"',
    'restore_status=$?',
    'AIFLOW4_IMAGE_REPOSITORY="${IMAGE%:*}" AIFLOW4_IMAGE_TAG="${IMAGE##*:}" AIFLOW4_VERSION="$VERSION" docker compose -f "$COMPOSE" up -d --no-deps --force-recreate control-api-a control-api-b control-loops',
    'start_status=$?',
    'if [ "$restore_status" -ne 0 ]; then exit "$restore_status"; fi',
    'exit "$start_status"'
  ].join('; ');
  return 'sudo bash -c ' + arg(script) + ' _ "$HOME/Downloads/aiflow4-backup-key" ' + arg(source) + ' ' + arg(endpoint) + ' ' + arg(region);
}
function renderBackup() {
  var root = $('#view-backup'); if (!root) return;
  if (!consoleCan('backup.read')) {
    root.innerHTML = '<section class="panel"><h1 style="margin:0">Backup &amp; Restore</h1><div class="muted">Sign in as an owner or super administrator to manage Command Center backups.</div></section>';
    return;
  }
  var b = S.backup, d = b.data, f = b.form || {}, canManage = consoleCan('backup.manage');
  if (!d) {
    root.innerHTML = '<section class="panel"><div class="panel-h"><h1 style="margin:0">Backup &amp; Restore</h1><span class="spacer"></span><button class="btn" data-act="backup-refresh">Refresh</button></div>' +
      (b.error ? '<div style="color:var(--crit)">' + esc(b.error) + '</div>' : '<div class="empty">' + (b.loading ? 'Loading backup status…' : 'Backup status is not available yet.') + '</div>') + '</section>';
    return;
  }
  var backups = d.backups || [], selected = backups.filter(function (x) { return x.source === b.selected; })[0];
  var backupStatus = d.last_backup_status || 'never', testStatus = d.last_test_status || 'never';
  var h = '<section class="panel"><div class="panel-h"><h1 style="margin:0;font:600 20px var(--f-cond)">Backup &amp; Restore</h1>' +
    '<span class="hint">Encrypted backups for the database, stored secrets, control repository settings and knowledge.</span><span class="spacer"></span>' +
    '<button class="btn" data-act="backup-refresh">Refresh</button></div>';
  if (b.error) h += '<div style="color:var(--crit)">' + esc(b.error) + '</div>';
  if (d.storage_error) h += '<div style="color:var(--warn)">' + esc(d.storage_error) + '</div>';
  if (b.notice) h += '<div class="notice">' + esc(b.notice) + '</div>';
  h += '<div class="grid-2"><div class="stack"><section class="panel"><div class="panel-h"><h2 style="margin:0">Backup schedule and destination</h2></div>' +
    '<div class="backup-form"><label>Schedule<select data-backup-field="schedule"' + (!canManage ? ' disabled' : '') + '>' +
    '<option value="daily"' + (f.schedule === 'daily' ? ' selected' : '') + '>Daily</option><option value="weekly"' + (f.schedule === 'weekly' ? ' selected' : '') + '>Weekly</option></select></label>' +
    '<label>Destination<select data-backup-field="destination_kind"' + (!canManage ? ' disabled' : '') + '>' +
    [['local','Folder on this machine'],['mounted','Mounted path'],['s3','S3-compatible storage']].map(function (x) { return '<option value="' + x[0] + '"' + (f.destination_kind === x[0] ? ' selected' : '') + '>' + x[1] + '</option>'; }).join('') + '</select></label>';
  if (f.destination_kind === 's3') {
    h += '<label>S3 endpoint<input data-backup-field="s3_endpoint" value="' + esc(f.s3_endpoint || '') + '" placeholder="https://s3.example.com"' + (!canManage ? ' disabled' : '') + '></label>' +
      '<label>Bucket<input data-backup-field="s3_bucket" value="' + esc(f.s3_bucket || '') + '" placeholder="aiflow-backups"' + (!canManage ? ' disabled' : '') + '></label>' +
      '<label>Folder prefix<input data-backup-field="s3_prefix" value="' + esc(f.s3_prefix || '') + '" placeholder="command-center"' + (!canManage ? ' disabled' : '') + '></label>' +
      '<label>Region<input data-backup-field="s3_region" value="' + esc(f.s3_region || 'us-east-1') + '"' + (!canManage ? ' disabled' : '') + '></label>' +
      '<div class="small backup-help">The control service reads AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY and optional AWS_SESSION_TOKEN from its environment. Credentials are not stored in backup settings.</div>';
  } else {
    h += '<label class="wide">Folder path<input data-backup-field="destination_path" value="' + esc(f.destination_path || '') + '" placeholder="/srv/aiflow/backups"' + (!canManage ? ' disabled' : '') + '></label>' +
      (f.destination_kind === 'mounted' ? '<div class="small backup-help">Mount this folder into both control-api containers and control-loops at the same path.</div>' : '');
  }
  h += '<label>Keep backups for days<input data-backup-field="retention_days" type="number" min="1" max="3650" value="' + esc(f.retention_days || 30) + '"' + (!canManage ? ' disabled' : '') + '></label></div>' +
    '<div class="project-actions">' + (canManage ? '<button class="btn primary" data-act="backup-save"' + (b.saving ? ' disabled' : '') + '>Save settings</button><button class="btn" data-act="backup-run">Run backup now</button>' : '') + '</div></section>' +
    '<section class="panel"><div class="panel-h"><h2 style="margin:0">Encryption key</h2><span class="pill" data-tone="' + (d.key_downloaded ? 'ok' : 'warn') + '">' + (d.key_downloaded ? 'Downloaded once' : 'Not downloaded') + '</span></div>' +
    '<div class="small">The key is stored privately on this control machine for scheduled encryption and is excluded from every backup. Keep the downloaded copy somewhere separate; it cannot be downloaded again.</div>' +
    (d.key_downloaded ? '<div class="small">Downloaded: ' + esc(d.key_downloaded_at || 'recorded') + '</div>' : canManage ? '<button class="btn primary" data-act="backup-key">Download encryption key once</button>' : '') + '</section></div>';
  h += '<div class="stack"><section class="panel"><div class="panel-h"><h2 style="margin:0">Latest backup</h2>' + pill(backupResultTone(backupStatus), backupStatus) + '</div>' +
    '<div class="small">' + esc(d.last_backup_name || 'No backup has completed') + '</div>' +
    '<div class="small">' + (d.last_backup_at ? esc(when(d.last_backup_at)) : 'Schedule: ' + (f.schedule === 'weekly' ? 'weekly' : 'daily')) + '</div>' +
    (d.last_backup_detail ? '<div class="small">' + esc(d.last_backup_detail) + '</div>' : '') + '</section>' +
    '<section class="panel"><div class="panel-h"><h2 style="margin:0">Weekly restore test</h2>' + pill(backupResultTone(testStatus), testStatus) + '</div>' +
    '<div class="small">Runs against a scratch database and temporary folder. It never replaces the live installation.</div>' +
    '<div class="small">' + (d.last_test_at ? 'Latest test: ' + esc(when(d.last_test_at)) : 'No restore test has completed yet') + '</div>' +
    (d.last_test_detail ? '<div class="small">' + esc(d.last_test_detail) + '</div>' : '') +
    (d.last_test_components && Object.keys(d.last_test_components).length ? '<div class="backup-outcomes">' + Object.keys(d.last_test_components).map(function (name) {
      var item = d.last_test_components[name] || {}; return '<div>' + esc({ database: 'Database', stored_secrets: 'Stored secrets', control_repository: 'Control repository settings', knowledge: 'Knowledge' }[name] || name) + ': ' + pill(backupResultTone(item.status), item.status) + (item.detail ? '<span class="small">' + esc(item.detail) + '</span>' : '') + '</div>';
    }).join('') + '</div>' : '') + '</section>' +
    '<section class="panel"><div class="panel-h"><h2 style="margin:0">Restore on a new control machine</h2></div>' +
    '<div class="small">Choose a backup, put the downloaded key at <span class="mono">~/Downloads/aiflow4-backup-key</span> on the new machine, then run this command there. It stops control services, restores the four components, and recreates the services to load restored settings.</div>' +
    '<label>Backup<select data-backup-select>' + (backups.length ? backups.map(function (x) { return '<option value="' + esc(x.source) + '"' + (x.source === b.selected ? ' selected' : '') + '>' + esc(x.name) + ' · ' + esc(x.created_at || '') + '</option>'; }).join('') : '<option value="">No backups found</option>') + '</select></label>' +
    (selected ? '<textarea class="backup-command" readonly rows="6" aria-label="Restore command">' + esc(backupSourceCommand(selected.source, d)) + '</textarea><button class="btn" data-act="backup-copy">Copy restore command</button>' : '<div class="empty">A backup will appear here after the first scheduled backup.</div>') + '</section></div></div></section>';
  root.innerHTML = h;
}
function submitKnowledge() {
  var e = S.knowledge.editor;
  if (!e) return;
  var actor = String(S.knowledge.actor || '').trim();
  if (!actor) { toast('Editor name required', 'Enter your name before saving or restoring a version.', 'warn'); return; }
  var body = { type: e.type, id: String(e.id || knowledgeSlug(e.title)), title: e.title, content: e.content,
    actor: actor, archived: Boolean(e.archived), version: e.version };
  fetch('/v1/knowledge/resources', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
    .then(function (r) { return r.json().then(function (d) { if (!r.ok) throw new Error(d.detail || ('HTTP ' + r.status)); return d; }); })
    .then(function (d) {
      toast(d.pending ? 'Policy pull request opened' : 'Resource saved', d.pending ? 'PR #' + d.pr_number + ' waits for owner approval.' : d.type + ' · ' + d.id + ' · v' + d.version, d.pending ? 'info' : 'ok');
      S.knowledge.editor = null; loadKnowledge(true);
    }).catch(function (err) { toast('Resource save failed', String(err.message || err), 'crit'); });
}
function archiveKnowledge(type, id, archived) {
  var row = (S.knowledge.data.resources || []).filter(function (r) { return r.type === type && r.id === id; })[0];
  if (!row) return;
  if (!S.knowledge.actor.trim()) { toast('Editor name required', 'Enter your name before archiving a resource.', 'warn'); return; }
  fetch('/v1/knowledge/resources', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({
    type: type, id: id, title: row.title, content: row.content, actor: S.knowledge.actor, archived: archived, version: row.version
  }) }).then(function (r) { return r.json().then(function (d) { if (!r.ok) throw new Error(d.detail || ('HTTP ' + r.status)); return d; }); })
    .then(function (d) { toast(d.pending ? 'Policy archive proposed' : (archived ? 'Resource archived' : 'Resource restored'), d.pending ? 'Waiting for owner approval.' : type + ' · ' + id, 'ok'); loadKnowledge(true); })
    .catch(function (err) { toast('Archive failed', String(err.message || err), 'crit'); });
}
function restoreKnowledge(type, id, version) {
  if (!S.knowledge.actor.trim()) { toast('Editor name required', 'Enter your name before restoring a version.', 'warn'); return; }
  fetch('/v1/knowledge/resources/' + encodeURIComponent(type) + '/' + encodeURIComponent(id) + '/restore', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ version: Number(version), actor: S.knowledge.actor })
  }).then(function (r) { return r.json().then(function (d) { if (!r.ok) throw new Error(d.detail || ('HTTP ' + r.status)); return d; }); })
    .then(function (d) { toast(d.pending ? 'Policy restore proposed' : 'Version restored', d.pending ? 'Waiting for owner approval.' : 'Saved as a new version.', 'ok'); loadKnowledge(true); })
    .catch(function (err) { toast('Restore failed', String(err.message || err), 'crit'); });
}
function approveKnowledge(proposalId) {
  var tokenField = $('[data-owner-token]'); S.knowledge.ownerToken = tokenField ? tokenField.value : S.knowledge.ownerToken;
  var approver = String(S.knowledge.owner || (S.consoleUser && S.consoleUser.user) || '').trim();
  if ((!consoleCan('knowledge.approve') && !S.knowledge.ownerToken) || !approver) {
    toast('Owner access required', 'Enter an approver name and, without an authorized console session, an access token.', 'warn'); return;
  }
  var headers = { 'Content-Type': 'application/json' };
  if (S.knowledge.ownerToken) headers.Authorization = 'Bearer ' + S.knowledge.ownerToken;
  fetch('/v1/knowledge/proposals/' + encodeURIComponent(proposalId) + '/approve', {
    method: 'POST', headers: headers,
    body: JSON.stringify({ approved_by: approver })
  }).then(function (r) { return r.json().then(function (d) { if (!r.ok) throw new Error(d.detail || ('HTTP ' + r.status)); return d; }); })
    .then(function (d) { if (!d.merged) throw new Error(d.message || 'GitHub refused the merge'); toast('Policy merged', 'The policy is now active.', 'ok'); loadKnowledge(true); })
    .catch(function (err) { toast('Policy approval failed', String(err.message || err), 'crit'); });
}
function knowledgeItemRequest(id, action, body) {
  fetch('/v1/knowledge/items/' + encodeURIComponent(id) + '/' + encodeURIComponent(action), {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {})
  }).then(function (r) { return r.json().then(function (d) { if (!r.ok) throw new Error(d.detail || ('HTTP ' + r.status)); return d; }); })
    .then(function () { toast('Knowledge updated', 'The item was ' + (action === 'retire' ? 'retired' : action === 'merge' ? 'merged' : action === 'correct' ? 'corrected' : action === 'stale' ? 'flagged for review' : 'approved') + '.', 'ok'); loadKnowledge(true); })
    .catch(function (err) { toast('Knowledge update failed', String(err.message || err), 'crit'); });
}
function submitLearningItem() {
  var k = S.knowledge, e = k.itemEditor;
  if (!e) return;
  var body = { project: e.scope === 'general' ? '' : (S.project || ''), scope: e.scope || 'project',
    epic: e.scope === 'general' ? '' : String(e.epic || '').trim().toUpperCase(), kind: e.kind,
    title: String(e.title || '').trim(), content: String(e.content || '').trim(),
    actor: String(k.actor || (S.consoleUser && S.consoleUser.user) || '').trim() };
  var url = e.id ? '/v1/knowledge/items/' + encodeURIComponent(e.id) + '/correct' : '/v1/knowledge/items';
  fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
    .then(function (r) { return r.json().then(function (d) { if (!r.ok) throw new Error(d.detail || ('HTTP ' + r.status)); return d; }); })
    .then(function () { k.itemEditor = null; toast('Knowledge saved', 'The item is in the Knowledge store.', 'ok'); loadKnowledge(true); })
    .catch(function (err) { toast('Knowledge save failed', String(err.message || err), 'crit'); });
}

