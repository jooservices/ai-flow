(function (global) {
  function capture(root, userAdmin, selectOne, selectAll) {
    var email = selectOne('[data-user-email]', root), draft = userAdmin.addForm;
    if (!email || userAdmin.temporaryPassword) return;
    var form = email.closest('.project-form');
    if (!form.contains(document.activeElement)) return;
    draft.email = email.value;
    draft.name = selectOne('[data-user-name]', form).value;
    draft.roles = selectAll('.new-role:checked', form).map(function (item) { return item.value; });
    draft.all = selectOne('[data-user-all]', form).checked;
    draft.projects = selectAll('[data-user-projects] option:checked', form).map(function (item) { return item.value; });
  }

  global.AIFlowUserAdminForm = { capture: capture };
})(window);
