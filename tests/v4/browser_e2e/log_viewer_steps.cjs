const assert = require('node:assert/strict');

const openLogViewer = {
  name: 'open log viewer',
  async run({ page, config }) {
    await page.goto(`${config.baseUrl}/logs`);
  },
};

const waitForLogSource = {
  name: 'wait for the first log source',
  async run({ page, config }) {
    await page.locator('.source', { hasText: config.firstSource }).first().waitFor({ timeout: 15000 });
  },
};

const requestLogRead = {
  name: 'request logs from the first source',
  async run({ page, config }) {
    await page.locator('.source', { hasText: config.firstSource }).first()
      .getByRole('button', { name: 'Read logs' }).click();
  },
};

const waitForLogLines = {
  name: 'wait for the log lines',
  async run({ page }) {
    await page.locator('#lines').waitFor({ state: 'visible', timeout: 20000 });
  },
};

const readLogLines = {
  name: 'read the displayed log lines',
  async run(context) {
    context.logText = await context.page.locator('#lines').innerText();
  },
};

const assertLogLinesVisible = {
  name: 'assert the log lines are shown',
  async run({ logText }) {
    assert.ok(logText.includes('server started'), 'log lines are shown');
  },
};

const assertRedactionMarkerVisible = {
  name: 'assert the redaction marker is shown',
  async run({ logText }) {
    assert.ok(logText.includes('[REDACTED]'), 'secret marker is shown');
  },
};

const assertSecretHidden = {
  name: 'assert the secret value is not shown',
  async run({ logText, config }) {
    assert.ok(!logText.includes(config.secret), 'secret value is not shown');
  },
};

const waitForRecentLogStatus = {
  name: 'wait for the recent log status',
  async run({ page }) {
    await page.locator('#lines-status', { hasText: 'recent log lines' }).waitFor({ timeout: 5000 });
  },
};

const waitForAnotherLogRead = {
  name: 'wait for a log source read button to be enabled',
  async run({ page }) {
    await page.locator('.source button:not([disabled])').first().waitFor({ timeout: 5000 });
  },
};

const waitForEmptyLogStatus = {
  name: 'wait for the empty log status',
  async run({ page }) {
    await page.locator('#lines-status', { hasText: 'No recent log lines for this source.' })
      .waitFor({ timeout: 20000 });
  },
};

const assertEmptyLogBlockHidden = {
  name: 'assert an empty result hides the log block',
  async run({ page }) {
    assert.equal(await page.locator('#lines').isHidden(), true, 'empty result shows no log block');
  },
};

const assertNoTraceback = {
  name: 'assert the page has no traceback',
  async run({ page }) {
    assert.ok(!(await page.content()).includes('Traceback'));
  },
};

module.exports = {
  openLogViewer,
  waitForLogSource,
  requestLogRead,
  waitForLogLines,
  readLogLines,
  assertLogLinesVisible,
  assertRedactionMarkerVisible,
  assertSecretHidden,
  waitForRecentLogStatus,
  waitForAnotherLogRead,
  waitForEmptyLogStatus,
  assertEmptyLogBlockHidden,
  assertNoTraceback,
};
