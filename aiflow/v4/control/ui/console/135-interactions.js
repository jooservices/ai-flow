/* ---------- interactions ---------- */
document.addEventListener('click', function (e) {
  var target = e.target && e.target.nodeType === 1 ? e.target : e.target && e.target.parentElement;
  if (!target || !target.closest) return;
  if (target.closest('[data-stop]')) return;
  if (S.projectPickerOpen && !target.closest('#project-picker')) {
    S.projectPickerOpen = false;
    renderProjectPickerOptions();
  }
  var el = target.closest('[data-act]');
  if (!el) { if (target.id === 'overlay') closeAll(); return; }
  var act = el.getAttribute('data-act');
  if (act === 'view') setView(el.getAttribute('data-view'));
  else if (act === 'dashboard-tab') setDashboardTab(el.getAttribute('data-tab'));
  else if (act === 'capacity-refresh') { S.capacity.loadedAt = 0; loadCapacity(true); }
  else if (act === 'dashboard-card') openDashboardCard(el.getAttribute('data-card'));
  else if (act === 'flow-step') openDashboardWorkFilter(el.getAttribute('data-status'));
  else if (act.indexOf('layout-') === 0) layoutAction(act, el);
  else if (act === 'hint-dismiss') { try { localStorage.setItem('aiflow4-hint-' + S.view, '1'); } catch (x) { /* storage off */ } renderHelp(); }
  else if (act === 'support-build') buildSupportBundle();
  else if (act === 'support-clear') { S.support.bundle = null; S.support.error = ''; renderSupport(); }
  else if (act === 'license-refresh') { S.licensing.data = null; S.licensing.error = ''; loadLicensing(); }
  else if (act === 'license-activate') {
    var licenseKeyField = $('[data-license-key]'), licenseKey = licenseKeyField ? licenseKeyField.value.trim() : '';
    if (licenseKeyField) licenseKeyField.value = '';
    if (!licenseKey) { S.licensing.error = 'Enter a licence key.'; renderLicensing(); }
    else submitLicense('/v1/licensing/activate', { key: licenseKey }, 'Licence activated');
  }
  else if (act === 'license-offline') {
    var licenseFileField = $('[data-license-file]'), licenseFile = licenseFileField && licenseFileField.files && licenseFileField.files[0];
    if (!licenseFile) { S.licensing.error = 'Choose a signed licence file.'; renderLicensing(); }
    else {
      S.licensing.busy = true; S.licensing.error = ''; renderLicensing();
      licenseFile.text().then(function (contents) {
        return fetch('/v1/licensing/offline', { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ license_file: contents }) }).then(licensingResponse);
      }).then(function (data) { S.licensing.data = data; toast('Licence activated', 'Offline licence file accepted.', 'ok'); })
        .catch(function (error) { S.licensing.error = String(error.message || error); })
        .then(function () { S.licensing.busy = false; if (!S.licensing.error) loadLicensing(); else renderLicensing(); });
    }
  }
  else if (act === 'license-save-limits') {
    var limitPlanField = $('[data-license-plan]'), limitPlan = limitPlanField ? limitPlanField.value : 'free', limits = {};
    $$('[data-license-limit]').forEach(function (field) { limits[field.getAttribute('data-license-limit')] = Number(field.value); });
    S.licensing.limitsPlan = limitPlan;
    submitLicense('/v1/licensing/limits', { plan: limitPlan, limits: limits }, 'Plan limits saved');
  }
  else if (act === 'notifications-refresh') loadNotifications();
  else if (act === 'runtime-settings-refresh') loadRuntimeSettings();
  else if (act === 'runtime-setting-save') saveRuntimeSetting();
  else if (act === 'notification-test') notificationTest(el.getAttribute('data-id'));
  else if (act === 'notification-preferences-save') notificationPreferencesSubmit();
  else if (act === 'notification-edit') {
    var editChannel = (S.notifications.data && S.notifications.data.channels || []).filter(function (c) { return String(c.id) === el.getAttribute('data-id'); })[0];
    if (editChannel) { S.notifications.editId = editChannel.id; S.notifications.form = Object.assign({ enabled: editChannel.enabled }, editChannel.config, { name: editChannel.name, kind: editChannel.kind }); renderNotifications(); }
  }
  else if (act === 'notification-cancel') { S.notifications.editId = null; S.notifications.form = { kind: 'email', name: '', enabled: true }; S.notifications.error = ''; renderNotifications(); }
  else if (act === 'notification-delete') notificationDelete(el.getAttribute('data-id'));
  else if (act === 'notification-toggle') {
    var toggleChannel = (S.notifications.data && S.notifications.data.channels || []).filter(function (c) { return String(c.id) === el.getAttribute('data-id'); })[0];
    if (toggleChannel) fetch('/v1/notifications/channels', { method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: toggleChannel.id, name: toggleChannel.name, kind: toggleChannel.kind, enabled: !toggleChannel.enabled, config: {} }) })
      .then(function (r) { return projectResponseError(r); }).then(function () { loadNotifications(); })
      .catch(function (err) { S.notifications.error = String(err.message || err); renderNotifications(); });
  }
  else if (act === 'machine-add-toggle') openMachineForm();
  else if (act === 'machine-join-create') submitMachineJoin();
  else if (act === 'machine-copy-join') copyMachineCommand();
  else if (act === 'machine-add-account') { S.machines.addAccount = el.getAttribute('data-name'); S.machines.error = ''; renderMachines(); }
  else if (act === 'machine-account-save') addMachineAccount(el.getAttribute('data-name'));
  else if (act === 'machine-account-cancel') { S.machines.addAccount = ''; S.machines.error = ''; renderMachines(); }
  else if (act === 'machine-work') toggleMachineWork(el.getAttribute('data-name'), el.getAttribute('data-accept') === 'true');
  else if (act === 'machine-remove') removeMachine(el.getAttribute('data-name'));
  else if (act === 'account-pause') setAccountPaused(el.getAttribute('data-id'), el.getAttribute('data-paused') === 'true');
  else if (act === 'account-edit') { S.machines.accountEditor = el.getAttribute('data-id'); S.machines.error = ''; renderMachines(); }
  else if (act === 'account-edit-save') saveAccountSettings(el.getAttribute('data-id'));
  else if (act === 'account-edit-cancel') { S.machines.accountEditor = null; S.machines.error = ''; renderMachines(); }
  else if (act === 'account-signin') startAccountSignIn(el.getAttribute('data-id'));
  else if (act === 'account-signin-complete') completeAccountSignIn(el.getAttribute('data-id'));
  else if (act === 'account-remove') removeMachineAccount(el.getAttribute('data-id'));
  else if (act === 'search-open') openCommandSearch();
  else if (act === 'search-close') closeCommandSearch();
  else if (act === 'search-result') chooseSearchResult(el.getAttribute('data-kind'), el.getAttribute('data-key'), el);
  else if (act === 'dashboard-project') {
    S.project = el.getAttribute('data-p'); S.epic = 'all'; S.search = ''; savePrefs(); setView('work');
  }
  else if (act === 'dashboard-sort') {
    var sortKey = el.getAttribute('data-key');
    if (S.dashboardSort === sortKey) S.dashboardSortDir *= -1;
    else { S.dashboardSort = sortKey; S.dashboardSortDir = sortKey === 'key' ? 1 : -1; }
    renderDashboard();
    var sortFocus = $('[data-act="dashboard-sort"][data-key="' + sortKey + '"]'); if (sortFocus) sortFocus.focus();
  }
  else if (act === 'epic-dashboard') {
    openEpicOnWorkBoard(el.getAttribute('data-key'));
  }
  else if (act === 'select-work-project') {
    S.project = el.getAttribute('data-p'); S.epic = 'all'; S.search = ''; S.projectQuery = ''; S.projectPickerOpen = false;
    savePrefs(); render(); var selectedInput = $('#work-project'); if (selectedInput) selectedInput.focus();
  }
  else if (act === 'project-star') {
    var starIndex = S.starredProjects.indexOf(S.project);
    if (starIndex >= 0) S.starredProjects.splice(starIndex, 1); else if (S.project) S.starredProjects.push(S.project);
    savePrefs(); renderDHead(); var starButton = $('.star-button'); if (starButton) starButton.focus();
  }
  else if (act === 'master-budget-save') saveMasterBudget();
  else if (act === "esc-answer") answerDecision(el.getAttribute("data-id"), el.getAttribute("data-choice"));
  else if (act === "esc-retire") retireDecision(el.getAttribute("data-id"));
  else if (act === "esc-add") addDecision();
  else if (act === 'project-add-open') {
    S.projectAdmin.showForm = true; S.projectAdmin.error = ''; renderProjects(); loadProjectCatalog(true);
  }
  else if (act === 'project-catalog-refresh') loadProjectCatalog(true);
  else if (act === 'project-repositories') loadProjectRepositories(el.getAttribute('data-key'));
  else if (act === 'project-repositories-close') { S.projectAdmin.repositoryEditor = null; renderProjects(); }
  else if (act === 'project-repository-add') { var addSelect = $('[data-repository-add]'); addProjectRepository(addSelect ? addSelect.value : ''); }
  else if (act === 'project-repositories-save') saveProjectRepositories();
  else if (act === 'project-environment') loadProjectEnvironment(el.getAttribute('data-key'));
  else if (act === 'project-environment-close') { S.projectAdmin.envEditor = null; renderProjects(); }
  else if (act === 'project-environment-add') { var envEd = S.projectAdmin.envEditor; if (envEd && envEd.data) { envEd.data.items.push({ name: '', kind: 'base_url', value: '', runs: ['dev', 'verify'], description: '' }); renderProjects(); } }
  else if (act === 'project-environment-remove') { var envEd2 = S.projectAdmin.envEditor; if (envEd2 && envEd2.data) { envEd2.data.items.splice(Number(el.getAttribute('data-index')), 1); renderProjects(); } }
  else if (act === 'project-environment-save') saveProjectEnvironment();
  else if (act === 'environment-scan') runEnvironmentScan();
  else if (act === 'github-refresh') loadGitHubIntegrations();
  else if (act === 'github-manifest') githubManifestStart();
  else if (act === 'github-add-token') addGitHubToken();
  else if (act === 'github-test') githubConnectionAction(el.getAttribute('data-id'), 'POST', '/test', null, 'Connection tested.');
  else if (act === 'github-replace-token') replaceGitHubToken(el.getAttribute('data-id'));
  else if (act === 'github-replace-app') { S.integrations.target = el.getAttribute('data-id'); S.integrations.notice = 'Add the replacement GitHub App below; it takes over this connection.'; renderIntegrations(); }
  else if (act === 'github-remove') removeGitHubConnection(el.getAttribute('data-id'));
  else if (act === 'github-import') importGitHubApp();
  else if (act === 'github-import-refresh') loadImportedInstallations();
  else if (act === 'github-import-connect') connectImportedGitHubApp();
  else if (act === 'project-add-cancel') { S.projectAdmin.showForm = false; S.projectAdmin.preflight = null; S.projectAdmin.error = ''; renderProjects(); }
  else if (act === 'project-preflight') runProjectPreflight();
  else if (act === 'project-save') saveProject();
  else if (act === 'project-services-save') saveProjectServices(el.getAttribute('data-key'), el);
  else if (act === 'project-pause') setProjectPaused(el.getAttribute('data-key'), el.getAttribute('data-paused') === 'true');
  else if (act === 'project-remove') removeManagedProject(el.getAttribute('data-key'));
  else if (act === 'refresh-reviews') loadReviews();
  else if (act.indexOf('review-') === 0) {
    var card = el.closest('[data-review-card]'), note = card && $('[data-review-note]', card);
    var reviewKey = el.getAttribute('data-key'), reviewComment = note ? note.value : (S.reviewNoteDrafts[reviewKey] || '');
    if (note) S.reviewNoteDrafts[reviewKey] = reviewComment;
    submitReview(reviewKey, act.slice('review-'.length), reviewComment);
  }
  else if (act === 'stakeholder-refresh') { S.sh.renderKey = ''; loadStakeholder(S.project, true); }
  else if (act === 'stakeholder-submit') submitStakeholder(el.getAttribute('data-key'), el.getAttribute('data-verdict'));
  else if (act === 'integ-refresh') loadJiraIntegrations();
  else if (act === 'integ-connect') integConnect();
  else if (act === 'integ-api-token') integApiToken();
  else if (act === 'integ-disconnect') integDisconnect();
  else if (act === 'knowledge-new') knowledgeNew();
  else if (act === 'knowledge-refresh') loadKnowledge(true);
  else if (act === 'knowledge-filter') { S.knowledge.filter = el.getAttribute('data-filter'); renderKnowledge(); }
  else if (act === 'knowledge-cancel') { S.knowledge.editor = null; renderKnowledge(); }
  else if (act === 'knowledge-save') submitKnowledge();
  else if (act === 'knowledge-edit') knowledgeEdit(el.getAttribute('data-kind'), el.getAttribute('data-id'));
  else if (act === 'knowledge-archive') archiveKnowledge(el.getAttribute('data-kind'), el.getAttribute('data-id'), el.getAttribute('data-archived') === 'true');
  else if (act === 'knowledge-history-close') { S.knowledge.history = null; renderKnowledge(); }
  else if (act === 'knowledge-history') {
    var kt = el.getAttribute('data-kind'), ki = el.getAttribute('data-id');
    fetch('/v1/knowledge/resources/' + encodeURIComponent(kt) + '/' + encodeURIComponent(ki) + '/history', { cache: 'no-store' })
      .then(function (r) { return r.json().then(function (d) { if (!r.ok) throw new Error(d.detail || ('HTTP ' + r.status)); return d; }); })
      .then(function (d) { S.knowledge.history = d; renderKnowledge(); })
      .catch(function (err) { toast('History unavailable', String(err.message || err), 'warn'); });
  }
  else if (act === 'knowledge-restore') restoreKnowledge(el.getAttribute('data-kind'), el.getAttribute('data-id'), el.getAttribute('data-version'));
  else if (act === 'knowledge-approve') approveKnowledge(el.getAttribute('data-proposal'));
  else if (act === 'backup-refresh') loadBackups(true);
  else if (act === 'backup-save') saveBackupSettings();
  else if (act === 'backup-run') requestBackupRun();
  else if (act === 'backup-key') downloadBackupKey();
  else if (act === 'backup-copy') {
    var command = backupSourceCommand((S.backup.data.backups || []).filter(function (x) { return x.source === S.backup.selected; })[0].source, S.backup.data);
    if (!navigator.clipboard || !navigator.clipboard.writeText) { toast('Clipboard unavailable', 'Select and copy the restore command.', 'warn'); }
    else navigator.clipboard.writeText(command).then(function () { toast('Restore command copied', '', 'ok'); })
      .catch(function () { toast('Clipboard unavailable', 'Select and copy the restore command.', 'warn'); });
  }
  else if (act === 'knowledge-search') { S.knowledge.search = ($('[data-knowledge-search]') || {}).value || ''; loadKnowledge(true); }
  else if (act === 'knowledge-item-new') { S.knowledge.itemEditor = { kind: 'convention', scope: 'project', epic: '', title: '', content: '' }; renderKnowledge(); }
  else if (act === 'knowledge-item-cancel') { S.knowledge.itemEditor = null; renderKnowledge(); }
  else if (act === 'knowledge-item-save') submitLearningItem();
  else if (act === 'knowledge-item-edit') {
    var itemId = Number(el.getAttribute('data-id')), learningItems = (S.knowledge.learning && S.knowledge.learning.items) || [];
    var selectedItem = learningItems.filter(function (x) { return Number(x.id) === itemId; })[0];
    if (selectedItem) { S.knowledge.itemEditor = { id: selectedItem.id, kind: selectedItem.kind, scope: selectedItem.scope,
      epic: selectedItem.epic, title: selectedItem.title, content: selectedItem.content }; renderKnowledge(); }
  }
  else if (act === 'knowledge-item-approve') knowledgeItemRequest(el.getAttribute('data-id'), 'approve', { actor: S.consoleUser && S.consoleUser.user || S.knowledge.actor });
  else if (act === 'knowledge-item-stale') knowledgeItemRequest(el.getAttribute('data-id'), 'stale', { actor: S.consoleUser && S.consoleUser.user || S.knowledge.actor });
  else if (act === 'knowledge-item-retire') knowledgeItemRequest(el.getAttribute('data-id'), 'retire', { actor: S.consoleUser && S.consoleUser.user || S.knowledge.actor });
  else if (act === 'knowledge-item-merge') {
    var duplicate = window.prompt('ID of the duplicate item to merge into this one');
    if (duplicate && /^\d+$/.test(duplicate)) knowledgeItemRequest(el.getAttribute('data-id'), 'merge',
      { duplicate_id: Number(duplicate), actor: S.consoleUser && S.consoleUser.user || S.knowledge.actor });
  }
  else if (act === 'jump') flash(el.getAttribute('data-target'));
  else if (act === 'open-node') openDrawer('node', el.getAttribute('data-id'), el);
  else if (act === 'open-acc') openDrawer('acc', el.getAttribute('data-id'), el);
  else if (act === 'open-inc') openDrawer('inc', el.getAttribute('data-id'), el);
  else if (act === 'open-ticket') openDrawer('ticket', el.getAttribute('data-key'), el);
  else if (act === 'open-report') openProjectReport(el);
  else if (act === 'close-project') closeProject(el.getAttribute('data-p'), el);
  else if (act === 'copy-report') copyReport();
  else if (act === 'copy-join') {
    var joinNodeName = el.getAttribute('data-node');
    if (joinNodeName) copyJoinCommand(joinNodeName);
    else if (!navigator.clipboard || !navigator.clipboard.writeText) {
      S.join.error = 'Clipboard access failed; select and copy the command manually.'; renderNodeJoin(true);
    } else {
      navigator.clipboard.writeText(S.join.command).then(function () { S.join.copied = true; renderNodeJoin(true); })
        .catch(function () { S.join.error = 'Clipboard access failed; select and copy the command manually.'; renderNodeJoin(true); });
    }
  }
  else if (act === 'node-install') startNodeOperation(el.getAttribute('data-node'), 'install');
  else if (act === 'node-repair') startNodeOperation(el.getAttribute('data-node'), 'repair');
  else if (act === 'join-command') createJoinCommand(el.getAttribute('data-node'));
  else if (act === 'add-node') {
    var newName = window.prompt('Node name');
    if (newName) {
      var projectChoices = (D().projects || []).map(function (p) { return p.key; });
      var newProject = window.prompt('Project key (' + projectChoices.join(', ') + ')', projectChoices[0] || '');
      if (newProject && projectChoices.indexOf(newProject) >= 0) {
        var newRole = window.prompt('Node role', 'worker') || 'worker';
        if (!nodeBy(newName)) D().nodes.push({ name: newName, ip: '', role: newRole, status: 'joined',
          has_agent: false, tools: {}, metrics: {}, projects: [newProject], desired_services: [],
          missing_tools: [], heartbeat_age_s: null, slots_total: 1 });
        openDrawer('node', newName, el);
        createJoinCommand(newName, newRole, newProject);
      } else if (newProject) {
        toast('Could not create join command', 'Choose a project shown in Machines and AI.', 'warn');
      }
    }
  }
  else if (act === 'close') closeAll();
  else if (act === 'acc-create') createSharedAccount();
  else if (act === 'acc-retry-signin') retryAccountSignin(el.getAttribute('data-account'), el.getAttribute('data-node'));
  else if (act === 'acc-enable')enableAccountMachine(el.getAttribute('data-account'));
  else if (act === 'acc-limit') saveAccountLimit(el.getAttribute('data-account'));
  else if (act === 'acc-remove-machine') removeAccountMachine(el.getAttribute('data-account'), el.getAttribute('data-node'), !!el.getAttribute('data-last'));
  else if (act === 'acc-remove-all') removeAccountEverywhere(el.getAttribute('data-account'), el.getAttribute('data-count'));
  else if (act === 'accfilter') { S.accFilter = el.getAttribute('data-f'); renderDashboard(); renderCC(); }
  else if (act === 'project') { S.project = el.getAttribute('data-p'); S.epic = 'all'; S.sh.renderKey = ''; savePrefs(); render(); }
  else if (act === 'type') { S.typeFilter = el.getAttribute('data-t'); savePrefs(); renderDHead(); renderDBody(); tick(); }
  else if (act === 'epic') { S.epic = el.getAttribute('data-key'); savePrefs(); renderDHead(); renderDBody(); tick(); flash('d-board'); }
  else if (act === 'theme') {
    var cur = document.documentElement.getAttribute('data-theme');
    saveConsoleTheme(cur === 'light' ? 'dark' : 'light');
  }
});
function backupRequestError(response) {
  return response.json().then(function (data) {
    if (response.ok) return data;
    throw new Error(data.detail || ('HTTP ' + response.status));
  });
}
function saveBackupSettings() {
  var b = S.backup; if (!b.form || b.saving) return;
  var body = Object.assign({}, b.form);
  $$('[data-backup-field]').forEach(function (field) {
    var name = field.getAttribute('data-backup-field');
    body[name] = name === 'retention_days' ? Number(field.value) : field.value;
  });
  b.saving = true; b.error = ''; renderBackup();
  fetch('/v1/backups/config', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
    .then(backupRequestError).then(function (d) { b.data = d; b.form = { schedule: d.schedule, destination_kind: d.destination_kind,
      destination_path: d.destination_path, s3_endpoint: d.s3_endpoint, s3_bucket: d.s3_bucket, s3_prefix: d.s3_prefix,
      s3_region: d.s3_region, retention_days: d.retention_days }; b.notice = 'Backup settings saved.'; b.saving = false; renderBackup(); })
    .catch(function (e) { b.error = String(e.message || e); b.saving = false; renderBackup(); });
}
function requestBackupRun() {
  fetch('/v1/backups/run', { method: 'POST' }).then(backupRequestError)
    .then(function () { S.backup.notice = 'Backup queued. The control worker will start it shortly.'; renderBackup(); })
    .catch(function (e) { S.backup.error = String(e.message || e); renderBackup(); });
}
function downloadBackupKey() {
  var button = $('[data-act="backup-key"]'); if (button) button.disabled = true;
  fetch('/v1/backups/key', { method: 'POST' }).then(function (r) {
    if (!r.ok) return r.json().then(function (d) { throw new Error(d.detail || ('HTTP ' + r.status)); });
    return r.blob();
  }).then(function (blob) {
    var url = URL.createObjectURL(blob), link = document.createElement('a');
    link.href = url; link.download = 'aiflow4-backup-key'; document.body.appendChild(link); link.click(); link.remove(); URL.revokeObjectURL(url);
    S.backup.notice = 'The key was downloaded. Store it outside this machine; it cannot be downloaded again.';
    loadBackups(true);
  }).catch(function (e) { S.backup.error = String(e.message || e); renderBackup(); });
}
document.addEventListener('input', function (e) {
  if (e.target && e.target.matches('[data-user-email]')) S.userAdmin.addForm.email = e.target.value;
  if (e.target && e.target.matches('[data-user-name]')) S.userAdmin.addForm.name = e.target.value;
  if (e.target && e.target.matches('[data-notify-field]')) S.notifications.form[e.target.getAttribute('data-notify-field')] = e.target.value;
  if (e.target && e.target.matches('[data-notify-checkbox]')) S.notifications.form[e.target.getAttribute('data-notify-checkbox')] = e.target.checked;
  if (e.target && (e.target.id === 'notification-quiet-start' || e.target.id === 'notification-quiet-end')) captureNotificationPreferences();
  if (e.target && e.target.id === 'command-query') renderCommandSearchResults(e.target.value);
  if (e.target && e.target.id === 'work-project') {
    S.projectQuery = e.target.value; S.projectPickerOpen = true; renderProjectPickerOptions();
  }
  if (e.target && e.target.id === 'dash-project-search') { S.dashboardQuery = e.target.value; renderDashboard(); }
  if (e.target && e.target.matches('[data-project-field]')) {
    var field = e.target.getAttribute('data-project-field');
    S.projectAdmin.form[field] = field === 'containers' ? e.target.value === 'true' : e.target.value;
    S.projectAdmin.preflight = null;
    var saveButton = $('[data-act="project-save"]'); if (saveButton) saveButton.disabled = true;
    var oldResult = $('.project-checks'); if (oldResult) oldResult.remove();
  }
  if (e.target && e.target.matches('[data-repository-config]')) {
    var editor = S.projectAdmin.repositoryEditor, data = editor && editor.data;
    if (data) {
      var repoName = e.target.getAttribute('data-repo'), fieldName = e.target.getAttribute('data-repository-config');
      var config = (data.repositories || []).filter(function (r) { return r.repo === repoName; })[0];
      if (config && fieldName === 'connection') config.connection_picked = true;
      if (config) config[fieldName] = fieldName === 'checks' ? e.target.value.split('\n').map(function (x) { return x.trim(); }).filter(Boolean) :
        fieldName === 'containers' ? (e.target.value === 'inherit' ? null : e.target.value === 'true') : e.target.value;
    }
  }
  if (e.target && e.target.matches('[data-env-field], [data-env-run]')) {
    var envEditor = S.projectAdmin.envEditor, envItems = envEditor && envEditor.data && envEditor.data.items;
    var envItem = envItems && envItems[Number(e.target.getAttribute('data-index'))];
    if (envItem && e.target.matches('[data-env-run]')) {
      var runName = e.target.getAttribute('data-env-run');
      envItem.runs = (envItem.runs || []).filter(function (r) { return r !== runName; });
      if (e.target.checked) envItem.runs.push(runName);
    } else if (envItem) {
      var envField = e.target.getAttribute('data-env-field');
      envItem[envField] = envField === 'name' ? e.target.value.toUpperCase() : e.target.value;
      if (envField === 'kind') { if (envItem.kind === 'secret') envItem.value = ''; renderProjects(); }
    }
  }
  if (e.target && e.target.matches('[data-reviewer-token]')) S.reviewerToken = e.target.value;
  if (e.target && e.target.matches('[data-review-note]')) S.reviewNoteDrafts[e.target.closest('[data-review-card]').getAttribute('data-key')] = e.target.value;
  if (e.target && e.target.matches('[data-knowledge-actor]')) S.knowledge.actor = e.target.value;
  if (e.target && e.target.matches('[data-knowledge-search]')) S.knowledge.search = e.target.value;
  if (e.target && e.target.matches('[data-learning-field]') && S.knowledge.itemEditor) {
    S.knowledge.itemEditor[e.target.getAttribute('data-learning-field')] = e.target.value;
  }
  if (e.target && e.target.matches('[data-owner-token]')) S.knowledge.ownerToken = e.target.value;
  if (e.target && e.target.matches('[data-integ-owner-token]')) S.integ.ownerToken = e.target.value;
  if (e.target && e.target.matches('[data-backup-field]')) S.backup.form[e.target.getAttribute('data-backup-field')] = e.target.value;
  if (e.target && e.target.matches('[data-runtime-key]')) S.runtimeSettings.key = e.target.value;
  if (e.target && e.target.matches('[data-runtime-value]')) S.runtimeSettings.value = e.target.value;
  if (e.target && e.target.matches('[data-knowledge-owner]')) S.knowledge.owner = e.target.value;
  if (e.target && e.target.matches('[data-knowledge-field]') && S.knowledge.editor) {
    S.knowledge.editor[e.target.getAttribute('data-knowledge-field')] = e.target.value;
  }
});
document.addEventListener('change', function (e) {
  if (e.target && e.target.matches('.new-role')) S.userAdmin.addForm.roles = Array.from(e.target.closest('.project-form').querySelectorAll('.new-role:checked')).map(function (item) { return item.value; });
  if (e.target && e.target.matches('[data-user-all]') && e.target.closest('.project-form') &&
      $('[data-user-add]', e.target.closest('.project-form'))) S.userAdmin.addForm.all = e.target.checked;
  if (e.target && e.target.matches('[data-user-projects]') && e.target.closest('.project-form') &&
      $('[data-user-add]', e.target.closest('.project-form'))) S.userAdmin.addForm.projects = Array.from(e.target.options).filter(function (option) { return option.selected; }).map(function (option) { return option.value; });
  if (e.target && (e.target.matches('[data-route-kind]') || e.target.id === 'notification-quiet-start' || e.target.id === 'notification-quiet-end')) captureNotificationPreferences();
  if (e.target && e.target.matches('[data-notify-kind-select]')) {
    S.notifications.form.kind = e.target.value;
    ['host', 'port', 'username', 'password', 'from', 'to', 'tls_mode', 'bot_token', 'chat_id', 'ba_chat_enabled', 'project_key', 'webhook_url', 'url', 'auth_token'].forEach(function (key) { delete S.notifications.form[key]; });
    renderNotifications();
  }
  if (e.target && e.target.matches('[data-notify-field]')) S.notifications.form[e.target.getAttribute('data-notify-field')] = e.target.value;
  if (e.target && e.target.matches('[data-notify-checkbox]')) S.notifications.form[e.target.getAttribute('data-notify-checkbox')] = e.target.checked;
  if (e.target && e.target.matches('[data-backup-select]')) { S.backup.selected = e.target.value; renderBackup(); }
  if (e.target && e.target.matches('[data-runtime-scope]')) { S.runtimeSettings.scope = e.target.value; renderRuntimeSettings(); }
  if (e.target && e.target.matches('[data-runtime-project]')) { S.runtimeSettings.project = e.target.value; renderRuntimeSettings(); }
  if (e.target && e.target.matches('[data-backup-field]')) {
    var backupField = e.target.getAttribute('data-backup-field');
    S.backup.form[backupField] = e.target.value;
    if (backupField === 'destination_kind') renderBackup();
  }
  if (e.target && e.target.matches('[data-license-plan]')) { S.licensing.limitsPlan = e.target.value; renderLicensing(); }
  if (e.target && e.target.id === 'ai-filter') {
    S.aiFilter = e.target.value; savePrefs(); renderDBody(); tick();
  }
  if (e.target && e.target.id === 'work-status-filter') {
    S.workStatusFilter = e.target.value; savePrefs();
    if (S.view === 'work') {
      var boardPath = '/work' + (S.workStatusFilter === 'all' ? '' : '?status=' + encodeURIComponent(S.workStatusFilter));
      try { history.replaceState(null, '', boardPath); } catch (x) { /* file preview */ }
    }
    renderDBody(); tick();
  }
  if (e.target && e.target.matches('[data-github-installation]')) S.integrations.selected = e.target.value;
  if (e.target && e.target.matches('[data-review-project]')) { S.reviewProject = e.target.value; loadReviews(); }
  if (e.target && e.target.matches('[data-project-field]')) {
    var field = e.target.getAttribute('data-project-field');
    S.projectAdmin.form[field] = field === 'containers' ? e.target.value === 'true' : e.target.value;
    S.projectAdmin.preflight = null;
    var saveButton = $('[data-act="project-save"]'); if (saveButton) saveButton.disabled = true;
    var oldResult = $('.project-checks'); if (oldResult) oldResult.remove();
  }
  if (e.target && e.target.matches('[data-epic-repository]')) {
    var editor = S.projectAdmin.repositoryEditor, data = editor && editor.data;
    if (data) {
      var epicKey = e.target.getAttribute('data-epic-repository'), repoName = e.target.value;
      data.epic_repositories = data.epic_repositories || {};
      if (repoName) data.epic_repositories[epicKey] = repoName; else delete data.epic_repositories[epicKey];
      if (repoName) addProjectRepository(repoName); else renderProjects();
    }
  }
  if (e.target && e.target.matches('[data-repository-config]')) {
    var editor = S.projectAdmin.repositoryEditor, data = editor && editor.data;
    if (data) {
      var repoName = e.target.getAttribute('data-repo'), fieldName = e.target.getAttribute('data-repository-config');
      var config = (data.repositories || []).filter(function (r) { return r.repo === repoName; })[0];
      if (config && fieldName === 'connection') config.connection_picked = true;
      if (config) config[fieldName] = fieldName === 'checks' ? e.target.value.split('\n').map(function (x) { return x.trim(); }).filter(Boolean) :
        fieldName === 'containers' ? (e.target.value === 'inherit' ? null : e.target.value === 'true') : e.target.value;
    }
  }
});
document.addEventListener('keydown', function (e) {
  if ((e.ctrlKey || e.metaKey) && !e.altKey && String(e.key).toLowerCase() === 'k') {
    e.preventDefault(); openCommandSearch(); return;
  }
  if (e.key === 'Escape' && S.commandSearchOpen) { e.preventDefault(); closeCommandSearch(); return; }
  if (S.commandSearchOpen && e.key === 'Tab') {
    var dialog = $('.command-dialog'), focusable = dialog ? $$('input,button,[tabindex]:not([tabindex="-1"])', dialog).filter(function (x) { return !x.disabled; }) : [];
    if (focusable.length) {
      var first = focusable[0], last = focusable[focusable.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
  }
  if (S.commandSearchOpen && (e.key === 'ArrowDown' || e.key === 'Enter') && e.target.id === 'command-query') {
    var firstResult = $('.command-result');
    if (firstResult) { e.preventDefault(); if (e.key === 'Enter') firstResult.click(); else firstResult.focus(); }
  }
  if (e.target.id === 'work-project' && (e.key === 'ArrowDown' || e.key === 'Enter')) {
    var firstProject = $('.project-option');
    if (firstProject) { e.preventDefault(); if (e.key === 'Enter') firstProject.click(); else firstProject.focus(); }
  }
  if (e.target.matches && e.target.matches('.project-option') && (e.key === 'ArrowDown' || e.key === 'ArrowUp')) {
    var projectOptions = $$('.project-option'), projectIndex = projectOptions.indexOf(e.target), projectDelta = e.key === 'ArrowDown' ? 1 : -1;
    if (projectIndex >= 0 && projectOptions.length) { e.preventDefault(); projectOptions[(projectIndex + projectDelta + projectOptions.length) % projectOptions.length].focus(); }
  }
  if (S.commandSearchOpen && e.target.matches && e.target.matches('.command-result') && (e.key === 'ArrowDown' || e.key === 'ArrowUp')) {
    var searchResults = $$('.command-result'), resultIndex = searchResults.indexOf(e.target), resultDelta = e.key === 'ArrowDown' ? 1 : -1;
    if (resultIndex >= 0 && searchResults.length) { e.preventDefault(); searchResults[(resultIndex + resultDelta + searchResults.length) % searchResults.length].focus(); }
  }
  if (e.key === 'Escape' && S.projectPickerOpen) { S.projectPickerOpen = false; renderProjectPickerOptions(); }
  if (e.key === 'Escape' && S.drawer) closeAll();
  if (e.key === 'Enter' && e.target.matches && e.target.matches('tr[data-act]')) e.target.click();
  if ((e.key === 'ArrowLeft' || e.key === 'ArrowRight') && e.target.matches && e.target.matches('.dashboard-tab')) {
    e.preventDefault(); setDashboardTab(S.dashboardTab === 'ai' ? 'work' : 'ai');
  }
  if ((e.key === 'ArrowLeft' || e.key === 'ArrowRight') && e.target.matches && e.target.matches('[role="tab"]')) {
    var tabs = $$('.tab').filter(function (tab) { return !tab.hidden; }), here = tabs.indexOf(e.target), delta = e.key === 'ArrowRight' ? 1 : -1;
    if (here >= 0 && tabs.length) { e.preventDefault(); var next = tabs[(here + delta + tabs.length) % tabs.length]; next.focus(); setView(next.getAttribute('data-view')); }
  }
});
var CR_FIELDS = { 'cr-title': 'title', 'cr-desc': 'description', 'cr-epic': 'epic' };
var JOIN_FIELDS = { 'join-name': 'name' };
document.addEventListener('input', function (e) {
  if (e.target.id === 'q') { S.search = e.target.value; renderDBody(); tick(); }
  if (e.target.id === 'rh-window') S.rh.window = e.target.value;
  if (e.target.id === 'rh-percent') S.rh.percent = e.target.value;
  if (CR_FIELDS[e.target.id]) S.cr.v[CR_FIELDS[e.target.id]] = e.target.value;
  if (JOIN_FIELDS[e.target.id]) S.join[JOIN_FIELDS[e.target.id]] = e.target.value;
  if (e.target.id === 'machine-name') S.machines.join.name = e.target.value;
  if (e.target.getAttribute && e.target.getAttribute('data-sh-key') && e.target.getAttribute('data-sh-field') !== 'screenshot') {
    var key = e.target.getAttribute('data-sh-key'), field = e.target.getAttribute('data-sh-field');
    S.sh.forms[key] = S.sh.forms[key] || {}; S.sh.forms[key][field] = e.target.value;
  }
});
document.addEventListener('change', function (e) {
  if (e.target.matches && e.target.matches('[data-knowledge-actor]')) S.knowledge.actor = e.target.value;
  if (e.target.matches && e.target.matches('[data-learning-field]') && S.knowledge.itemEditor) {
    S.knowledge.itemEditor[e.target.getAttribute('data-learning-field')] = e.target.value;
  }
  if (e.target.matches && e.target.matches('[data-knowledge-owner]')) S.knowledge.owner = e.target.value;
  if (e.target.id === 'knowledge-kind') { S.knowledge.type = e.target.value; renderKnowledge(); }
  if (e.target.matches && e.target.matches('[data-knowledge-field]') && S.knowledge.editor) {
    S.knowledge.editor[e.target.getAttribute('data-knowledge-field')] = e.target.value;
    if (e.target.getAttribute('data-knowledge-field') === 'type') renderKnowledge();
  }
  if (e.target.id === 'epicSel') { S.epic = e.target.value; savePrefs(); renderDBody(); tick(); }
  else if (e.target.id === 'leadProject') {
    S.project = e.target.value; S.leadEpic = null; S.leadReport = null; S.epic = 'all'; savePrefs(); renderLead();
  }
  else if (e.target.id === 'leadEpic') { S.leadEpic = e.target.value; S.leadReport = null; renderLead(); }
  if (CR_FIELDS[e.target.id]) S.cr.v[CR_FIELDS[e.target.id]] = e.target.value;
  if (e.target.id === 'join-role') S.join.role = e.target.value;
  if (e.target.id === 'join-project') S.join.project = e.target.value;
  if (e.target.id === 'machine-role') S.machines.join.role = e.target.value;
  if (e.target.id === 'machine-project') S.machines.join.project = e.target.value;
  if (e.target.getAttribute && e.target.getAttribute('data-sh-key')) {
    var key = e.target.getAttribute('data-sh-key'), field = e.target.getAttribute('data-sh-field');
    S.sh.forms[key] = S.sh.forms[key] || {};
    if (field === 'screenshot') S.sh.files[key] = e.target.files && e.target.files[0] || null;
    else S.sh.forms[key][field] = e.target.value;
  }
});
document.addEventListener('submit', function (e) {
  if (e.target.id === 'cr-form') { e.preventDefault(); submitCR(); }
  if (e.target.id === 'node-join-form') { e.preventDefault(); submitNodeJoin(); }
  if (e.target.id === 'notification-channel-form') { e.preventDefault(); notificationFormSubmit(); }
  if (e.target.id === 'rh-form') { e.preventDefault(); saveRoleHealth(); }
});
window.addEventListener('popstate', function () {
  S.view = viewOf(location.pathname);
  var status = new URLSearchParams(location.search || '').get('status');
  S.workStatusFilter = status || 'all';
  syncMenuVisibility(); showView(); render();
});

