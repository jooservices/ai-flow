"""Routes of the knowledge feature, installed from its route groups."""
import secrets  # noqa: F401 - retained for the former routes module import path
from pathlib import Path  # noqa: F401 - retained for the former routes module import path
from types import SimpleNamespace

from fastapi import Depends, HTTPException, Request  # noqa: F401 - retained for the former routes module import path
from ... import knowledge  # noqa: F401 - retained for the former routes module import path
from ...platform.events.wiring import publish_for  # noqa: F401 - retained for the former routes module import path
from .bodies import (KnowledgeRestoreBody)  # noqa: F401 - retained for the former routes module import path
from .events import (ResourceCommitted)  # noqa: F401 - retained for the former routes module import path
from .routes_items import install_item_routes
from .routes_proposals import install_proposal_routes
from .routes_resources import install_resource_routes


def install(app, ctx, deps) -> SimpleNamespace:
    items = install_item_routes(app, ctx, deps)
    resources = install_resource_routes(app, ctx, deps)
    proposals = install_proposal_routes(app, ctx, deps)
    return SimpleNamespace(
        knowledge_items=items.knowledge_items,
        knowledge_item_save=items.knowledge_item_save,
        knowledge_item_action=items.knowledge_item_action,
        load_managed_resources=resources.load_managed_resources,
        submit_resource_change=resources.submit_resource_change,
        knowledge_resources=resources.knowledge_resources,
        knowledge_history=resources.knowledge_history,
        knowledge_save=resources.knowledge_save,
        knowledge_restore=resources.knowledge_restore,
        knowledge_approve=proposals.knowledge_approve,
    )
