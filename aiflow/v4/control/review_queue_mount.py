"""Named registration and response-building steps for the human review queue route."""
from __future__ import annotations

from typing import Any, Callable

from . import review_queue_api as review_queue_api


def install_human_reviews_route(queue: Any, app: review_queue_api.FastAPI, console_user: Callable,
                                optional_console_user: Callable) -> None:
    """Register the review queue route, keeping authorization and audit at the route edge."""
    endpoint = HumanReviewsEndpoint(queue, console_user, optional_console_user)
    app.get("/v1/reviews")(endpoint.human_reviews)


class HumanReviewsEndpoint:
    """Bind route collaborators without capturing them in a nested endpoint closure."""

    def __init__(self, queue: Any, console_user: Callable, optional_console_user: Callable):
        self.queue = queue
        self.console_user = console_user
        self.optional_console_user = optional_console_user

    def human_reviews(self, request: review_queue_api.Request,
                      project: str = review_queue_api.Query(default="")) -> dict:
        queue = self.queue
        ctx = queue.ctx
        user = (self.console_user(request, "view.human_review") if ctx.settings.console_login
                else self.optional_console_user(request))
        repo = ctx.repo()
        scope = user.project_keys if user and user.project_keys is not None else None
        if project and scope is not None and project not in scope:
            queue.audit("console", "access_denied", "", "", None, user.subject, False,
                        "not available for your role", {"path": request.url.path, "project": project,
                                                         "roles": list(user.effective_roles)})
            raise review_queue_api.HTTPException(403, "not available for your role")
        return review_queue_api._build_human_reviews_response(queue, ctx, repo, project, scope)

