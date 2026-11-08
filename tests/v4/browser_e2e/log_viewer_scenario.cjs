const suites = require('./log_viewer_suites.cjs');

module.exports = {
  name: 'log viewer returns redacted lines and handles empty reads',
  suites: [suites.redactedLogRead, suites.emptyLogRead],
};
