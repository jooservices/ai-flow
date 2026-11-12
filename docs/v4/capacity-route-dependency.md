# Capacity checks in routes

`control.route_deps.require_capacity(ctx, resource, count=None, *, connection=None)` makes the existing licence
capacity check available to independently defined routers. It reads the public key from the supplied application
context on each call, forwards the optional count and connection to `licensing.require_capacity`, and returns its
result unchanged. A `LicenseError` becomes HTTP 409 with the existing detail; other errors pass through.

## Use the helper

1. Get the app-specific context with `route_deps.context(request)` or `request.app.state.ctx`.
2. Call `route_deps.require_capacity(ctx, resource)` for the existing count, or pass an explicit count.
3. When already inside a transaction, pass its connection with `connection=connection`; the caller keeps transaction
   ownership.

The installer namespace keeps its `require_capacity(resource, count=None, *, connection=None)` closure for current
callers. New routers can call the shared function directly with their context; no route registration or licence policy
change is needed.
