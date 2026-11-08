"""Mount the incident feature's console routes through v4 route discovery."""
from ..features.incidents.console_routes import router

__all__ = ["router"]  # route discovery mounts this module through its `router`
