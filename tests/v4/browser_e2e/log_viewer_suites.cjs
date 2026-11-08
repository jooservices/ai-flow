const steps = require('./log_viewer_steps.cjs');

const redactedLogRead = {
  name: 'show redacted log lines',
  steps: [
    steps.openLogViewer,
    steps.waitForLogSource,
    steps.requestLogRead,
    steps.waitForLogLines,
    steps.readLogLines,
    steps.assertLogLinesVisible,
    steps.assertRedactionMarkerVisible,
    steps.assertSecretHidden,
    steps.waitForRecentLogStatus,
    steps.waitForAnotherLogRead,
  ],
};

const emptyLogRead = {
  name: 'show the empty log result',
  steps: [
    steps.openLogViewer,
    steps.waitForLogSource,
    steps.requestLogRead,
    steps.waitForEmptyLogStatus,
    steps.assertEmptyLogBlockHidden,
    steps.assertNoTraceback,
  ],
};

module.exports = { redactedLogRead, emptyLogRead };
