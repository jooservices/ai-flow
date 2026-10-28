/* DOM-based layout controls; injected before the console boot code. */
function applyLayout(view, root) {
  var page = LAYOUT_PAGES[view], model = layoutModel(view);
  if (!page || !root || !model || !root.children) return;
  var editing = S.layouts.mode === page, meta = {}, hidden = layoutHidden(model.blocks, view);
  model.blocks.forEach(function (b) { meta[b.id] = b; });
  var bar = document.createElement('div');
  bar.className = 'layout-bar'; bar.setAttribute('data-layout-bar', page);
  bar.innerHTML = layoutBarHtml(editing, hidden, page);
  var grid = document.createElement('div'), els, byId = {};
  grid.className = 'blocks' + (editing ? ' editing' : ''); grid.setAttribute('data-layout-page', page);
  if (view === 'cc') {
    var dashboardPanel = root.querySelector('#dashboard-panel') || root, tabById = {};
    var headings = document.createElement('div');
    headings.className = 'dashboard-layout-headings';
    headings.setAttribute('aria-label', 'Dashboard sections');
    Array.prototype.slice.call(dashboardPanel.querySelectorAll('[data-dashboard-panel]')).forEach(function (section) {
      var title = section.querySelector('.dash-section-title');
      var tab = section.getAttribute('data-dashboard-panel');
      if (title) { title.setAttribute('data-dashboard-tab', tab); title.hidden = !editing && tab !== S.dashboardTab; headings.appendChild(title); }
    });
    els = Array.prototype.slice.call(root.querySelectorAll('[data-block]'));
    els.forEach(function (el) {
      var section = el.closest('[data-dashboard-panel]');
      byId[el.getAttribute('data-block')] = el;
      tabById[el.getAttribute('data-block')] = section ? section.getAttribute('data-dashboard-panel') : 'work';
    });
    var dashboardIds = layoutOrder(model.blocks, Object.keys(byId));
    var dashboardShown = dashboardIds.filter(function (id) { return !(meta[id] && meta[id].hidden); });
    grid.setAttribute('id', 'dashboard-layout-grid');
    dashboardPanel.appendChild(bar); dashboardPanel.appendChild(headings); dashboardPanel.appendChild(grid);
    dashboardIds.forEach(function (id) {
      var el = byId[id]; if (!el) return;
      var b = meta[id] || { id: id, title: id, size: 'full', hidden: false };
      var cell = document.createElement('div');
      var tab = tabById[id];
      cell.className = 'block-cell'; cell.setAttribute('data-block-id', id); cell.setAttribute('data-dashboard-tab', tab);
      cell.setAttribute('data-size', b.size); cell.hidden = !!b.hidden || (!editing && tab !== S.dashboardTab);
      if (editing) {
        cell.appendChild(layoutTools(b, dashboardShown.indexOf(id) + 1, dashboardShown.length));
      }
      cell.appendChild(el); grid.appendChild(cell);
    });
    Array.prototype.slice.call(dashboardPanel.querySelectorAll('[data-dashboard-panel]')).forEach(function (section) { section.remove(); });
    dashboardPanel.insertBefore(bar, dashboardPanel.firstChild);
    dashboardPanel.insertBefore(headings, bar.nextSibling);
    dashboardPanel.insertBefore(grid, headings.nextSibling);
    return;
  }
  els = layoutBlocksIn(root);
  if (!els.length) return;
  root.insertBefore(bar, els[0]);
  root.insertBefore(grid, els[0]);
  els.forEach(function (el) { byId[el.getAttribute('data-block')] = el; });
  var ids = layoutOrder(model.blocks, Object.keys(byId));
  var shown = ids.filter(function (id) { return !(meta[id] && meta[id].hidden); });
  ids.forEach(function (id) {
    var b = meta[id] || { id: id, title: id, size: 'full', hidden: false };
    var cell = document.createElement('div');
    cell.className = 'block-cell'; cell.setAttribute('data-block-id', id); cell.setAttribute('data-size', b.size); cell.hidden = !!b.hidden;
    if (editing) cell.appendChild(layoutTools(b, shown.indexOf(id) + 1, shown.length));
    cell.appendChild(byId[id]); grid.appendChild(cell);
  });
}

function layoutTools(b, position, total) {
  var title = b.title || b.id, wrapper = document.createElement('div');
  wrapper.className = 'block-tools'; wrapper.setAttribute('role', 'group'); wrapper.setAttribute('aria-label', 'Layout of ' + title);
  var handle = document.createElement('button');
  handle.type = 'button'; handle.className = 'block-handle'; handle.setAttribute('data-layout-handle', '');
  handle.setAttribute('data-id', b.id); handle.title = 'Drag to move, or use the arrow keys';
  handle.setAttribute('aria-label', 'Move ' + title + ', position ' + position + ' of ' + total + '. Arrow keys move it.');
  var grip = document.createElement('span'); grip.setAttribute('aria-hidden', 'true'); grip.textContent = '⠿'; handle.appendChild(grip);
  var label = document.createElement('span'); label.className = 'block-title'; label.textContent = title; handle.appendChild(label);
  wrapper.appendChild(handle);
  var sizes = document.createElement('span'); sizes.className = 'block-sizes'; sizes.setAttribute('role', 'group');
  sizes.setAttribute('aria-label', 'Width of ' + title);
  LAYOUT_SIZES.forEach(function (s) {
    var button = document.createElement('button');
    button.type = 'button'; button.className = 'btn sm'; button.setAttribute('data-act', 'layout-size');
    button.setAttribute('data-id', b.id); button.setAttribute('data-size', s[0]);
    button.setAttribute('aria-pressed', String(b.size === s[0]));
    button.setAttribute('aria-label', s[1] + ' width for ' + title); button.textContent = s[1];
    sizes.appendChild(button);
  });
  wrapper.appendChild(sizes);
  var hide = document.createElement('button');
  hide.type = 'button'; hide.className = 'btn sm'; hide.setAttribute('data-act', 'layout-hide');
  hide.setAttribute('data-id', b.id); hide.setAttribute('aria-label', 'Hide ' + title); hide.textContent = 'Hide';
  wrapper.appendChild(hide);
  return wrapper;
}

function layoutCommit(blocks, say, focus) {
  var model = layoutModel(S.view), page = LAYOUT_PAGES[S.view]; if (!model) return;
  model.blocks = blocks; model.customized = true;
  layoutPersist(page, model);
  render();
  layoutAnnounce(say);
  var target = focus && (typeof focus === 'function' ? focus() : $(focus)); if (target && target.focus) target.focus();
}

function layoutHandleById(id) {
  var handles = document.querySelectorAll('[data-layout-handle]');
  for (var i = 0; i < handles.length; i++) if (handles[i].getAttribute('data-id') === id) return handles[i];
  return null;
}

function layoutDragEnd() {
  var drag = S.layouts.drag; if (!drag) return;
  S.layouts.drag = null;
  drag.cell.classList.remove('dragging');
  var model = layoutModel(S.view); if (!model) return;
  var cells = Array.prototype.slice.call(drag.cell.parentNode.children);
  var present = cells.map(function (c) {
    var id = c.getAttribute('data-block-id');
    return model.blocks.filter(function (b) { return b.id === id; })[0] || { id: id, title: id, size: 'full', hidden: false };
  });
  var merged = layoutMerge(model.blocks, present);
  if (merged.map(function (b) { return b.id; }).join() === model.blocks.map(function (b) { return b.id; }).join()) { render(); return; }
  var shown = cells.filter(function (c) { return !c.hidden; }).map(function (c) { return c.getAttribute('data-block-id'); });
  layoutCommit(merged, layoutTitle(model, drag.id) + ' moved to position ' + (shown.indexOf(drag.id) + 1) + ' of ' + shown.length + '.',
    function () { return layoutHandleById(drag.id); });
}
