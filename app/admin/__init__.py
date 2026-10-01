"""Admin dashboard: menu management, orders and integration settings."""

from fastapi import APIRouter

from app.admin.routes import (auth, bot_replies, customers, dashboard, deliveries, imports,
                               kitchens, menu, orders, settings)

router = APIRouter(prefix="/admin")
router.include_router(auth.router)
router.include_router(dashboard.router)
router.include_router(menu.router)
router.include_router(imports.router)
router.include_router(orders.router)
router.include_router(deliveries.router)
router.include_router(kitchens.router)
router.include_router(customers.router)
router.include_router(bot_replies.router)
router.include_router(settings.router)

__all__ = ["router"]
