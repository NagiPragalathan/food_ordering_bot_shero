"""Reading and saving the nudge settings and campaigns."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models import NudgeCampaign, NudgeSettings, Promotion
from app.services import promotions
from app.services.nudges.config import (
    NudgeConfigError,
    GlobalSettings,
    build_campaign_config,
    build_settings,
    dump_config,
    parse_kind,
)

log = get_logger(__name__)

# The settings row has a fixed id, so two first saves at once cannot make two.
SETTINGS_ID = uuid.UUID(int=1)

CAMPAIGN_FIELDS = {"name", "kind", "is_active", "priority", "promotion_code", "config"}


# --- global settings ---------------------------------------------------------------
async def _settings_row(session: AsyncSession) -> NudgeSettings | None:
    return await session.get(NudgeSettings, SETTINGS_ID)


async def load_settings(session: AsyncSession) -> tuple[bool, GlobalSettings]:
    """(master switch, settings). Nothing saved yet means off, with defaults."""
    row = await _settings_row(session)
    if row is None:
        return False, GlobalSettings()
    try:
        return bool(row.enabled), build_settings(None, current=row.values or {})
    except NudgeConfigError as exc:
        # A value that no longer validates (an option renamed in an upgrade)
        # must not stop STOP from working; fall back and say so.
        log.error("nudge_settings_invalid_using_defaults", error=str(exc))
        return bool(row.enabled), GlobalSettings()


async def is_enabled(session: AsyncSession) -> bool:
    row = await _settings_row(session)
    return bool(row and row.enabled)


async def save_settings(session: AsyncSession, raw: dict, *, actor: str) -> tuple[bool, GlobalSettings]:
    """Merge `raw` into the saved settings. `enabled` is the master switch."""
    raw = dict(raw or {})
    enabled = raw.pop("enabled", None)
    if enabled is not None and not isinstance(enabled, bool):
        raise NudgeConfigError("enabled must be true or false")

    row = await _settings_row(session)
    current = row.values if row else {}
    settings = build_settings(raw, current=current)
    if row is None:
        row = NudgeSettings(id=SETTINGS_ID, enabled=False, values={})
        session.add(row)
    row.values = settings.model_dump(mode="json")
    if enabled is not None:
        row.enabled = enabled
    row.updated_by = actor
    await session.flush()
    log.info("nudge_settings_saved", actor=actor, enabled=row.enabled,
             changed=sorted(raw) + (["enabled"] if enabled is not None else []))
    return row.enabled, settings


async def set_enabled(session: AsyncSession, enabled: bool, *, actor: str) -> None:
    await save_settings(session, {"enabled": enabled}, actor=actor)
    log.warning("nudges_switched_" + ("on" if enabled else "off"), actor=actor)


# --- campaigns ---------------------------------------------------------------------------
async def get_campaign(session: AsyncSession, campaign_id) -> NudgeCampaign | None:
    try:
        return await session.get(NudgeCampaign, uuid.UUID(str(campaign_id)))
    except (ValueError, TypeError):
        return None


async def list_campaigns(session: AsyncSession, *, active_only: bool = False) -> list[NudgeCampaign]:
    query = select(NudgeCampaign).order_by(NudgeCampaign.priority.desc(),
                                           NudgeCampaign.created_at)
    if active_only:
        query = query.where(NudgeCampaign.is_active.is_(True))
    return list((await session.execute(query)).scalars())


async def promotion_of(session: AsyncSession, campaign: NudgeCampaign) -> Promotion | None:
    if campaign.promotion_id is None:
        return None
    return await session.get(Promotion, campaign.promotion_id)


def _check_fields(data: dict) -> None:
    unknown = set(data) - CAMPAIGN_FIELDS
    if unknown:
        raise NudgeConfigError(f"{sorted(unknown)} are not campaign fields; "
                               f"use {sorted(CAMPAIGN_FIELDS)}")


async def _promotion_for(session: AsyncSession, code) -> Promotion | None:
    if code in (None, ""):
        return None
    promotion = await promotions.find_by_code(session, str(code))
    if promotion is None:
        raise NudgeConfigError(f"There is no coupon with the code {promotions.normalize_code(code)}. "
                               "Create it on the Offers page first.")
    return promotion


def _name(raw) -> str:
    name = " ".join(str(raw or "").split())
    if not name:
        raise NudgeConfigError("name is required")
    if len(name) > 120:
        raise NudgeConfigError("name is at most 120 characters")
    return name


def _priority(raw) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or not -1000 <= raw <= 1000:
        raise NudgeConfigError("priority is a whole number from -1000 to 1000")
    return raw


async def create_campaign(session: AsyncSession, data: dict, *, actor: str) -> NudgeCampaign:
    _check_fields(data)
    kind = parse_kind(data.get("kind"))
    promotion = await _promotion_for(session, data.get("promotion_code"))
    config = build_campaign_config(kind, data.get("config") or {},
                                   has_coupon=promotion is not None)
    is_active = data.get("is_active", False)
    if not isinstance(is_active, bool):
        raise NudgeConfigError("is_active must be true or false")

    campaign = NudgeCampaign(
        name=_name(data.get("name")), kind=str(kind), is_active=is_active,
        priority=_priority(data.get("priority", 0)),
        promotion_id=promotion.id if promotion else None,
        config=dump_config(kind, config), created_by=actor, updated_by=actor,
    )
    session.add(campaign)
    await session.flush()
    log.info("nudge_campaign_created", campaign=campaign.name, kind=campaign.kind,
             active=campaign.is_active, actor=actor)
    return campaign


async def update_campaign(session: AsyncSession, campaign: NudgeCampaign, data: dict, *,
                          actor: str) -> NudgeCampaign:
    """A partial update: only the fields given change, and `config` is merged
    section by section into what is saved."""
    _check_fields(data)
    kind = parse_kind(data.get("kind", campaign.kind))
    current = dict(campaign.config or {})
    if str(kind) != campaign.kind:
        # Trigger options belong to the old kind; start from the new kind's.
        current.pop("trigger", None)

    if "promotion_code" in data:
        promotion = await _promotion_for(session, data["promotion_code"])
        promotion_id = promotion.id if promotion else None
    else:
        promotion_id = campaign.promotion_id
        if promotion_id is not None and await session.get(Promotion, promotion_id) is None:
            promotion_id = None

    config = build_campaign_config(kind, data.get("config") or {}, current=current,
                                   has_coupon=promotion_id is not None)
    if "name" in data:
        campaign.name = _name(data["name"])
    if "priority" in data:
        campaign.priority = _priority(data["priority"])
    if "is_active" in data:
        if not isinstance(data["is_active"], bool):
            raise NudgeConfigError("is_active must be true or false")
        campaign.is_active = data["is_active"]
    campaign.kind = str(kind)
    campaign.promotion_id = promotion_id
    campaign.config = dump_config(kind, config)
    campaign.updated_by = actor
    await session.flush()
    log.info("nudge_campaign_updated", campaign=campaign.name, actor=actor,
             changed=sorted(data))
    return campaign


async def duplicate_campaign(session: AsyncSession, campaign: NudgeCampaign, *,
                             actor: str) -> NudgeCampaign:
    """A paused copy, to try a variation without touching the original."""
    copy = NudgeCampaign(
        name=f"Copy of {campaign.name}"[:120], kind=campaign.kind, is_active=False,
        priority=campaign.priority, promotion_id=campaign.promotion_id,
        config=dict(campaign.config or {}), created_by=actor, updated_by=actor,
    )
    session.add(copy)
    await session.flush()
    return copy


async def delete_campaign(session: AsyncSession, campaign: NudgeCampaign, *, actor: str) -> None:
    """Delete it. Its sends stay in the log, under the campaign's name."""
    log.info("nudge_campaign_deleted", campaign=campaign.name, actor=actor)
    await session.delete(campaign)
    await session.flush()


def campaign_config(campaign: NudgeCampaign, *, has_coupon: bool):
    """The stored config, validated (defaults fill anything missing)."""
    return build_campaign_config(campaign.kind, None, current=campaign.config or {},
                                 has_coupon=has_coupon)


def campaign_view(campaign: NudgeCampaign, promotion: Promotion | None = None) -> dict:
    return {
        "id": str(campaign.id),
        "name": campaign.name,
        "kind": campaign.kind,
        "is_active": campaign.is_active,
        "priority": campaign.priority,
        "promotion_code": promotion.code if promotion else None,
        "promotion_title": promotion.title if promotion else None,
        "config": campaign.config,
        "created_by": campaign.created_by,
        "updated_by": campaign.updated_by,
        "created_at": campaign.created_at.isoformat() if campaign.created_at else None,
        "updated_at": campaign.updated_at.isoformat() if campaign.updated_at else None,
    }
