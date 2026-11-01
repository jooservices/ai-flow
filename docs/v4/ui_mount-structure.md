# UI mount structure

`mount_ui` registers the console pages, their scripts, the help site and the two read-only drawer endpoints. The
calls below are the registration order, and that order is the route order:

1. `mount_feature_assets` serves provider routing, the combined console-features script and the licensing notice.
   `console_feature_script` joins the licensing notice and provider routing in that order.
2. `mount_panel_assets` serves owner escalation, release readiness, scale proof, flow doctor, run inbox, user form
   state and quota refresh.
3. `mount_console_pages` redirects `/` to `/dashboard` and registers every other path in `CONSOLE_PAGE_PATHS` except
   `/`, `/help`, `/notifications` and `/settings`. `console` is the shared page.
4. `mount_logs_page` serves host and service logs through `require_console_user` with `view.logs`.
5. `mount_help` redirects `/help` to `/help/`. `help_document` serves a file from the built documentation site.
6. `mount_notifications_page` serves the console through `require_console_user` with `notifications.manage`.
7. `mount_settings_page` serves the console through `require_console_user` with `console.manage`.
8. `mount_ticket_route` serves ticket detail. `ticket_detail` loads the row, `ticket_ancestors` walks parents,
   `present_runs` shapes runs and `capacity_view` shapes the capacity decision.
9. `mount_service_status` serves the monitor summary. `gatus_services` reads it and `service_rows` shapes each row.

To add a script, add a named asset in the group that owns it and call that group's function from `mount_ui` at the
position the route should occupy. To add a console page, add its path to `CONSOLE_PAGE_PATHS`. Keep each function
within 80 lines and complexity 12, and record a parity case for a new branch.

**Console screens:** `dashboard`, `work`, `board`, `delivery`, `sa`, `business`, `review`, `stakeholder`, `machines`, `users`, `knowledge`, `backup`, `integrations`, `licensing`, `lead`, `projects`, `system-check`, `support`, `cc`, `security`, `notifications`, `settings`

Routes: GET /v1/tickets/{key}, GET /v1/services
