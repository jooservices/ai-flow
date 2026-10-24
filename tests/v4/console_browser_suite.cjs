/* Browser journeys for JAIF-252. The Python harness starts an isolated v4 control plane and supplies sample data. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(process.env.AIFLOW4_CONSOLE_BROWSER_MODULE);

const config = JSON.parse(fs.readFileSync(process.env.AIFLOW4_CONSOLE_BROWSER_CONFIG, 'utf8'));
const traceDir = process.env.AIFLOW4_PLAYWRIGHT_TRACE_DIR;
const screens = [
  'Dashboard', 'Work board', 'Delivery', 'SA', 'Team Lead', 'Business', 'Human review', 'Stakeholder',
  'Machines and AI', 'Users and roles', 'Knowledge', 'Backup & Restore', 'Integrations', 'Projects', 'Licence',
  'Notifications', 'Support'
];
const viewPath = {
  Dashboard: '/cc', 'Work board': '/work', Delivery: '/delivery', SA: '/sa', 'Team Lead': '/lead',
  Business: '/business', 'Human review': '/review', Stakeholder: '/stakeholder', 'Machines and AI': '/machines',
  'Users and roles': '/users', Knowledge: '/knowledge', 'Backup & Restore': '/backup', Integrations: '/integrations',
  Projects: '/projects', Licence: '/licensing', Notifications: '/notifications', Support: '/support'
};
const landingPath = {
  product_owner: '/cc', delivery: '/delivery', team_lead: '/lead', sa: '/sa', ba: '/business',
  business: '/business', reviewer: '/review', stakeholder: '/stakeholder'
};

function safeFile(value) {
  return String(value).toLowerCase().replace(/[^a-z0-9-]+/g, '-').replace(/^-|-$/g, '');
}

async function traceOnFailure(context, label, run) {
  await context.tracing.start({ screenshots: false, snapshots: false, sources: false, title: label });
  let failed = false;
  try {
    await run();
  } catch (error) {
    failed = true;
    const trace = path.join(traceDir, `${safeFile(label)}.zip`);
    await context.tracing.stop({ path: trace });
    throw error;
  } finally {
    if (!failed) await context.tracing.stop();
  }
}

async function auditScreen(page, label) {
  const problems = await page.evaluate(() => {
    const visible = (element) => !!(element.getClientRects().length && getComputedStyle(element).visibility !== 'hidden');
    const selectedTab = document.querySelector('[aria-label="Console views"] [role="tab"][aria-selected="true"]');
    const panel = selectedTab && document.getElementById(selectedTab.getAttribute('aria-controls'));
    const issues = [];
    if (!document.querySelector('main')) issues.push('missing main landmark');
    if (!document.querySelector('header')) issues.push('missing header landmark');
    if (!document.querySelector('footer')) issues.push('missing footer landmark');
    if (!document.querySelector('[role="tablist"][aria-label], nav[aria-label]')) issues.push('missing named navigation landmark');
    if (!panel) issues.push('missing visible screen panel');

    const accessibleName = (element) => {
      const labelledBy = element.getAttribute('aria-labelledby');
      const labelledText = labelledBy && labelledBy.split(/\s+/).map((id) => document.getElementById(id)?.textContent || '').join(' ').trim();
      const labels = element.labels ? Array.from(element.labels).map((item) => item.textContent || '').join(' ').trim() : '';
      return element.getAttribute('aria-label') || labelledText || labels || element.getAttribute('title') ||
        element.getAttribute('placeholder') || (element.innerText || element.textContent || '').trim();
    };
    for (const control of Array.from(document.querySelectorAll('input:not([type="hidden"]),select,textarea')).filter(visible)) {
      if (!accessibleName(control)) issues.push(`unlabelled ${control.tagName.toLowerCase()} control`);
    }
    for (const control of Array.from(document.querySelectorAll('button,a[href],[role="button"],[role="link"]')).filter(visible)) {
      if (!accessibleName(control)) issues.push(`unnamed ${control.tagName.toLowerCase()} action`);
    }
    const keyboardTargets = panel ? Array.from(panel.querySelectorAll('button,a[href],input:not([type="hidden"]),select,textarea,[tabindex]'))
      .filter((element) => visible(element) && !element.disabled && element.tabIndex >= 0) : [];
    if (!keyboardTargets.length) issues.push('no screen control is reachable by keyboard');
    return issues;
  });
  assert.deepEqual(problems, [], `${label} accessibility audit: ${problems.join('; ')}`);

  // Start at the active tab and use only Tab presses until focus enters this screen's panel.
  const tab = page.getByRole('tab', { name: label, exact: true });
  await tab.focus();
  let enteredPanel = false;
  for (let index = 0; index < 40; index += 1) {
    await page.keyboard.press('Tab');
    enteredPanel = await page.evaluate(() => {
      const selectedTab = document.querySelector('[aria-label="Console views"] [role="tab"][aria-selected="true"]');
      const panel = selectedTab && document.getElementById(selectedTab.getAttribute('aria-controls'));
      return !!panel && panel.contains(document.activeElement);
    });
    if (enteredPanel) break;
  }
  assert.ok(enteredPanel, `${label} cannot be reached by keyboard tab order`);
}

async function signInInPage(page, person, mode) {
  const dimensions = mode === 'phone' ? { width: 390, height: 844 } : { width: 1365, height: 900 };
  await page.goto(`${config.baseUrl}/login?next=%2Fcc`, { waitUntil: 'domcontentloaded' });
  await page.getByRole('heading', { name: 'AI Flow Command Center' }).waitFor({ state: 'visible' });
  await auditLogin(page);
  await page.getByLabel('Email').fill(person.email);
  await page.getByLabel('Password').fill(person.password);
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await page.waitForURL('**/cc');
  await page.getByRole('heading', { name: 'Dashboard', exact: true }).waitFor({ state: 'visible', timeout: 15000 });
  await page.waitForFunction(() => !document.documentElement.classList.contains('role-loading'));
  const actualScheme = await page.evaluate(() => matchMedia('(prefers-color-scheme: dark)').matches);
  assert.equal(actualScheme, mode === 'dark', `${mode} color scheme was not applied`);
  assert.equal(page.viewportSize().width, dimensions.width);
}

async function auditLogin(page) {
  const issues = await page.evaluate(() => {
    const form = document.querySelector('form');
    const controls = Array.from(document.querySelectorAll('input,button'));
    return [
      !document.querySelector('main') && 'missing main landmark',
      !form && 'missing sign-in form',
      ...controls.filter((element) => element.getClientRects().length &&
        !(element.getAttribute('aria-label') || element.labels?.length || element.innerText?.trim() || element.textContent?.trim()))
        .map((element) => `unnamed ${element.tagName.toLowerCase()} control`)
    ].filter(Boolean);
  });
  assert.deepEqual(issues, [], `Sign-in accessibility audit: ${issues.join('; ')}`);
  await page.getByLabel('Email').focus();
  await page.keyboard.press('Tab');
  assert.equal(await page.evaluate(() => document.activeElement.type), 'password', 'Sign-in password is not keyboard reachable');
}

async function visitScreens(page, expected) {
  const expectedSet = [...expected].sort();
  const visible = (await page.getByRole('tablist', { name: 'Console views' }).getByRole('tab').allInnerTexts()).map((value) => value.trim()).sort();
  assert.deepEqual(visible, expectedSet, 'role navigation exposes views outside the assigned role or hides an allowed view');
  for (const label of expected) {
    const tab = page.getByRole('tab', { name: label, exact: true });
    await tab.click();
    await tab.waitFor({ state: 'visible' });
    await page.waitForFunction((name) => {
      const selected = Array.from(document.querySelectorAll('[aria-label="Console views"] [role="tab"]')).find((item) => item.getAttribute('aria-selected') === 'true');
      const panel = selected && document.getElementById(selected.getAttribute('aria-controls'));
      return selected?.innerText.trim() === name && !!panel && !!panel.textContent.trim();
    }, label, { timeout: 12000 });
    await auditScreen(page, label);
  }
}

async function exerciseWorkAndSearch(page) {
  await page.getByRole('tab', { name: 'Work board', exact: true }).click();
  const workPanel = page.getByRole('tabpanel', { name: 'Work board' });
  const projectPicker = page.getByRole('combobox', { name: 'Choose a project' });
  await projectPicker.fill(config.project);
  const projectList = page.getByRole('listbox', { name: 'Projects' });
  const projectOption = projectList.getByRole('option', { name: new RegExp(config.project) });
  await projectOption.waitFor({ state: 'visible' });
  await projectOption.click();
  await page.getByRole('button', { name: `Star ${config.project}`, exact: true }).click();
  const unstar = page.getByRole('button', { name: `Unstar ${config.project}`, exact: true });
  await unstar.waitFor({ state: 'visible' });
  assert.equal(await unstar.getAttribute('aria-pressed'), 'true');
  await projectPicker.fill(config.project);
  await page.getByText('Starred projects', { exact: true }).waitFor({ state: 'visible' });
  await projectList.getByRole('option', { name: new RegExp(config.project) }).waitFor({ state: 'visible' });
  await projectList.getByRole('option', { name: new RegExp(config.project) }).click();

  const card = (key) => page.getByRole('button', { name: new RegExp(key) });
  const expectVisible = async (shown, hidden, why) => {
    for (const key of shown) await card(key).first().waitFor({ state: 'visible' });
    for (const key of hidden) {
      await card(key).first().waitFor({ state: 'hidden' });
      assert.equal(await card(key).count(), 0, `${key} should be hidden by the ${why} filter`);
    }
  };
  // TESTWP-980 is a ready Bug, TESTWP-981 a ready Story, TESTWP-982 a Task that is not ready for AI.
  await expectVisible(['TESTWP-980', 'TESTWP-981', 'TESTWP-982'], [], 'unfiltered board');
  await page.getByRole('button', { name: 'Bug', exact: true }).click();
  await expectVisible(['TESTWP-980'], ['TESTWP-981', 'TESTWP-982'], 'Bug type');
  await page.getByRole('button', { name: 'All types', exact: true }).click();
  await workPanel.getByLabel('Filter by AI state').selectOption('ready');
  assert.equal(await workPanel.getByLabel('Filter by AI state').inputValue(), 'ready');
  await expectVisible(['TESTWP-980', 'TESTWP-981'], ['TESTWP-982'], 'Ready for AI');
  await workPanel.getByLabel('Filter by AI state').selectOption('all');
  await expectVisible(['TESTWP-982'], [], 'All AI states');
  await workPanel.getByLabel('Search tickets').fill('TESTWP-980');
  await expectVisible(['TESTWP-980'], ['TESTWP-981', 'TESTWP-982'], 'search');
  await workPanel.getByLabel('Search tickets').fill('');
  await expectVisible(['TESTWP-980', 'TESTWP-981', 'TESTWP-982'], [], 'cleared search');

  await page.getByRole('tab', { name: 'Dashboard', exact: true }).click();
  for (const heading of ['Work', 'Flow at a glance', 'Projects', 'What is being built', 'Waiting for you']) {
    await page.getByRole('heading', { name: heading, exact: true }).waitFor({ state: 'visible' });
  }
  await page.getByRole('tab', { name: 'AI and machines', exact: true }).click();
  for (const heading of ['AI and machines', 'AI usage', 'Tokens and cost', 'Quota by AI']) {
    await page.getByRole('heading', { name: heading, exact: true }).waitFor({ state: 'visible' });
  }
  await page.getByRole('tab', { name: 'Work', exact: true }).click();
  await page.getByRole('button', { name: 'Search views, projects, and tickets (Ctrl K)', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Jump to a view, project, or ticket' });
  const search = page.getByRole('searchbox', { name: 'Search views, projects, and tickets' });
  await search.fill(config.browserCommandKey);
  const ticketResult = page.getByRole('option', { name: new RegExp(config.browserCommandKey) });
  await ticketResult.waitFor({ state: 'visible' });
  await ticketResult.click();
  await dialog.waitFor({ state: 'hidden' });
  await page.getByRole('tab', { name: 'Work board', exact: true }).waitFor({ state: 'visible' });
  await page.getByRole('button', { name: 'Close details', exact: true }).click();
}

async function exerciseReviews(page, mode) {
  await page.getByRole('tab', { name: 'Human review', exact: true }).click();
  await page.getByRole('heading', { name: 'Human review queue', exact: true }).waitFor({ state: 'visible' });
  const reviewerToken = page.getByLabel('Reviewer access token', { exact: true });
  if (await reviewerToken.count()) await reviewerToken.fill(config.reviewerToken);
  const reviewTraffic = [];
  page.on('request', (item) => {
    const url = new URL(item.url());
    if (url.pathname.startsWith('/v1/reviews/')) reviewTraffic.push(`${item.method()} ${url.pathname}`);
  });
  const pageErrors = [];
  page.on('pageerror', (error) => pageErrors.push(`${error.name}: ${error.message}`));
  const actions = [
    ['approve', 'Approve and merge', ''],
    ['reject', 'Reject', 'The browser fixture rejects this sample change.'],
    ['rework', 'Send for rework', 'The browser fixture asks for one more test.'],
    ['ask-ai', 'Ask AI a question', 'The browser fixture asks how the change handles retries.']
  ];
  for (const [action, buttonName, note] of actions) {
    const key = config.actionKeys[mode][action];
    const card = page.getByRole('article').filter({ hasText: key });
    await card.getByRole('heading', { name: new RegExp(key) }).waitFor({ state: 'visible' });
    if (note) {
      const noteField = card.getByRole('textbox', { name: `Reviewer note for ${key}` });
      await noteField.fill(note);
      if (action === 'ask-ai') {
        const questionField = await noteField.elementHandle();
        const stateRefresh = page.waitForResponse((item) => item.request().method() === 'GET' &&
          new URL(item.url()).pathname === '/v1/state', { timeout: 12000 });
        await stateRefresh;
        await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
        const questionState = await questionField.evaluate((field) => ({
          connected: field.isConnected,
          focused: document.activeElement === field,
          value: field.value
        }));
        assert.deepEqual(questionState, { connected: true, focused: true, value: note },
          'state polling replaced or blurred the focused review question');
      }
      if (action === 'reject') {
        const refreshResponse = page.waitForResponse((item) => item.request().method() === 'GET' &&
          new URL(item.url()).pathname === '/v1/reviews');
        await page.getByRole('button', { name: 'Refresh', exact: true }).click();
        await refreshResponse;
        assert.equal(await noteField.inputValue(), note, 'Human review refresh discarded the typed note');
      }
    }
    const endpoint = action === 'ask-ai' ? 'ask-ai' : action === 'rework' ? 'rework' : action;
    const actionButton = card.getByRole('button', { name: buttonName, exact: true });
    if (mode === 'phone' && action === 'ask-ai') {
      assert.equal(await actionButton.evaluate((button) => {
        const rect = button.getBoundingClientRect();
        const hit = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
        return hit === button || button.contains(hit);
      }), true, 'a toast is covering the phone review action');
    }
    const response = page.waitForResponse((item) => item.request().method() === 'POST' &&
      new URL(item.url()).pathname === `/v1/reviews/${key}/${endpoint}`, { timeout: 5000 }).catch(async (error) => {
        const details = await page.evaluate((reviewKey) => ({
          toastTitles: Array.from(document.querySelectorAll('#toasts .t-title')).map((item) => item.textContent || ''),
          button: Array.from(document.querySelectorAll('[data-review-card]')).filter((card) =>
            card.getAttribute('data-key') === reviewKey).flatMap((card) =>
            Array.from(card.querySelectorAll('button')).map((button) => ({
              action: button.getAttribute('data-act'), disabled: button.disabled
            }))),
          noteLength: Array.from(document.querySelectorAll('[data-review-card]')).find((card) =>
            card.getAttribute('data-key') === reviewKey)?.querySelector('[data-review-note]')?.value.length ?? -1
        }), key);
        throw new Error(`${buttonName} sent no review request. Observed: ${reviewTraffic.join(', ') || 'none'}. ` +
          `Browser errors: ${pageErrors.join(' | ') || 'none'}. UI: ${JSON.stringify(details)}. ${error.message}`);
      });
    await actionButton.click();
    const actionResponse = await response;
    const submittedRequest = actionResponse.request();
    if (note) {
      const expectedBody = action === 'reject' ? { reason: note } : action === 'rework' ? { comment: note } :
        action === 'ask-ai' ? { question: note } : {};
      assert.deepEqual(JSON.parse(submittedRequest.postData()), expectedBody,
        `${buttonName} dropped its review note for ${key}`);
    }
    assert.equal(actionResponse.status(), 200, `${buttonName} failed for ${key}`);
  }
}

async function exerciseMachineAndUserAdministration(page, mode) {
  await page.getByRole('tab', { name: 'Machines and AI', exact: true }).click();
  await page.getByRole('heading', { name: 'Machines and AI', exact: true }).waitFor({ state: 'visible' });
  await page.getByRole('button', { name: 'Add a machine', exact: true }).click();
  await page.getByLabel('Machine name').fill(`jaif252-${mode}-machine`);
  await page.getByLabel('Machine role', { exact: true }).selectOption('worker');
  await page.getByLabel('Machine project', { exact: true }).selectOption(config.project);
  const joinResponse = page.waitForResponse((item) => item.request().method() === 'POST' &&
    new URL(item.url()).pathname.endsWith('/join-command'));
  await page.getByRole('button', { name: 'Show one-time command', exact: true }).click();
  const joinResult = await joinResponse;
  const joinBody = await joinResult.json();
  assert.equal(joinResult.status(), 200, `Add machine did not create its one-time join command: ${joinBody.detail || ''}`);
  await page.getByRole('button', { name: 'Copy command', exact: true }).waitFor({ state: 'visible' });

  const addAccount = page.getByRole('button', { name: 'Add AI account', exact: true }).first();
  await addAccount.click();
  await page.getByLabel('AI provider', { exact: true }).selectOption('codex');
  await page.getByLabel('Sessions at once', { exact: true }).fill('1');
  const accountResponse = page.waitForResponse((item) => item.request().method() === 'POST' &&
    /\/v1\/machines\/[^/]+\/accounts$/.test(new URL(item.url()).pathname));
  await page.getByRole('button', { name: 'Create sign-in link and code', exact: true }).click();
  assert.equal((await accountResponse).status(), 200, 'Add AI account did not complete');

  await page.getByRole('tab', { name: 'Users and roles', exact: true }).click();
  await page.getByRole('heading', { name: 'People and roles', exact: true }).waitFor({ state: 'visible' });
  const email = `jaif252-${mode}-person@example.test`;
  const reviewerRole = page.getByRole('checkbox', { name: 'Reviewer', exact: true });
  await reviewerRole.waitFor({ state: 'visible' });
  const emailField = page.getByRole('textbox', { name: 'Email', exact: true });
  await emailField.fill(email);
  assert.equal(await emailField.inputValue(), email, 'Add person email field did not retain its value');
  await page.getByLabel('Name', { exact: true }).fill(`Browser Person ${mode}`);
  await reviewerRole.check();
  assert.equal(await reviewerRole.isChecked(), true, 'Reviewer role was not selected for the new person');
  assert.equal(await emailField.inputValue(), email, 'Add person form lost its email while selecting a role');
  const userResponse = page.waitForResponse((item) => item.request().method() === 'POST' &&
    new URL(item.url()).pathname === '/v1/console/users');
  await page.getByRole('button', { name: 'Add person', exact: true }).click();
  const createdUser = await userResponse;
  const userResult = await createdUser.json();
  assert.equal(createdUser.status(), 200, `Add person did not complete: ${JSON.stringify(userResult.detail || {})}`);
  const submitted = JSON.parse(createdUser.request().postData() || '{}');
  assert.equal(submitted.email, email, 'Add person submitted a different email');
  const submittedRoles = [].concat(submitted.roles || submitted.role || []);
  assert.deepEqual(submittedRoles, ['reviewer'], 'Add person did not submit exactly the Reviewer role');
  // The create response only returns the one-time password, so read the stored roles back as the signed-in owner.
  const listed = await (await page.request.get(`${config.baseUrl}/v1/console/users`)).json();
  const created = listed.users.find((entry) => entry.email === email);
  assert.ok(created, 'The created person is missing from the people list');
  assert.deepEqual(created.roles, ['reviewer'], 'The created person was not assigned exactly the Reviewer role');
  await page.getByText(`One-time password for ${email}`, { exact: true }).waitFor({ state: 'visible' });
  await page.getByRole('button', { name: 'Dismiss', exact: true }).click();

  await page.getByRole('tab', { name: 'Projects', exact: true }).click();
  await page.getByRole('heading', { name: 'Projects', exact: true }).waitFor({ state: 'visible' });
  await page.getByRole('tabpanel', { name: 'Projects' }).getByText(config.project, { exact: true })
    .waitFor({ state: 'visible' });
}

async function exerciseMode(browser, mode) {
  const administrator = config.roles.find((person) => person.role === 'administrator');
  const dimensions = mode === 'phone' ? { width: 390, height: 844 } : { width: 1365, height: 900 };
  const context = await browser.newContext({ viewport: dimensions, colorScheme: mode === 'dark' ? 'dark' : 'light',
    hasTouch: mode === 'phone' });
  const page = await context.newPage();
  await traceOnFailure(context, `administrator-${mode}`, async () => {
    await signInInPage(page, administrator, mode);
    await visitScreens(page, administrator.views);
    await exerciseWorkAndSearch(page);
    await exerciseReviews(page, mode);
    await exerciseMachineAndUserAdministration(page, mode);
    await page.getByRole('tab', { name: 'Dashboard', exact: true }).click();
    await page.waitForFunction(() => document.querySelector('[aria-label="Console views"] [role="tab"][aria-selected="true"]')?.innerText.trim() === 'Dashboard');
  });
  await context.close();
}

async function exerciseRoleMenus(browser) {
  const people = config.roles.filter((entry) => entry.role !== 'administrator');
  for (const mode of ['light', 'dark', 'phone']) {
    const dimensions = mode === 'phone' ? { width: 390, height: 844 } : { width: 1280, height: 800 };
    for (const person of people) {
      const context = await browser.newContext({
        viewport: dimensions,
        colorScheme: mode === 'dark' ? 'dark' : 'light'
      });
      const page = await context.newPage();
      await traceOnFailure(context, `role-${person.role}-${mode}`, async () => {
        await page.goto(`${config.baseUrl}/login`, { waitUntil: 'domcontentloaded' });
        await auditLogin(page);
        await page.getByLabel('Email').fill(person.email);
        await page.getByLabel('Password').fill(person.password);
        await page.getByRole('button', { name: 'Sign in', exact: true }).click();
        await page.waitForURL(`**${landingPath[person.role]}`);
        await page.waitForFunction(() => !document.documentElement.classList.contains('role-loading'));
        const actualScheme = await page.evaluate(() => matchMedia('(prefers-color-scheme: dark)').matches);
        assert.equal(actualScheme, mode === 'dark', `${person.role} ${mode} color scheme was not applied`);
        assert.equal(page.viewportSize().width, dimensions.width, `${person.role} ${mode} viewport was not applied`);
        await visitScreens(page, person.views);
        for (const forbidden of screens.filter((label) => !person.views.includes(label))) {
          await page.goto(`${config.baseUrl}${viewPath[forbidden]}`, { waitUntil: 'domcontentloaded' });
          await page.getByText('This view is not available for your role.', { exact: true }).waitFor({ state: 'visible' });
        }
      });
      await context.close();
    }
  }
}

(async () => {
  fs.mkdirSync(traceDir, { recursive: true });
  const browser = await chromium.launch({ headless: true });
  try {
    for (const mode of ['light', 'dark', 'phone']) await exerciseMode(browser, mode);
    await exerciseRoleMenus(browser);
  } finally {
    await browser.close();
  }
  process.stdout.write('JAIF-252 browser suite passed\n');
})().catch((error) => {
  // Error messages contain test labels and assertion details only; credentials and response bodies are never logged.
  process.stderr.write(`${error.stack || String(error)}\n`);
  process.exitCode = 1;
});
