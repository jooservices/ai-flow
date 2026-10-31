/* ---------- customizable layout (docs/v4/console-layouts.md) ----------
   Every section of a page is a block (data-block="<id>"). The server keeps one layout per person and page
   (/v1/console/layouts): order, size (small, medium, large, full) and hidden. applyLayout() wraps the rendered blocks
   in a grid after each render; layout mode adds a handle, size buttons and Hide to every block. */
var LAYOUT_PAGES = { cc: 'dashboard', delivery: 'delivery', review: 'review', machines: 'machines', knowledge: 'knowledge', users: 'users' };
var LAYOUT_SIZES = [['small', 'Small'], ['medium', 'Medium'], ['large', 'Large'], ['full', 'Full width']];
/* Ids in the model's order; ids the model does not know (a block newer than the saved layout) go to the end. */
function layoutOrder(blocks, ids) {
  var rank = {}; (blocks || []).forEach(function (b, i) { rank[b.id] = i; });
  var known = ids.filter(function (id) { return rank[id] != null; }).sort(function (a, b) { return rank[a] - rank[b]; });
  return known.concat(ids.filter(function (id) { return rank[id] == null; }));
}
/* Put the blocks on screen (in their new order) back into the model; blocks that are not on screen keep their place. */
function layoutMerge(blocks, present) {
  var onScreen = {}; present.forEach(function (b) { onScreen[b.id] = true; });
  var queue = present.slice(), out = [];
  (blocks || []).forEach(function (b) { out.push(onScreen[b.id] && queue.length ? queue.shift() : b); });
  return out.concat(queue);
}
/* Move one block among the blocks that are shown. where: -1, 1, 'start' or 'end'. */
function layoutMove(blocks, id, where, shown) {
  var vis = blocks.map(function (b) { return b.id; }).filter(function (i) { return shown.indexOf(i) >= 0; });
  var at = vis.indexOf(id); if (at < 0) return null;
  var to = where === 'start' ? 0 : where === 'end' ? vis.length - 1 : at + where;
  if (to < 0 || to >= vis.length || to === at) return { blocks: blocks, position: at + 1, total: vis.length, moved: false };
  var list = blocks.slice(), from = list.map(function (b) { return b.id; }).indexOf(id), item = list.splice(from, 1)[0];
  var ti = list.map(function (b) { return b.id; }).indexOf(vis[to]);
  list.splice(to > at ? ti + 1 : ti, 0, item);
  return { blocks: list, position: to + 1, total: vis.length, moved: true };
}
function layoutModel(view) { var page = LAYOUT_PAGES[view]; return page && S.layouts.pages[page] || null; }
function layoutRoot(view) { return view === 'delivery' ? $('#dbody') : $('#view-' + view); }
function layoutBlocksIn(container) {
  return Array.prototype.filter.call(container.children, function (el) { return el.hasAttribute && el.hasAttribute('data-block'); });
}
function layoutHidden(blocks, view) {
  var hidden = (blocks || []).filter(function (b) { return b.hidden; });
  if (view === 'cc' && S.consoleUser && !consoleCan('view.machines_ai')) {
    var restricted = ['tool-waits', 'autonomy'];
    hidden = hidden.filter(function (b) { return restricted.indexOf(b.id) < 0; });
  }
  return hidden;
}
/* Every Dashboard block shares one grid so a person can move it across Work, AI and machines, and extra panels. */
function layoutBarHtml(editing, hidden, page) {
  if (!editing) return '<button type="button" class="btn sm" data-act="layout-edit">Customize layout</button>';
  var h = '<button type="button" class="btn sm primary" data-act="layout-done">Done</button><button type="button" class="btn sm" data-act="layout-reset">Reset this page</button>';
  h += '<span class="small muted">' + (page === 'dashboard' ? 'Arrange all Dashboard blocks together across Work and AI and machines. ' : '') +
    'Drag a handle to move a block, or focus it and press the arrow keys. Choose a width or hide a block. Changes are saved for you.</span>';
  h += '<div class="layout-hidden" role="group" aria-label="Hidden blocks">';
  if (!hidden.length) h += '<span class="small muted">No hidden blocks.</span>';
  else {
    h += '<span class="small">Hidden blocks:</span>';
    hidden.forEach(function (b) { h += '<button type="button" class="btn sm" data-act="layout-show" data-id="' + esc(b.id) + '" aria-label="Show ' + esc(b.title || b.id) + '">Show ' + esc(b.title || b.id) + '</button>'; });
  }
  return h + '</div>';
}
function layoutAnnounce(text) {
  var el = document.getElementById('layout-live'); if (!el) return;
  el.textContent = '';
  setTimeout(function () { el.textContent = text; }, 30);
}
function layoutTitle(model, id) { var b = model.blocks.filter(function (x) { return x.id === id; })[0]; return b ? (b.title || b.id) : id; }
function layoutRequest(page, method, payload) {
  var options = { method: method };
  if (payload != null) { options.headers = { 'Content-Type': 'application/json' }; options.body = payload; }
  var request = S.layouts.chain.then(function () {
    return fetch('/v1/console/layouts/' + page, options).then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return method === 'DELETE' ? r.json() : null;
    });
  });
  S.layouts.chain = request.catch(function () {});
  return request;
}
function layoutPersist(page, model) {
  var payload = JSON.stringify({ blocks: model.blocks.map(function (b) { return { id: b.id, size: b.size, hidden: !!b.hidden }; }) });
  layoutRequest(page, 'PUT', payload).catch(function () {
    toast('Layout not saved', 'The change is shown here but could not be stored. Try it again.', 'warn');
  });
}
/* Apply a changed block list: store it, redraw, announce it and put the focus where the person was working. */
function layoutAction(act, el) {
  var page = LAYOUT_PAGES[S.view], model = layoutModel(S.view), id = el.getAttribute('data-id');
  if (!page || !model) return;
  var handle = '[data-layout-handle][data-id="' + id + '"]';
  if (act === 'layout-edit') {
    S.layouts.mode = page; render(); layoutAnnounce('Layout mode is on. Each block has a handle: focus it and use the arrow keys to move the block.');
    var first = $('[data-layout-handle]'); if (first && first.focus) first.focus();
  } else if (act === 'layout-done') {
    S.layouts.mode = ''; render(); layoutAnnounce('Layout mode is off. Your layout is saved.');
    var edit = $('[data-act="layout-edit"]'); if (edit && edit.focus) edit.focus();
  } else if (act === 'layout-size') {
    var size = el.getAttribute('data-size'), label = LAYOUT_SIZES.filter(function (s) { return s[0] === size; })[0];
    var changed = model.blocks.map(function (b) { return b.id === id ? Object.assign({}, b, { size: size }) : b; });
    layoutCommit(changed, layoutTitle(model, id) + ' set to ' + (label ? label[1].toLowerCase() : size) + ' width.', '[data-act="layout-size"][data-id="' + id + '"][data-size="' + size + '"]');
  } else if (act === 'layout-hide' || act === 'layout-show') {
    var hide = act === 'layout-hide';
    var next = model.blocks.map(function (b) { return b.id === id ? Object.assign({}, b, { hidden: hide }) : b; });
    layoutCommit(next, layoutTitle(model, id) + (hide ? ' is hidden. It is listed under hidden blocks.' : ' is shown again.'), hide ? '[data-act="layout-show"][data-id="' + id + '"], [data-layout-handle]' : handle);
  } else if (act === 'layout-reset') {
    if (typeof window.confirm === 'function' && !window.confirm('Return this page to the default layout for your role?')) return;
    layoutRequest(page, 'DELETE').then(function (d) {
      S.layouts.pages[page] = d; render();
      layoutAnnounce('This page is back to the default layout for your role.');
      var done = $('[data-act="layout-done"]'); if (done && done.focus) done.focus();
    }).catch(function () { toast('Layout not reset', 'The default layout could not be restored. Try again.', 'warn'); });
  }
}
function layoutKey(e) {
  var handle = e.target.closest && e.target.closest('[data-layout-handle]'); if (!handle) return;
  var where = { ArrowUp: -1, ArrowLeft: -1, ArrowDown: 1, ArrowRight: 1, Home: 'start', End: 'end' }[e.key];
  if (where == null) return;
  e.preventDefault();
  var model = layoutModel(S.view), id = handle.getAttribute('data-id'); if (!model) return;
  var siblings = Array.prototype.slice.call(handle.closest('.blocks').children);
  var shown = siblings.filter(function (c) { return !c.hidden; }).map(function (c) { return c.getAttribute('data-block-id'); });
  var result = layoutMove(model.blocks, id, where, shown); if (!result) return;
  var title = layoutTitle(model, id);
  if (!result.moved) { layoutAnnounce(title + ' is already ' + (result.position === 1 ? 'first' : 'last') + '. Position ' + result.position + ' of ' + result.total + '.'); return; }
  layoutCommit(result.blocks, title + ' moved to position ' + result.position + ' of ' + result.total + '.', '[data-layout-handle][data-id="' + id + '"]');
}
function layoutDragStart(e) {
  var handle = e.target.closest && e.target.closest('[data-layout-handle]');
  if (!handle || (e.button != null && e.button > 0)) return;
  var cell = handle.closest('.block-cell'); if (!cell) return;
  e.preventDefault();
  try { handle.setPointerCapture(e.pointerId); } catch (x) { /* not supported */ }
  S.layouts.drag = { cell: cell, id: cell.getAttribute('data-block-id') };
  cell.classList.add('dragging');
}
function layoutDragMove(e) {
  var drag = S.layouts.drag; if (!drag) return;
  if (e.clientY < 70) window.scrollBy(0, -24); else if (e.clientY > window.innerHeight - 70) window.scrollBy(0, 24);
  var over = document.elementFromPoint(e.clientX, e.clientY), target = over && over.closest && over.closest('.block-cell');
  if (!target || target === drag.cell || target.parentNode !== drag.cell.parentNode || target.hidden) return;
  var kids = Array.prototype.slice.call(drag.cell.parentNode.children);
  // The other blocks make room: the dragged block takes the place of the one it passes.
  if (kids.indexOf(drag.cell) < kids.indexOf(target)) target.parentNode.insertBefore(drag.cell, target.nextSibling);
  else target.parentNode.insertBefore(drag.cell, target);
}
function loadLayouts() {
  var who = S.consoleUser && S.consoleUser.user;
  if (!who || S.layouts.busy || S.layouts.person === who) return;
  S.layouts.busy = true;
  fetch('/v1/console/layouts', { cache: 'no-store' }).then(function (r) {
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  }).then(function (d) { S.layouts.pages = d.pages || {}; S.layouts.person = who; })
    .catch(function () { /* the pages keep their natural layout until the next try */ })
    .then(function () { S.layouts.busy = false; render(); });
}
document.addEventListener('pointerdown', layoutDragStart);
document.addEventListener('pointermove', layoutDragMove);
document.addEventListener('pointerup', layoutDragEnd);
document.addEventListener('pointercancel', layoutDragEnd);
document.addEventListener('keydown', layoutKey);

