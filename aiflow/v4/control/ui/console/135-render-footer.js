/* ---------- render and live tick ---------- */
/* ---------- footer ---------- */
function footerVersion(d) { return ((d.versions || {}).ai_flow || {}).ref || d.version || '—'; }
function renderFooter() {
  var d = D(), links = d.links || {};
  $('#f-year').textContent = String(new Date().getFullYear());
  if (!S.data) return;
  $('#f-version').textContent = footerVersion(d);
  $('#f-env').textContent = d.env || '—';
  $('#f-control').textContent = String(d.control_repo || '—').replace('ref: refs/heads/', '');
  [['#f-o2', links.o2], ['#f-mail', links.mail], ['#f-gatus', links.gatus]].forEach(function (l) { if (l[1]) $(l[0]).setAttribute('href', l[1]); });
}

