"""Discovered read-only console session route."""
from fastapi import APIRouter, Request

from .. import route_deps
from ..console_auth import CANONICAL_ROLES, ROLE_LABELS, ROLE_PERMISSIONS, landing_page

router = APIRouter()


@router.get("/v1/console/session")
def console_session(request: Request) -> dict:
    ctx = route_deps.context(request)
    user = (route_deps.console_user_for(request, None) if ctx.settings.console_login
            else route_deps.optional_console_user(ctx, request))
    if user is None:
        return {"authenticated": False}
    effective = user.view_as or user.role
    return {"authenticated": True, "user": user.subject, "role": effective,
            "role_label": ROLE_LABELS[effective], "roles": list(user.assigned_roles),
            "role_labels": [ROLE_LABELS[role] for role in user.assigned_roles],
            "permissions": sorted(user.permissions), "landing_page": landing_page((effective,)),
            "can_view_as": user.is_administrator, "viewing_as": user.view_as,
            "role_options": [{"id": role, "label": ROLE_LABELS[role],
                              "permissions": sorted(ROLE_PERMISSIONS[role]),
                              "landing_page": landing_page((role,))} for role in CANONICAL_ROLES]}
