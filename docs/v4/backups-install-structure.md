# Backups routes structure (JAIF-554)

`install(app, ctx, deps)` in `aiflow/v4/control/features/backups/routes.py` registers the four backups routes and keeps the publishing of their domain events in the route handlers. The reading and validating of the backup settings moved to `backup_settings.py`:

| Function | Owns |
|---|---|
| `read_backup_config` | the settings row as the console sees it (dates as text, key state, the listing of existing backups) |
| `destination_values` | the values to store for the requested destination; uses `folder_destination` (local or mounted folder: absolute, under the durable folder for `local`, clear of the control repository and secrets) or `s3_destination` (HTTPS endpoint, bucket name, cleaned prefix, region) |
| `create_backup_key` | the one-time creation of the encryption key file and the record that it was downloaded |

`tests/v4/test_backups_install_parity.py` compares the route registrations and the outcome of 19 configure requests (status, message, stored values, published event) with a record written from the unchanged code.

## Routes

`GET /v1/backups`, `PUT /v1/backups/config`, `POST /v1/backups/key` and `POST /v1/backups/run`.
