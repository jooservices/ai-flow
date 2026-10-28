function licensingNotice(state, escapeHtml) {
  if (state.licence_required === false) return '<div class="alert" data-tone="ok"><span class="a-detail">No licence required: no issuer public key or licence service is configured, so this installation runs without licence limits. The limits below only apply once a licence is configured.</span></div>';
  if (state.over_limit_since) return '<div class="alert" data-tone="warn"><span class="a-detail">Usage is above a plan limit since ' + escapeHtml(new Date(state.over_limit_since).toLocaleString()) + '. Existing machines, accounts and projects keep working for 14 days; creating new ones is refused. After that, new work for the extra ones stops and a critical incident is raised. Raise the limit below or remove the extra ones.</span></div>';
  return '';
}
