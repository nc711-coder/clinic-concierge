"""The front door: one patient message in, one reply out.

    1. A booking waiting for "yes"? Handle the reply in code: yes books, no/changes go back to the agent.
    2. Otherwise route (app/router.py):
         answer  -> RAG pipeline (app/chat.py): cited answers, relevance gate, guardrails
         booking -> booking agent (app/agent.py): tools to find times, save leads, book, hand off
         handoff -> front desk notified straight away
    3. Save both messages and the booking state to Postgres, so the next message picks up where this one left off.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from . import booking, sessions, timing
from .agent import BookingAgent
from .router import (about_existing_appointment, been_before_reply, confirmation_reply, declines_callback,
                     frustrated, just_yes, route, says_no_times, visit_type_hint)
from .tools import (ToolContext, confirmation_message, next_open, parse_contact, pick_offered_slot,
                    remember_offered, run_tool, shown_order)


@dataclass
class TurnResult:
    session_id: str
    reply: str
    route: str                                        # answer | booking | confirm | handoff
    citations: list[dict] = field(default_factory=list)
    appointment: dict | None = None                   # set when this message completed a booking
    awaiting_confirmation: bool = False
    handoff_id: int | None = None
    guardrail: str | None = None
    debug: dict = field(default_factory=dict)         # search query, sources, tool calls


class Concierge:
    def __init__(self, conn, engine, agent: BookingAgent, schedule: booking.Schedule):
        self.conn, self.engine, self.agent, self.schedule = conn, engine, agent, schedule
        self._lock = threading.Lock()                 # one booking/session connection shared across API threads
        with self._lock:
            booking.apply_schema(conn)

    def db(self, fn):
        with self._lock:
            return fn()

    # ------------------------------------------------------------------------------------------
    def handle(self, message: str, session_id=None) -> TurnResult:
        message = message.strip()
        timing.start()
        started = time.perf_counter()
        sid, state, history = self.db(lambda: sessions.load_or_create(self.conn, session_id))
        ctx = ToolContext(self.conn, self.schedule, sid, state)

        result = None
        if state.get("pending_booking"):
            result = self._confirm(message, ctx, history)
        elif state.get("handoff_pending"):
            result = self._finish_handoff(message, ctx, history)    # None: not an answer, route normally
        if result is None:
            r = route(message, state)
            if r == "handoff":
                result = self._handoff(message, ctx, history)
            elif r == "booking":
                result = self._book(message, ctx, history)
            else:
                result = self._answer(message, state, history)
        result.session_id = str(sid)
        result.debug["timings"] = timing.collect()
        result.debug["total_seconds"] = round(time.perf_counter() - started, 2)

        meta = {k: v for k, v in {"citations": result.citations, "appointment": result.appointment,
                                  "handoff_id": result.handoff_id, "guardrail": result.guardrail}.items() if v}
        self.db(lambda: sessions.save_turn(self.conn, sid, message, result.reply, result.route, ctx.state, meta))
        return result

    # ------------------------------------------------------------------------------------------
    def _answer(self, message, state, history) -> TurnResult:
        r = self.engine.answer(message, history)
        reply = r.answer
        if state.get("mode") == "booking":
            reply += "\n\nWhenever you're ready, we can carry on with your booking."
        return TurnResult("", reply, "answer", citations=r.citations, guardrail=r.guardrail,
                          debug={"search_query": r.search_query, "retrieved": r.retrieved, "gated": r.gated})

    def _book(self, message, ctx, history) -> TurnResult:
        ctx.state["mode"] = "booking"
        ask, message = self._new_or_returning(message, ctx.state)
        if ask:
            return ask
        which = self._which_one(message, ctx.state)
        if which:
            return which
        if frustrated(message) and ctx.state.get("visit_type"):
            return self._offer_next(ctx, "Sorry about that, let me make it easier.", apologise=True)
        # The patient picked one of the times just offered and we already have their name and phone:
        # no need to ask the model, go straight to "Shall I book it?".
        shown_before = ctx.state.get("shown_offered")     # the list the patient was looking at
        direct = self._book_choice_in_code(message, ctx, shown_before, ask_for_contact=True)
        if direct:
            return direct
        offered_before = ctx.state.get("last_offered")
        turn = self.agent.run(ctx, history, message, run_db=self.db)
        new_search = any(c["name"] == "check_availability" for c in turn.tool_calls)
        self._remember_shown(turn, ctx.state)
        if turn.handoff_id:
            ctx.state["mode"] = None
        elif not turn.awaiting_confirmation and says_no_times(turn.text):
            # Never leave the patient at "we have nothing": the code lists the next open times itself.
            offer = self._offer_next(ctx, "Nothing's open for that, but here are the next available times:")
            offer.debug = {"tool_calls": turn.tool_calls, "replaced": turn.text}
            return offer
        elif not turn.awaiting_confirmation and not new_search:
            # Small models often save the contact details but forget to book the time the patient chose.
            # (Skipped after a new search: then the patient's words can't refer to times they haven't seen.)
            direct = self._book_choice_in_code(message, ctx, shown_before, offered_before, turn.tool_calls)
            if direct:
                return direct
        return TurnResult("", turn.text, "booking", awaiting_confirmation=turn.awaiting_confirmation,
                          handoff_id=turn.handoff_id, guardrail=turn.guardrail,
                          debug={"tool_calls": turn.tool_calls})

    def _new_or_returning(self, message, state) -> tuple[TurnResult | None, str]:
        """New patients book an initial evaluation, returning patients a follow-up. The code settles which
        before any times are searched (llama3.1:8b tended to assume "evaluation" without asking).

        Returns (question to send, or None) and the message the agent should work from: after the patient
        answers, that's their original request plus the answer ("schedule me on thursday. I've been there
        before."), so the model doesn't forget "thursday"."""
        asked = state.get("asked_visit_type", 0)
        hint = been_before_reply(message) if asked else visit_type_hint(message)
        request = state.pop("visit_request", None) if (hint or asked >= 2) else state.get("visit_request")
        if hint:
            state.update(visit_type=hint, visit_type_fixed=True)
            state.pop("asked_visit_type", None)
            if asked and request:
                answer = "I'm a new patient" if hint == "evaluation" else "I've been there before"
                return None, f"{request.rstrip(' .!')}. {answer}."
            return None, message
        if state.get("visit_type") or about_existing_appointment(message) or asked >= 2:
            state.pop("asked_visit_type", None)       # known already, not a new booking, or still unclear: agent's turn
            return None, message
        state["asked_visit_type"] = asked + 1
        state.setdefault("visit_request", message)
        reply = (f"Happy to help you book! Have you been to {self.agent.clinic_name} before? New patients start "
                 f"with an initial evaluation; returning patients book a follow-up visit.")
        if asked:
            reply = "Sorry, I didn't catch that. Have you been to see us before? (yes / no)"
        return TurnResult("", reply, "booking"), message

    def _offer_next(self, ctx, lead: str, apologise: bool = False) -> TurnResult:
        """List the earliest open times (numbered, so the widget shows them as buttons)."""
        visit_type = ctx.state.get("visit_type") or "evaluation"
        slots = self.db(lambda: next_open(ctx, visit_type))
        self.db(lambda: remember_offered(ctx, slots))
        ctx.state["shown_offered"] = slots or None
        phone = self.agent.clinic_phone
        if not slots:
            return TurnResult("", f"{lead} I can't find an opening in the next three weeks. Say \"call me\" and "
                                  f"the front desk will phone you, or call us at {phone}.", "booking")
        label = self.schedule.visit_types[visit_type]["label"]
        options = "\n".join(f"{i}. {s['when']} with {s['provider']}" for i, s in enumerate(slots, 1))
        if apologise:
            lead += f" The next available {label} times are:"
        tail = "Would one of these work? Or say \"call me\" and the front desk will phone you."
        return TurnResult("", f"{lead}\n{options}\n{tail}", "booking")

    def _which_one(self, message, state) -> TurnResult | None:
        """A bare "yes" to a list of times doesn't pick one: ask which, instead of letting the model search again."""
        shown = state.get("shown_offered") or []
        if len(shown) < 2 or not just_yes(message):
            return None
        options = "\n".join(f"{i}. {s['when']} with {s['provider']}" for i, s in enumerate(shown, 1))
        return TurnResult("", f"Great! Which time works best?\n{options}", "booking")

    @staticmethod
    def _remember_shown(turn, state):
        """Record which times the reply listed, in the order the patient read them, so "the second one"
        means the second time they saw, not the second one the tool returned."""
        if turn.awaiting_confirmation or turn.handoff_id:
            return
        new_search = any(c["name"] == "check_availability" for c in turn.tool_calls)
        shown = shown_order(turn.text, list((state.get("offered_slots") or {}).values()))
        if shown or shown is None or new_search:
            state["shown_offered"] = shown or None        # [] after a new search: we don't know what they saw

    def _book_choice_in_code(self, message, ctx, shown, offered=None, earlier_calls=(),
                             ask_for_contact=False) -> TurnResult | None:
        """Book the time the patient chose, in code, when that's unambiguous.

        - They pick a time ("the first one") and we know their name and phone -> "Shall I book it?"
        - They pick a time but we don't know who they are -> remember the time, ask for name and phone
        - Next message has the name and phone ("Jane Doe 703-555-0199") -> save them, "Shall I book it?"
        Anything less clear is left to the model.
        """
        state = ctx.state
        visit_type = state.get("visit_type")
        offered = offered if offered is not None else state.get("last_offered")
        picked = pick_offered_slot(message, offered or [], shown=shown)
        if not picked and shown and len(shown) == 1 and just_yes(message):
            picked = shown[0]                         # "I have Thu at 10 AM, does that work?" -> "yes"
        if picked:
            state["chosen_slot"] = picked
        slot = picked or state.get("chosen_slot")
        if slot and str(slot["slot_id"]) not in (state.get("offered_slots") or {}):
            slot = None                               # a new search replaced the times they chose from
        if not (slot and visit_type):
            return None
        calls = list(earlier_calls)
        contact = state.get("contact") or {}
        if not (contact.get("name") and contact.get("phone")):
            name, phone, email = parse_contact(message)
            if name and phone:
                lead = {"name": name, "phone": phone, "email": email}
                saved = self.db(lambda: run_tool("save_lead", lead, ctx))
                calls.append({"name": "save_lead", "arguments": lead, "result": saved, "by": "code"})
                if "error" in saved:
                    return TurnResult("", "That phone number doesn't look complete. Could you send your full name "
                                          "and a 10-digit phone number?", "booking", debug={"tool_calls": calls})
                contact = state["contact"]
            elif picked and ask_for_contact and not (name or phone):   # gave nothing yet: ask
                return TurnResult("", f"Great choice: {slot['when']} with {slot['provider']}. To hold it for you, "
                                      f"what's your full name and phone number?", "booking")
            else:
                return None
        args = {"slot_id": slot["slot_id"], "visit_type": visit_type, "name": contact["name"],
                "phone": contact["phone"], "email": contact.get("email")}
        result = self.db(lambda: run_tool("book_appointment", args, ctx))
        calls.append({"name": "book_appointment", "arguments": args, "result": result, "by": "code"})
        if result.get("status") != "awaiting_patient_confirmation":
            return None
        return TurnResult("", confirmation_message(ctx.state["pending_booking"], self.schedule), "booking",
                          awaiting_confirmation=True, debug={"tool_calls": calls})

    def _confirm(self, message, ctx, history) -> TurnResult:
        pending = ctx.state["pending_booking"]
        answer = confirmation_reply(message)
        if answer == "unclear":
            return TurnResult("", "Sorry, I didn't catch that. " + confirmation_message(pending, self.schedule),
                              "confirm", awaiting_confirmation=True)
        ctx.state.pop("pending_booking")
        if answer == "no":                            # a change of plan: let the agent take it from here
            return self._book(message, ctx, history)
        try:
            appt = self.db(lambda: booking.book(self.conn, self.schedule, ctx.session_id, **{
                k: pending[k] for k in ("slot_id", "visit_type", "name", "phone", "email", "reason")}))
        except booking.BookingError as e:
            ctx.state.get("offered_slots", {}).pop(str(pending["slot_id"]), None)
            return TurnResult("", f"{e} Would you like me to find another time?", "confirm")
        ctx.state.update(mode=None, lead_id=appt["lead_id"], booked=appt)
        for key in ("offered_slots", "chosen_slot", "shown_offered", "last_offered"):
            ctx.state.pop(key, None)
        reply = (f"You're booked! {self.schedule.visit_types[appt['visit_type']]['label'].capitalize()} with "
                 f"{appt['provider']} on {appt['when']}. Please arrive 15 minutes early")
        if appt["visit_type"] == "evaluation":
            reply += (" and bring a photo ID and your insurance card. You'll get a link to our patient portal for "
                      "your intake forms; please complete them at least 24 hours before your visit if you can")
        reply += ". Anything else I can help with?"
        return TurnResult("", reply, "confirm", appointment=appt)

    def _handoff(self, message, ctx, history) -> TurnResult:
        recent = [m["content"] for m in history if m["role"] == "user"][-3:] + [message]
        summary = "Patient asked for a person. Recent messages: " + " | ".join(m[:200] for m in recent)
        if not (ctx.state.get("contact") or {}).get("phone"):
            # No way for the front desk to reach them yet: ask first, create the handoff when the number arrives.
            ctx.state["handoff_pending"] = {"reason": "asked for a person", "summary": summary, "asked": 1}
            return TurnResult("", self.agent.ask_for_phone(), "handoff")
        return self._create_handoff(ctx, "asked for a person", summary)

    def _create_handoff(self, ctx, reason: str, summary: str, thanks: str = "") -> TurnResult:
        handoff_id = self.db(lambda: booking.create_handoff(self.conn, ctx.session_id, reason=reason,
                                                            summary=summary, lead_id=ctx.state.get("lead_id")))
        ctx.state.update(handed_off=True, mode=None)
        return TurnResult("", thanks + self.agent.handoff_message(ctx.state), "handoff", handoff_id=handoff_id)

    def _finish_handoff(self, message, ctx, history) -> TurnResult | None:
        """The patient was asked for a phone number so the front desk can call them back."""
        pending = ctx.state["handoff_pending"]
        name, phone, email = parse_contact(message)
        if phone:
            known = ctx.state.get("contact") or {}
            lead = {"name": name or known.get("name") or "Website visitor", "phone": phone, "email": email,
                    "reason": pending["reason"]}
            saved = self.db(lambda: run_tool("save_lead", lead, ctx))
            if "error" in saved:
                return TurnResult("", "That phone number doesn't look complete. Could you send the full 10-digit "
                                      "number?", "handoff")
            ctx.state.pop("handoff_pending")
            first = (name or known.get("name") or "").split(" ")[0]
            thanks = f"Thanks, {first}! " if first else "Thanks! "
            return self._create_handoff(ctx, pending["reason"], pending["summary"], thanks)
        if declines_callback(message):
            ctx.state.pop("handoff_pending")
            return TurnResult("", f"No problem. You can reach our front desk at {self.agent.clinic_phone} during "
                                  f"business hours. Anything else I can help with?", "handoff")
        if pending["asked"] < 2:
            pending["asked"] += 1
            return TurnResult("", "Sorry, I didn't catch a phone number. What's the best number for our front desk "
                                  f"to call you on? (Or say \"no thanks\" and call us at {self.agent.clinic_phone}.)",
                              "handoff")
        ctx.state.pop("handoff_pending")              # asked twice: treat this as a new message
        return None
