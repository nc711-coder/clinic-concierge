"""Tool-calling wire formats for Ollama, OpenAI and Claude, checked against fake servers using the real SDKs,
plus the router rules. No API keys, network or database needed."""
import json

import httpx
import pytest

from app.llm import AnthropicLLM, OllamaLLM, OpenAILLM
from app.router import confirmation_reply, route
from app.tools import tool_specs

TOOLS = tool_specs()
# A conversation that already contains one tool call and its result.
CONVO = [
    {"role": "system", "content": "You are the booking assistant."},
    {"role": "user", "content": "Any evaluations Thursday evening?"},
    {"role": "assistant", "content": "", "tool_calls": [
        {"id": "call_1", "name": "check_availability", "arguments": {"visit_type": "evaluation", "day_of_week": "thu"}}]},
    {"role": "tool", "tool_call_id": "call_1", "name": "check_availability",
     "content": json.dumps({"slots": [{"slot_id": 73, "when": "Thu, Oct 22 at 5:00 PM"}]})},
]


def test_tool_specs_are_plain_json_schema():
    names = [t["name"] for t in TOOLS]
    assert names == ["check_availability", "save_lead", "book_appointment", "escalate_to_human"]
    book = next(t for t in TOOLS if t["name"] == "book_appointment")
    assert set(book["parameters"]["required"]) == {"slot_id", "visit_type", "name", "phone"}
    assert "anyOf" not in json.dumps(TOOLS)                   # Optional fields flattened for small models


# --------------------------------------------------------------------------- Ollama

def test_ollama_request_and_response():
    seen = {}

    def handler(req):
        seen.update(json.loads(req.content))
        return httpx.Response(200, json={"message": {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "book_appointment",
                          "arguments": {"slot_id": 73, "visit_type": "evaluation", "name": "Jo", "phone": "7035550100"}}}]}})

    llm = OllamaLLM(model="llama3.1:8b", url="http://ollama.test")
    llm.client = httpx.Client(transport=httpx.MockTransport(handler))
    reply = llm.chat_tools(CONVO, TOOLS)
    assert reply.tool_calls[0].name == "book_appointment" and reply.tool_calls[0].arguments["slot_id"] == 73
    assert seen["tools"][0] == {"type": "function", "function": TOOLS[0]}
    assistant, tool = seen["messages"][2], seen["messages"][3]
    assert assistant["tool_calls"][0]["function"] == {"name": "check_availability",
                                                      "arguments": {"visit_type": "evaluation", "day_of_week": "thu"}}
    assert tool == {"role": "tool", "content": CONVO[3]["content"], "tool_name": "check_availability"}


def test_ollama_string_arguments_and_model_without_tools():
    llm = OllamaLLM(model="tiny", url="http://ollama.test")
    llm.client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"message": {
        "content": "", "tool_calls": [{"function": {"name": "save_lead", "arguments": '{"name": "Jo", "phone": "1"}'}}]}})))
    assert llm.chat_tools(CONVO, TOOLS).tool_calls[0].arguments == {"name": "Jo", "phone": "1"}

    from app.llm import LLMError
    llm.client = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(400, json={"error": "registry.ollama.ai/library/tiny does not support tools"})))
    with pytest.raises(LLMError, match="can't call tools"):
        llm.chat_tools(CONVO, TOOLS)


# --------------------------------------------------------------------------- OpenAI (real SDK, fake server)

def test_openai_request_and_response():
    from openai import OpenAI
    seen = {}

    def handler(req):
        seen.update(json.loads(req.content))
        return httpx.Response(200, json={
            "id": "x", "object": "chat.completion", "created": 0, "model": "gpt-5-mini",
            "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": None, "tool_calls": [
                    {"id": "call_9", "type": "function", "function": {
                        "name": "escalate_to_human", "arguments": '{"reason": "asked", "summary": "s"}'}}]}}]})

    client = OpenAI(api_key="test", max_retries=0, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    reply = OpenAILLM(client=client).chat_tools(CONVO, TOOLS)
    assert reply.tool_calls[0].id == "call_9" and reply.tool_calls[0].arguments == {"reason": "asked", "summary": "s"}
    assert seen["tools"][1]["function"]["name"] == "save_lead" and "temperature" not in seen
    assert seen["messages"][2]["tool_calls"][0]["function"]["arguments"] == json.dumps(
        {"visit_type": "evaluation", "day_of_week": "thu"})
    assert seen["messages"][3] == {"role": "tool", "tool_call_id": "call_1", "content": CONVO[3]["content"]}


# --------------------------------------------------------------------------- Claude (real SDK, fake server)

def test_anthropic_request_and_response():
    import anthropic
    try:
        import httpx2 as hx
    except ImportError:
        hx = httpx
    seen = {}

    def handler(req):
        seen.update(json.loads(req.content))
        return hx.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-haiku-5-5",
            "content": [{"type": "text", "text": "Booking that now."},
                        {"type": "tool_use", "id": "toolu_1", "name": "book_appointment",
                         "input": {"slot_id": 73, "visit_type": "evaluation", "name": "Jo", "phone": "7035550100"}}],
            "stop_reason": "tool_use", "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}})

    client = anthropic.Anthropic(api_key="test", max_retries=0, http_client=hx.Client(transport=hx.MockTransport(handler)))
    reply = AnthropicLLM(client=client).chat_tools(CONVO, TOOLS)
    assert reply.text == "Booking that now." and reply.tool_calls[0].id == "toolu_1"
    assert seen["system"] == "You are the booking assistant."
    assert seen["tools"][0]["input_schema"] == TOOLS[0]["parameters"]
    roles = [m["role"] for m in seen["messages"]]
    assert roles == ["user", "assistant", "user"]                  # tool result travels in a user turn
    assert seen["messages"][1]["content"][0]["type"] == "tool_use"
    assert seen["messages"][2]["content"][0] == {"type": "tool_result", "tool_use_id": "call_1",
                                                 "content": CONVO[3]["content"]}


# --------------------------------------------------------------------------- router

@pytest.mark.parametrize("msg", ["I'd like to book an appointment", "can I schedule an evaluation for next week?",
                                 "Do you have any openings Thursday?", "book me in with Dr. Hsu",
                                 "I need to reschedule my appointment", "Could I get in to see someone this week?"])
def test_booking_intent(msg):
    assert route(msg, {}) == "booking"


@pytest.mark.parametrize("msg", ["What should I bring to my first appointment?", "How do I book my first appointment?",
                                 "what happens if I miss my first appointment this year?", "do you take kaiser?",
                                 "How soon can a new patient get in?"])
def test_questions_stay_with_answers(msg):
    assert route(msg, {}) == "answer"


def test_eval_questions_route_to_answers():
    from evals.grading import load_questions
    routed = {q["id"]: route(q["question"], {}) for q in load_questions()}
    # "Can I book my first evaluation on a Saturday?" is a reasonable booking request; the agent knows the rule.
    assert {i for i, r in routed.items() if r != "answer"} == {"visit-saturday-eval"}


@pytest.mark.parametrize("msg,expected", [
    ("i want an appointment on thursday", "booking"),     # real transcript: went to answers before
    ("I need an appointment", "booking"), ("I'd like an evaluation next week", "booking"),
    ("we would like a follow-up", "booking"), ("appointment on friday please", "booking"),
    ("Do I need an evaluation before starting?", "answer"), ("Do I need a referral for my first appointment?", "answer"),
    ("What should I bring to my appointment?", "answer"), ("how much is an appointment?", "answer"),
    ("when is the next available appointment", "booking"),     # real transcript: went to the documents
    ("what's the earliest opening?", "booking"), ("when can I come in?", "booking"),
    ("How long is the wait for a new patient evaluation?", "answer"), ("When are you open on Saturday?", "answer")])
def test_booking_requests_without_book_or_schedule(msg, expected):
    assert route(msg, {}) == expected


@pytest.mark.parametrize("msg,expected", [("Jane Doe", "booking"), ("703-555-0199", "booking"),
                                          ("the first one", "booking"), ("Thursday evening works", "booking"),
                                          ("do you take Aetna?", "answer"), ("is there parking?", "answer"),
                                          ("Can I do 5pm instead?", "booking")])
def test_mid_booking_routing(msg, expected):
    assert route(msg, {"mode": "booking"}) == expected


@pytest.mark.parametrize("msg", ["can I talk to a real person?", "I want to speak with someone", "have someone call me"])
def test_handoff(msg):
    assert route(msg, {}) == "handoff"


@pytest.mark.parametrize("msg,expected", [("yes", "yes"), ("Yes please", "yes"), ("sounds good!", "yes"), ("ok", "yes"),
                                          ("no", "no"), ("can we do Friday instead", "no"),
                                          ("actually make it 6pm", "no"), ("hmm", "unclear")])
def test_confirmation_replies(msg, expected):
    assert confirmation_reply(msg) == expected


def test_no_times_and_frustration_detection():
    from app.router import frustrated, says_no_times
    assert says_no_times("It looks like we don't have any open times available for an evaluation today.")
    assert says_no_times("Sorry, we're fully booked on Friday.") and says_no_times("There's no availability then.")
    assert not says_no_times("No problem, which times work for you?")
    assert not says_no_times("We have openings on Thursday at 10:00 AM.")
    assert frustrated("this is very inconvenient") and frustrated("ugh, this isn't working")
    assert not frustrated("Thursday afternoon works")
