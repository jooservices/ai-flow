# Console layouts: blocks each person can drag, resize, hide and reset

Every section of a console page that shows several sections is a **block**. Each person arranges the blocks of each
page the way they work, and the arrangement follows them to another browser or computer because the server stores it
(table `console_layouts`, keyed by the person's email and the page).

Pages in scope: Dashboard (the "Work" and "AI and machines" sections), Human review, Machines and AI, Knowledge,
Delivery, and Users and roles.

## Using it

1. Open a page and select **Customize layout**. The page enters layout mode; **Done** leaves it.
2. Every block shows a **handle**. Drag the handle (mouse or touch); the other blocks make room while you drag.
3. Choose **Small**, **Medium**, **Large** or **Full width** for a block (a 12-column grid: 4, 6, 8 or 12 columns), or
   **Hide** it. Hidden blocks are listed at the top in layout mode, each with a **Show** button.
4. **Reset this page** returns the page to the default layout of your role.

Keyboard: focus a handle and press the arrow keys (up/left moves the block earlier, down/right later, Home and End go
to the first and last place). Every change is announced to screen readers ("Quota moved to position 2 of 4",
"Tokens and cost is hidden", "Layout mode is on"), and focus stays on the control you used.

At phone width (720 px and narrower) the blocks show one per row in the saved order. Layout mode works on touch: the handle uses pointer events and the buttons are
large enough for a finger. Dragging and the size buttons are optional: the arrow keys work everywhere.

## Block identifiers

Blocks have stable identifiers: the `data-block` attribute in `aiflow/v4/control/ui/console.html`, listed with their
titles and default sizes in `PAGES` of `aiflow/v4/control/layouts.py`. A test checks that the two agree. To add a block
to a page, mark it with `data-block="<id>"` as a direct child of the view (or of a `.dash-section`) and add it to
`PAGES`.

A saved layout is always *resolved* against the blocks that exist now:

- a block named in the saved layout that no longer exists is ignored, without error;
- a block that the saved layout does not name (added by a later release) is appended at the end of the layout, visible,
  so a saved layout never loses it. The console also puts a block the server does not know yet at the end.

## Role defaults

Without a saved layout a person gets the default of their role. The defaults use the console roles of
`console_auth.ROLE_PERMISSIONS` through `ROLE_PROFILE` in `layouts.py`:

| Role | Profile | Dashboard starts with |
| --- | --- | --- |
| `owner`, `super_admin` | owner | summary cards, then AI usage, tokens and cost, quota (cost and quota first) |
| `lead` | lead | summary cards, flow, waiting for you, what is being built (the board first); tokens and autonomy hidden |

Profiles also exist for `business`, `sa` and `reviewer` (Human review first); they apply when a role named `team_lead`,
`business`, `sa`, `human_reviewer` or `reviewer` is added to `ROLE_PERMISSIONS`. A role without a profile gets the
canonical layout, so adding a role never breaks the console.

## API

All three calls need a console session and act on the signed-in person; the person is never taken from the request.

| Call | Result |
| --- | --- |
| `GET /v1/console/layouts` | every page: `blocks` (`id`, `title`, `size`, `hidden`, in display order) and `customized` |
| `PUT /v1/console/layouts/{page}` | saves `{"blocks": [{"id", "size", "hidden"}]}` and returns the resolved page; unknown blocks are dropped, a size other than `small`, `medium`, `large`, `full` is refused with 400, an unknown page is 404 |
| `DELETE /v1/console/layouts/{page}` | forgets the saved layout and returns the role default |

Saving a layout is a personal preference: it is not blocked by an expired licence.

## Tests

`tests/v4/test_console_layouts.py` covers saving, restoring in a second browser, resetting to the role default, stale
layouts (removed blocks ignored, new blocks added at the end), validation, the role defaults, the keyboard and merge
functions of the script, the agreement between `PAGES` and the console markup, and, where Playwright is installed, a
browser run of drag, resize, hide, keyboard, reset and phone width.
