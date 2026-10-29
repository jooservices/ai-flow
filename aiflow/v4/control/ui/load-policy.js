/* Load-aware offer status and owner controls for the Machines and AI screen. */
function loadPolicySummary(node) {
  if (!node) return '';
  var loadState = node.load_state || {}, hostLoadState = node.host_load_state || {}, html = '';
  if (loadState.mode === 'held') html += '<span class="offnote" style="color:var(--crit)">' + esc(loadState.reason || 'Held back by machine load') + ' · observed ' + esc(when(loadState.measured_at)) + '</span>';
  else if (loadState.mode === 'throttled') html += '<span class="offnote" style="color:var(--warn)">' + esc(loadState.reason || 'Reduced offers due to machine load') + ' · observed ' + esc(when(loadState.measured_at)) + '</span>';
  if (hostLoadState.mode === 'held') html += '<span class="offnote" style="color:var(--crit)">' + esc(hostLoadState.reason || 'Held back by host load') + ' · observed ' + esc(when(hostLoadState.measured_at)) + '</span>';
  return html;
}
var loadPolicyInDrawer = false;
var nodeMetricsBeforeLoadPolicy = nodeMetrics;
nodeMetrics = function (node) {
  return nodeMetricsBeforeLoadPolicy(node) + (loadPolicyInDrawer ? '' : loadPolicySummary(node));
};

function loadPolicyDetails(node) {
  if (!node) return '';
  var metrics = node.metrics || {}, loadState = node.load_state || {}, hostLoadState = node.host_load_state || {};
  var machineLimits = node.load_limits || { cpu_load_per_cpu: 0.85, memory_used_pct: 85, disk_free_pct: 10 };
  var hostLimits = node.host_load_limits || { cpu_load_per_cpu: 0.85 };
  var html = '<div class="dsec"><h4>Load-aware offers</h4><div class="small">Held-back offers resume below the limit by a fixed recovery margin: 0.05 load per CPU or 5 percentage points. Existing runs continue.</div>';
  if (loadState.mode === 'held') html += '<div class="offnote" style="color:var(--crit);margin-top:6px">' + esc(loadState.reason || 'Machine held back') + ' · observed ' + esc(when(loadState.measured_at)) + '</div>';
  else if (loadState.mode === 'throttled') html += '<div class="offnote" style="color:var(--warn);margin-top:6px">' + esc(loadState.reason || 'Offers reduced by half') + ' · observed ' + esc(when(loadState.measured_at)) + '</div>';
  if (hostLoadState.mode === 'held') html += '<div class="offnote" style="color:var(--crit);margin-top:6px">' + esc(hostLoadState.reason || 'Host held back') + ' · observed ' + esc(when(hostLoadState.measured_at)) + '</div>';
  if (consoleCan('nodes.manage')) {
    html += '<div class="flexrow" style="margin-top:8px"><label>CPU load / CPU <input id="load-cpu-limit" type="number" min="0.01" max="8" step="0.01" value="' + esc(machineLimits.cpu_load_per_cpu) + '" style="width:92px"></label>';
    html += '<label>Memory used % <input id="load-memory-limit" type="number" min="0.01" max="100" step="0.1" value="' + esc(machineLimits.memory_used_pct) + '" style="width:92px"></label>';
    html += '<label>Free disk % <input id="load-disk-limit" type="number" min="0" max="99.9" step="0.1" value="' + esc(machineLimits.disk_free_pct) + '" style="width:92px"></label>';
    html += '<label>Host CPU load / CPU <input id="load-host-cpu-limit" type="number" min="0.01" max="8" step="0.01" value="' + esc(hostLimits.cpu_load_per_cpu) + '"' + (metrics.hostname ? '' : ' disabled title="Set after this machine reports its hostname"') + ' style="width:92px"></label>';
    html += '<button class="btn sm primary" data-act="node-load-save" data-node="' + esc(node.name) + '">Save load limits</button></div>';
    if (!metrics.hostname) html += '<div class="small">Host limits become editable after the next machine heartbeat reports its hostname.</div>';
  }
  return html + '</div>';
}
var drawerNodeBeforeLoadPolicy = drawerNode;
drawerNode = function (name) {
  loadPolicyInDrawer = true;
  var html;
  try { html = drawerNodeBeforeLoadPolicy(name); }
  finally { loadPolicyInDrawer = false; }
  var node = (D().nodes || []).find(function (item) { return item.name === name; });
  return html + loadPolicyDetails(node);
};

function saveMachineLoadLimits(name) {
  var payload = { cpu_load_per_cpu: Number($('#load-cpu-limit').value),
    memory_used_pct: Number($('#load-memory-limit').value), disk_free_pct: Number($('#load-disk-limit').value),
    host_cpu_load_per_cpu: $('#load-host-cpu-limit').value === '' ? null : Number($('#load-host-cpu-limit').value) };
  machineRequest('/v1/machines/' + encodeURIComponent(name) + '/load-limits', {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload)
  }).then(function () { toast('Load limits saved', name + ' will use the new machine and host limits.', 'ok'); poll(); })
    .catch(function (e) { toast('Could not save load limits', String(e.message || e), 'warn'); });
}
document.addEventListener('click', function (event) {
  var target = event.target && event.target.closest && event.target.closest('[data-act="node-load-save"]');
  if (!target) return;
  event.preventDefault();
  saveMachineLoadLimits(target.getAttribute('data-node'));
});
