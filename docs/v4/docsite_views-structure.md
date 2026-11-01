# Console view mapping structure

`changed_views` delegates console diff mapping to small named steps in `aiflow/v4/core/docsite_views.py`.

1. `_extension_changed_views` maps changes to standalone UI extensions.
2. `_console_context` records the current and base page lines, view sections, renderers, Help regions, layout regions and security lines.
3. `_map_hunk` handles removed sections and shared lines, then sends each changed line to `_classify_console_line`.
4. `_classify_console_line` applies section, navigation, renderer, layout, Help, shared UI and screen-specific rules in their existing order.
5. `_raise_unmapped` keeps the refusal message for UI lines that have no screen mapping.

To add a mapping, add a literal case to `tests/v4/test_docsite_views_parity.py` first, then extend the relevant mapping table in `docsite.py`. Preserve the rule order and expected view set; add a direct helper test when the new rule can be tested on its own.
