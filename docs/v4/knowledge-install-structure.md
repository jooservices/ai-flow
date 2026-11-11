# Knowledge route installer structure

`knowledge.routes.install` keeps the registration order and return namespace for versioned resource and owner approval
routes. Learned-knowledge item endpoints use the discovered router in `control/routes/knowledge_items.py`.

## Routes

### Discovered item router

The item handlers live in `features/knowledge/routes_items.py`; the route registry mounts them once during app setup.

### `install_resource_routes`

- GET `/v1/knowledge/resources`
- GET `/v1/knowledge/resources/{kind}/{resource_id}/history`
- POST `/v1/knowledge/resources`
- POST `/v1/knowledge/resources/{kind}/{resource_id}/restore`

### `install_proposal_routes`

- POST `/v1/knowledge/proposals/{proposal_id}/approve`

To add an item route, place its handler in the feature router and expose that router through `control/routes/`. Add
resource and proposal routes to their installer group, pass shared dependencies explicitly, and preserve that group's
registration order. Update the registration parity tests and this route list.
