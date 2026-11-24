"""Admin dashboard: menu management, orders and integration settings."""

from fastapi import APIRouter

from app.sft import admin as sft_admin

from app.admin.routes import (auth, bot_replies, customers, dashboard, deliveries, feedback,
                               imports, kitchens, loyalty, menu, nudges, order_alerts, orders,
                               payouts, promotions, settings)

router = APIRouter(prefix="/admin")
router.include_router(auth.router)
router.include_router(dashboard.router)
router.include_router(menu.router)
router.include_router(imports.router)
router.include_router(orders.router)
router.include_router(promotions.router)
router.include_router(loyalty.router)
router.include_router(deliveries.router)
router.include_router(kitchens.router)
router.include_router(payouts.router)
router.include_router(customers.router)
router.include_router(bot_replies.router)
router.include_router(settings.router)
router.include_router(order_alerts.router)
router.include_router(nudges.router)
router.include_router(feedback.router)
# Shero Food Training: /admin/sft, its own tab beside the businesses (app/sft/admin.py).
router.include_router(sft_admin.router)

__all__ = ["router"]
