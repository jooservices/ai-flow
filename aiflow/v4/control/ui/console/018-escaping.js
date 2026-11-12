function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
function safeUrl(u) { return /^https?:\/\//.test(u || '') ? u : ''; }
function safeHref(u) { u = String(u == null ? '' : u); return /^https?:\/\//i.test(u) || /^\/(?![\/\\])/.test(u) ? u : ''; }
