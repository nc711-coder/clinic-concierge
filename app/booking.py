"""Appointments, leads and handoffs in Postgres.

Availability comes from data/schedule.json: providers, their hours, clinic hours, holiday closures and
which visit types each day allows. `python -m app.seed` turns that into bookable one-hour slots and marks
some as already taken by other patients, so the calendar looks like a real one.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time as clock
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
SCHEDULE_FILE = ROOT / "data" / "schedule.json"
SCHEMA_FILE = ROOT / "sql" / "002_booking.sql"
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
MIN_NOTICE = timedelta(hours=2)          # don't offer a slot starting sooner than this
log = logging.getLogger("booking")


class BookingError(ValueError):
    """A problem the agent should tell the patient about (slot taken, wrong visit type, ...)."""


# --------------------------------------------------------------------------- schedule config

@dataclass
class Schedule:
    timezone: str
    slot_minutes: int
    clinic_hours: dict
    visit_types: dict
    closures: dict = field(default_factory=dict)       # "2026-11-26" -> note
    early_close: dict = field(default_factory=dict)    # "2026-11-25" -> {"open":..,"close":..}
    providers: list = field(default_factory=list)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def provider(self, provider_id: str) -> dict | None:
        return next((p for p in self.providers if p["id"] == provider_id), None)

    def find_provider(self, text: str | None) -> dict | None:
        """Match "Hsu", "dr. hsu", "Daniel" or "hsu" to a provider."""
        if not text:
            return None
        t = text.lower().replace("dr.", "").replace("dr ", "").strip()
        for p in self.providers:
            if t == p["id"] or t in p["name"].lower():
                return p
        return None

    def visit_allowed(self, visit_type: str, provider: dict, day: date) -> bool:
        vt = self.visit_types[visit_type]
        if DAYS[day.weekday()] not in vt["days"]:
            return False
        return not (vt.get("needs") and not provider.get(vt["needs"]))


def load_schedule(path: Path = SCHEDULE_FILE) -> Schedule:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return Schedule(
        timezone=raw["timezone"], slot_minutes=raw.get("slot_minutes", 60), clinic_hours=raw["clinic_hours"],
        visit_types=raw["visit_types"], closures={c["date"]: c.get("note", "") for c in raw.get("closures", [])},
        early_close={c["date"]: c for c in raw.get("early_close", [])}, providers=raw["providers"])


def _hm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def day_window(schedule: Schedule, provider: dict, day: date) -> tuple[time, time] | None:
    """The hours this provider can see patients on this day, after clinic hours and holidays."""
    key, iso = DAYS[day.weekday()], day.isoformat()
    if iso in schedule.closures or key not in provider["hours"] or key not in schedule.clinic_hours:
        return None
    start, end = (_hm(x) for x in provider["hours"][key])
    c_open, c_close = (_hm(x) for x in schedule.clinic_hours[key])
    special = schedule.early_close.get(iso, {})
    if "open" in special:
        c_open = _hm(special["open"])
    if "close" in special:
        c_close = _hm(special["close"])
    start, end = max(start, c_open), min(end, c_close)
    return (start, end) if start < end else None


def label(dt: datetime, tz: ZoneInfo) -> str:
    """'Tue, Oct 27 at 9:00 AM' (no platform-specific strftime flags, so it works on Windows)."""
    local = dt.astimezone(tz)
    return f"{local:%a, %b} {local.day} at {local.strftime('%I:%M %p').lstrip('0')}"


# --------------------------------------------------------------------------- schema + seeding

def apply_schema(conn) -> None:
    with conn.transaction():
        conn.execute(SCHEMA_FILE.read_text(encoding="utf-8"))


def _taken(provider_id: str, starts: datetime, busy_pct: int) -> bool:
    """Deterministic 'already booked by someone else', so re-seeding gives the same calendar."""
    h = int(hashlib.sha256(f"{provider_id}{starts.isoformat()}".encode()).hexdigest(), 16)
    return h % 100 < busy_pct


def seed_slots(conn, schedule: Schedule, start: date, days: int = 21, busy_pct: int = 35,
               reset: bool = False) -> dict:
    """Create slots from `start` for `days` days. Existing slots are kept unless reset=True."""
    apply_schema(conn)
    tz = schedule.tz
    with conn.transaction():
        if reset:
            conn.execute("delete from appointments")
            conn.execute("delete from slots")
        for p in schedule.providers:
            conn.execute("""insert into providers (id, name, title, can_evaluate, focus) values (%s,%s,%s,%s,%s)
                            on conflict (id) do update set name=excluded.name, title=excluded.title,
                                can_evaluate=excluded.can_evaluate, focus=excluded.focus""",
                         (p["id"], p["name"], p.get("title"), bool(p.get("can_evaluate")), p.get("focus")))
        created = taken = 0
        step = timedelta(minutes=schedule.slot_minutes)
        for offset in range(days):
            day = start + timedelta(days=offset)
            for p in schedule.providers:
                window = day_window(schedule, p, day)
                if not window:
                    continue
                t = datetime.combine(day, window[0], tz)
                end = datetime.combine(day, window[1], tz)
                while t + step <= end:
                    status = "blocked" if _taken(p["id"], t, busy_pct) else "open"
                    cur = conn.execute("""insert into slots (provider_id, starts_at, ends_at, status)
                                          values (%s,%s,%s,%s) on conflict (provider_id, starts_at) do nothing""",
                                       (p["id"], t, t + step, status))
                    created += cur.rowcount
                    taken += cur.rowcount and status == "blocked"
                    t += step
    return {"created": created, "already_taken": taken}


# --------------------------------------------------------------------------- availability

TIME_OF_DAY = {"morning": (0, 12), "afternoon": (12, 17), "evening": (17, 24)}


def find_slots(conn, schedule: Schedule, visit_type: str, *, provider_id: str | None = None,
               on_date: date | None = None, day_of_week: str | None = None, time_of_day: str | None = None,
               earliest: datetime | None = None, limit: int = 5) -> list[dict]:
    """Open slots that fit, earliest first, one provider per start time."""
    tz = schedule.tz
    now = datetime.now(tz)
    earliest = max(earliest or now, now + MIN_NOTICE)
    rows = conn.execute(
        """select s.id, s.starts_at, s.provider_id, p.name as provider_name, p.can_evaluate
           from slots s join providers p on p.id = s.provider_id
           where s.status = 'open' and s.starts_at >= %s
             and (%s::text is null or s.provider_id = %s)
           order by s.starts_at, s.provider_id
           limit 2000""", (earliest, provider_id, provider_id)).fetchall()
    out, seen = [], set()
    for r in rows:
        local = r["starts_at"].astimezone(tz)
        p = schedule.provider(r["provider_id"]) or {"can_evaluate": r["can_evaluate"]}
        if not schedule.visit_allowed(visit_type, p, local.date()):
            continue
        if on_date and local.date() != on_date:
            continue
        if day_of_week and DAYS[local.weekday()] != day_of_week:
            continue
        if time_of_day:
            lo, hi = TIME_OF_DAY[time_of_day]
            if not (lo <= local.hour < hi):
                continue
        if r["starts_at"] in seen and not provider_id:
            continue
        seen.add(r["starts_at"])
        out.append({"slot_id": r["id"], "when": label(r["starts_at"], tz), "provider": r["provider_name"],
                    "provider_id": r["provider_id"], "starts_at": local.isoformat()})
        if len(out) >= limit:
            break
    return out


def get_slot(conn, slot_id: int) -> dict | None:
    return conn.execute("""select s.*, p.name as provider_name from slots s join providers p on p.id = s.provider_id
                           where s.id = %s""", (slot_id,)).fetchone()


# --------------------------------------------------------------------------- leads, bookings, handoffs

def save_lead(conn, session_id, *, name: str, phone: str, email: str | None = None, reason: str | None = None,
              insurance: str | None = None, urgency: str | None = None) -> int:
    row = conn.execute(
        """insert into leads (session_id, name, phone, email, reason, insurance, urgency)
           values (%s,%s,%s,%s,%s,%s,%s)
           on conflict (session_id, phone) do update set
               name = excluded.name,
               email = coalesce(excluded.email, leads.email),
               reason = coalesce(excluded.reason, leads.reason),
               insurance = coalesce(excluded.insurance, leads.insurance),
               urgency = coalesce(excluded.urgency, leads.urgency),
               updated_at = now()
           returning id""", (session_id, name, phone, email, reason, insurance, urgency)).fetchone()
    return row["id"]


def book(conn, schedule: Schedule, session_id, *, slot_id: int, visit_type: str, name: str, phone: str,
         email: str | None = None, reason: str | None = None) -> dict:
    """Book atomically: the slot flips from open to booked in the same transaction as the appointment row,
    so two patients can never get the same slot."""
    tz = schedule.tz
    with conn.transaction():
        slot = conn.execute("""select s.*, p.name as provider_name from slots s join providers p on p.id = s.provider_id
                               where s.id = %s for update""", (slot_id,)).fetchone()
        if not slot:
            raise BookingError(f"There is no appointment slot {slot_id}.")
        if slot["status"] != "open" or slot["starts_at"] < datetime.now(tz) + MIN_NOTICE:
            raise BookingError("That time was just taken or is too soon. Please choose another time.")
        provider = schedule.provider(slot["provider_id"])
        if not schedule.visit_allowed(visit_type, provider, slot["starts_at"].astimezone(tz).date()):
            raise BookingError(f"A {schedule.visit_types[visit_type]['label']} can't be booked in that slot "
                               f"(initial evaluations need a physical therapist, Monday to Friday).")
        conn.execute("update slots set status = 'booked' where id = %s", (slot_id,))
        lead_id = save_lead(conn, session_id, name=name, phone=phone, email=email, reason=reason)
        appt = conn.execute(
            """insert into appointments (slot_id, lead_id, visit_type, patient_name, phone, email, reason)
               values (%s,%s,%s,%s,%s,%s,%s) returning id""",
            (slot_id, lead_id, visit_type, name, phone, email, reason)).fetchone()
        conn.execute("update leads set status = 'booked', updated_at = now() where id = %s", (lead_id,))
    result = {"appointment_id": appt["id"], "lead_id": lead_id, "when": label(slot["starts_at"], tz),
              "starts_at": slot["starts_at"].astimezone(tz).isoformat(), "provider": slot["provider_name"],
              "visit_type": visit_type, "visit_label": schedule.visit_types[visit_type]["label"],
              "name": name, "phone": phone, "email": email, "reason": reason}
    notify("appointment.booked", result)
    return result


def create_handoff(conn, session_id, *, reason: str, summary: str, lead_id: int | None = None) -> int:
    row = conn.execute("""insert into handoffs (session_id, lead_id, reason, summary) values (%s,%s,%s,%s)
                          returning id""", (session_id, lead_id, reason, summary)).fetchone()
    lead = None
    if lead_id:
        conn.execute("update leads set status = 'handed_off', updated_at = now() where id = %s", (lead_id,))
        lead = conn.execute("select name, phone, email from leads where id = %s", (lead_id,)).fetchone()
    notify("handoff.created", {"handoff_id": row["id"], "session_id": str(session_id) if session_id else None,
                               "lead_id": lead_id, "reason": reason, "summary": summary,
                               "name": lead and lead["name"], "phone": lead and lead["phone"],
                               "email": lead and lead["email"]})
    return row["id"]


def log_tool_call(conn, session_id, tool: str, arguments: dict, result: dict, ok: bool) -> None:
    try:
        conn.execute("insert into tool_calls (session_id, tool, arguments, result, ok) values (%s,%s,%s,%s,%s)",
                     (session_id, tool, json.dumps(arguments, default=str), json.dumps(result, default=str), ok))
    except Exception:                                    # logging must never break a conversation
        log.exception("could not log tool call")


# --------------------------------------------------------------------------- notifications (n8n in week 3)

def notify(event: str, payload: dict, *, wait: bool = False) -> int | None:
    """POST an event to EVENT_WEBHOOK_URL (an n8n webhook; Zapier or Make work the same way).

        {"event": "appointment.booked", "id": "<unique>", "sent_at": "...", "data": {...}}

    - Sent in a background thread, so the patient's "You're booked!" never waits for n8n.
    - Retried up to 3 times if n8n is down or answers 5xx.
    - EVENT_WEBHOOK_SECRET goes in an X-Webhook-Secret header; n8n rejects requests without it, so a
      stranger who finds the webhook URL can't post fake bookings.
    - Failures are logged, never raised: a booking must not fail because a notification did.
    wait=True sends in the foreground and returns the HTTP status (for `python -m app.notify`).
    """
    url = os.environ.get("EVENT_WEBHOOK_URL")
    if not url:
        return None
    body = {"event": event, "id": uuid.uuid4().hex, "sent_at": datetime.now(timezone.utc).isoformat(),
            "data": payload}
    headers = {"Content-Type": "application/json"}
    if os.environ.get("EVENT_WEBHOOK_SECRET"):
        headers["X-Webhook-Secret"] = os.environ["EVENT_WEBHOOK_SECRET"]

    def send() -> int | None:
        import httpx
        for attempt in range(3):
            try:
                # n8n replies when the email and sheet row are done; on Cloud Run it may also be waking up (~30 s).
                # This runs in the background, so a long wait never delays the patient.
                timeout = float(os.environ.get("EVENT_WEBHOOK_TIMEOUT") or 90)
                r = httpx.post(url, content=json.dumps(body, default=str), headers=headers, timeout=timeout)
                if r.status_code < 500:
                    if r.status_code >= 400:
                        log.warning("webhook %s rejected: HTTP %s %s", event, r.status_code, r.text[:200])
                    return r.status_code
            except Exception as e:                                  # n8n not running, DNS, timeout...
                log.warning("webhook %s attempt %d failed: %s", event, attempt + 1, e)
            clock.sleep(2 ** attempt)
        log.error("webhook %s gave up after 3 attempts", event)
        return None

    if wait:
        return send()
    threading.Thread(target=send, name=f"notify-{event}", daemon=True).start()
    return None
