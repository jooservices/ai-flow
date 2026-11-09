const JOIN_FIELDS = ['name', 'role', 'project'];

function appendFieldFailures(failures, label, actual, expected) {
  for (const field of JOIN_FIELDS) {
    if (actual[field] !== expected[field]) failures.push(`${label} ${field}`);
  }
}

function isObjectRecord(value) {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function parseRequestBody(postData) {
  try {
    const body = JSON.parse(postData || '');
    return isObjectRecord(body) ? body : null;
  } catch {
    return null;
  }
}

function isMachineJoinSubmissionResponse(response, origin) {
  const request = response.request();
  return request.method() === 'POST' && new URL(request.url()).origin === origin;
}

function waitForMachineJoinResponse({ page, mode, expected, form }) {
  assertMachineJoinDiagnostics({ mode, expected, form });
  const origin = new URL(page.url()).origin;
  return page.waitForResponse((response) => isMachineJoinSubmissionResponse(response, origin));
}

function assertMachineJoinDiagnostics({ mode, status, expected, form, pathname, postData }) {
  const failures = [];
  if (form) appendFieldFailures(failures, 'form', form, expected);
  const expectedPath = `/v1/nodes/${encodeURIComponent(expected.name)}/join-command`;
  if (pathname !== undefined && pathname !== expectedPath) failures.push('request pathname');
  if (postData !== undefined) {
    const requestBody = parseRequestBody(postData);
    if (requestBody) appendFieldFailures(failures, 'request JSON', requestBody, expected);
    else failures.push('request JSON body');
  }
  if (status !== undefined && status !== 200) failures.push('HTTP status');
  if (failures.length) {
    const safeMode = ['light', 'dark', 'phone'].includes(mode) ? mode : 'unknown';
    const safeStatus = Number.isInteger(status) ? status : 'unavailable';
    throw new Error(`Machine join checks failed (mode ${safeMode}, status ${safeStatus}): ${failures.join(', ')}`);
  }
}

module.exports = {
  assertMachineJoinDiagnostics,
  isMachineJoinSubmissionResponse,
  waitForMachineJoinResponse,
};
