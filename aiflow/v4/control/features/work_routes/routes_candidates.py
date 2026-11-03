"""Installer for the worker and master candidate route group."""


def install_candidate_routes(app, ctx, deps, register_routes, releases, entitled_candidates, master_candidate):
    """Register the candidate routes before the work claim route."""
    register_routes(app, ctx, deps.node_auth, releases, entitled_candidates, master_candidate)
