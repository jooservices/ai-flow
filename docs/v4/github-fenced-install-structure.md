# GitHub fenced route installer structure

The `github_fenced` installer in `routes.py` builds a frozen runtime bundle and calls small route-group installers in
registration order. The returned namespace keeps the names used by the control app and review services. Groups live in
`routes_access.py`, `routes_pull_requests.py`, `routes_reviews.py` and `routes_maintenance.py`.

## Routes

### `install_github_token_routes`

- `POST /v1/github/token`

### `install_git_proxy_routes`

- `GET /v1/github/git/{git_path:path}`
- `POST /v1/github/git/{git_path:path}`

### `install_pull_request_routes`

- `GET /v1/github/pr`
- `POST /v1/github/pr`

### `install_github_merge_routes`

- `POST /v1/github/merge`

### `install_review_queue_routes`

- `POST /v1/github/webhook`
- `GET /v1/reviews`

### `install_review_export_routes`

- `GET /v1/reviews/export.csv`

### `install_review_approval_routes`

- `POST /v1/reviews/{key}/approve`
- `POST /v1/reviews/{key}/rework`

### `install_review_rejection_and_question_routes`

- `POST /v1/reviews/{key}/reject`
- `POST /v1/reviews/{key}/ask-ai`

### `install_review_answer_and_gate_routes`

- `POST /v1/reviews/{key}/answer`
- `POST /v1/reviews/{key}/skip-gate`

### `install_github_maintenance_routes`

- `POST /v1/github/update-branch`
- `GET /v1/github/checks`
- `POST /v1/github/comment`
- `POST /v1/github/delete-branch`

To add a route, place its handler with the matching group installer, pass any captured services through the installer
arguments, and call that installer from `routes.install` at the position matching the desired registration order. Keep
publish, audit and event calls in the handlers, and add the route to this list and a parity case for any extracted
behavior helper.
