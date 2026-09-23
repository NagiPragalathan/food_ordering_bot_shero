"""Admin dashboard: menu management, orders and integration settings."""

from fastapi import APIRouter

from app.admin.routes import auth, chat, dashboard, imports, menu, orders, settings

router = APIRouter(prefix="/admin")
router.include_router(auth.router)
router.include_router(dashboard.router)
router.include_router(menu.router)
router.include_router(imports.router)
router.include_router(orders.router)
router.include_router(chat.router)
router.include_router(settings.router)

__all__ = ["router"]
