# Console escaping source

**Console screens:** Dashboard (`cc`), Work (`work`), Delivery (`delivery`), SA (`sa`), Machines (`machines`), Users (`users`), Security (`security`), Team Lead (`lead`), Business (`business`), Human review (`review`), Stakeholder (`stakeholder`), Knowledge (`knowledge`), Backup (`backup`), Integrations (`integrations`), Projects (`projects`), Licensing (`licensing`), Notifications (`notifications`), Settings (`settings`), Support (`support`)

`aiflow/v4/control/ui/console/018-escaping.js` owns the shared `esc`, `safeUrl` and `safeHref` declarations. Keep their
distinct conversion and URL acceptance rules intact when editing them. The fragment is loaded inside the existing
console IIFE immediately after `015-script-open.html` and before `020-state.js`.

To add another shared console helper, put its declaration in this fragment when it belongs to HTML escaping or URL
safety, or create a separately named fragment for a different responsibility. Add each new fragment once to
`manifest.txt` in page order. `console_template()` and `build_console_page()` assemble the same source used by the
served console; tests should inspect that assembled page rather than loading fragment paths directly.
