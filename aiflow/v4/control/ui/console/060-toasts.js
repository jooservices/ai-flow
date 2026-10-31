/* ---------- toasts ---------- */
function toast(title, body, toneName) {
  var box = $('#toasts'); if (!box) return;
  var el = document.createElement('div');
  el.className = 'toast'; el.setAttribute('role', 'status');
  var ic = toneName === 'crit' || toneName === 'warn' ? ICON.warn : toneName === 'ok' ? ICON.ok : ICON.info;
  var col = toneName === 'crit' ? 'var(--crit)' : toneName === 'warn' ? 'var(--warn)' : toneName === 'ok' ? 'var(--good)' : 'var(--accent)';
  el.innerHTML = '<span style="color:' + col + '">' + ic + '</span><div><div class="t-title">' + esc(title) + '</div>' + (body ? '<div class="t-body">' + esc(body) + '</div>' : '') + '</div>';
  box.appendChild(el);
  while (box.children.length > 4) box.removeChild(box.firstChild);
  setTimeout(function () { el.classList.add('out'); setTimeout(function () { el.remove(); }, 320); }, 6500);
}

