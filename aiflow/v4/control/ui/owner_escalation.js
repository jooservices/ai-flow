/* Command Center owner-question panel, split out so the shared console stays below the giant-file limit. */
(function () {
'use strict';

function panelHeader(state, escape) {
  var html = '<section class="panel" data-block="escalations" id="p-escalations"><div class="panel-h"><h2>Escalations and owner decisions</h2><span class="hint">Only a new business decision or an irreversible or production action reaches a person</span></div>';
  if (state.esc.error) html += '<div class="stakeholder-error">' + escape(state.esc.error) + '</div>';
  return html;
}

function renderUnavailable(state) {
  return '<div class="muted">' + (state.busy ? 'Loading escalations…' : 'Escalations unavailable.') + '</div></section>';
}

function renderMetricSummary(metrics, escape) {
  var now = metrics.this_week || {}, causes = metrics.top_causes || [];
  var html = '<div class="grid-2"><div class="review-fact"><div class="label">This week</div><div class="value">' + escape(now.total || 0) + ' escalations · ' +
    (now.share_without_person_pct == null ? 'no data' : escape(now.share_without_person_pct) + '% resolved without a person') +
    '</div><div class="muted">Target: a falling line' + (metrics.falling ? ' · falling since last week' : '') + '</div></div>';
  html += '<div class="review-fact"><div class="label">Top three causes · the fix for each</div>';
  if (!causes.length) html += '<div class="muted">No escalations recorded yet.</div>';
  causes.forEach(function (cause) {
    html += '<div class="value">' + escape(cause.label) + ' · ' + escape(cause.count) + '</div><div class="muted">' + escape(cause.fix) + '</div>';
  });
  return html + '</div></div>';
}

function renderWeeklyMetrics(metrics, escape) {
  var classes = metrics.classes || [], labels = metrics.labels || {}, person = metrics.person_classes || [];
  var html = '<div class="tablebox"><table><thead><tr><th>Week</th>';
  classes.forEach(function (name) {
    html += '<th>' + escape(labels[name] || name) + (person.indexOf(name) >= 0 ? ' (person)' : '') + '</th>';
  });
  html += '<th>Total</th><th>Without a person</th></tr></thead><tbody>';
  (metrics.weeks || []).forEach(function (week) {
    html += '<tr><td class="mono">' + escape(week.week_start) + '</td>';
    classes.forEach(function (name) { html += '<td>' + escape((week.by_class || {})[name] || 0) + '</td>'; });
    html += '<td>' + escape(week.total) + '</td><td>' +
      (week.share_without_person_pct == null ? '—' : escape(week.share_without_person_pct) + '%') + '</td></tr>';
  });
  return html + '</tbody></table></div>';
}

function projectRows(weeks) {
  var rows = [];
  weeks.forEach(function (week) {
    Object.keys(week.by_project || {}).sort().forEach(function (project) {
      rows.push({ week: week.week_start, project: project, counts: week.by_project[project] || {} });
    });
  });
  return rows;
}

function renderProjectMetrics(metrics, escape) {
  var rows = projectRows(metrics.weeks || []), classes = metrics.classes || [];
  if (!rows.length) return '';
  var html = '<div class="tablebox"><table><thead><tr><th>Week</th><th>Project</th>';
  classes.forEach(function (name) { html += '<th>' + escape((metrics.labels || {})[name] || name) + '</th>'; });
  html += '</tr></thead><tbody>';
  rows.forEach(function (row) {
    html += '<tr><td class="mono">' + escape(row.week) + '</td><td class="mono">' + escape(row.project) + '</td>';
    classes.forEach(function (name) { html += '<td>' + escape(row.counts[name] || 0) + '</td>'; });
    html += '</tr>';
  });
  return html + '</tbody></table></div>';
}

function renderMetrics(metrics, escape) {
  return renderMetricSummary(metrics, escape) + renderWeeklyMetrics(metrics, escape) + renderProjectMetrics(metrics, escape);
}

function renderQuestionOptions(decision, saving, escape) {
  var html = '';
  (decision.options || []).forEach(function (option) {
    var recommended = option === decision.recommended;
    html += '<button class="btn sm' + (recommended ? ' primary' : '') + '" data-act="esc-answer" data-id="' + escape(decision.id) +
      '" data-choice="' + escape(option) + '"' + (saving ? ' disabled' : '') + '>' + escape(option) +
      (recommended ? ' (recommended)' : '') + '</button> ';
  });
  return html;
}

function renderWaitingQuestion(decision, saving, writer, escape) {
  var html = '<div class="review-fact"><div class="label mono">D' + escape(decision.id) + ' · ' +
    escape((decision.waiting_tickets || []).join(', ')) + '</div><div class="value">' + escape(decision.title) +
    '</div><div class="muted">' + escape(decision.question) + '</div>';
  if (writer && decision.jira_answer_required) {
    html += '<div class="muted">Answer in Jira with a comment after the question, then move the ticket to To Do.</div>';
  } else if (writer) {
    html += renderQuestionOptions(decision, saving, escape);
  } else {
    html += '<div class="muted">Options: ' + escape((decision.options || []).join(' · ')) + ' · Recommended: ' +
      escape(decision.recommended) + '</div>';
  }
  return html + '</div>';
}

function renderWaitingQuestions(register, saving, writer, escape) {
  var asked = register.asked || [], html = '<h3>Waiting for the owner</h3>';
  if (!asked.length) html += '<div class="muted">No open question. A new kind of decision is asked once, with options and a recommended default.</div>';
  asked.forEach(function (decision) { html += renderWaitingQuestion(decision, saving, writer, escape); });
  return html;
}

function renderResuming(register, escape) {
  var resuming = register.resuming || [];
  if (!resuming.length) return '';
  var html = '<h3>Applying saved owner decisions</h3><div class="muted">Jira updates retry automatically; the saved owner choice is retained.</div>';
  resuming.forEach(function (decision) {
    html += '<div class="review-fact"><div class="label mono">D' + escape(decision.id) + ' · ' +
      escape((decision.resuming_tickets || []).join(', ')) + '</div><div class="value">' + escape(decision.title) +
      '</div><div class="muted">Saved answer: ' + escape(decision.decision) + '</div></div>';
  });
  return html;
}

function renderDecisionRows(decisions, writer, escape) {
  var html = '';
  if (!decisions.length) html = '<tr><td colspan="5" class="empty">No decision recorded.</td></tr>';
  decisions.forEach(function (decision) {
    html += '<tr><td class="mono">D' + escape(decision.id) + '</td><td><b>' + escape(decision.title) + '</b><div class="small">' +
      escape(decision.decision) + '</div></td><td>' + escape(decision.cited_count) + '×</td><td>' +
      escape(decision.decided_by || '') + '</td><td>' + (writer ? '<button class="btn sm" data-act="esc-retire" data-id="' +
      escape(decision.id) + '">Retire</button>' : '') + '</td></tr>';
  });
  return html;
}

function renderRecordForm(saving, escape) {
  return '<div class="review-fact"><div class="label">Record a decision</div><input data-esc-title placeholder="Title" aria-label="Decision title" style="width:30%;min-height:36px"> <input data-esc-decision placeholder="The decision" aria-label="The decision" style="width:40%;min-height:36px"> <input data-esc-keywords placeholder="Keywords that match the question (two or more, comma separated)" aria-label="Keywords" style="width:25%;min-height:36px"> <button class="btn sm primary" data-act="esc-add"' + (saving ? ' disabled' : '') + '>Add</button></div>';
}

function renderRegister(register, saving, writer, escape) {
  var decisions = register.decisions || [];
  var html = renderResuming(register, escape) + '<h3>Owner decision register</h3><div class="tablebox"><table><thead><tr><th>#</th><th>Decision</th><th>Applied</th><th>By</th><th></th></tr></thead><tbody>';
  html += renderDecisionRows(decisions, writer, escape) + '</tbody></table></div>';
  if (writer) html += renderRecordForm(saving, escape);
  return html;
}

function renderAmendments(metrics, escape) {
  var amendments = metrics.amendments || [], html = '<h3>Ticket amendments for review</h3><div class="muted">Amendments never block; they are listed here and in the weekly report.</div>';
  if (!amendments.length) html += '<div class="muted">No criterion was amended.</div>';
  amendments.forEach(function (amendment) {
    html += '<div class="review-fact"><b class="mono">' + escape(amendment.ticket_key) + '</b> · ' + escape(amendment.title) +
      '<div class="muted">' + escape(amendment.decision) + '</div></div>';
  });
  return html;
}

window.AIFlowOwnerEscalations = {
  render: function (ui) {
    var state = ui.state, escape = ui.escape;
    if (!ui.reader()) return '';
    if (!state.esc.data && !state.esc.busy) ui.load(false);
    var html = panelHeader(state, escape);
    if (!state.esc.data) return html + renderUnavailable(state.esc);
    var metrics = state.esc.data.metrics, register = state.esc.data.register;
    var writer = ui.can('console.write');
    return html + renderMetrics(metrics, escape) + renderWaitingQuestions(register, state.esc.saving, writer, escape) +
      renderRegister(register, state.esc.saving, writer, escape) + renderAmendments(metrics, escape) + '</section>';
  }
};
})();
