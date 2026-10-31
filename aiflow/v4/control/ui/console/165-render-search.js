function quickSearchResults(query) {
  var q = String(query || '').trim().toLowerCase(), role = S.consoleUser && S.consoleUser.role;
  var views = MENU_ITEMS.filter(function (item) { return viewAllowed(item.view, role) && (!q || item.label.toLowerCase().indexOf(q) >= 0); })
    .slice(0, 8).map(function (item) { return { kind: 'view', key: item.view, label: item.label, detail: 'View' }; });
  var projects = D().projects.filter(function (p) { return !q || (p.key + ' ' + p.repo).toLowerCase().indexOf(q) >= 0; });
  projects.sort(function (a, b) {
    var as = S.starredProjects.indexOf(a.key) >= 0, bs = S.starredProjects.indexOf(b.key) >= 0;
    return as !== bs ? (as ? -1 : 1) : a.key.localeCompare(b.key);
  });
  projects = projects.slice(0, q ? 6 : 4).map(function (p) { return { kind: 'project', key: p.key, label: p.key, detail: p.repo }; });
  var tickets = q ? D().tickets.filter(function (t) { return (t.key + ' ' + t.summary + ' ' + t.project).toLowerCase().indexOf(q) >= 0; })
    .slice(0, 8).map(function (t) { return { kind: 'ticket', key: t.key, label: t.key, detail: t.summary }; }) : [];
  return views.concat(projects, tickets);
}
function renderCommandSearchResults(query) {
  var root = $('#command-results'); if (!root) return;
  var rows = quickSearchResults(query);
  root.innerHTML = rows.length ? rows.map(function (row) {
    return '<button class="command-result" type="button" role="option" data-act="search-result" data-kind="' + esc(row.kind) + '" data-key="' + esc(row.key) + '"><strong>' + esc(row.label) + '</strong><span>' + esc(row.detail) + '</span><span class="kind">' + esc(row.kind) + '</span></button>';
  }).join('') : '<div class="empty">No matching views, projects, or tickets.</div>';
}
function openCommandSearch() {
  var root = $('#command-search'); if (!root) return;
  S.commandSearchOpen = true; root.hidden = false;
  var input = $('#command-query'); if (input) { input.value = ''; renderCommandSearchResults(''); input.focus(); }
}
function closeCommandSearch() {
  var root = $('#command-search'); if (!root) return;
  S.commandSearchOpen = false; root.hidden = true;
  var opener = $('[data-act="search-open"]'); if (opener) opener.focus();
}
function openEpicOnWorkBoard(key) {
  S.epic = key; S.search = ''; S.aiFilter = 'all'; savePrefs(); setView('work');
}
function chooseSearchResult(kind, key, source) {
  closeCommandSearch();
  if (kind === 'view') { setView(key); return; }
  if (kind === 'project') {
    var alreadyWork = S.view === 'work';
    S.project = key; S.epic = 'all'; S.search = ''; S.projectQuery = ''; S.projectPickerOpen = false; savePrefs(); setView('work');
    if (alreadyWork) { render(); var picker = $('#work-project'); if (picker) picker.focus(); }
    return;
  }
  if (kind === 'ticket') {
    setView('work'); openDrawer('ticket', key, source); return;
  }
}

