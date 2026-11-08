function reviewRoundsPanel() {
  var state = S.dashboardReviewRounds, data = state.data, tickets = (data && data.tickets) || [];
  var target = data ? Number(data.target_rounds || 2) : 2;
  var average = data && data.average_rounds != null ? Number(data.average_rounds).toFixed(2) : '—';
  var h = '<section class="panel" data-block="review-rounds"><div class="panel-h"><h2>AI review rounds</h2>' +
    '<span class="hint">Merged tickets · target average ' + target + ' rounds</span></div>';
  if (state.error) return h + '<div class="dashboard-empty">Review metrics unavailable: ' + esc(state.error) + '</div></section>';
  if (!data) return h + '<div class="dashboard-empty">' + (state.loading ? 'Loading review rounds…' : 'Review metrics are not available yet.') + '</div></section>';
  h += '<div class="facts"><div class="fact"><span class="fl">Average rounds</span><span class="fv mono">' + average + ' / ' + target + ' target</span></div>' +
    '<div class="fact"><span class="fl">Merged tickets</span><span class="fv mono">' + Number(data.merged_tickets || 0) + '</span></div></div>';
  if (!tickets.length) return h + '<div class="dashboard-empty">No merged tickets with completed AI reviews.</div></section>';
  h += '<div class="tablebox"><table><thead><tr><th>Ticket</th><th>Total rounds</th><th>Review</th><th>Findings</th><th>New findings · rounds 2+</th></tr></thead><tbody>';
  tickets.forEach(function (ticket) {
    var reviews = ticket.reviews || [];
    reviews.forEach(function (review, index) {
      h += '<tr>';
      if (index === 0) h += '<td rowspan="' + reviews.length + '"><button class="linkbtn mono" data-act="open-ticket" data-key="' + esc(ticket.key) + '">' + esc(ticket.key) + '</button></td><td rowspan="' + reviews.length + '">' + Number(ticket.rounds || reviews.length) + '</td>';
      h += '<td>' + Number(review.round) + '</td><td>' + Number(review.findings || 0) + '</td><td>' + (Number(review.round) > 1 ? Number(review.new_findings || 0) : '—') + '</td></tr>';
    });
  });
  return h + '</tbody></table></div></section>';
}
