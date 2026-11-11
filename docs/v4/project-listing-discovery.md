# Managed project listing discovery

The projects feature owns `GET /v1/projects` in `features/projects/discovered_listing.py`. The control route package
re-exports its router from `routes/project_listing.py`, where the existing route discovery mechanism finds and mounts
it. The installer continues to register the project catalog route.

## Routes

- `GET /v1/projects` lists managed projects and invalid project files visible to the current console user.
