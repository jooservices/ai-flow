# Command Center V2

The console follows the warm graphite design and interaction model in the [V2 prototype](design/command-center-v2.html). The prototype is a single-file HTML and JavaScript demo with sample data and no backend; it is the visual and behavioural reference for this console. The Dashboard combines work, AI usage, and machine account status. The Work board and Dashboard are visible to every signed-in console role; the remaining views follow the existing `super_admin`, `owner`, and `lead` roles.

## Navigation

The menu starts with Dashboard and Work board, followed by Delivery, SA, Team Lead, Business, Human review, Stakeholder, Machines and AI, Users and roles, Knowledge, Integrations, and Projects. The old Command Center tab is now Dashboard. The previous machine, account, incident, and live event details remain under Machines and AI.

## Dashboard

The Work section shows four summary cards, ticket flow by status, a searchable and sortable project table, active runs, an epic roadmap, and tickets waiting for a person. Choosing a project from the table updates the shared project selection. Selecting an epic roadmap bar opens the Work board with that epic selected.

The AI and machines section shows daily run usage by provider, quota windows and reset times for each configured AI account, plus token and cost totals for the last 24 hours and 14 days. Provider colours are named in the chart legend. Status labels include text so colour is never the only signal. The prototype also illustrates machine setup, AI account sign-in, worker sessions and jobs; the Users and roles, Human review, and Knowledge views retain their relevant management and review workflows.

## Work board and search

The Work board uses the selected project's Jira tickets and supports epic, AI state, issue type, and text filters. Its project picker searches project keys and repositories, shows starred projects first, and limits the visible results while searching; starred projects and the selected project are saved in the browser.

Press **Ctrl K** or **Cmd K**, or use the Search button, to open quick search. Results can open a view, project board, or ticket drawer. Escape closes the search. Search uses the console's existing `/v1/state` snapshot and does not add a separate API or dependency.

## Themes and responsive layout

The warm light palette and graphite dark palette use the same theme control and stored theme preference. The menu, picker, table sort controls, filters, and quick search are keyboard reachable. The board scrolls horizontally when its workflow columns do not fit; dashboard grids collapse to a single column on narrow screens. The prototype's data is illustrative and is not shipped as product data.
