function adminRequest(url, options) {
  return fetch(url, Object.assign({ cache: 'no-store' }, options || {})).then(function (r) {
    return r.json().then(function (d) { if (!r.ok) throw new Error(d.detail || ('HTTP ' + r.status)); return d; });
  });
}
function loadUsers(force) {
  var u = S.userAdmin;
  if (!consoleCan('console.manage') || u.loading || (u.users && !force)) return;
  u.loading = true; u.error = ''; renderUsers();
  adminRequest('/v1/console/users').then(function (d) {
    u.users = d.users || []; u.projects = d.projects || []; u.roleOptions = d.role_options || []; u.audit = d.audit || [];
    u.error = ''; u.loadedAt = Date.now(); u.loading = false; renderUsers();
  }).catch(function (e) { u.error = String(e.message || e); u.loading = false; renderUsers(); });
}
var ROLE_VIEW_LABELS = { 'view.dashboard': 'Dashboard', 'view.work_board': 'Work board', 'view.delivery': 'Delivery',
  'view.sa': 'SA', 'view.team_lead': 'Team Lead', 'view.business': 'Business', 'view.human_review': 'Human review',
  'view.stakeholder': 'Stakeholder', 'view.machines_ai': 'Machines and AI', 'view.knowledge': 'Knowledge',
  'view.integrations': 'Integrations', 'projects.manage': 'Projects', 'console.manage': 'Users and roles' };
function roleChecks(root, className, selected) {
  return S.userAdmin.roleOptions.map(function (r) {
    return '<label class="chip2" style="cursor:pointer"><input type="checkbox" class="' + className + '" value="' + esc(r.id) + '"' +
      (selected.indexOf(r.id) >= 0 ? ' checked' : '') + '> ' + esc(r.label) + '</label>';
  }).join('');
}
function projectChoices(selected, all) {
  return '<label class="fld">Projects <select class="in" data-user-projects multiple size="' + Math.min(5, Math.max(2, S.userAdmin.projects.length)) + '"' + (all ? ' disabled' : '') + '>' +
    S.userAdmin.projects.map(function (key) { return '<option value="' + esc(key) + '"' + (selected.indexOf(key) >= 0 ? ' selected' : '') + '>' + esc(key) + '</option>'; }).join('') +
    '</select></label><label class="chip2" style="cursor:pointer"><input type="checkbox" data-user-all' + (all ? ' checked' : '') + '> All projects</label>';
}
function renderUsers() {
  var root = $('#view-users'); if (!root) return;
  if (!consoleCan('console.manage')) { root.innerHTML = '<section class="panel"><h2>View unavailable</h2><p>This view is not available for your role.</p></section>'; return; }
  var u = S.userAdmin, h = '<section class="panel" data-block="users"><div class="panel-h"><h2>People and roles</h2><span class="hint">Role changes, project access and account status take effect immediately.</span><span class="spacer"></span><button class="btn sm" data-user-refresh>Refresh</button></div>';
  if (u.error) h += '<div class="stakeholder-error">' + esc(u.error) + '</div>';
  if (u.temporaryPassword) h += '<div class="project-checks"><strong>One-time password for ' + esc(u.temporaryPassword.email) + '</strong><p>Copy this password and share it with the person. It will not be shown again.</p><code>' + esc(u.temporaryPassword.value) + '</code><button class="btn sm" data-user-password-dismiss>Dismiss</button></div>';
  if (u.loading && !u.users) h += '<div class="muted">Loading people…</div>';
  else {
    h += '<div class="project-table"><table><thead><tr><th>Person</th><th>Roles</th><th>Projects</th><th>Last sign-in</th><th>Status</th><th></th></tr></thead><tbody>';
    (u.users || []).forEach(function (person) {
      var labels = (person.roles || []).map(function (role) { return (u.roleOptions.filter(function (r) { return r.id === role; })[0] || {}).label || role; });
      var projects = person.project_keys == null ? 'All projects' : person.project_keys.length ? person.project_keys.join(', ') : 'No projects';
      h += '<tr><td><b>' + esc(person.display_name || person.email) + '</b><div class="small muted">' + esc(person.email) + '</div></td><td>' +
        labels.map(function (label) { return '<span class="pill">' + esc(label) + '</span>'; }).join(' ') + '</td><td>' + esc(projects) + '</td><td>' +
        esc(person.last_login_at ? new Date(person.last_login_at).toLocaleString() : 'Never') + '</td><td>' +
        (person.disabled ? '<span class="pill" data-tone="crit">Disabled</span>' : '<span class="pill" data-tone="ok">Active</span>') +
        '</td><td><div class="project-actions"><button class="btn sm" data-user-edit="' + esc(person.email) + '">Edit</button><button class="btn sm' +
        (person.disabled ? '' : ' danger') + '" data-user-toggle="' + esc(person.email) + '" data-disabled="' + (!person.disabled) + '">' +
        (person.disabled ? 'Enable' : 'Disable') + '</button></div></td></tr>';
      if (u.editing === person.email) {
        h += '<tr><td colspan="6"><div class="project-form"><div class="wide"><strong>Roles</strong><div>' +
          roleChecks(root, 'edit-role', person.roles || []) + '</div></div><div>' + projectChoices(person.project_keys || [], person.project_keys == null) +
          '</div><div class="project-actions"><button class="btn primary sm" data-user-save="' + esc(person.email) + '">Save changes</button>' +
          '<button class="btn sm" data-user-cancel>Cancel</button></div></div></td></tr>';
      }
    });
    if (!(u.users || []).length) h += '<tr><td colspan="6" class="muted">No people have been added.</td></tr>';
    h += '</tbody></table></div>';
  }
  var addForm = u.addForm;
  h += '</section><div class="users-layout-secondary" data-block="access"><section class="panel"><div class="panel-h"><h2>Add a person</h2><span class="hint">AI Flow creates a one-time password for the new person.</span></div>' +
    '<div class="project-form"><label>Email<input class="in" type="email" data-user-email autocomplete="off" required value="' + esc(addForm.email) + '"></label>' +
    '<label>Name<input class="in" data-user-name autocomplete="off" value="' + esc(addForm.name) + '"></label><div><strong>Roles</strong><div>' + roleChecks(root, 'new-role', addForm.roles || []) +
    '</div></div><div>' + projectChoices(addForm.projects || [], addForm.all !== false) + '</div><div class="project-actions"><button class="btn primary" data-user-add>Add person</button></div></div></section>';
  h += '<section class="panel"><div class="panel-h"><h2>Views by role</h2><span class="hint">A person with multiple roles sees the union of their views.</span></div>' +
    '<div class="project-table"><table><thead><tr><th>View</th>' + u.roleOptions.map(function (r) { return '<th>' + esc(r.label) + '</th>'; }).join('') +
    '</tr></thead><tbody>' + Object.keys(ROLE_VIEW_LABELS).map(function (permission) {
      return '<tr><td>' + esc(ROLE_VIEW_LABELS[permission]) + '</td>' + u.roleOptions.map(function (r) {
        var on = r.permissions.indexOf('*') >= 0 || r.permissions.indexOf(permission) >= 0;
        return '<td>' + (on ? '✓' : '—') + '</td>';
      }).join('') + '</tr>';
    }).join('') + '</tbody></table></div></section>';
  h += '<section class="panel"><div class="panel-h"><h2>Change history</h2><span class="hint">Who made each account or role change</span></div>' +
    '<div class="project-table"><table><thead><tr><th>When</th><th>Who</th><th>Change</th><th>Details</th></tr></thead><tbody>' +
    (u.audit || []).map(function (item) { return '<tr><td>' + esc(item.ts ? new Date(item.ts).toLocaleString() : '') + '</td><td>' + esc(item.actor) +
      '</td><td>' + esc(item.action.replace(/_/g, ' ')) + '</td><td>' + esc(item.result) + '<div class="small muted">' +
      esc(JSON.stringify(item.payload || {})) + '</div></td></tr>'; }).join('') +
    '</tbody></table></div></section></div>';
  root.innerHTML = h;
  applyLayout('users', root);
  $$('[data-user-refresh]', root).forEach(function (b) { b.onclick = function () { loadUsers(true); }; });
  $$('[data-user-password-dismiss]', root).forEach(function (b) { b.onclick = function () { u.temporaryPassword = ''; renderUsers(); }; });
  $$('[data-user-edit]', root).forEach(function (b) { b.onclick = function () { u.editing = b.dataset.userEdit; renderUsers(); }; });
  $$('[data-user-cancel]', root).forEach(function (b) { b.onclick = function () { u.editing = ''; renderUsers(); }; });
  $$('[data-user-all]', root).forEach(function (box) { box.onchange = function () { var select = $('[data-user-projects]', box.closest('.project-form')); if (select) select.disabled = box.checked; }; });
  $$('[data-user-toggle]', root).forEach(function (b) { b.onclick = function () {
    adminRequest('/v1/console/users/' + encodeURIComponent(b.dataset.userToggle) + '/disabled', {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ disabled: b.dataset.disabled === 'true' })
    }).then(function () { loadUsers(true); }).catch(function (e) { toast('Account update failed', String(e.message || e), 'crit'); });
  }; });
  $$('[data-user-save]', root).forEach(function (b) { b.onclick = function () {
    var edit = b.closest('.project-form'), roles = $$('.edit-role:checked', edit).map(function (x) { return x.value; });
    var all = $('[data-user-all]', edit).checked, projects = all ? null : $$('[data-user-projects] option:checked', edit).map(function (x) { return x.value; });
    adminRequest('/v1/console/users/' + encodeURIComponent(b.dataset.userSave), { method: 'PUT',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ roles: roles, project_keys: projects })
    }).then(function () { u.editing = ''; loadUsers(true); }).catch(function (e) { toast('Person update failed', String(e.message || e), 'crit'); });
  }; });
  $$('[data-user-add]', root).forEach(function (b) { b.onclick = function () {
    var form = b.closest('.project-form'), email = $('[data-user-email]', form).value, name = $('[data-user-name]', form).value;
    var roles = $$('.new-role:checked', form).map(function (x) { return x.value; }), all = $('[data-user-all]', form).checked;
    var projects = all ? null : $$('[data-user-projects] option:checked', form).map(function (x) { return x.value; });
    adminRequest('/v1/console/users', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email: email, display_name: name, roles: roles, project_keys: projects })
    }).then(function (d) {
      u.addForm = { email: '', name: '', roles: [], projects: [], all: true };
      u.temporaryPassword = { email: d.email, value: d.temporary_password }; loadUsers(true);
    })
      .catch(function (e) { toast('Person could not be added', String(e.message || e), 'crit'); });
  }; });
}
