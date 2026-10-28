/* Keep the active add-person field intact when the Users view refreshes. */
(function () {
  var state = { field: '', value: '', start: null, end: null };
  function fieldName(target) {
    if (target.matches('[data-user-email]')) return 'email';
    if (target.matches('[data-user-name]')) return 'name';
    return '';
  }
  function save(target, field) {
    state.field = field;
    state.value = target.value;
    state.start = field === 'name' ? target.selectionStart : null;
    state.end = field === 'name' ? target.selectionEnd : null;
  }
  document.addEventListener('focusin', function (event) {
    var field = fieldName(event.target);
    if (field) save(event.target, field);
    else state.field = '';
  });
  document.addEventListener('input', function (event) {
    var field = fieldName(event.target);
    if (field) save(event.target, field);
  });

  var root = document.getElementById('view-users');
  if (!root || !window.MutationObserver) return;
  new MutationObserver(function () {
    if (!state.field) return;
    var selector = state.field === 'email' ? '[data-user-email]' : '[data-user-name]';
    var target = root.querySelector(selector);
    if (!target || document.activeElement === target) return;
    target.value = state.value;
    target.focus();
    if (state.start !== null && state.end !== null) target.setSelectionRange(state.start, state.end);
  }).observe(root, { childList: true, subtree: true });
})();
