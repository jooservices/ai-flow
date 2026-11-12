# Project listing repository

The Projects screen gets its listing aggregates through `ctx.project_listing_repository.listing_data()` in `features/projects/service.py`. `ProjectListingRepository` owns the single batched read in `features/projects/repository.py`; its result is the existing `ProjectListingData`. The module-level `listing_data(db)` function remains available for compatibility and backs the repository method.

## Routes

- `GET /v1/projects` Lists managed projects and invalid project files visible to the current console user.

`ApplicationContext` creates a repository bound to its own database when one is not supplied. Tests or other callers can supply an object with the same `listing_data()` method. The `/v1/projects` route and its authorization, filtering and response behavior remain unchanged.

To extend this read, update the project-owned query and `ProjectListingData`, then consume the returned values in the project service. Keep the query aggregated so listing does not add per-project or per-node database calls.
