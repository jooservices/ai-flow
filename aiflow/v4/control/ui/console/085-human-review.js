/* ---------- Human review ---------- */
function renderReviews() {
  var root = $('#view-review'); if (!root) return;
  var noteDrafts = S.reviewNoteDrafts || (S.reviewNoteDrafts = {});
  var active = document.activeElement;
  var activeInsideReview = typeof root.contains === 'function' && root.contains(active);
  if (activeInsideReview && active.matches && active.matches('[data-review-note]')) {
    var activeCard = active.closest('[data-review-card]');
    if (activeCard) noteDrafts[activeCard.getAttribute('data-key')] = active.value;
    return;
  }
  if (activeInsideReview && active.matches && active.matches('button[data-act^="review-"]')) {
    return;
  }
  if (typeof root.querySelectorAll === 'function') {
    root.querySelectorAll('[data-review-note]').forEach(function (field) {
      var card = field.closest('[data-review-card]');
      if (card && noteDrafts[card.getAttribute('data-key')] == null) {
        noteDrafts[card.getAttribute('data-key')] = field.value;
      }
    });
  }
  var rows = S.reviews || [], metrics = S.reviewMetrics || {}, h = '<section class="panel" data-block="queue"><div class="panel-h"><h2>Human review queue</h2><span class="hint">Waiting items and the reasons people are needed · refreshed every 15 seconds</span><span class="spacer"></span><button class="btn sm" data-act="refresh-reviews">Refresh</button></div>';
  h += '<div class="review-filters"><label>Project <select data-review-project aria-label="Filter human review by project"><option value="">All projects</option>';
  (S.reviewProjects || []).forEach(function (p) { h += '<option value="' + esc(p) + '"' + (S.reviewProject === p ? ' selected' : '') + '>' + esc(p) + '</option>'; });
  h += '</select></label><a class="btn sm" href="/v1/reviews/export.csv' + (S.reviewProject ? '?project=' + encodeURIComponent(S.reviewProject) : '') + '" download>Export metrics CSV</a></div>';
  if (!reviewByRole()) h += '<label class="review-auth">Reviewer access token <input type="password" autocomplete="new-password" data-reviewer-token value="' + esc(S.reviewerToken) + '" aria-label="Reviewer access token"></label><span class="small muted">Reviewer access is required for actions; the token stays in this page until it is closed.</span>';
  if (S.reviewsError) h += '<div class="empty" style="color:var(--crit)">Could not load the review queue: ' + esc(S.reviewsError) + '</div>';
  else if (S.reviews === null) h += '<div class="empty">Loading review queue…</div>';
  else if (!rows.length) h += '<div class="empty">No items are waiting for a person in this project.</div>';
  h += '</section>' + reviewMetricsPanel(metrics);
  if (rows.length) {
    h += '<div class="review-list" data-block="items">';
    rows.forEach(function (t) {
      var pr = t.pr || {}, ver = t.verify || {}, diff = [
        ['Files changed', pr.changed_files], ['Additions', pr.additions], ['Deletions', pr.deletions]
      ];
      var actionList = t.actions || (t.status === 'Needs owner' ? ['answer', 'ask-ai'] : ['approve', 'reject', 'rework', 'ask-ai', 'skip-gate']);
      var labels = { question_from_ai: 'Question from AI', decision_needed: 'Decision needed', review_before_merge: 'Review before merge', blocked: 'Blocked' };
      h += '<article class="panel review-card" data-review-card data-key="' + esc(t.key) + '"><div class="review-title"><h2><span class="mono muted">' + esc(t.key) + '</span> ' + esc(t.summary) + '</h2><span class="spacer"></span>' + pill(t.status === 'Needs owner' ? 'warn' : 'info', labels[t.kind] || t.status || 'Human review', true) + '</div>';
      if (t.description) h += '<div class="review-content">' + esc(t.description) + '</div>';
      h += '<div class="review-facts"><div class="review-fact"><div class="label">Repository</div><div class="value mono">' + esc(t.repository || '—') + '</div></div><div class="review-fact"><div class="label">Pull request</div><div class="value">' + (pr.url ? '<a href="' + esc(safeHref(pr.url)) + '" target="_blank" rel="noopener">' + esc(pr.title || ('PR #' + pr.number)) + ' ' + ICON.ext + '</a>' : '—') + '</div></div>';
      diff.forEach(function (x) { h += '<div class="review-fact"><div class="label">' + x[0] + '</div><div class="value mono">' + (x[1] == null ? '—' : esc(x[1])) + '</div></div>'; });
      h += '<div class="review-fact"><div class="label">Verify</div><div class="value">' + (ver.outcome ? pill(runTone(ver.outcome), (ver.verdict ? ver.verdict + ' · ' : '') + ver.outcome) : '—') + (ver.checks ? '<div class="small muted" style="margin-top:4px">' + esc(ver.checks) + '</div>' : '') + '</div></div></div>';
      if (ver.summary) h += '<div><strong>Verify result:</strong> ' + esc(ver.summary) + '</div>';
      if (ver.findings) h += '<div class="review-content"><strong>Findings:</strong>\n' + esc(ver.findings) + '</div>';
      h += '<div style="display:flex;gap:8px;flex-wrap:wrap"><a class="btn sm" href="' + esc(safeHref(t.jira_url)) + '" target="_blank" rel="noopener">Open ticket in Jira ' + ICON.ext + '</a>';
      if (t.preview_url) h += '<a class="btn sm" href="' + esc(safeHref(t.preview_url)) + '" target="_blank" rel="noopener">Preview environment ' + ICON.ext + '</a>';
      if (t.thread && t.thread.length) {
        h += '<div class="review-thread"><strong>AI question thread</strong>';
        t.thread.forEach(function (line) { h += '<div class="review-thread-line"><span class="muted">' + esc(line.author || line.kind) + ' · ' + esc(line.created_at || '') + '</span><div>' + esc(line.text || '') + '</div></div>'; });
        h += '</div>';
      }
      if (actionList.length) {
        h += '<div class="review-actions"><textarea data-review-note aria-label="Reviewer note for ' + esc(t.key) + '" placeholder="Required for reject, rework, answer, or an AI question">' + esc(noteDrafts[t.key] || '') + '</textarea>';
        actionList.forEach(function (action) {
          var names = { approve: 'Approve and merge', reject: 'Reject', rework: 'Send for rework', 'ask-ai': 'Ask AI a question', answer: 'Answer', 'skip-gate': 'Skip gate and merge' };
          h += '<button class="btn' + (action === 'reject' || action === 'rework' ? ' danger' : '') + '" data-act="review-' + esc(action) + '" data-key="' + esc(t.key) + '">' + esc(names[action] || action) + '</button>';
        });
        h += '</div>';
      }
      h += '</article>';
    });
    h += '</div>';
  }
  h += reviewThreadsPanel(S.reviewThreads || [], rows);
  root.innerHTML = h;
  applyLayout('review', root);
}
function reviewThreadsPanel(threads, waiting) {
  var waitingKeys = {}; (waiting || []).forEach(function (ticket) { waitingKeys[ticket.key] = true; });
  var archived = (threads || []).filter(function (item) { return !waitingKeys[item.key] && item.thread && item.thread.length; });
  if (!archived.length) return '';
  var h = '<section class="panel" data-block="threads"><div class="panel-h"><h2>AI question threads</h2><span class="hint">Recent questions and answers, including work no longer waiting</span></div>';
  archived.forEach(function (item) {
    h += '<article class="review-thread"><strong><span class="mono muted">' + esc(item.key) + '</span> ' + esc(item.summary || '') + '</strong>';
    item.thread.forEach(function (line) { h += '<div class="review-thread-line"><span class="muted">' + esc(line.author || line.kind) + ' · ' + esc(line.created_at || '') + '</span><div>' + esc(line.text || '') + '</div></div>'; });
    h += '</article>';
  });
  return h + '</section>';
}
function reviewMetricsPanel(metrics) {
  var weekly = metrics.weekly || [], max = 1;
  weekly.forEach(function (w) { max = Math.max(max, w.total || 0); });
  function value(v, suffix) { return v == null ? '—' : String(v) + (suffix || ''); }
  var h = '<section class="panel" data-block="metrics"><div class="panel-h"><h2>People needed</h2><span class="hint">UTC calendar weeks</span></div><div class="review-kpis">';
  h += '<div class="review-kpi"><div class="label">This week · eight weeks ago</div><strong>' + value(metrics.people_needed_this_week, '') + ' · ' + value(metrics.people_needed_eight_weeks_ago, '') + '</strong></div>';
  h += '<div class="review-kpi"><div class="label">Finished with no person</div><strong>' + value(metrics.finished_with_no_person_pct, '%') + '</strong></div>';
  h += '<div class="review-kpi"><div class="label">Median person wait</div><strong>' + value(metrics.median_wait_minutes, ' min') + '</strong></div>';
  h += '<div class="review-kpi"><div class="label">Approved first time</div><strong>' + value(metrics.approved_first_time_pct, '%') + '</strong></div>';
  h += '<div class="review-kpi"><div class="label">Sent back or rejected</div><strong>' + value(metrics.sent_back_or_rejected_pct, '%') + '</strong></div></div>';
  h += '<div class="review-breakdown"><div><div class="review-chart" role="img" aria-label="Weekly human need by reason">';
  var reasons = [['question_from_ai', 'Question from AI'], ['decision_needed', 'Decision needed'], ['review_before_merge', 'Review before merge'], ['blocked', 'Blocked']];
  weekly.forEach(function (w) {
    h += '<div class="review-chart-row"><span class="mono">' + esc(w.week) + '</span><div class="review-chart-bar">';
    reasons.forEach(function (r) { var count = (w.by_reason || {})[r[0]] || 0;
      h += '<span class="reason-' + r[0] + '" title="' + esc(r[1] + ': ' + count) + '" style="width:' + (100 * count / max) + '%">' + (count ? esc(count) : '') + '</span>'; });
    h += '</div><span class="mono">' + (w.total || 0) + '</span></div>';
  });
  h += '</div><div class="review-legend">';
  reasons.forEach(function (r) { h += '<span><i class="reason-' + r[0] + '"></i>' + esc(r[1]) + '</span>'; });
  h += '</div></div><div class="review-suggestions"><strong>Changes to reduce each reason</strong><table><thead><tr><th>Reason</th><th>Suggested change</th></tr></thead><tbody>';
  (metrics.suggestions || []).forEach(function (s) { var match = reasons.filter(function (r) { return r[0] === s.reason; })[0];
    h += '<tr><td>' + esc(match ? match[1] : s.reason) + '</td><td>' + esc(s.suggestion) + '</td></tr>'; });
  h += '</tbody></table></div></div></section>';
  return h;
}
// A signed-in person whose roles grant review.actions acts with the session; the shared token is the fallback.
function reviewByRole() { return !!S.consoleUser && consoleCan('review.actions'); }
function submitReview(key, action, comment) {
  var tokenField = $('[data-reviewer-token]'), byRole = reviewByRole();
  S.reviewerToken = tokenField ? tokenField.value : S.reviewerToken;
  if (!String(comment || '').trim() && S.reviewNoteDrafts[key]) comment = S.reviewNoteDrafts[key];
  if (!byRole && !S.reviewerToken) { toast('Reviewer access required', 'Enter the reviewer access token before taking action.', 'warn'); return; }
  if (['rework', 'reject', 'ask-ai', 'answer'].indexOf(action) >= 0 && !String(comment || '').trim()) {
    toast('A note is required', 'Enter the required reason, comment, answer, or question before taking this action.', 'warn'); return;
  }
  var body = action === 'rework' ? { comment: comment } : action === 'reject' ? { reason: comment } :
    action === 'ask-ai' ? { question: comment } : action === 'answer' ? { answer: comment } : {};
  fetch('/v1/reviews/' + encodeURIComponent(key) + '/' + action, {
    method: 'POST', headers: byRole ? { 'Content-Type': 'application/json' }
      : { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + S.reviewerToken },
    body: JSON.stringify(body)
  }).then(function (r) {
    return r.json().then(function (d) { if (!r.ok) throw new Error(d.detail || ('HTTP ' + r.status)); return d; });
  }).then(function (d) {
    var card = Array.from(document.querySelectorAll('[data-review-card]')).find(function (item) {
      return item.getAttribute('data-key') === key;
    });
    var noteField = card && $('[data-review-note]', card);
    delete S.reviewNoteDrafts[key];
    if (noteField) noteField.value = '';
    if (action === 'approve' && !d.merged) toast('Merge refused', d.message || 'GitHub refused the merge.', 'warn');
    else toast('Review action recorded', key + (d.status ? ' · ' + d.status : ''), 'ok');
    var focusedAction = document.activeElement;
    if (focusedAction && focusedAction.matches && focusedAction.matches('button[data-act^="review-"]')) focusedAction.blur();
    loadReviews();
  }).catch(function (e) { toast('Review action failed', String(e.message || e), 'crit'); });
}

