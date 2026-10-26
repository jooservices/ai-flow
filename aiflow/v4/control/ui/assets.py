"""Console JavaScript assets and their HTTP registration."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

HERE = Path(__file__).parent
ASSETS = (("licensing-notice.js", "licensing_notice.js"),
          ("dashboard-attention.js", "dashboard_attention.js"))


def _asset_endpoint(filename: str):
    def serve() -> FileResponse:
        return FileResponse(HERE / filename, media_type="application/javascript",
                            headers={"Cache-Control": "no-cache"})
    return serve


def register_assets(app: FastAPI) -> None:
    for route, filename in ASSETS:
        app.add_api_route(f"/console-assets/{route}", _asset_endpoint(filename), methods=["GET"],
                          include_in_schema=False)
