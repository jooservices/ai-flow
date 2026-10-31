# Console page source

The console is one page, but its source is split into files under `aiflow/v4/control/ui/console/`.
`console/manifest.txt` lists them in page order, one name per line, and `console_template()` in
`aiflow/v4/control/ui/__init__.py` joins them. `build_console_page()` then adds the Security, System check and
policy extensions and is what the control API serves.

| Part | Content |
| --- | --- |
| `000-head.html` to `030-script-open.html` | document head, the stylesheet, the page markup and the start of the script |
| `040-state.js` to `135-interactions.js` | one file per section of the script (state, fetch, each screen's renderers, drawers, layout, interactions) |
| `140-boot.html` | the boot section and the closing tags |

Every part is a verbatim slice of the page, so the joined page is identical to the former single `console.html`
and editing one part edits only its lines. To add a part, create the file and add its name to the manifest at
the position it should have in the page; the numbers in the names only keep the listing in page order.

Tests and tools read the page through `console_template()` (or `build_console_page()`), never by file path.
The documentation check assembles the page at the base and at HEAD and compares the two as one page, so edits are
still mapped to the screens they change.

No screen behaviour changes with this note.

**Console screens:** `cc`, `work`, `delivery`, `sa`, `machines`, `users`, `security`, `lead`, `business`, `review`, `stakeholder`, `knowledge`, `backup`, `integrations`, `projects`, `licensing`, `notifications`, `settings`, `support`
