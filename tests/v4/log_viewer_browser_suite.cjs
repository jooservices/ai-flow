/* Browser journey for the JAIF-175 log viewer. The Python harness runs the control API and a fake node agent. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const { chromium } = require(process.env.AIFLOW4_LOG_BROWSER_MODULE);

const config = JSON.parse(fs.readFileSync(process.env.AIFLOW4_LOG_BROWSER_CONFIG, 'utf8'));

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const context = await browser.newContext();
    await context.addCookies([{ name: config.cookieName, value: config.cookie, url: config.baseUrl }]);
    const page = await context.newPage();
    await page.goto(config.baseUrl + '/logs');
    const row = () => page.locator('.source', { hasText: config.firstSource }).first();
    await row().waitFor({ timeout: 15000 });

    // First read: the fake node agent returns lines, one holding a secret that the server must have redacted.
    await row().getByRole('button', { name: 'Read logs' }).click();
    await page.locator('#lines').waitFor({ state: 'visible', timeout: 20000 });
    const text = await page.locator('#lines').innerText();
    assert.ok(text.includes('server started'), 'log lines are shown');
    assert.ok(text.includes('[REDACTED]'), 'secret marker is shown');
    assert.ok(!text.includes(config.secret), 'secret value is not shown');
    await page.locator('#lines-status', { hasText: 'recent log lines' }).waitFor({ timeout: 5000 });
    await page.locator('.source button:not([disabled])').first().waitFor({ timeout: 5000 });

    // Second read: the fake node agent returns no lines; the screen says so.
    await row().getByRole('button', { name: 'Read logs' }).click();
    await page.locator('#lines-status', { hasText: 'No recent log lines for this source.' }).waitFor({ timeout: 20000 });
    assert.equal(await page.locator('#lines').isHidden(), true, 'empty result shows no log block');
    assert.ok(!(await page.content()).includes('Traceback'));
    console.log('JAIF-175 log viewer browser suite passed');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
