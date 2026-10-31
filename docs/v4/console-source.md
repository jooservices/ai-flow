# Console page source

The console is one page assembled by `aiflow/v4/control/ui/__init__.py`. `console_template()` returns the page
before the Security, System check and policy extensions are injected; `build_console_page()` adds those extensions
and is what the control API serves.

Tests and tools read the page through `console_template()` (or `build_console_page()`) instead of opening
`console.html` by path. That keeps them working when the page is later split into sources joined by a manifest
(JAIF-507): only `console_template()` changes, the assembled page stays byte-identical.

No screen behaviour changes with this note.

**Console screens:** `cc`, `work`, `delivery`, `sa`, `machines`, `users`, `security`, `lead`, `business`, `review`, `stakeholder`, `knowledge`, `backup`, `integrations`, `projects`, `licensing`, `notifications`, `settings`, `support`
