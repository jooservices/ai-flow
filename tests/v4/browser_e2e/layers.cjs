async function runStep(step, context) {
  await step.run(context);
}

async function runSuite(suite, context) {
  for (const step of suite.steps) await runStep(step, context);
}

async function runScenario(scenario, context) {
  for (const suite of scenario.suites) await runSuite(suite, context);
}

module.exports = { runStep, runSuite, runScenario };
