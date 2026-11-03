# Console route install structure

`features/console/routes.py:install` calls the route-group installers in registration order and combines their returned
handlers and helpers into the same `SimpleNamespace` used by existing callers. Shared validation helpers are passed to
the groups that need them. Event publication still appears directly in the endpoint handlers.

## Routes

1. `install_console_read_routes` in `routes_console_read.py`

   - `GET /v1/console/session`
   - `GET /v1/console/users`

2. `install_console_user_create_routes` in `routes_console_user_create.py`

   - `POST /v1/console/users`

3. `install_console_user_change_routes` in `routes_console_user_change.py`

   - `PUT /v1/console/users/{email}`
   - `PATCH /v1/console/users/{email}/disabled`

4. `install_view_as_routes` in `routes_view_as.py`

   - `POST /v1/console/view-as`
   - `POST /v1/console/view-as/stop`

5. `install_layout_preference_routes` in `routes_layouts.py`

   - `GET /v1/console/layouts`
   - `PUT /v1/console/layouts/{page}`
   - `DELETE /v1/console/layouts/{page}`
   - `GET /v1/console/preferences`
   - `PUT /v1/console/preferences`

6. `install_notification_routes` in `routes_notifications.py`

   - `GET /v1/notifications`
   - `PUT /v1/notifications/channels`
   - `DELETE /v1/notifications/channels/{channel_id}`
   - `PUT /v1/notifications/preferences`
   - `POST /v1/notifications/channels/{channel_id}/test`

7. `install_licensing_routes` in `routes_licensing.py`

   - `GET /v1/licensing`
   - `GET /v1/licensing/status`
   - `POST /v1/licensing/activate`
   - `POST /v1/licensing/offline`
   - `PUT /v1/licensing/limits`
   - `GET /v1/licensing/usage`
   - `GET /v1/licensing/usage.csv`

## Adding a route group

Put related routes in a sibling `routes_<group>.py` module. Keep each installer and handler within the function size and
complexity limits, pass shared helpers and dependencies explicitly, and call the installer from `routes.install` at its
registration point. Keep each route decorator beside its handler and list every method and path in this page.
