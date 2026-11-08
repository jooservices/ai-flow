/* Browser scenario for the JAIF-175 log viewer. The Python harness runs the control API and a fake node agent. */
const fs = require('node:fs');
const { chromium } = require(process.env.AIFLOW4_LOG_BROWSER_MODULE);
const { runScenario } = require('./browser_e2e/layers.cjs');
const scenario = require('./browser_e2e/log_viewer_scenario.cjs');

const config = JSON.parse(fs.readFileSync(process.env.AIFLOW4_LOG_BROWSER_CONFIG, 'utf8'));

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const context = await browser.newContext();
    await context.addCookies([
      { name: config.cookieName, value: config.cookie, url: config.baseUrl },
      { name: config.csrfCookieName, value: config.csrf, url: config.baseUrl },
    ]);
    const page = await context.newPage();
    await runScenario(scenario, { page, config });
    console.log('JAIF-175 log viewer browser suite passed');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
