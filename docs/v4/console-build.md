# Console page assembly

The UI routes call `build_served_console_page()` from
`aiflow/v4/control/ui/console_build.py`. It reads the manifest and fragments on each request, concatenates them
without added separators, inserts the Security and System check views and policy scripts, then replaces only the
first licensing-notice script tag. `build_console_page()` returns the assembled page before that final tag
replacement. The UI package re-exports `console_source_names()`, `console_template()`, and
`build_console_page()` for existing callers.

To add a fragment, create a UTF-8 file in `aiflow/v4/control/ui/console/` and add its filename to
`manifest.txt` at the position where its content belongs. The manifest's whitespace-separated names determine
page order; the assembler does not deduplicate names or insert separators.

**Console screens:** `cc`, `work`, `delivery`, `sa`, `machines`, `users`, `security`, `lead`,
`business`, `review`, `stakeholder`, `knowledge`, `backup`, `integrations`, `projects`, `licensing`,
`notifications`, `settings`, `support`
