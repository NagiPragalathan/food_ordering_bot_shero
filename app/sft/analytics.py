"""The SFT numbers the admin page shows (the KPI list in the journey design).

Computed from the leads that entered in the period (created_at), so every
rate compares like with like: of the leads who came in, how many got how far.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import SftEvent, SftLead
from app.sft import content as c
from app.sft.leads import now

PERIODS = {"7": "Last 7 days", "30": "Last 30 days", "90": "Last 90 days", "all": "All time"}


@dataclass
class Rate:
    label: str
    part: int
    whole: int
    hint: str = ""

    @property
    def percent(self) -> str:
        return f"{100 * self.part / self.whole:.0f}%" if self.whole else "-"


@dataclass
class Report:
    leads: int = 0
    rates: list[Rate] = field(default_factory=list)
    funnel: list[tuple[str, int]] = field(default_factory=list)       # reached each stage
    now_at: list[tuple[str, int]] = field(default_factory=list)       # sitting at each stage
    sources: list[dict] = field(default_factory=list)
    goals: list[tuple[str, int]] = field(default_factory=list)
    lost_reasons: list[tuple[str, int]] = field(default_factory=list)
    team_minutes: int | None = None
    recovered: Rate | None = None
    revenue: str = "-"
    with_team: int = 0
    needs_attention: int = 0


def since(period: str) -> datetime | None:
    return None if period == "all" else now() - timedelta(days=int(period))


async def report(session: AsyncSession, period: str = "30") -> Report:
    start = since(period if period in PERIODS else "30")
    query = select(SftLead).where(SftLead.source != "Test chat")
    if start is not None:
        query = query.where(SftLead.created_at >= start)
    rows = (await session.execute(query)).scalars().all()
    out = Report(leads=len(rows))
    if not rows:
        return out

    def got(stage: str) -> int:
        return sum(1 for r in rows if stage in (r.stage_times or {}))

    contacted = sum(1 for r in rows if set(r.stage_times or {}) - {c.NEW})
    interested = sum(1 for r in rows if r.goal)
    brochure = got(c.BROCHURE_SENT)
    after_brochure = sum(1 for r in rows if c.BROCHURE_SENT in (r.stage_times or {}) and (
        set(r.stage_times or {}) & {c.PAYMENT_PENDING, c.ENROLLED, c.NURTURE, c.LOST}
        or r.with_team or r.question))
    links = sum(1 for r in rows if r.link_sent_at)
    clicked = sum(1 for r in rows if r.link_clicked_at)
    paid = [r for r in rows if r.paid_at and not r.test_payment]
    paid_all = [r for r in rows if r.paid_at]
    accessed = sum(1 for r in paid_all if r.access_confirmed_at)

    out.rates = [
        Rate("Lead to contact", contacted, len(rows), "answered the welcome"),
        Rate("Interest captured", interested, len(rows), "chose a goal"),
        Rate("Brochure engagement", after_brochure, brochure,
             "answered after the brochure (yes, not now, a question)"),
        Rate("Payment link opened", clicked, links, "of the links sent"),
        Rate("Link to paid", len(paid_all), links, "of the links sent"),
        Rate("Overall conversion", len(paid_all), len(rows), "leads who enrolled"),
        Rate("Course access", accessed, len(paid_all), "said they opened the course"),
    ]
    out.funnel = [(c.STAGES[s], got(s)) for s in c.FUNNEL]
    counts = Counter(r.stage for r in rows)
    out.now_at = [(c.STAGES[s], counts.get(s, 0)) for s in c.STAGES]
    out.goals = [(c.GOALS[g], sum(1 for r in rows if r.goal == g)) for g in c.GOALS]
    out.lost_reasons = Counter(r.lost_reason for r in rows if r.lost_reason).most_common(6)
    out.with_team = sum(1 for r in rows if r.with_team)
    out.needs_attention = sum(1 for r in rows if r.needs_attention)
    total = sum((r.amount_paid or 0) for r in paid)
    out.revenue = f"${total:,.2f}".removesuffix(".00") if paid else "$0"

    by_source: dict[str, dict] = {}
    for r in rows:
        row = by_source.setdefault(r.source, {"source": r.source, "leads": 0, "brochure": 0,
                                              "links": 0, "paid": 0})
        row["leads"] += 1
        row["brochure"] += int(c.BROCHURE_SENT in (r.stage_times or {}))
        row["links"] += int(bool(r.link_sent_at))
        row["paid"] += int(bool(r.paid_at))
    for row in by_source.values():
        row["rate"] = f"{100 * row['paid'] / row['leads']:.0f}%" if row["leads"] else "-"
    out.sources = sorted(by_source.values(), key=lambda r: -r["leads"])

    ids = [r.id for r in rows]
    events = (await session.execute(
        select(SftEvent).where(SftEvent.lead_id.in_(ids),
                               SftEvent.kind.in_(("resolved", "followup", "attention", "stage")))
        .order_by(SftEvent.at))).scalars().all()
    minutes = [e.data.get("minutes") for e in events
               if e.kind == "resolved" and isinstance((e.data or {}).get("minutes"), int)
               and (e.data or {}).get("by") != "the bot"]
    out.team_minutes = int(sum(minutes) / len(minutes)) if minutes else None

    # Recovery: of the leads followed up (automatically or by the team), how
    # many moved forward afterwards.
    nudged: dict = {}
    moved: set = set()
    for e in events:
        if e.kind in ("followup", "attention"):
            nudged.setdefault(e.lead_id, e.at)
        elif e.kind == "stage" and e.lead_id in nudged and \
                (e.data or {}).get("to") in (c.INTERESTED, c.BROCHURE_SENT, c.QUESTIONS,
                                             c.PAYMENT_PENDING, c.ENROLLED):
            moved.add(e.lead_id)
    out.recovered = Rate("Recovered after a follow-up", len(moved), len(nudged),
                         "moved forward after a follow-up or the team")
    return out
