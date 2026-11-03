# Knowledge route installer structure

`knowledge.routes.install` keeps the feature registration order and return namespace. It installs item routes, versioned
resource routes, then owner approval routes. Each group owns its handlers and the helpers those handlers call.

## Routes

### `install_item_routes`

- GET `/v1/knowledge/items`
- POST `/v1/knowledge/items`
- POST `/v1/knowledge/items/{item_id}/{action}`

### `install_resource_routes`

- GET `/v1/knowledge/resources`
- GET `/v1/knowledge/resources/{kind}/{resource_id}/history`
- POST `/v1/knowledge/resources`
- POST `/v1/knowledge/resources/{kind}/{resource_id}/restore`

### `install_proposal_routes`

- POST `/v1/knowledge/proposals/{proposal_id}/approve`

To add a route, place its handler in the installer that owns the same purpose, pass shared dependencies explicitly, and
keep its registration at the right point in that installer's order. Update the registration golden and this route list.
