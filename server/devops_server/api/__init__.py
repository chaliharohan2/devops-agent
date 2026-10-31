from fastapi import APIRouter

from . import routes_admin, routes_apps, routes_changes, routes_chat, routes_peers

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(routes_admin.router)
api_router.include_router(routes_apps.router)
api_router.include_router(routes_changes.router)
api_router.include_router(routes_peers.router)
api_router.include_router(routes_chat.router)
