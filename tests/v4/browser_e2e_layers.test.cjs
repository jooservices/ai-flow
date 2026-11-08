const assert = require('node:assert/strict');
const { test } = require('node:test');
const { runStep, runSuite, runScenario } = require('./browser_e2e/layers.cjs');
const steps = require('./browser_e2e/log_viewer_steps.cjs');
const { redactedLogRead, emptyLogRead } = require('./browser_e2e/log_viewer_suites.cjs');

function fakePage({ logText = '', hidden = false, content = '' } = {}) {
  const calls = [];
  const page = {
    async goto(url) {
      calls.push(['goto', url]);
    },
    locator(selector, options) {
      calls.push(['locator', selector, options]);
      const locator = {
        first() {
          calls.push(['first']);
          return locator;
        },
        getByRole(role, options) {
          calls.push(['getByRole', role, options]);
          return locator;
        },
        async waitFor(options) {
          calls.push(['waitFor', options]);
        },
        async click() {
          calls.push(['click']);
        },
        async innerText() {
          calls.push(['innerText']);
          return logText;
        },
        async isHidden() {
          calls.push(['isHidden']);
          return hidden;
        },
      };
      return locator;
    },
    async content() {
      calls.push(['content']);
      return content;
    },
  };
  return { page, calls };
}

test('runStep invokes one step with its context', async () => {
  const calls = [];
  const context = {};
  await runStep({ run: async (value) => calls.push(value) }, context);
  assert.deepEqual(calls, [context]);
});

test('runSuite runs steps in declaration order', async () => {
  const calls = [];
  const context = {};
  const suite = { steps: ['first', 'second'].map((name) => ({ run: async () => calls.push(name) })) };
  await runSuite(suite, context);
  assert.deepEqual(calls, ['first', 'second']);
});

test('runScenario runs suites in declaration order', async () => {
  const calls = [];
  const context = {};
  const scenario = { suites: ['first', 'second'].map((name) => ({
    steps: [{ run: async () => calls.push(name) }],
  })) };
  await runScenario(scenario, context);
  assert.deepEqual(calls, ['first', 'second']);
});

test('both log viewer suites reuse the same navigation, source and read steps', () => {
  for (const sharedStep of [steps.openLogViewer, steps.waitForLogSource, steps.requestLogRead]) {
    assert.ok(redactedLogRead.steps.includes(sharedStep));
    assert.ok(emptyLogRead.steps.includes(sharedStep));
  }
});

test('openLogViewer navigates to the log viewer', async () => {
  const { page, calls } = fakePage();
  await steps.openLogViewer.run({ page, config: { baseUrl: 'http://test' } });
  assert.deepEqual(calls, [['goto', 'http://test/logs']]);
});

test('waitForLogSource waits for the configured source', async () => {
  const { page, calls } = fakePage();
  await steps.waitForLogSource.run({ page, config: { firstSource: 'test source' } });
  assert.deepEqual(calls, [
    ['locator', '.source', { hasText: 'test source' }], ['first'], ['waitFor', { timeout: 15000 }],
  ]);
});

test('requestLogRead clicks the source read action', async () => {
  const { page, calls } = fakePage();
  await steps.requestLogRead.run({ page, config: { firstSource: 'test source' } });
  assert.deepEqual(calls, [
    ['locator', '.source', { hasText: 'test source' }], ['first'],
    ['getByRole', 'button', { name: 'Read logs' }], ['click'],
  ]);
});

test('waitForLogLines waits for visible lines', async () => {
  const { page, calls } = fakePage();
  await steps.waitForLogLines.run({ page });
  assert.deepEqual(calls, [['locator', '#lines', undefined], ['waitFor', { state: 'visible', timeout: 20000 }]]);
});

test('readLogLines stores the displayed lines', async () => {
  const { page, calls } = fakePage({ logText: 'server started' });
  const context = { page };
  await steps.readLogLines.run(context);
  assert.equal(context.logText, 'server started');
  assert.deepEqual(calls, [['locator', '#lines', undefined], ['innerText']]);
});

test('assertLogLinesVisible accepts the expected log line', async () => {
  await steps.assertLogLinesVisible.run({ logText: 'server started' });
});

test('assertRedactionMarkerVisible accepts the redaction marker', async () => {
  await steps.assertRedactionMarkerVisible.run({ logText: '[REDACTED]' });
});

test('assertSecretHidden accepts a redacted secret', async () => {
  await steps.assertSecretHidden.run({ logText: 'password=[REDACTED]', config: { secret: 'sample-secret' } });
});

test('waitForRecentLogStatus waits for the populated result status', async () => {
  const { page, calls } = fakePage();
  await steps.waitForRecentLogStatus.run({ page });
  assert.deepEqual(calls, [
    ['locator', '#lines-status', { hasText: 'recent log lines' }], ['waitFor', { timeout: 5000 }],
  ]);
});

test('waitForAnotherLogRead waits for an enabled source action', async () => {
  const { page, calls } = fakePage();
  await steps.waitForAnotherLogRead.run({ page });
  assert.deepEqual(calls, [
    ['locator', '.source button:not([disabled])', undefined], ['first'], ['waitFor', { timeout: 5000 }],
  ]);
});

test('waitForEmptyLogStatus waits for the empty result status', async () => {
  const { page, calls } = fakePage();
  await steps.waitForEmptyLogStatus.run({ page });
  assert.deepEqual(calls, [
    ['locator', '#lines-status', { hasText: 'No recent log lines for this source.' }],
    ['waitFor', { timeout: 20000 }],
  ]);
});

test('assertEmptyLogBlockHidden accepts a hidden block', async () => {
  const { page, calls } = fakePage({ hidden: true });
  await steps.assertEmptyLogBlockHidden.run({ page });
  assert.deepEqual(calls, [['locator', '#lines', undefined], ['isHidden']]);
});

test('assertNoTraceback accepts a page without traceback text', async () => {
  const { page, calls } = fakePage({ content: 'log viewer ready' });
  await steps.assertNoTraceback.run({ page });
  assert.deepEqual(calls, [['content']]);
});
