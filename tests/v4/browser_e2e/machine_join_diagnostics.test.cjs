const assert = require('node:assert/strict');
const { test } = require('node:test');
const {
  assertMachineJoinDiagnostics,
  isMachineJoinSubmissionResponse,
  waitForMachineJoinResponse,
} = require('./machine_join_diagnostics.cjs');

const mode = 'light';
const expected = { name: 'jaif559-light-machine', role: 'worker', project: 'TESTWP' };

function matchingRequest(overrides = {}) {
  return {
    mode,
    status: 200,
    expected,
    form: { ...expected },
    pathname: `/v1/nodes/${encodeURIComponent(expected.name)}/join-command`,
    postData: JSON.stringify(expected),
    ...overrides,
  };
}

test('machine join diagnostics accept the expected form and request', () => {
  assert.doesNotThrow(() => assertMachineJoinDiagnostics(matchingRequest()));
});

test('machine join diagnostics validate request data when no form snapshot is supplied', () => {
  assert.doesNotThrow(() => assertMachineJoinDiagnostics(matchingRequest({ form: undefined })));
});

test('machine join diagnostics identify an empty name and wrong request path safely', () => {
  const request = matchingRequest({
    status: 404,
    form: { ...expected, name: '' },
    pathname: '/v1/nodes/wrong-machine/join-command',
    postData: JSON.stringify({ ...expected, name: '' }),
  });
  assert.throws(() => assertMachineJoinDiagnostics(request), (error) => {
    assert.match(error.message, /mode light/);
    assert.match(error.message, /status 404/);
    assert.match(error.message, /form name/);
    assert.match(error.message, /request pathname/);
    assert.match(error.message, /request JSON name/);
    assert.doesNotMatch(error.message, /wrong-machine|jaif559|TESTWP/);
    return true;
  });
});

test('machine join diagnostics identify changed form and request role and project', () => {
  const wrongForm = matchingRequest({ form: { ...expected, role: 'admin', project: 'OTHER' } });
  assert.throws(() => assertMachineJoinDiagnostics(wrongForm), /form role, form project/);

  const wrongRequest = matchingRequest({
    postData: JSON.stringify({ ...expected, role: 'admin', project: 'OTHER' }),
  });
  assert.throws(() => assertMachineJoinDiagnostics(wrongRequest), /request JSON role, request JSON project/);
});

test('machine join diagnostics report a non-200 status without response content', () => {
  const request = matchingRequest({ status: 404 });
  assert.throws(() => assertMachineJoinDiagnostics(request), (error) => {
    assert.match(error.message, /mode light/);
    assert.match(error.message, /status 404/);
    assert.match(error.message, /HTTP status/);
    assert.doesNotMatch(error.message, /command|token|cookie|header|Not Found/);
    return true;
  });
});

test('machine join diagnostics keep malformed request data out of the message', () => {
  const request = matchingRequest({ mode: 'unexpected', postData: 'sensitive-command-token-cookie-header' });
  assert.throws(() => assertMachineJoinDiagnostics(request), (error) => {
    assert.match(error.message, /mode unknown/);
    assert.match(error.message, /request JSON body/);
    assert.doesNotMatch(error.message, /sensitive-command-token-cookie-header/);
    return true;
  });
});

test('machine join diagnostics reject JSON values that are not request objects', () => {
  for (const postData of ['null', '42', '[]']) {
    assert.throws(() => assertMachineJoinDiagnostics(matchingRequest({ postData })), /request JSON body/);
  }
});

test('pre-submit form diagnostics say status is unavailable when no request was sent', () => {
  const request = matchingRequest({ form: { ...expected, project: '' } });
  delete request.status;
  delete request.pathname;
  delete request.postData;
  assert.throws(() => assertMachineJoinDiagnostics(request), (error) => {
    assert.match(error.message, /mode light/);
    assert.match(error.message, /status unavailable/);
    assert.match(error.message, /form project/);
    return true;
  });
});

test('a form mismatch fails before starting the machine join response waiter', () => {
  let waiterStarted = false;
  const page = {
    url: () => 'http://console.test/machines',
    waitForResponse: () => {
      waiterStarted = true;
      return Promise.resolve();
    },
  };

  assert.throws(() => waitForMachineJoinResponse({
    page,
    mode,
    expected,
    form: { ...expected, name: '' },
  }), (error) => {
    assert.match(error.message, /mode light/);
    assert.match(error.message, /status unavailable/);
    assert.match(error.message, /form name/);
    return true;
  });
  assert.equal(waiterStarted, false);
});

test('the response capture accepts a same-origin POST with a wrong suffix for diagnosis', async () => {
  const origin = 'http://console.test';
  const response = {
    status: () => 404,
    request: () => ({
      method: () => 'POST',
      url: () => `${origin}/v1/nodes/${encodeURIComponent(expected.name)}/join-commands`,
      postData: () => JSON.stringify(expected),
    }),
  };
  let responsePredicate;
  const page = {
    url: () => `${origin}/machines`,
    waitForResponse: (predicate) => {
      responsePredicate = predicate;
      return Promise.resolve(predicate(response) ? response : null);
    },
  };

  assert.equal(isMachineJoinSubmissionResponse(response, origin), true);
  const capturedResponse = await waitForMachineJoinResponse({ page, mode, expected, form: { ...expected } });
  assert.equal(capturedResponse, response);
  assert.equal(responsePredicate(response), true);
  assert.throws(() => assertMachineJoinDiagnostics({
    mode,
    status: response.status(),
    expected,
    pathname: new URL(response.request().url()).pathname,
    postData: response.request().postData(),
  }), (error) => {
    assert.match(error.message, /mode light/);
    assert.match(error.message, /status 404/);
    assert.match(error.message, /HTTP status/);
    assert.match(error.message, /request pathname/);
    return true;
  });
});

test('the response capture ignores GET and cross-origin requests', () => {
  const origin = 'http://console.test';
  const requestResponse = (method, url) => ({ request: () => ({ method: () => method, url: () => url }) });
  assert.equal(isMachineJoinSubmissionResponse(requestResponse('GET', `${origin}/v1/nodes/x/join-command`), origin), false);
  assert.equal(isMachineJoinSubmissionResponse(requestResponse('POST', 'http://api.test/v1/nodes/x/join-command'), origin), false);
});
