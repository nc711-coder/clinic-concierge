"""The tools the booking agent can call.

Each tool has a Pydantic model for its arguments. The model's JSON is validated before anything runs;
validation errors go back to the model as plain sentences ("phone: needs 10 digits") so it can ask the
patient and try again.

book_appointment never books on the model's say-so. The first call only records a *pending* booking.
The code then asks the patient to confirm, and books only after the patient replies yes
(see app/concierge.py). A small model can't skip that step or book a slot it made up.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Literal, Optional

from pydantic import BaseModel, Field, ValidationError, field_validator

from . import booking

# --------------------------------------------------------------------------- argument models

DayName = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_DAY_ALIASES = {d: d for d in booking.DAYS} | {
    "monday": "mon", "tuesday": "tue", "tues": "tue", "wednesday": "wed", "thursday": "thu", "thur": "thu",
    "thurs": "thu", "friday": "fri", "saturday": "sat", "sunday": "sun"}


def _blank_to_none(v):
    return None if isinstance(v, str) and v.strip().lower() in ("", "none", "null", "any", "n/a") else v


class CheckAvailability(BaseModel):
    """Find open appointment times. All filters are optional."""
    visit_type: Literal["evaluation", "follow_up"] = Field(
        description="'evaluation' for a new patient's first visit, 'follow_up' for returning patients")
    provider: Optional[str] = Field(None, description="Therapist's last name, e.g. 'Hsu', if the patient asked for one")
    date: Optional[str] = Field(None, description="A specific day as YYYY-MM-DD")
    day_of_week: Optional[DayName] = Field(None, description="mon, tue, wed, thu, fri or sat")
    time_of_day: Optional[Literal["morning", "afternoon", "evening"]] = None

    _blanks = field_validator("provider", "date", "day_of_week", "time_of_day", mode="before")(_blank_to_none)

    @field_validator("visit_type", mode="before")
    @classmethod
    def _visit(cls, v):
        v = str(v).lower().strip().replace("-", "_").replace(" ", "_")
        return {"initial_evaluation": "evaluation", "new_patient": "evaluation", "eval": "evaluation",
                "followup": "follow_up", "follow_up_visit": "follow_up"}.get(v, v)

    @field_validator("day_of_week", mode="before")
    @classmethod
    def _day(cls, v):
        return _DAY_ALIASES.get(str(v).lower().strip(), v) if v else v

    @field_validator("time_of_day", mode="before")
    @classmethod
    def _tod(cls, v):
        return str(v).lower().strip() if v else v

    @field_validator("date")
    @classmethod
    def _date(cls, v):
        if v:
            try:
                date.fromisoformat(v)
            except ValueError:
                raise ValueError("use the format YYYY-MM-DD")
        return v


def _clean_phone(v: str) -> str:
    digits = re.sub(r"\D", "", str(v))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        raise ValueError("needs a 10-digit US phone number")
    return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"


def _clean_name(v: str) -> str:
    v = " ".join(str(v).split())
    if len(re.findall(r"[A-Za-z]", v)) < 2 or len(v) > 80:
        raise ValueError("needs the patient's name")
    return v


def _clean_email(v):
    v = _blank_to_none(v)
    if v is None:
        return None
    v = str(v).strip()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}", v):
        raise ValueError("isn't a valid email address")
    return v.lower()


class SaveLead(BaseModel):
    """Save a prospective patient's contact details so the clinic can follow up."""
    name: str
    phone: str
    email: Optional[str] = None
    reason: Optional[str] = Field(None, description="Why they want physical therapy, in their words, max one sentence")
    insurance: Optional[str] = None
    urgency: Optional[Literal["low", "normal", "high"]] = None

    _n = field_validator("name")(lambda v: _clean_name(v))
    _p = field_validator("phone", mode="before")(lambda v: _clean_phone(v))
    _e = field_validator("email", mode="before")(lambda v: _clean_email(v))
    _b = field_validator("reason", "insurance", "urgency", mode="before")(_blank_to_none)


class BookAppointment(BaseModel):
    """Book a slot from check_availability. The patient will be asked to confirm before it's final."""
    slot_id: int = Field(description="slot_id from check_availability")
    visit_type: Literal["evaluation", "follow_up"]
    name: str
    phone: str
    email: Optional[str] = None
    reason: Optional[str] = None

    _v = field_validator("visit_type", mode="before")(lambda v: CheckAvailability._visit(v))
    _n = field_validator("name")(lambda v: _clean_name(v))
    _p = field_validator("phone", mode="before")(lambda v: _clean_phone(v))
    _e = field_validator("email", mode="before")(lambda v: _clean_email(v))
    _b = field_validator("reason", mode="before")(_blank_to_none)

    @field_validator("slot_id", mode="before")
    @classmethod
    def _slot(cls, v):
        m = re.search(r"\d+", str(v))
        if not m:
            raise ValueError("must be a slot_id number from check_availability")
        return int(m.group())


class EscalateToHuman(BaseModel):
    """Hand the conversation to front-desk staff."""
    reason: str = Field(description="Why a person is needed, e.g. 'patient asked for a person', 'billing dispute'")
    summary: str = Field(description="Two sentences: who the patient is and what they need")


TOOLS = {
    "check_availability": CheckAvailability,
    "save_lead": SaveLead,
    "book_appointment": BookAppointment,
    "escalate_to_human": EscalateToHuman,
}


def tool_specs() -> list[dict]:
    """Provider-neutral tool definitions: name, description, JSON schema for the arguments."""
    specs = []
    for name, model in TOOLS.items():
        schema = model.model_json_schema()
        schema.pop("title", None)
        for prop in schema.get("properties", {}).values():
            prop.pop("title", None)
            if "anyOf" in prop:                              # Optional[X] -> X; small models handle it better
                options = [o for o in prop.pop("anyOf") if o.get("type") != "null"]
                if len(options) == 1:
                    prop.update(options[0])
            prop.pop("default", None)
        specs.append({"name": name, "description": (model.__doc__ or "").strip(), "parameters": schema})
    return specs


# --------------------------------------------------------------------------- running a tool

class ToolContext:
    """What a tool needs: the database, the clinic schedule and this conversation's state."""

    def __init__(self, conn, schedule: booking.Schedule, session_id, state: dict):
        self.conn, self.schedule, self.session_id, self.state = conn, schedule, session_id, state


def _explain(e: ValidationError) -> str:
    parts = []
    for err in e.errors():
        field = ".".join(str(x) for x in err["loc"]) or "arguments"
        msg = err["msg"].removeprefix("Value error, ")
        if err["type"] == "missing":
            msg = "is required"
        parts.append(f"{field}: {msg}")
    return "; ".join(parts)


def run_tool(name: str, arguments: dict, ctx: ToolContext) -> dict:
    """Validate and run one tool call. Always returns a dict; errors are {"error": "..."} for the model."""
    model = TOOLS.get(name)
    if model is None:
        return {"error": f"unknown tool '{name}'. Available: {', '.join(TOOLS)}"}
    try:
        args = model.model_validate(arguments or {})
    except ValidationError as e:
        result = {"error": f"invalid arguments ({_explain(e)}). Ask the patient for the missing or wrong details."}
        booking.log_tool_call(ctx.conn, ctx.session_id, name, arguments or {}, result, ok=False)
        return result
    try:
        result = globals()[f"_{name}"](args, ctx)
        ok = "error" not in result
    except booking.BookingError as e:
        result, ok = {"error": str(e)}, False
    booking.log_tool_call(ctx.conn, ctx.session_id, name, args.model_dump(), result, ok)
    return result


def _fixed_visit_type(a, ctx: ToolContext):
    """Once the patient has said whether they're new or returning, the code's answer wins over the model's."""
    if ctx.state.get("visit_type_fixed") and a.visit_type != ctx.state.get("visit_type"):
        return a.model_copy(update={"visit_type": ctx.state["visit_type"]})
    return a


def _parse_day(text: str, today: date) -> date | None:
    t = text.strip().lower()
    if t == "today":
        return today
    if t == "tomorrow":
        return today + timedelta(days=1)
    try:
        return date.fromisoformat(t[:10])
    except ValueError:
        return None


def next_open(ctx: ToolContext, visit_type: str, limit: int = 3) -> list[dict]:
    """The earliest open times with no filters: what to offer when the asked-for day is full."""
    slots = booking.find_slots(ctx.conn, ctx.schedule, visit_type, limit=limit)
    return [{"slot_id": s["slot_id"], "when": s["when"], "provider": s["provider"]} for s in slots]


def remember_offered(ctx: ToolContext, slots: list[dict]) -> None:
    ctx.state.setdefault("offered_slots", {}).update({str(s["slot_id"]): s for s in slots})
    ctx.state["last_offered"] = list(slots)
    ctx.state.pop("chosen_slot", None)               # new times on the table: an earlier pick no longer counts


# --------------------------------------------------------------------------- reading name + phone in code

_PHONE = re.compile(r"(?:\+?1[\s.-]?)?\(?\b\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}\b")
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
_NAME_INTRO = re.compile(r"\b(my\s+name\s+is|name\s+is|name:|i\s+am|i'?m|im|it'?s|this\s+is|call\s+me)\b", re.I)
_NOT_A_NAME = set("""the a an one first second third last yes no ok okay sure please book it that this time slot
option phone number email and or my is at on for with thanks thank you hi hello hey mon monday tue tuesday wed
wednesday thu thursday fri friday sat saturday sun sunday morning afternoon evening am pm dr doctor cell mobile
works good great fine i me""".split())


def parse_contact(message: str) -> tuple[str | None, str | None, str | None]:
    """("Jane Doe", "703-555-0199", None) from "Jane Doe, 703-555-0199". Only returns a name when the rest of
    the message looks like one: 2-4 words, letters only, none of them everyday words."""
    phone = _PHONE.search(message)
    email = _EMAIL.search(message)
    rest = _EMAIL.sub(" ", _PHONE.sub(" ", message))
    rest = _NAME_INTRO.sub(" ", rest)
    rest = re.sub(r"\b(my|phone|number|cell|mobile|email|and|is|it|here)\b", " ", rest, flags=re.I)
    name = None
    for chunk in re.split(r"[,;\n]|\s-\s|[.!?](?:\s|$)", rest):
        chunk = " ".join(chunk.split()).strip(" .!:")
        words = re.findall(r"[^\W\d_][\w'’-]*", chunk)
        if (2 <= len(words) <= 4 and not any(w.lower() in _NOT_A_NAME for w in words)
                and len(" ".join(words)) >= len(chunk) - 2):                 # nothing but the words
            name = " ".join(w if any(c.isupper() for c in w) else w.capitalize() for w in words)
            break
    return name, phone.group(0) if phone else None, email.group(0) if email else None


def _check_availability(a: CheckAvailability, ctx: ToolContext) -> dict:
    a = _fixed_visit_type(a, ctx)
    on_date = None
    if a.date:
        today = datetime.now(ctx.schedule.tz).date()
        on_date = _parse_day(a.date, today)
        # Small models often guess the year from their training data ("2023-10-22").
        if on_date is None:
            return {"error": f"date must be YYYY-MM-DD. Today is {today.isoformat()}."}
        if on_date < today or on_date > today + timedelta(days=90):
            return {"error": f"{a.date} isn't a date we can book: today is {today.isoformat()} ({today:%A}). "
                             f"Use a date from the calendar in your instructions."}
    provider_id = None
    if a.provider:
        p = ctx.schedule.find_provider(a.provider)
        if not p:
            names = ", ".join(p["name"] for p in ctx.schedule.providers)
            return {"error": f"no therapist called '{a.provider}'. Therapists: {names}"}
        if a.visit_type == "evaluation" and not p.get("can_evaluate"):
            return {"error": f"{p['name']} doesn't do initial evaluations; leave provider empty or pick a PT"}
        provider_id = p["id"]
    found = booking.find_slots(ctx.conn, ctx.schedule, a.visit_type, provider_id=provider_id, on_date=on_date,
                               day_of_week=a.day_of_week, time_of_day=a.time_of_day, limit=3)
    slots = [{"slot_id": s["slot_id"], "when": s["when"], "provider": s["provider"]} for s in found]
    ctx.state["visit_type"] = a.visit_type
    if slots:
        remember_offered(ctx, slots)                 # only these slot_ids can be booked later
        return {"slots": slots}
    # Nothing on that day / time: never leave the patient with just "no". Offer the next open times.
    alternatives = next_open(ctx, a.visit_type)
    remember_offered(ctx, alternatives)
    if not alternatives:
        return {"slots": [], "note": "no open times in the next 3 weeks; offer to have the front desk call them"}
    return {"slots": [], "next_available": alternatives,
            "note": "nothing open for what was asked; offer these next available times instead"}


def _save_lead(a: SaveLead, ctx: ToolContext) -> dict:
    lead_id = booking.save_lead(ctx.conn, ctx.session_id, name=a.name, phone=a.phone, email=a.email,
                                reason=a.reason, insurance=a.insurance, urgency=a.urgency)
    ctx.state["lead_id"] = lead_id
    ctx.state["contact"] = {"name": a.name, "phone": a.phone, "email": a.email}
    return {"saved": True, "lead_id": lead_id}


def _book_appointment(a: BookAppointment, ctx: ToolContext) -> dict:
    a = _fixed_visit_type(a, ctx)
    # Only slots this conversation was actually shown can be booked: stops invented or stale slot ids.
    offered = ctx.state.get("offered_slots", {})
    if str(a.slot_id) not in offered:
        return {"error": "that slot_id wasn't offered in this conversation. Call check_availability and use one "
                         "of the slot_id values it returns."}
    slot = booking.get_slot(ctx.conn, a.slot_id)
    if not slot or slot["status"] != "open":
        return {"error": "that time is no longer available. Call check_availability again."}
    when = booking.label(slot["starts_at"], ctx.schedule.tz)
    ctx.state["pending_booking"] = a.model_dump() | {"when": when, "provider": slot["provider_name"]}
    ctx.state["contact"] = {"name": a.name, "phone": a.phone, "email": a.email}
    return {"status": "awaiting_patient_confirmation", "when": when, "provider": slot["provider_name"]}


def _escalate_to_human(a: EscalateToHuman, ctx: ToolContext) -> dict:
    # A handoff is only useful if the front desk can reach the patient: without a phone number, the
    # code asks for one first (app/concierge.py) and creates the handoff when it arrives.
    if not (ctx.state.get("contact") or {}).get("phone"):
        ctx.state["handoff_pending"] = {"reason": a.reason, "summary": a.summary, "asked": 1}
        return {"status": "need_phone", "note": "the system will ask the patient for their phone number"}
    handoff_id = booking.create_handoff(ctx.conn, ctx.session_id, reason=a.reason, summary=a.summary,
                                        lead_id=ctx.state.get("lead_id"))
    ctx.state["handed_off"] = True
    return {"handoff_id": handoff_id, "status": "front desk notified"}


def confirmation_message(pending: dict, schedule: booking.Schedule) -> str:
    """The exact question the patient must say yes to. Written by code, not the model."""
    label = schedule.visit_types[pending["visit_type"]]["label"]
    contact = f"{pending['name']}, {pending['phone']}" + (f", {pending['email']}" if pending.get("email") else "")
    article = "an" if label[0].lower() in "aeiou" else "a"
    return (f"Just to confirm: {article} {label} with {pending['provider']} on {pending['when']}, for {contact}. "
            f"Shall I book it? (yes / no)")


# --------------------------------------------------------------------------- picking an offered time in code

_ORDINALS = {"first": 0, "1st": 0, "earliest": 0, "second": 1, "2nd": 1, "third": 2, "3rd": 2, "fourth": 3, "4th": 3,
             "fifth": 4, "5th": 4, "last": -1, "latest": -1}
_ORDINAL = re.compile(r"\b(?:the\s+(first|1st|earliest|second|2nd|third|3rd|fourth|4th|fifth|5th|last|latest)"
                      r"(?!\s+(?:time|visit|appointment|evaluation)\b)"
                      r"|(first|1st|earliest|second|2nd|third|3rd|fourth|4th|fifth|5th|last|latest)\s+(?:one|option|slot|spot))\b",
                      re.I)
_CLOCK = re.compile(r"\b(\d{1,2})(?::(\d\d))?\s*(am|pm|a\.m\.|p\.m\.)|\bat\s+(\d{1,2})(?::(\d\d))?\b", re.I)
_DAY = re.compile(r"\b(mon|tue|wed|thu|fri|sat)[a-z]*\b", re.I)
_NUMBER_ONLY = re.compile(r"^\s*(?:#|no\.?\s*|number\s+|option\s+)?([1-9])\s*[.)!]?\s*$", re.I)


_SAME = object()


def pick_offered_slot(message: str, offered: list[dict], shown=_SAME) -> dict | None:
    """Which of the times just offered does the patient mean? Only returns a slot when it's unambiguous:
    "the first one", "the second option", "8am", "Wednesday at 9", "the 10:00 one".

    `shown` is the list in the order the reply showed it to the patient (see shown_order). Ordinals count
    in that list, because the model doesn't always list every slot the tool returned, or in the same order.
    shown=None means we don't know what the patient saw, so "the second one" is left to the model."""
    if not offered:
        return None
    m = _ORDINAL.search(message)
    n = _NUMBER_ONLY.match(message)                 # "2" (the widget's buttons send the option number)
    if m or n:
        seen = offered if shown is _SAME else shown
        if not seen:
            return None
        i = int(n.group(1)) - 1 if n else _ORDINALS[(m.group(1) or m.group(2)).lower()]
        return seen[i] if -len(seen) <= i < len(seen) else None
    c = _CLOCK.search(message)
    if not c:
        return None
    hour = int(c.group(1) or c.group(4))
    minute = int(c.group(2) or c.group(5) or 0)
    ampm = (c.group(3) or "").lower().replace(".", "")
    day = _DAY.search(message)
    matches = []
    for s in offered:
        t = re.search(r"at (\d{1,2}):(\d\d) (AM|PM)$", s["when"])
        if not t:
            continue
        h, mi, ap = int(t.group(1)), int(t.group(2)), t.group(3).lower()
        if h != hour or mi != minute or (ampm and ampm != ap):
            continue
        if day and not s["when"].lower().startswith(day.group(1).lower()):
            continue
        matches.append(s)
    return matches[0] if len(matches) == 1 else None


_TIME_MENTION = re.compile(r"\b(\d{1,2})(?::(\d\d))?\s*(am|pm|a\.m\.|p\.m\.)|\b(\d{1,2}):(\d\d)\b|\b(noon)\b", re.I)
_DAY_MENTION = re.compile(r"\b(mon|tue|wed|thu|fri|sat|sun)(?:day|sday|nesday|rsday|rs|urday|s)?\b", re.I)


def _slot_parts(s: dict):
    t = re.search(r"^(\w{3}).*at (\d{1,2}):(\d\d) (AM|PM)$", s["when"])
    surname = (s.get("provider") or "").split()[-1].lower() if s.get("provider") else ""
    return (t.group(1).lower(), int(t.group(2)), int(t.group(3)), t.group(4).lower(), surname) if t else None


def shown_order(reply: str, offered: list[dict]) -> list[dict] | None:
    """The offered slots in the order the reply mentions them.

    Returns [] if the reply mentions no times, and None if it mentions a time we can't pin to exactly one
    offered slot (made up, or ambiguous) - then nobody can be sure what "the second one" means.
    """
    mentions = list(_TIME_MENTION.finditer(reply or ""))
    if not mentions:
        return []
    parts = [(s, p) for s in offered if (p := _slot_parts(s))]
    order, seen = [], set()
    for i, m in enumerate(mentions):
        if m.group(6):
            hour, minute, ampm = 12, 0, "pm"
        else:
            hour = int(m.group(1) or m.group(4))
            minute = int(m.group(2) or m.group(5) or 0)
            ampm = (m.group(3) or "").lower().replace(".", "")
        cands = [(s, p) for s, p in parts if p[1] == hour and p[2] == minute and (not ampm or p[3] == ampm)]
        # Narrow by the therapist named next to this time ("10:00 AM with Dr. Bell"), then by the day:
        # "10:00 AM ... on Thursday" right after it, else the last day named before it ("Thursday: 8 AM, 10 AM").
        before = reply[mentions[i - 1].end() if i else 0:m.start()].lower()
        after = reply[m.end():mentions[i + 1].start() if i + 1 < len(mentions) else len(reply)].lower()
        if len(cands) > 1:
            named = ([(s, p) for s, p in cands if p[4] and p[4] in after]
                     or [(s, p) for s, p in cands if p[4] and p[4] in before])
            cands = named or cands
        if len(cands) > 1:
            on_day = re.search(r"\bon\s+" + _DAY_MENTION.pattern, after, re.I)
            days = list(_DAY_MENTION.finditer(reply[:m.start()]))
            day = on_day.group(1) if on_day else (days[-1].group(1) if days else None)
            if day:
                cands = [(s, p) for s, p in cands if p[0] == day.lower()] or cands
        if len(cands) != 1:
            return None
        s = cands[0][0]
        if s["slot_id"] not in seen:
            seen.add(s["slot_id"])
            order.append(s)
    return order
