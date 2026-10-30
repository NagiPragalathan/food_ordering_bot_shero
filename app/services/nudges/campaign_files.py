"""Campaigns in and out of Excel, CSV and JSON.

The team builds campaigns on the Nudges page, or writes them in a spreadsheet
and imports them. Nothing about a campaign is fixed by the page: who it is
for, when it starts, the follow-up stages and how often each reply group is
messaged, the words, the template, the limits.

One row is one campaign, matched on its name: a name already here is updated,
a new name is added, paused. Every file is checked first and shown row by
row, so nothing changes until the team says so; a row with a problem is
skipped with the reason. On an update only the cells filled in change, and a
cell with just "-" clears that setting. An export imports straight back.

A follow-up journey's timing is one cell per reply group, in days:

    3x3, 10x3, 30   every 3 days 3 times, then every 10 days 3 times,
                    then every 30 days until the journey ends
"""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models import NudgeCampaign, NudgeKind
from app.services.nudges import store
from app.services.nudges.config import (ACTIVE_KINDS, NudgeConfigError, build_campaign_config,
                                        dump_config, parse_active_kind)

log = get_logger(__name__)

MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_ROWS = 500
CLEAR = "-"

KIND_LABELS = {
    NudgeKind.FOLLOW_UP: "Follow-up journey",
    NudgeKind.ABANDONED_CART: "Abandoned cart",
    NudgeKind.ABANDONED_CHECKOUT: "Unpaid order",
}
assert set(KIND_LABELS) == set(ACTIVE_KINDS)

# (path, heading, how the cell is read, help). "steps" paths are a reply
# group's timing ("3x3, 10x3, 30"); the rest are config paths or the
# campaign's own fields.
_GROUP = ('Days between follow-ups for the {g} group: "3x3, 10x3, 30" is every 3 days 3 times, '
          'then every 10 days 3 times, then every 30 days until the journey ends.')
COLUMNS: list[tuple[str, str, str, str]] = [
    ("name", "Name", "text", "The campaign's name. A name already here is updated; a new one is added, paused."),
    ("kind", "Type", "kind", "Follow-up journey (the SMO six months), Abandoned cart or Unpaid order."),
    ("trigger.after_minutes", "Wait (minutes)", "raw", "Abandoned cart, Unpaid order: minutes after the cart was left or the payment link lapsed."),
    ("trigger.within_hours", "Give up after (hours)", "raw", "Abandoned cart, Unpaid order: not sent once this many hours have passed."),
    ("trigger.min_items", "Minimum dishes", "raw", "Abandoned cart: only carts with at least this many dishes."),
    ("trigger.min_value", "Minimum cart ($)", "raw", "Abandoned cart: only carts worth at least this much."),
    ("trigger.payment_statuses", "Unpaid because", "list", "Unpaid order: expired, failed (either or both)."),
    ("trigger.restore_cart", "Put dishes back in the cart", "bool", "Unpaid order: Yes or No."),
    ("trigger.days_since_signup", "Days since first message", "raw", "First order, Follow-up journey: starts this many days after they first wrote in."),
    ("trigger.max_days_since_signup", "Up to days since first message", "raw", "First order: not sent to anyone who first wrote in longer ago."),
    ("trigger.require_details", "Only with name and email", "bool", "First order, Follow-up journey: Yes = only people who gave their details."),
    ("message.text", "Message", "text", "Sent inside WhatsApp's 24-hour window. Use placeholders like {first_name}. A follow-up journey sends its Messages library instead."),
    ("message.button", "Button", "raw", "order_link (an Order now button), url, or none."),
    ("message.button_text", "Button text", "text", "e.g. Order now."),
    ("message.button_url", "Button link", "text", "For Button = url."),
    ("message.template.name", "WhatsApp template", "text", "An approved Gallabox template, for sends outside the 24-hour window."),
    ("message.template.params", "Template values", "pipe", "Values for the template's {{1}}, {{2}}..., separated by |."),
    ("message.template.button", "Template button", "raw", "order_link or none."),
    ("message.template.always", "Always use the template", "bool", "Yes: the template even inside the 24-hour window."),
    ("schedule.send_window.start", "Send from", "time", "Their local time, e.g. 09:00. Empty: the Settings tab's hours."),
    ("schedule.send_window.end", "Send until", "time", "e.g. 21:00. Fill both or neither."),
    ("schedule.days", "Send on", "list", "mon, tue, wed, thu, fri, sat, sun. Empty: every day."),
    ("limits.max_per_customer", "Max per customer", "raw", "Most times one customer gets it. - for no limit."),
    ("limits.cooldown_days", "Days before again", "raw", "Days before the same customer can get it again."),
    ("limits.daily_cap", "Max per day", "raw", "Most sends a day. - for no limit."),
    ("limits.per_run_cap", "Max per run", "raw", "Most sends each run (every 5 minutes). - for no limit."),
    ("zoho.note", "Zoho note", "bool", "Yes: a note on their Zoho record for each send."),
    ("journey.pool", "Message pool", "text", "Follow-up journey: the Messages library pool it picks from, e.g. SMO."),
    ("journey.length_days", "Journey length (days)", "raw", "Follow-up journey: how long it runs, e.g. 183 for six months."),
    ("journey.stage_days", "Days per stage", "raw", "Follow-up journey: 30 = month 1 messages first, then month 2..."),
    ("journey.pick", "Pick messages", "raw", "Follow-up journey: random or in_order."),
    ("journey.avoid_repeat", "Don't repeat the last", "raw", "Follow-up journey: how many recent messages one customer is not sent again."),
    ("journey.avoid_angle_repeat", "Don't repeat the angle of the last", "raw", "Follow-up journey: nor a message with the same angle as any of their last N."),
    ("journey.borrow_other_months", "Borrow from other months", "bool", "Follow-up journey: Yes = use the nearest month's messages when a month runs out."),
    ("journey.pause_after_chat_hours", "Wait after a chat (hours)", "raw", "Follow-up journey: no follow-up this soon after they messaged the bot."),
    ("journey.resume_after_days", "Resume after an unfinished order (days)", "raw", "Follow-up journey: after ORDER FOOD with no order, follow-ups resume this many days later."),
    ("journey.no_response.steps", "No response: follow-ups", "steps", _GROUP.format(g="No response")),
    ("journey.no_response.max_attempts", "No response: most sends", "raw", "Then their journey ends. - for no limit."),
    ("journey.later.steps", "Later: follow-ups", "steps", _GROUP.format(g="Later") + " Counted from their LATER reply."),
    ("journey.later.max_attempts", "Later: most sends", "raw", "- for no limit."),
    ("journey.no.steps", "No: follow-ups", "steps", _GROUP.format(g="No")),
    ("journey.no.max_attempts", "No: most sends", "raw", "- for no limit."),
]
PATHS = [path for path, *_ in COLUMNS]
HEADINGS = {path: heading for path, heading, *_ in COLUMNS}
READ_AS = {path: how for path, _, how, _ in COLUMNS}
TOP = ("name", "kind")


def _key(text) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


_BY_HEADING = {_key(heading): path for path, heading, *_ in COLUMNS}
_BY_HEADING.update({_key(path): path for path in PATHS})
_BY_HEADING.update({_key("Campaign"): "name", _key("Kind"): "kind"})
_KINDS = {**{_key(label): str(kind) for kind, label in KIND_LABELS.items()},
          **{_key(kind): str(kind) for kind in ACTIVE_KINDS}}


def columns_help() -> list[dict]:
    return [{"field": path, "heading": heading, "help": text} for path, heading, _, text in COLUMNS]


# --- reading cells -------------------------------------------------------------------------
def _blank(value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _cell(path: str, value):
    """A cell as the config wants it. CLEAR is None (the setting cleared)."""
    if isinstance(value, str):
        value = value.strip()
        if value == CLEAR:
            return [] if READ_AS[path] in ("list", "pipe") else None
    how = READ_AS[path]
    if how == "kind":
        kind = _KINDS.get(_key(value))
        if kind is None:
            raise NudgeConfigError(f"Type {value!r} is not one of: {', '.join(KIND_LABELS.values())}")
        return kind
    if how == "int":
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise NudgeConfigError(f"{HEADINGS[path]}: {value!r} is not a whole number") from None
        if number != int(number):
            raise NudgeConfigError(f"{HEADINGS[path]}: {value!r} is not a whole number")
        return int(number)
    if how == "bool":
        if isinstance(value, bool):
            return value
        text = _key(value)
        if text in ("yes", "y", "true", "1", "on"):
            return True
        if text in ("no", "n", "false", "0", "off"):
            return False
        raise NudgeConfigError(f"{HEADINGS[path]}: use Yes or No, not {value!r}")
    if how in ("list", "pipe"):
        if isinstance(value, list):
            return [str(v).strip() for v in value if str(v).strip()]
        parts = str(value).split("|") if how == "pipe" else re.split(r"[,;\n]", str(value))
        return [p.strip() for p in parts if p.strip()]
    if how == "time":
        if isinstance(value, (time, datetime)):
            return value.strftime("%H:%M")
        if isinstance(value, float) and 0 <= value < 1:        # Excel stores a time as a day fraction
            minutes = round(value * 24 * 60)
            return f"{minutes // 60:02d}:{minutes % 60:02d}"
        return str(value)
    if how == "datetime":
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, date):
            return datetime.combine(value, time(0)).isoformat()
        text = str(value)
        return f"{text}T00:00:00" if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text) else text
    if how == "steps":
        return parse_steps(value)
    if how == "text":
        return str(value)
    return value


def parse_steps(value) -> list[dict]:
    """ "3x3, 10x3, 30" -> every 3 days x3, every 10 days x3, every 30 days. """
    if isinstance(value, list):
        return value
    steps = []
    for part in re.split(r"[,;\n]", str(value)):
        part = part.strip().lower().replace("days", "").replace("day", "")
        if not part:
            continue
        match = re.fullmatch(r"(?:every\s*)?(\d+(?:\.\d+)?)\s*(?:[x*×]\s*(\d+))?\s*(?:times)?", part.strip())
        if not match:
            raise NudgeConfigError(f'Follow-ups: {part!r} is not "days" or "days x times", e.g. 3x3, 10x3, 30')
        every = float(match.group(1))
        steps.append({"every_days": int(every) if every.is_integer() else every,
                      "times": int(match.group(2)) if match.group(2) else None})
    if not steps:
        raise NudgeConfigError("Follow-ups: give at least one gap in days, e.g. 3x3, 10x3, 30")
    return steps


def format_steps(steps: list[dict]) -> str:
    out = []
    for step in steps or []:
        every = step.get("every_days")
        every = int(every) if isinstance(every, float) and every.is_integer() else every
        out.append(f"{every}x{step['times']}" if step.get("times") else f"{every}")
    return ", ".join(out)


def _set(tree: dict, path: str, value) -> None:
    *parents, leaf = path.split(".")
    for part in parents:
        tree = tree.setdefault(part, {})
    tree[leaf] = value


@dataclass
class Row:
    number: int
    top: dict                    # name, kind, priority, promotion_code (those given)
    config: dict                 # nested, only the cells filled in
    error: str | None = None


def _row(number: int, cells: dict, *, nulls: bool = False) -> Row:
    """`nulls`: a JSON null clears the setting, as "-" does in a spreadsheet."""
    row = Row(number, {}, {})
    try:
        for path, value in cells.items():
            if value is None and nulls and path not in ("name", "kind", "priority"):
                value = CLEAR
            if _blank(value):
                continue
            value = _cell(path, value)
            if path in TOP:
                row.top[path] = value
            else:
                _set(row.config, path, value)
    except NudgeConfigError as exc:
        row.error = str(exc)
    return row


# --- reading a file -------------------------------------------------------------------------
@dataclass
class FileRows:
    rows: list[Row]
    columns: list[str]
    ignored: list[str] = field(default_factory=list)


def read_file(filename: str, raw: bytes) -> FileRows:
    """The campaigns in an uploaded .xlsx, .csv or .json file."""
    if len(raw) > MAX_FILE_BYTES:
        raise NudgeConfigError(f"The file is over {MAX_FILE_BYTES // 1024 // 1024} MB")
    if not raw.strip():
        raise NudgeConfigError("The file is empty")
    name = (filename or "").lower()
    if name.endswith(".json") or raw.lstrip()[:1] in (b"[", b"{"):
        parsed = _read_json(raw)
    elif name.endswith((".xlsx", ".xlsm")) or raw[:2] == b"PK":
        parsed = _read_xlsx(raw)
    elif name.endswith(".xls"):
        raise NudgeConfigError("Old .xls files cannot be read; save it as .xlsx or CSV and try again")
    else:
        parsed = _read_csv(raw)
    if not parsed.rows:
        raise NudgeConfigError("No campaigns found in the file")
    if len(parsed.rows) > MAX_ROWS:
        raise NudgeConfigError(f"At most {MAX_ROWS} campaigns in one file")
    return parsed


def _flatten(tree: dict, prefix: str = "") -> dict:
    """{"journey": {"no": {"steps": [...]}}} -> {"journey.no.steps": [...]}."""
    flat = {}
    for key, value in tree.items():
        path = f"{prefix}{key}"
        if path in READ_AS:
            flat[path] = value
        elif isinstance(value, dict):
            flat.update(_flatten(value, f"{path}."))
        else:
            flat[path] = value
    return flat


def _read_json(raw: bytes) -> FileRows:
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise NudgeConfigError(f"That is not valid JSON ({exc})") from None
    if isinstance(data, dict):
        data = data.get("campaigns", [data] if "name" in data else None)
    if not isinstance(data, list):
        raise NudgeConfigError('JSON must be a list of campaigns, or {"campaigns": [...]}')
    rows, columns, ignored = [], [], []
    for number, item in enumerate(data, start=1):
        if not isinstance(item, dict):
            raise NudgeConfigError(f"Campaign {number} in the JSON is not an object")
        item = dict(item)
        flat = _flatten(item.pop("config", None) or {})
        for key, value in item.items():
            flat[key] = value
        cells = {}
        for key, value in flat.items():
            path = key if key in READ_AS else _BY_HEADING.get(_key(key))
            if path is None:
                if key not in ignored and key not in ("active", "is_active", "id"):
                    ignored.append(key)
                continue
            cells[path] = value
            if path not in columns:
                columns.append(path)
        rows.append(_row(number, cells, nulls=True))
    if "name" not in columns:
        raise NudgeConfigError('Each campaign needs a "name"')
    return FileRows(rows, columns, ignored)


def _read_csv(raw: bytes) -> FileRows:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    return _table([row for row in csv.reader(io.StringIO(text), dialect)])


def _read_xlsx(raw: bytes) -> FileRows:
    from openpyxl import load_workbook

    try:
        book = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001 - any unreadable workbook
        raise NudgeConfigError(f"That Excel file cannot be opened ({exc})") from None
    try:
        sheets = [s for s in book.worksheets if s.title.lower() == "campaigns"] or book.worksheets
        for sheet in sheets:
            table = [list(row) for row in sheet.iter_rows(values_only=True)]
            if any(_BY_HEADING.get(_key(c)) == "name" for row in table[:10] for c in row):
                return _table(table)
        raise NudgeConfigError("No sheet has a Name column")
    finally:
        book.close()


def _table(table: list[list]) -> FileRows:
    start = next((i for i, row in enumerate(table[:10])
                  if any(_BY_HEADING.get(_key(c)) == "name" for c in row)), None)
    if start is None:
        raise NudgeConfigError("The file needs a Name column")
    mapping, ignored = {}, []
    for index, heading in enumerate(table[start]):
        path = _BY_HEADING.get(_key(heading))
        if path and path not in mapping.values():
            mapping[index] = path
        elif str(heading or "").strip():
            ignored.append(str(heading).strip())
    rows = []
    for number, cells in enumerate(table[start + 1:], start=start + 2):
        values = {path: cells[index] if index < len(cells) else None for index, path in mapping.items()}
        if all(_blank(v) for v in values.values()):
            continue
        rows.append(_row(number, values))
    return FileRows(rows, list(mapping.values()), ignored)


# --- importing ------------------------------------------------------------------------------
UPSERT, ADD_ONLY = "upsert", "add_only"
MODES = (UPSERT, ADD_ONLY)


@dataclass
class RowResult:
    row: int
    code: str                    # the campaign's name
    action: str                  # create | update | unchanged | skip | error
    title: str = ""              # its type
    changes: list[str] = field(default_factory=list)
    error: str | None = None


def _changed(old: dict, new: dict, prefix: str = "") -> list[str]:
    """The column paths that differ between two configs."""
    paths = []
    for key in sorted(set(old) | set(new)):
        path = f"{prefix}{key}"
        a, b = old.get(key), new.get(key)
        if isinstance(a, dict) and isinstance(b, dict) and path not in READ_AS:
            paths += _changed(a, b, f"{path}.")
        elif a != b:
            paths.append(path)
    return paths


async def import_rows(session: AsyncSession, parsed: FileRows, *, mode: str = UPSERT,
                      commit: bool = False, actor: str) -> dict:
    """Check (commit=False) or apply an import. The same report either way."""
    if mode not in MODES:
        raise NudgeConfigError(f"mode is one of {list(MODES)}")
    by_name: dict[str, list[NudgeCampaign]] = {}
    for campaign in await store.list_campaigns(session):
        by_name.setdefault(" ".join(campaign.name.lower().split()), []).append(campaign)
    results, seen = [], {}

    for row in parsed.rows:
        name = " ".join(str(row.top.get("name") or "").split())
        label = KIND_LABELS.get(row.top.get("kind"), "")
        if row.error:
            results.append(RowResult(row.number, name, "error", label, error=row.error))
            continue
        if not name:
            results.append(RowResult(row.number, "", "error", label, error="Name is empty"))
            continue
        key = name.lower()
        if key in seen:
            results.append(RowResult(row.number, name, "error", label,
                                     error=f"Same name as row {seen[key]}; only the first is used"))
            continue
        seen[key] = row.number
        matches = by_name.get(key, [])
        if len(matches) > 1:
            results.append(RowResult(row.number, name, "error", label, error=(
                f"{len(matches)} campaigns here are called {name!r}; rename one first")))
            continue
        current = matches[0] if matches else None
        if current is not None and mode == ADD_ONLY:
            results.append(RowResult(row.number, name, "skip", KIND_LABELS.get(current.kind, ""),
                                     error="Already here (adding new campaigns only)"))
            continue
        try:
            result, data = await _check(session, row, name, current)
        except NudgeConfigError as exc:
            results.append(RowResult(row.number, name, "error", label, error=str(exc)))
            continue
        results.append(result)
        if commit and result.action == "create":
            await store.create_campaign(session, {**data, "is_active": False}, actor=actor)
        elif commit and result.action == "update":
            await store.update_campaign(session, current, data, actor=actor)

    if commit:
        await session.flush()
        log.info("nudge_campaigns_imported", actor=actor,
                 created=sum(r.action == "create" for r in results),
                 updated=sum(r.action == "update" for r in results),
                 errors=sum(r.action == "error" for r in results))
    counts = {action: sum(r.action == action for r in results)
              for action in ("create", "update", "unchanged", "skip", "error")}
    return {"committed": commit, "mode": mode, "counts": counts, "turned_off": [],
            "columns": [HEADINGS[c] for c in parsed.columns], "ignored_columns": parsed.ignored,
            "rows": [r.__dict__ for r in results]}


async def _check(session, row: Row, name: str, current: NudgeCampaign | None):
    """What this row would do, worked out exactly as saving it would."""
    if current is None and "kind" not in row.top:
        raise NudgeConfigError("Type is needed for a new campaign")
    kind = parse_active_kind(row.top.get("kind") or current.kind)
    # Matched without regard to capitals; the saved name keeps its own.
    data: dict = {"name": current.name if current else name, "kind": str(kind), "config": row.config}
    has_coupon = current is not None and current.promotion_id is not None

    stored = dict(current.config or {}) if current else {}
    if current is not None and str(kind) != current.kind:
        stored.pop("trigger", None)
    config = dump_config(kind, build_campaign_config(kind, row.config, current=stored or None,
                                                     has_coupon=has_coupon))
    if current is None:
        return RowResult(row.number, name, "create", KIND_LABELS[kind]), data

    changes = _changed(dict(current.config or {}), config)
    if str(kind) != current.kind:
        changes.insert(0, "kind")
    return RowResult(row.number, name, "update" if changes else "unchanged", KIND_LABELS[kind],
                     changes=[HEADINGS.get(c, c) for c in _dedupe(changes)]), data


def _dedupe(paths: list[str]) -> list[str]:
    out = []
    for path in paths:
        # A whole reply group that changed reads as its follow-ups column.
        if path in ("journey.no_response", "journey.later", "journey.no"):
            path += ".steps"
        if path not in out:
            out.append(path)
    return out


# --- writing a file ----------------------------------------------------------------------------
MISSING = object()


def _get(tree: dict, path: str, default=None):
    for part in path.split("."):
        if not isinstance(tree, dict) or part not in tree:
            return default
        tree = tree[part]
    return tree


def _defaults(kind: str) -> dict:
    try:
        return dump_config(kind, build_campaign_config(kind, None, has_coupon=True))
    except NudgeConfigError:
        return {}


def _cells(view: dict) -> dict:
    """One campaign as {path: cell text}. A setting left empty on purpose,
    where the type's default is not, is written "-" so it imports back empty."""
    config = view.get("config") or {}
    defaults = _defaults(view["kind"])
    cells = {"name": view["name"], "kind": KIND_LABELS.get(view["kind"], view["kind"])}
    for path in PATHS:
        if path in TOP:
            continue
        value = _get(config, path, MISSING)
        how = READ_AS[path]
        if value is MISSING:
            cells[path] = ""
        elif value is None or value == []:
            cells[path] = CLEAR if _get(defaults, path) not in (None, []) else ""
        elif how == "steps":
            cells[path] = format_steps(value)
        elif how == "pipe":
            cells[path] = " | ".join(value)
        elif how == "list":
            cells[path] = ", ".join(str(v) for v in value)
        elif how == "bool":
            cells[path] = "Yes" if value else "No"
        else:
            cells[path] = value
    return cells


def _used(views: list[dict]) -> list[str]:
    """Columns worth writing: the campaign fields and anything any campaign uses."""
    rows = [_cells(v) for v in views]
    return [p for p in PATHS if p in TOP or p == "message.text"
            or any(str(r.get(p, "")).strip() for r in rows)]


def to_json(views: list[dict]) -> bytes:
    items = [{"name": v["name"], "type": KIND_LABELS.get(v["kind"], v["kind"]),
              "active": bool(v.get("is_active")), "config": _shown(v.get("config") or {})}
             for v in views]
    return json.dumps({"exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                       "count": len(items), "campaigns": items},
                      ensure_ascii=False, indent=2).encode("utf-8")


def _shown(config: dict) -> dict:
    """The config without the sections the page no longer offers."""
    return {k: v for k, v in config.items() if k not in ("audience", "offer")}


def to_csv(views: list[dict]) -> bytes:
    paths = _used(views)
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow([HEADINGS[p] for p in paths])
    for view in views:
        cells = _cells(view)
        writer.writerow([cells.get(p, "") for p in paths])
    return ("﻿" + out.getvalue()).encode("utf-8")


def to_xlsx(views: list[dict]) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.datavalidation import DataValidation

    paths = _used(views)
    book = Workbook()
    sheet = book.active
    sheet.title = "Campaigns"
    sheet.append([HEADINGS[p] for p in paths])
    for view in views:
        cells = _cells(view)
        sheet.append([cells.get(p, "") for p in paths])
    head = PatternFill("solid", fgColor="028582")
    for index, path in enumerate(paths, start=1):
        letter = get_column_letter(index)
        sheet.column_dimensions[letter].width = {"name": 28, "message.text": 60}.get(path, 18)
        cell = sheet.cell(row=1, column=index)
        cell.font, cell.fill = Font(bold=True, color="FFFFFF"), head
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    for row in sheet.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=paths[cell.column - 1] == "message.text")
    sheet.freeze_panes = "B2"
    rule = DataValidation(type="list", formula1='"' + ",".join(KIND_LABELS.values()) + '"', allow_blank=True)
    letter = get_column_letter(paths.index("kind") + 1)
    rule.add(f"{letter}2:{letter}{max(sheet.max_row, 200)}")
    sheet.add_data_validation(rule)

    guide = book.create_sheet("How to fill it in")
    guide.append(["Column", "What to put in it"])
    for path, heading, _, text in COLUMNS:
        guide.append([heading, text])
    guide.append([])
    guide.append(["Any column", "Only the columns you need: missing ones keep their saved value "
                                "(or the type's default for a new campaign). Add any other column "
                                "from this list. A cell with just - clears that setting."])
    guide.column_dimensions["A"].width = 34
    guide.column_dimensions["B"].width = 110
    for cell in guide[1]:
        cell.font, cell.fill = Font(bold=True, color="FFFFFF"), head
    for row in guide.iter_rows(min_row=2):
        if len(row) > 1:
            row[1].alignment = Alignment(wrap_text=True, vertical="top")
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


FORMATS = {
    "xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", to_xlsx),
    "csv": ("text/csv; charset=utf-8", to_csv),
    "json": ("application/json", to_json),
}


def export(views: list[dict], fmt: str) -> tuple[bytes, str]:
    if fmt not in FORMATS:
        raise NudgeConfigError(f"format is one of {list(FORMATS)}")
    media_type, writer = FORMATS[fmt]
    return writer(views), media_type


# An example file to start from - not built into the page.
SAMPLE = [
    {"name": "6-month follow-up (SMO)", "kind": "follow_up", "config": {
        "trigger": {"days_since_signup": 1, "require_details": False},
        "message": {"text": "Each follow-up uses a message from the Messages library.", "button": "none"},
        "journey": {"pool": "SMO", "length_days": 183, "stage_days": 30, "pick": "random",
                    "no_response": {"steps": [{"every_days": 3, "times": 3}, {"every_days": 10, "times": 3},
                                              {"every_days": 30, "times": None}]},
                    "later": {"steps": [{"every_days": 10, "times": 3}, {"every_days": 30, "times": None}]},
                    "no": {"steps": [{"every_days": 30, "times": None}], "max_attempts": 6}}}},
    {"name": "Cart reminder", "kind": "abandoned_cart", "config": {
        "trigger": {"after_minutes": 60, "within_hours": 23, "min_items": 1},
        "message": {"text": "Hi {first_name}, your cart is still waiting: {cart_items}. Tap below to finish your order.",
                    "button": "order_link", "button_text": "Order now"}}},
    {"name": "Unpaid order follow-up", "kind": "abandoned_checkout", "config": {
        "trigger": {"after_minutes": 180, "within_hours": 72, "payment_statuses": ["expired", "failed"],
                    "restore_cart": True},
        "message": {"text": "Hi {first_name}, your order {order_number} wasn't paid, so it hasn't been "
                            "cooked. Want to place it again?",
                    "button": "order_link", "button_text": "Order now"},
        "schedule": {"send_window": {"start": "10:00", "end": "19:00"}},
        "limits": {"cooldown_days": 3}}},
]


def sample(fmt: str) -> tuple[bytes, str]:
    views = [{**item, "config": dump_config(item["kind"], build_campaign_config(
        item["kind"], item["config"], has_coupon=False))} for item in SAMPLE]
    return export(views, fmt)
