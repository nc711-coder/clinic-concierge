"""Booking conversations end to end: router -> agent -> tools -> Postgres -> confirmation -> booking.

The language model is scripted (no Ollama needed); everything else is real, including the database.
Run with TEST_DATABASE_URL pointing at a throwaway Postgres.
"""
import json
import os
import re
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from app import booking
from app.agent import BookingAgent
from app.chat import ChatResult
from app.concierge import Concierge
from app.llm import LLMReply, ToolCall

DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DB, reason="set TEST_DATABASE_URL to run database tests")


# --------------------------------------------------------------------------- scripted model

class ScriptedLLM:
    """Each chat_tools() call takes the next step from the script. A step is a function of the messages
    (so it can read slot ids the tools returned) that returns an LLMReply."""

    model = "scripted"

    def __init__(self, *steps):
        self.steps, self.calls = list(steps), []

    def chat_tools(self, messages, tools):
        self.calls.append(messages)
        assert {t["name"] for t in tools} == {"check_availability", "save_lead", "book_appointment",
                                              "escalate_to_human"}
        return self.steps.pop(0)(messages)


def call(tool, /, **args):
    return lambda messages: LLMReply("", [ToolCall(id=f"c_{tool}", name=tool, arguments=args)])


def say(text):
    return lambda messages: LLMReply(text)


def last_tool_result(messages):
    return json.loads([m for m in messages if m["role"] == "tool"][-1]["content"])


def listing(slots):
    """How a model would read times out: '10:00 AM with Dr. Bell on Thu, Oct 22, ...'."""
    return ", ".join(f"{s['when'].split(' at ')[1]} with {s['provider']} on {s['when'].split(' at ')[0]}"
                     for s in slots)


def book_first_offered(name="Jane Doe", phone="703-555-0199", visit_type="evaluation"):
    """Book the first slot_id listed under 'Times already offered' in the system prompt."""
    def step(messages):
        slot_id = int(re.search(r"Times already offered.*?: (\d+):", messages[0]["content"]).group(1))
        return LLMReply("", [ToolCall("c_book", "book_appointment",
                                      {"slot_id": slot_id, "visit_type": visit_type, "name": name, "phone": phone})])
    return step


class StubEngine:
    """Stands in for the RAG pipeline: records questions, returns a fixed cited answer."""

    def __init__(self):
        self.questions = []
        self.llm = SimpleNamespace(model="scripted")

    def answer(self, message, history=None):
        self.questions.append(message)
        return ChatResult("We're in-network with Aetna [1].", answered=True, search_query=message,
                          citations=[{"n": 1, "source": "04_insurance_accepted.csv", "title": "Insurance", "section": None}])


# --------------------------------------------------------------------------- fixtures

@pytest.fixture(scope="module")
def conn():
    import psycopg
    from psycopg.rows import dict_row
    c = psycopg.connect(DB, row_factory=dict_row, autocommit=True)
    booking.apply_schema(c)
    for t in ("tool_calls", "messages", "handoffs", "appointments", "leads", "sessions", "slots", "providers"):
        c.execute(f"truncate {t} cascade")
    booking.seed_slots(c, booking.load_schedule(), date.today(), days=21, busy_pct=35)
    yield c
    c.close()


def concierge(conn, llm, engine=None):
    schedule = booking.load_schedule()
    return Concierge(conn, engine or StubEngine(), BookingAgent(llm, schedule, "Quillmere PT", "(703) 555-0142"),
                     schedule)


# --------------------------------------------------------------------------- the happy path

def test_full_booking_conversation(conn):
    llm = ScriptedLLM(
        call("check_availability", visit_type="evaluation", time_of_day="morning"),
        lambda m: LLMReply(f"I have {last_tool_result(m)['slots'][0]['when']}. What's your name and phone?"),
        book_first_offered(),
    )
    c = concierge(conn, llm)

    r1 = c.handle("I'd like to book an evaluation, mornings are best")
    # The model asked for name and phone before a time was picked: the code shows the times instead.
    assert r1.route == "booking" and "name" not in r1.reply and "Which one works best?" in r1.reply
    offered = r1.debug["tool_calls"][0]["result"]["slots"]
    assert all(f"{i}. {s['when']}" in r1.reply for i, s in enumerate(offered, 1))
    assert offered and all("AM" in s["when"] for s in offered)

    r2 = c.handle("The first one. Jane Doe, 703-555-0199", r1.session_id)
    assert r2.awaiting_confirmation and r2.reply.startswith("Just to confirm: an initial evaluation with")
    assert "(703) 555-0199" in r2.reply                       # phone normalised by validation
    assert conn.execute("select count(*) n from appointments").fetchone()["n"] == 0   # nothing booked yet

    r3 = c.handle("yes please", r2.session_id)
    assert r3.route == "confirm" and r3.reply.startswith("You're booked!") and r3.appointment
    appt = conn.execute("select * from appointments where id = %s", (r3.appointment["appointment_id"],)).fetchone()
    slot = conn.execute("select status from slots where id = %s", (appt["slot_id"],)).fetchone()
    lead = conn.execute("select status, phone from leads where id = %s", (appt["lead_id"],)).fetchone()
    assert slot["status"] == "booked" and lead == {"status": "booked", "phone": "(703) 555-0199"}
    assert appt["slot_id"] == offered[0]["slot_id"]
    msgs = conn.execute("select role, route from messages where session_id = %s order by id", (r1.session_id,)).fetchall()
    assert [m["route"] for m in msgs] == ["booking", "booking", "booking", "booking", "confirm", "confirm"]


# --------------------------------------------------------------------------- guardrails in the loop

def test_invented_slot_is_refused_and_model_recovers(conn):
    llm = ScriptedLLM(
        call("book_appointment", slot_id=999999, visit_type="evaluation", name="Sam Lee", phone="7035550111"),
        call("check_availability", visit_type="evaluation"),
        say("Here are some real times."),
    )
    r = concierge(conn, llm).handle("book me in for an evaluation on Tuesday, Sam Lee 703 555 0111")
    first = r.debug["tool_calls"][0]["result"]
    assert "wasn't offered" in first["error"] and not r.awaiting_confirmation
    tool_msgs = [m for m in llm.calls[1] if m["role"] == "tool"]
    assert "wasn't offered" in tool_msgs[0]["content"]        # the model was told what went wrong


def test_bad_phone_goes_back_to_the_model(conn):
    llm = ScriptedLLM(call("save_lead", name="Ana", phone="555-12"), say("Could you give me your full phone number?"))
    r = concierge(conn, llm).handle("I want to book an evaluation, I'm Ana, 555-12")
    assert "10-digit" in r.debug["tool_calls"][0]["result"]["error"] and r.reply.startswith("Could you")


def test_false_booking_claim_is_replaced(conn):
    r = concierge(conn, ScriptedLLM(say("Great, you're all booked for Thursday!"))).handle("book me a follow-up for thursday")
    assert r.reply.startswith("Nothing is booked yet") and r.guardrail


def test_medical_advice_in_booking_reply_is_replaced(conn):
    r = concierge(conn, ScriptedLLM(say("Sure! Until then, try some squats and ice it."))).handle(
        "I'm a new patient and I'd like to book a visit for my knee")
    assert r.reply.startswith("I can't give medical advice") and r.guardrail


def test_tool_call_written_as_json_text_still_runs(conn):
    llm = ScriptedLLM(say('{"name": "check_availability", "parameters": {"visit_type": "follow_up", "day_of_week": "saturday"}}'),
                      say("Saturday works."))
    r = concierge(conn, llm).handle("can I schedule a follow-up on Saturday?")
    slots = r.debug["tool_calls"][0]["result"]["slots"]
    assert slots and all(s["when"].startswith("Sat") for s in slots)


def test_tool_loop_is_capped(conn):
    llm = ScriptedLLM(*[call("check_availability", visit_type="evaluation")] * 6)
    r = concierge(conn, llm).handle("book an evaluation")
    assert "front desk" in r.reply and r.guardrail == "too many tool calls"


# --------------------------------------------------------------------------- confirmation step

def _pending(conn, name):
    """Start a conversation that ends with a booking awaiting yes/no."""
    llm = ScriptedLLM(call("check_availability", visit_type="follow_up"), say("Pick one."), book_first_offered(name, "7035550100", "follow_up"))
    c = concierge(conn, llm)
    r1 = c.handle("I want to book a follow-up")
    r2 = c.handle(f"the first one, {name} 7035550100", r1.session_id)
    assert r2.awaiting_confirmation
    return c, r2


def test_unclear_reply_asks_again_and_no_hands_back_to_agent(conn):
    c, r = _pending(conn, "Kim Park")
    r2 = c.handle("hmm", r.session_id)
    assert r2.awaiting_confirmation and r2.reply.startswith("Sorry, I didn't catch that")
    c.agent.llm.steps.append(say("No problem, which day would suit you better?"))
    r3 = c.handle("no, can we do another day instead", r.session_id)
    assert r3.route == "booking" and not r3.awaiting_confirmation and "which day" in r3.reply
    assert not conn.execute("select 1 from appointments a join leads l on l.id = a.lead_id where l.name = 'Kim Park'").fetchone()


def test_same_slot_cannot_be_booked_twice(conn):
    c1, r1 = _pending(conn, "First Patient")
    c2, r2 = _pending(conn, "Second Patient")                 # both were offered (and chose) the same earliest slot
    assert c1.handle("yes", r1.session_id).appointment
    second = c2.handle("yes", r2.session_id)
    assert second.appointment is None and "just taken" in second.reply


def test_pending_booking_survives_a_restart(conn):
    c, r = _pending(conn, "Restart Test")
    fresh = concierge(conn, ScriptedLLM())                    # new process, same database
    assert fresh.handle("yes", r.session_id).appointment


# --------------------------------------------------------------------------- routing between answers, booking, people

def test_question_mid_booking_goes_to_answers_and_keeps_the_booking(conn):
    engine = StubEngine()
    llm = ScriptedLLM(call("check_availability", visit_type="evaluation"), say("Which time suits you?"))
    c = concierge(conn, llm, engine)
    r1 = c.handle("I'd like to book an evaluation")
    r2 = c.handle("do you take Aetna?", r1.session_id)
    assert r2.route == "answer" and engine.questions == ["do you take Aetna?"]
    assert r2.citations and "carry on with your booking" in r2.reply
    state = conn.execute("select state from sessions where id = %s", (r1.session_id,)).fetchone()["state"]
    assert state["mode"] == "booking" and state["offered_slots"]


def test_asking_for_a_person_asks_for_a_phone_then_creates_the_handoff(conn):
    """Found in real use: "talk to a person" created a handoff the front desk had no way to follow up."""
    c = concierge(conn, ScriptedLLM())
    r1 = c.handle("can I talk to a real person please")
    assert r1.route == "handoff" and r1.handoff_id is None and "best phone number" in r1.reply
    r2 = c.handle("Jane Doe 703-555-0188", r1.session_id)
    row = conn.execute("""select h.summary, l.name, l.phone from handoffs h join leads l on l.id = h.lead_id
                          where h.id = %s""", (r2.handoff_id,)).fetchone()
    assert r2.reply.startswith("Thanks, Jane! I've passed this to our front desk team. Someone will call you at "
                               "(703) 555-0188")
    assert row["name"] == "Jane Doe" and row["phone"] == "(703) 555-0188" and "talk to a real person" in row["summary"]


def test_handoff_with_phone_only_and_with_known_contact(conn):
    c = concierge(conn, ScriptedLLM())
    r1 = c.handle("can I speak to someone?")
    r2 = c.handle("571 555 0144", r1.session_id)                       # number only is enough
    assert r2.handoff_id and "(571) 555-0144" in r2.reply
    llm = ScriptedLLM(call("save_lead", name="Kim Lee", phone="7035550177"), say("Thanks Kim, saved."))
    c2 = concierge(conn, llm)
    r3 = c2.handle("I want to book a follow-up, Kim Lee 703 555 0177")
    r4 = c2.handle("actually can I talk to a person", r3.session_id)   # phone known: no question
    assert r4.handoff_id and "(703) 555-0177" in r4.reply


def test_handoff_declined_or_unclear(conn):
    c = concierge(conn, ScriptedLLM())
    r1 = c.handle("can I talk to a person")
    r2 = c.handle("no thanks, I'll call", r1.session_id)
    assert r2.handoff_id is None and r2.reply.startswith("No problem. You can reach our front desk at (703) 555-0142")
    r3 = c.handle("can I talk to a person", r1.session_id)
    r4 = c.handle("why do you need it", r1.session_id)
    assert r4.reply.startswith("Sorry, I didn't catch a phone number")
    r5 = c.handle("703-555-01", r1.session_id)                          # too short: not taken as a number
    assert r5.handoff_id is None


def test_agent_escalation_without_phone_asks_for_one(conn):
    llm = ScriptedLLM(call("escalate_to_human", reason="wants to cancel an existing appointment",
                           summary="Patient wants to cancel next week's visit."))
    c = concierge(conn, llm)
    r1 = c.handle("I need to cancel my appointment")
    assert r1.handoff_id is None and "best phone number" in r1.reply
    r2 = c.handle("Ray Cho, 703-555-0123", r1.session_id)
    row = conn.execute("select reason, summary from handoffs where id = %s", (r2.handoff_id,)).fetchone()
    assert row["reason"] == "wants to cancel an existing appointment" and "(703) 555-0123" in r2.reply


def test_agent_escalation_uses_known_phone(conn):
    llm = ScriptedLLM(call("save_lead", name="Ray Cho", phone="7035550123"),
                      call("escalate_to_human", reason="wants to cancel an existing appointment",
                           summary="Ray Cho wants to cancel next week's visit."))
    r = concierge(conn, llm).handle("I need to cancel my appointment, this is Ray Cho 703-555-0123")
    assert r.handoff_id and "(703) 555-0123" in r.reply


def test_api_chat_keeps_the_session(conn, monkeypatch):
    from fastapi.testclient import TestClient

    from app import main
    llm = ScriptedLLM(call("check_availability", visit_type="evaluation"), say("Which time?"))
    monkeypatch.setattr(main, "concierge", concierge(conn, llm))
    with TestClient(main.app) as client:
        a = client.post("/chat", json={"message": "I'd like to book an evaluation"}).json()
        assert a["route"] == "booking" and a["session_id"] and "debug" not in a
        b = client.post("/chat", json={"message": "do you take Aetna?", "session_id": a["session_id"],
                                       "debug": True}).json()
        assert b["session_id"] == a["session_id"] and b["route"] == "answer" and "debug" in b


# --------------------------------------------------------------------------- picking a time in code

def test_real_transcript_model_saves_lead_but_forgets_to_book(conn):
    """Real llama3.1:8b behaviour: after "the first one, Jane Doe 703-555-0199" it only called save_lead and asked
    "would you like to book one of the spots?". The code now books the chosen time itself."""
    llm = ScriptedLLM(
        call("check_availability", visit_type="evaluation", time_of_day="morning"),
        lambda m: LLMReply("We have a few openings: " + listing(last_tool_result(m)["slots"][:3]) + "."),
        call("save_lead", name="Jane Doe", phone="703-555-0199"),
        say("We've saved your contact details, Jane. Would you like to book one of the Wednesday spots?"),
    )
    c = concierge(conn, llm)
    r1 = c.handle("I'd like to book an evaluation, mornings are best")
    first = r1.debug["tool_calls"][0]["result"]["slots"][0]
    r2 = c.handle("the first one, Jane Doe 703-555-0199", r1.session_id)
    assert r2.awaiting_confirmation and first["when"] in r2.reply
    assert [t["name"] for t in r2.debug["tool_calls"]] == ["book_appointment"]    # contact was saved in code
    assert r2.debug["tool_calls"][-1]["by"] == "code"
    assert c.handle("yes", r2.session_id).appointment["when"] == first["when"]


def test_choice_with_known_contact_skips_the_model(conn):
    llm = ScriptedLLM(call("save_lead", name="Lee Ann", phone="7035550177"),
                      call("check_availability", visit_type="follow_up", day_of_week="sat"),
                      lambda m: LLMReply("On Saturday: " + listing(last_tool_result(m)["slots"]) + "."))
    c = concierge(conn, llm)
    r1 = c.handle("I want to book a follow-up on Saturday, Lee Ann 703 555 0177")
    second = r1.debug["tool_calls"][1]["result"]["slots"][1]
    calls_before = len(llm.calls)
    r2 = c.handle("the second one", r1.session_id)
    assert len(llm.calls) == calls_before                      # no model call needed
    assert r2.awaiting_confirmation and second["when"] in r2.reply


def test_second_one_means_the_second_time_the_patient_saw(conn):
    """Real llama3.1:8b transcript: the tool returned 8:00, 10:00, 11:00, 12:00 ... but the reply skipped 8:00
    ("10:00 AM with Dr. Bell, 11:00 AM with Dr. Okonkwo-Reyes, or 12:00 PM ..."). "The second one" is 11:00,
    not the tool's second slot (10:00)."""
    llm = ScriptedLLM(call("save_lead", name="Jane Doe", phone="703-555-0199"),
                      call("check_availability", visit_type="evaluation"),
                      lambda m: LLMReply("We have a few openings. Would you like " +
                                         listing(last_tool_result(m)["slots"][1:3]) + "?"))
    c = concierge(conn, llm)
    r1 = c.handle("schedule an appointment for an evaluation, Jane Doe 703-555-0199")
    slots = r1.debug["tool_calls"][1]["result"]["slots"]
    r2 = c.handle("the second one", r1.session_id)
    assert r2.awaiting_confirmation and slots[2]["when"] in r2.reply and slots[1]["when"] not in r2.reply


def test_ordinal_is_left_to_the_model_when_the_reply_listed_no_times(conn):
    llm = ScriptedLLM(call("save_lead", name="Jane Doe", phone="703-555-0199"),
                      call("check_availability", visit_type="evaluation"),
                      say("I found some openings this week. Which day suits you?"),
                      say("Which day did you have in mind?"))
    c = concierge(conn, llm)
    r1 = c.handle("book an evaluation, Jane Doe 703-555-0199")
    r2 = c.handle("the second one", r1.session_id)
    assert not r2.awaiting_confirmation and r2.reply.startswith("Which day")


def test_shown_order():
    from app.tools import pick_offered_slot, shown_order
    offered = [{"slot_id": 62, "when": "Thu, Oct 22 at 8:00 AM", "provider": "Dr. Maya Okonkwo-Reyes"},
               {"slot_id": 76, "when": "Thu, Oct 22 at 10:00 AM", "provider": "Dr. Marcus Bell"},
               {"slot_id": 65, "when": "Thu, Oct 22 at 11:00 AM", "provider": "Dr. Maya Okonkwo-Reyes"},
               {"slot_id": 78, "when": "Thu, Oct 22 at 12:00 PM", "provider": "Dr. Marcus Bell"},
               {"slot_id": 90, "when": "Fri, Oct 23 at 10:00 AM", "provider": "Dr. Ravi Natarajan"}]
    real = ("We have a few openings on Thursday. Would you like to schedule an appointment at 10:00 AM with "
            "Dr. Marcus Bell, 11:00 AM with Dr. Maya Okonkwo-Reyes, or 12:00 PM with Dr. Marcus Bell?")
    shown = shown_order(real, offered)
    assert [s["slot_id"] for s in shown] == [76, 65, 78]
    assert pick_offered_slot("the second one", offered, shown)["slot_id"] == 65
    assert pick_offered_slot("the last one", offered, shown)["slot_id"] == 78
    # Same time on two days: the day named before it decides; the therapist named next to it decides too.
    assert [s["slot_id"] for s in shown_order("Friday at 10am or Thursday at 8am", offered)] == [90, 62]
    assert [s["slot_id"] for s in shown_order("10:00 AM with Dr. Natarajan", offered)] == [90]
    assert shown_order("Which day works for you?", offered) == []
    assert shown_order("10:00 AM works", offered) is None              # Thu or Fri? can't tell
    assert shown_order("How about 3:00 PM?", offered) is None          # not an offered time
    assert pick_offered_slot("the first one", offered, None) is None   # don't know what they saw


def test_ambiguous_time_is_left_to_the_model(conn):
    from app.tools import pick_offered_slot
    offered = [{"slot_id": 1, "when": "Wed, Oct 21 at 9:00 AM"}, {"slot_id": 2, "when": "Thu, Oct 22 at 9:00 AM"}]
    assert pick_offered_slot("9am", offered) is None
    assert pick_offered_slot("Thursday at 9", offered)["slot_id"] == 2
    assert pick_offered_slot("this is my first time here", offered) is None


# --------------------------------------------------------------------------- new or returning patient

def test_asks_new_or_returning_before_searching(conn):
    """Real llama3.1:8b transcript: "schedule an appointment for thursday" went straight to evaluation times."""
    llm = ScriptedLLM(call("check_availability", visit_type="evaluation", day_of_week="thu"), say("Thursday times."))
    c = concierge(conn, llm)
    r1 = c.handle("schedule an appointment for thursday")
    assert r1.route == "booking" and "been to Quillmere PT before" in r1.reply and not llm.calls
    r2 = c.handle("yes, I have", r1.session_id)
    check = r2.debug["tool_calls"][0]
    assert check["arguments"]["visit_type"] == "evaluation"            # what the model asked for...
    slots = check["result"]["slots"]
    assert slots and all(s["when"].startswith("Thu") for s in slots)
    state = conn.execute("select state from sessions where id = %s", (r1.session_id,)).fetchone()["state"]
    assert state["visit_type"] == "follow_up" and state["visit_type_fixed"]   # ...but returning = follow-up
    assert "returning patient; already settled" in llm.calls[0][0]["content"]


def test_no_means_new_patient(conn):
    llm = ScriptedLLM(call("check_availability", visit_type="follow_up"), say("Here are some times."))
    c = concierge(conn, llm)
    r1 = c.handle("can I book an appointment?")
    r2 = c.handle("no", r1.session_id)
    assert r2.debug["tool_calls"][0]["result"]["slots"]
    state = conn.execute("select state from sessions where id = %s", (r1.session_id,)).fetchone()["state"]
    assert state["visit_type"] == "evaluation"


def test_unclear_answer_asks_once_more_then_lets_the_model_ask(conn):
    llm = ScriptedLLM(say("No problem. Is this your first visit with us?"))
    c = concierge(conn, llm)
    r1 = c.handle("book an appointment please")
    r2 = c.handle("thursday afternoon", r1.session_id)
    assert r2.reply.startswith("Sorry, I didn't catch that") and not llm.calls
    r3 = c.handle("whatever is open", r1.session_id)
    assert r3.reply.startswith("No problem") and len(llm.calls) == 1


def test_no_question_when_the_message_already_says(conn):
    for message in ["I'd like to book an evaluation", "I've never been here before, can I book a visit?",
                    "can I schedule a follow-up?", "I need to cancel my appointment"]:
        llm = ScriptedLLM(say("Sure."))
        r = concierge(conn, llm).handle(message)
        assert r.reply == "Sure." and len(llm.calls) == 1, message


def test_i_have_not_means_new_patient_and_the_request_is_kept(conn):
    """Real transcript: "I have not" was read as "yes" (it starts with "I have"), and "thursday" was forgotten."""
    llm = ScriptedLLM(call("check_availability", visit_type="evaluation", day_of_week="thu"), say("Thursday times."))
    c = concierge(conn, llm)
    r1 = c.handle("schedule an appointment for me on thursday")
    c.handle("I have not", r1.session_id)
    state = conn.execute("select state from sessions where id = %s", (r1.session_id,)).fetchone()["state"]
    assert state["visit_type"] == "evaluation" and "visit_request" not in state
    assert [m for m in llm.calls[0] if m["role"] == "user"][-1]["content"] == "schedule an appointment for me on thursday. I'm a new patient."


def test_bare_yes_to_several_times_asks_which(conn):
    llm = ScriptedLLM(call("check_availability", visit_type="follow_up"),
                      lambda m: LLMReply("Would you like " + listing(last_tool_result(m)["slots"]) + "?"))
    c = concierge(conn, llm)
    r1 = c.handle("can I schedule a follow-up?")
    slots = r1.debug["tool_calls"][0]["result"]["slots"]
    assert len(slots) <= 3
    r2 = c.handle("yes", r1.session_id)
    assert r2.reply.startswith("Great! Which time works best?\n1. " + slots[0]["when"]) and len(llm.calls) == 2


def test_yes_to_a_single_time_books_it(conn):
    llm = ScriptedLLM(call("save_lead", name="Uma Ray", phone="7035550166"),
                      call("check_availability", visit_type="follow_up"),
                      lambda m: LLMReply("I have " + listing(last_tool_result(m)["slots"][:1]) + ". Does that work?"))
    c = concierge(conn, llm)
    r1 = c.handle("can I schedule a follow-up? Uma Ray 703 555 0166")
    first = r1.debug["tool_calls"][1]["result"]["slots"][0]
    r2 = c.handle("yes please", r1.session_id)
    assert r2.awaiting_confirmation and first["when"] in r2.reply


# --------------------------------------------------------------------------- "no open times" (real transcript)

def test_wrong_year_is_refused_and_no_times_reply_is_replaced(conn):
    """Real transcript: "how about tomorrow" -> "we don't have any open times ... tomorrow" although Thursday had
    evaluation slots. A date from the model's training years is now refused, and a "no times" reply is
    replaced by the next open times, listed by the code."""
    llm = ScriptedLLM(call("check_availability", visit_type="evaluation", date="2023-10-22"),
                      say("It looks like we don't have any open times available for an evaluation tomorrow. "
                          "Would you like to book an evaluation for another day?"),
                      say("Great choice! What's your full name and phone number?"))
    c = concierge(conn, llm)
    r = c.handle("I'd like to book an initial evaluation for tomorrow")
    assert "today is" in r.debug["tool_calls"][0]["result"]["error"]
    assert r.reply.startswith("Nothing's open for that, but here are the next available times:\n1. ")
    assert "don't have any open times" in r.debug["replaced"]
    r2 = c.handle("1", r.session_id)                                   # the widget's first button
    assert "name" in r2.reply                                          # no contact yet: the model asks for it


def test_full_day_returns_next_available(conn):
    from app.tools import ToolContext, run_tool
    sunday = date.today() + timedelta(days=(6 - date.today().weekday()) % 7 or 7)
    ctx = ToolContext(conn, booking.load_schedule(), None, {})
    out = run_tool("check_availability", {"visit_type": "evaluation", "date": sunday.isoformat()}, ctx)
    assert out["slots"] == [] and out["next_available"] and "instead" in out["note"]
    assert {str(s["slot_id"]) for s in out["next_available"]} <= set(ctx.state["offered_slots"])


def test_frustration_gets_the_next_times_not_a_shrug(conn):
    llm = ScriptedLLM(call("check_availability", visit_type="evaluation", day_of_week="sun"), say("Sorry, nothing on Sunday."))
    c = concierge(conn, llm)
    r1 = c.handle("I'd like to book an initial evaluation on Sunday")
    r2 = c.handle("this is very inconvenient", r1.session_id)
    assert r2.reply.startswith("Sorry about that") and "\n1. " in r2.reply and "call me" in r2.reply
    assert len(llm.calls) == 2                                     # handled by code, no extra model call


def test_pick_then_name_and_phone_is_handled_in_code(conn):
    """Real transcript: after "the first one" the model asked "Would you like to book this time?" instead of
    asking who the patient is. Now: pick -> code asks for name + phone -> code books -> "yes"."""
    llm = ScriptedLLM(call("check_availability", visit_type="evaluation"),
                      lambda m: LLMReply("We have " + listing(last_tool_result(m)["slots"]) + "."))
    c = concierge(conn, llm)
    r1 = c.handle("I'd like to book an initial evaluation")
    first = r1.debug["tool_calls"][0]["result"]["slots"][0]
    r2 = c.handle("the first one", r1.session_id)
    assert r2.reply == (f"Great choice: {first['when']} with {first['provider']}. To hold it for you, "
                        f"what's your full name and phone number?")
    r3 = c.handle("Jane Doe 703-555-0199", r1.session_id)
    assert r3.awaiting_confirmation and first["when"] in r3.reply and "(703) 555-0199" in r3.reply
    assert len(llm.calls) == 2                                   # only the first message needed the model
    assert c.handle("yes", r1.session_id).appointment["when"] == first["when"]


def test_model_collects_contact_then_code_books_the_chosen_time(conn):
    llm = ScriptedLLM(call("check_availability", visit_type="evaluation"),
                      lambda m: LLMReply("We have " + listing(last_tool_result(m)["slots"]) + "."),
                      call("save_lead", name="Jane Doe", phone="7035550199"),       # "it's jane, 7035550199"
                      say("Thanks Jane! Would you like to book that time?"))
    c = concierge(conn, llm)
    r1 = c.handle("I'd like to book an initial evaluation")
    second = r1.debug["tool_calls"][0]["result"]["slots"][1]
    c.handle("the second one", r1.session_id)
    r3 = c.handle("it's jane, 7035550199", r1.session_id)        # first name only: the model reads it
    assert r3.awaiting_confirmation and second["when"] in r3.reply


def test_parse_contact():
    from app.tools import parse_contact
    assert parse_contact("jane doe, (703) 555-0199")[:2] == ("Jane Doe", "(703) 555-0199")
    assert parse_contact("My name is Jane Doe and my phone is 703.555.0199")[0] == "Jane Doe"
    assert parse_contact("I'm Sam Lee 7035550111 sam@x.example") == ("Sam Lee", "7035550111", "sam@x.example")
    assert parse_contact("the first one") == (None, None, None)
    assert parse_contact("Jane 703-555-0199")[0] is None          # unsure -> leave it to the model


def test_debug_shows_where_the_time_went(conn):
    llm = ScriptedLLM(call("check_availability", visit_type="evaluation"), say("Here are some times."))
    r = concierge(conn, llm).handle("I'd like to book an initial evaluation")
    steps = [t["step"] for t in r.debug["timings"]]
    assert steps == ["model: booking agent, round 1", "tool: check_availability", "model: booking agent, round 2"]
    assert r.debug["total_seconds"] >= sum(t["seconds"] for t in r.debug["timings"])


# --------------------------------------------------------------------------- name and phone only after a time is picked

def test_no_preference_shows_times_without_asking_for_contact(conn):
    """Real transcript (gpt-oss-120b): "not really" to "any preferred day?" got "Could I get your full name and a
    phone number so we can find the best slots?". Now the code lists the next open times."""
    c = concierge(conn, ScriptedLLM(say("Do you have a preferred day or time of day?")))
    r1 = c.handle("id like to book an appointment Ive never been there")
    r2 = c.handle("not really", r1.session_id)
    assert "1. " in r2.reply and "name" not in r2.reply.lower() and len(c.agent.llm.calls) == 1


def test_model_asking_for_contact_early_is_replaced_with_times(conn):
    c = concierge(conn, ScriptedLLM(say("Sure! Could I get your full name and a phone number so we can find the best slots?")))
    r = c.handle("I'm a new patient, can I book something thursday?")
    assert "1. " in r.reply and ("Which one" in r.reply or "Would one of these" in r.reply)
    assert r.debug["replaced"].startswith("Sure! Could I get")


def test_contact_given_early_is_never_asked_for_again(conn):
    """Real transcript: "john doe 465 354 6981", then "3 pm works best" got "what's your full name and phone?"."""
    llm = ScriptedLLM(call("check_availability", visit_type="evaluation"),
                      lambda m: LLMReply("Which works? " + listing(last_tool_result(m)["slots"])))
    c = concierge(conn, llm)
    r1 = c.handle("I'm a new patient, book me in. john doe 465 354 6981")
    slots = r1.debug["tool_calls"][-1]["result"]["slots"]
    r2 = c.handle("the first one works best and yeah thats my phone number", r1.session_id)
    assert r2.awaiting_confirmation and slots[0]["when"] in r2.reply and "John Doe" in r2.reply


def test_no_preference_wording():
    from app.router import no_preference
    assert all(no_preference(m) for m in ["not really", "no preference", "any time works", "whenever", "nope",
                                          "the earliest one please", "anything is fine", "doesn't matter"])
    assert not any(no_preference(m) for m in ["no, can we do another day instead", "no, thursday afternoon",
                                              "not really, mornings are better", "no I'm new"])
