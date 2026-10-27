"""The booking agent: a tool-calling loop that collects details, finds times and books.

Facts about the clinic (prices, insurance, policies) are NOT answered here. The router sends those to
the RAG pipeline in app/chat.py, which is evaluated and guarded. This agent only books, saves leads and
hands off to people.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from . import timing
from .guardrails import SAFE_REPLY, check_answer
from .llm import LLMReply, ToolCall
from .tools import TOOLS, ToolContext, confirmation_message, run_tool, tool_specs

MAX_STEPS = 5          # tool-call rounds per patient message
HISTORY_TURNS = 10

SYSTEM = """You are the booking assistant for {clinic}. Today is {today}.

Your job: help the patient book an appointment, or take their contact details so the front desk can call them.

How to work:
- New patients book an "evaluation" (60 min, Monday to Friday only). Returning patients book a "follow_up".
  If you don't know which, ask "Have you been to our clinic before?"
- First find a time. If they haven't said a preferred day or time of day, ask once; if they have no preference,
  just search. Do NOT ask for their name or phone number until they have picked a time.
- Once they've picked a time, ask for their full name and phone number (email is optional).
- ALWAYS call check_availability before offering times. Offer at most 3 times, using the "when" text exactly.
  Never make up a time or a slot_id. If it returns "next_available", nothing matched what they asked:
  say so in a few words and offer those times instead. Never just say there's nothing open.
  For dates, use the calendar below (this year, not an earlier one).
- When the patient chooses a time and you have their name and phone, call book_appointment with that slot_id.
  The system then asks the patient to confirm. Never say an appointment is booked yourself.
- If they don't want to book yet but give a name and phone, call save_lead.
- Call escalate_to_human if they ask for a person, want to cancel or change an existing appointment, are upset,
  or need something you can't do.
- Never give medical advice or diagnose. Keep replies to 1-3 short, friendly sentences.

Dates for the next two weeks: {calendar}
Therapists: {therapists}
{known}"""

_JSON_CALL = re.compile(r"\{.*\}", re.S)
_FALSE_BOOKED = re.compile(r"\b(you'?re|you are|i'?ve|i have|it'?s|is|has been|have been|all)\s+(all\s+)?"
                           r"(booked|scheduled|confirmed)\b", re.I)


@dataclass
class AgentTurn:
    text: str
    tool_calls: list[dict] = field(default_factory=list)       # what was called and what came back
    awaiting_confirmation: bool = False
    handoff_id: int | None = None
    guardrail: str | None = None


def _calendar(today: date) -> str:
    days = [today + timedelta(days=i) for i in range(14)]
    return "; ".join(f"{d:%a} {d:%b} {d.day} = {d.isoformat()}" for d in days)


def _known(state: dict) -> str:
    lines = []
    if state.get("visit_type_fixed"):
        who = "new patient" if state["visit_type"] == "evaluation" else "returning patient"
        lines.append(f"Visit type: {state['visit_type']} ({who}; already settled, don't ask again)")
    elif state.get("visit_type"):
        lines.append(f"Visit type: {state['visit_type']}")
    c = state.get("contact") or {}
    if c:
        lines.append("Patient: " + ", ".join(f"{k} {v}" for k, v in c.items() if v))
    offered = list((state.get("offered_slots") or {}).values())[-6:]
    if offered:
        lines.append("Times already offered (slot_id: when, therapist): "
                     + "; ".join(f"{s['slot_id']}: {s['when']}, {s['provider']}" for s in offered))
    shown = state.get("shown_offered") or []
    if shown:
        lines.append("Times you listed in your last reply, in that order (\"the first one\" = 1): "
                     + "; ".join(f"{i}) slot_id {s['slot_id']}: {s['when']}, {s['provider']}"
                                 for i, s in enumerate(shown, 1)))
    return ("Known so far:\n- " + "\n- ".join(lines)) if lines else ""


def _text_tool_call(text: str) -> ToolCall | None:
    """Some local models write the call as JSON text instead of a real tool call:
    {"name": "check_availability", "parameters": {...}}. Treat that as a call."""
    m = _JSON_CALL.search(text or "")
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except ValueError:
        return None
    name = obj.get("name") or obj.get("function") or obj.get("tool")
    args = obj.get("parameters") or obj.get("arguments") or obj.get("args") or {}
    if isinstance(name, str) and name in TOOLS and isinstance(args, dict):
        return ToolCall(id="text_call", name=name, arguments=args)
    return None


class BookingAgent:
    def __init__(self, llm, schedule, clinic_name: str, clinic_phone: str):
        self.llm, self.schedule, self.clinic_name, self.clinic_phone = llm, schedule, clinic_name, clinic_phone
        self.specs = tool_specs()

    def system_prompt(self, state: dict, now: datetime | None = None) -> str:
        now = now or datetime.now(self.schedule.tz)
        therapists = "; ".join(f"{p['name']} ({p['focus']})" for p in self.schedule.providers)
        return SYSTEM.format(clinic=self.clinic_name, today=f"{now:%A, %B} {now.day}, {now.year}",
                             calendar=_calendar(now.date()), therapists=therapists, known=_known(state))

    def run(self, ctx: ToolContext, history: list[dict], message: str, run_db=lambda f: f()) -> AgentTurn:
        """One patient message. `run_db` wraps database work (the caller passes a lock)."""
        messages = [{"role": "system", "content": self.system_prompt(ctx.state)}]
        messages += [{"role": m["role"], "content": m["content"]} for m in history[-HISTORY_TURNS:]]
        messages.append({"role": "user", "content": message})
        turn = AgentTurn(text="")

        for n in range(1, MAX_STEPS + 1):
            with timing.step(f"model: booking agent, round {n}"):
                reply: LLMReply = self.llm.chat_tools(messages, self.specs)
            calls = reply.tool_calls or ([c] if (c := _text_tool_call(reply.text)) else [])
            if not calls:
                turn.text = reply.text
                break
            messages.append({"role": "assistant", "content": "" if not reply.tool_calls else reply.text,
                             "tool_calls": [{"id": c.id, "name": c.name, "arguments": c.arguments, "extra": c.extra}
                                            for c in calls]})
            for call in calls:
                with timing.step(f"tool: {call.name}"):
                    result = run_db(lambda: run_tool(call.name, call.arguments, ctx))
                turn.tool_calls.append({"name": call.name, "arguments": call.arguments, "result": result})
                messages.append({"role": "tool", "tool_call_id": call.id, "name": call.name,
                                 "content": json.dumps(result, default=str)})
                if call.name == "book_appointment" and result.get("status") == "awaiting_patient_confirmation":
                    turn.awaiting_confirmation = True
                    turn.text = confirmation_message(ctx.state["pending_booking"], self.schedule)
                    return turn                                   # the code asks; the model can't skip it
                if call.name == "escalate_to_human" and result.get("status") == "need_phone":
                    turn.text = self.ask_for_phone()
                    return turn
                if call.name == "escalate_to_human" and "handoff_id" in result:
                    turn.handoff_id = result["handoff_id"]
                    turn.text = self.handoff_message(ctx.state)
                    return turn
        else:
            turn.text = (f"Sorry, I'm having trouble with that. You can call our front desk at "
                         f"{self.clinic_phone} and they'll book you directly.")
            turn.guardrail = "too many tool calls"
            return turn

        if not turn.text:
            turn.text = "Could you tell me a little more about the appointment you're looking for?"
        if check_answer(turn.text):
            turn.text, turn.guardrail = SAFE_REPLY, "medical advice in booking reply"
        elif _FALSE_BOOKED.search(turn.text):
            turn.text = ("Nothing is booked yet. Once you pick a time, I'll confirm the details with you "
                         "before booking. Which time works best?")
            turn.guardrail = "model claimed a booking that didn't happen"
        return turn

    def ask_for_phone(self) -> str:
        return (f"I can have someone from our front desk call you back. What's your name and the best phone number "
                f"to reach you? (If it's urgent, you can also call us at {self.clinic_phone}.)")

    def handoff_message(self, state: dict) -> str:
        phone = (state.get("contact") or {}).get("phone")
        if phone:
            return f"I've passed this to our front desk team. Someone will call you at {phone} during business hours."
        return (f"I've let our front desk team know. For the fastest help, call them at {self.clinic_phone}, "
                f"or share your name and phone number and they'll call you back.")
