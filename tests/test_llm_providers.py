"""OpenAI and Claude adapters, tested with fake SDK clients (no API keys or network)."""
from types import SimpleNamespace

import pytest

from app.llm import AnthropicLLM, LLMError, OllamaLLM, OpenAILLM, get_llm

MESSAGES = [
    {"role": "system", "content": "You are the clinic assistant."},
    {"role": "user", "content": "hello"},
    {"role": "assistant", "content": "Hi! How can I help?"},
    {"role": "user", "content": "Sources: ...\n\nQuestion: do you take kaiser?"},
]


class FakeOpenAIClient:
    def __init__(self, content="No, Kaiser is out-of-network [1].", finish="stop"):
        self.calls, self.content, self.finish = [], content, finish
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        msg = SimpleNamespace(content=self.content)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason=self.finish)])


class FakeAnthropicClient:
    def __init__(self):
        self.calls = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(content=[SimpleNamespace(type="thinking", thinking="..."),
                                        SimpleNamespace(type="text", text="  Kaiser is out-of-network [1]. ")])


def test_provider_choice(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    assert isinstance(get_llm(), OllamaLLM)
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    assert isinstance(get_llm(), OpenAILLM)
    monkeypatch.setenv("LLM_PROVIDER", "Anthropic")
    assert isinstance(get_llm(), AnthropicLLM)
    with pytest.raises(LLMError, match="Unknown LLM_PROVIDER"):
        get_llm("mistral")


def test_openai_reasoning_model_gets_no_temperature_and_room_to_think(monkeypatch):
    monkeypatch.delenv("OPENAI_CHAT_MODEL", raising=False)
    fake = FakeOpenAIClient()
    llm = OpenAILLM(client=fake)
    assert llm.model == "gpt-5-mini"
    assert llm.chat(MESSAGES, temperature=0.1, max_tokens=350) == "No, Kaiser is out-of-network [1]."
    kw = fake.calls[0]
    assert "temperature" not in kw and kw["max_completion_tokens"] >= 2000 and kw["reasoning_effort"] == "low"
    assert kw["messages"] == MESSAGES                      # OpenAI takes system messages as-is


def test_openai_classic_model_keeps_temperature():
    fake = FakeOpenAIClient()
    OpenAILLM(model="gpt-4.1-mini", client=fake).chat(MESSAGES, temperature=0.1, max_tokens=350)
    assert fake.calls[0]["temperature"] == 0.1 and fake.calls[0]["max_completion_tokens"] == 350


def test_openai_empty_answer_after_thinking_explains_fix():
    llm = OpenAILLM(client=FakeOpenAIClient(content="", finish="length"))
    with pytest.raises(LLMError, match="OPENAI_REASONING_EFFORT=low"):
        llm.chat(MESSAGES)


def test_anthropic_moves_system_prompt_and_reads_text_blocks(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    fake = FakeAnthropicClient()
    llm = AnthropicLLM(client=fake)
    assert llm.model == "claude-haiku-5-5"
    assert llm.chat(MESSAGES, temperature=0.1, max_tokens=350) == "Kaiser is out-of-network [1]."
    kw = fake.calls[0]
    assert kw["system"] == "You are the clinic assistant."
    assert [m["role"] for m in kw["messages"]] == ["user", "assistant", "user"]
    assert kw["max_tokens"] == 350 and "temperature" not in kw    # current Claude API has no temperature


def test_anthropic_fixes_turn_order():
    fake = FakeAnthropicClient()
    AnthropicLLM(client=fake).chat([{"role": "system", "content": "s"},
                                    {"role": "assistant", "content": "earlier reply"},
                                    {"role": "user", "content": "a"}, {"role": "user", "content": "b"}])
    msgs = fake.calls[0]["messages"]
    assert msgs[0]["role"] == "user" and msgs[1]["role"] == "assistant"
    assert msgs[-1] == {"role": "user", "content": "a\n\nb"}           # consecutive user turns merged


def test_missing_keys_are_reported(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(LLMError, match="OPENAI_API_KEY"):
        OpenAILLM().check()
    with pytest.raises(LLMError, match="ANTHROPIC_API_KEY"):
        AnthropicLLM().check()


def test_real_sdk_error_is_translated():
    """Use the real Anthropic SDK against a fake HTTP server returning 401."""
    import anthropic
    try:                                    # newer anthropic SDKs use httpx2, older ones httpx
        import httpx2 as httpx
    except ImportError:
        import httpx

    transport = httpx.MockTransport(lambda req: httpx.Response(
        401, json={"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}}))
    client = anthropic.Anthropic(api_key="bad", max_retries=0, http_client=httpx.Client(transport=transport))
    with pytest.raises(LLMError, match="ANTHROPIC_API_KEY"):
        AnthropicLLM(client=client).chat(MESSAGES)


def test_real_openai_sdk_request_shape():
    """Use the real OpenAI SDK against a fake HTTP server and check what it sends."""
    import json

    import httpx
    from openai import OpenAI

    seen = {}

    def handler(req):
        seen.update(json.loads(req.content))
        return httpx.Response(200, json={"id": "x", "object": "chat.completion", "created": 0, "model": "gpt-5-mini",
                                         "choices": [{"index": 0, "finish_reason": "stop",
                                                      "message": {"role": "assistant", "content": "Yes [1]."}}]})

    client = OpenAI(api_key="test", max_retries=0, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert OpenAILLM(client=client).chat(MESSAGES) == "Yes [1]."
    assert seen["model"] == "gpt-5-mini" and "temperature" not in seen and seen["reasoning_effort"] == "low"


def test_real_anthropic_sdk_request_shape():
    """Use the real Anthropic SDK against a fake HTTP server and check what it sends."""
    import json

    import anthropic
    try:
        import httpx2 as httpx
    except ImportError:
        import httpx

    seen = {}

    def handler(req):
        seen.update(json.loads(req.content))
        return httpx.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-haiku-5-5",
            "content": [{"type": "text", "text": "Kaiser is out-of-network [1]."}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 5}})

    client = anthropic.Anthropic(api_key="test", max_retries=0, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert AnthropicLLM(client=client).chat(MESSAGES, max_tokens=350) == "Kaiser is out-of-network [1]."
    assert seen["model"] == "claude-haiku-5-5" and seen["system"] == "You are the clinic assistant."
    assert [m["role"] for m in seen["messages"]] == ["user", "assistant", "user"] and "temperature" not in seen


# --------------------------------------------------------------------------- Gemini and Groq (live demo)

import json as _json


def _compat(cls, handler):
    """An OpenAI-compatible provider whose HTTP calls go to `handler` instead of the internet."""
    import httpx
    from openai import OpenAI
    client = OpenAI(api_key="test", base_url=cls.BASE_URL, max_retries=0,
                    http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    return cls(client=client)


def _completion(message, finish="stop"):
    import httpx
    return httpx.Response(200, json={"id": "x", "object": "chat.completion", "created": 0, "model": "m",
                                     "choices": [{"index": 0, "finish_reason": finish, "message": message}]})


TOOLS = [{"name": "check_availability", "parameters": {"type": "object"}}]
HISTORY = [{"role": "user", "content": "book me"},
           {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "name": "check_availability",
                                                                 "arguments": {"visit_type": "evaluation"}}]},
           {"role": "tool", "tool_call_id": "c1", "name": "check_availability", "content": "{\"slots\": []}"}]


def test_gemini_settings(monkeypatch):
    from app.llm import GeminiLLM, get_llm
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-test")
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    llm = get_llm("gemini")
    assert isinstance(llm, GeminiLLM) and llm.model == "gemini-3.5-flash" and llm.client.api_key == "AIza-test"
    assert str(llm.client.base_url) == "https://generativelanguage.googleapis.com/v1beta/openai/"
    monkeypatch.delenv("GEMINI_API_KEY")
    with pytest.raises(LLMError, match="GEMINI_API_KEY"):
        llm.check()


def test_gemini_request_shape_and_tool_calls_without_ids():
    from app.llm import GeminiLLM
    seen = {}

    def handler(req):
        seen.update(url=str(req.url), body=_json.loads(req.content))
        return _completion({"role": "assistant", "content": None, "tool_calls": [
            {"id": "", "type": "function", "function": {"name": "check_availability", "arguments": "{}"}}]})
    reply = _compat(GeminiLLM, handler).chat_tools(HISTORY, TOOLS)
    body = seen["body"]
    assert seen["url"] == "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    assert body["reasoning_effort"] == "low" and body["max_tokens"] >= 2000 and "temperature" not in body
    assert "max_completion_tokens" not in body
    assert body["messages"][-1] == {"role": "tool", "tool_call_id": "c1", "content": "{\"slots\": []}",
                                    "name": "check_availability"}           # Gemini needs the function name
    assert reply.tool_calls[0].id == "call_0"                               # blank id replaced


def test_wrong_model_name_lists_the_real_ones():
    import httpx

    from app.llm import GeminiLLM

    def handler(req):
        if req.url.path.endswith("/models"):
            return httpx.Response(200, json={"object": "list", "data": [
                {"id": "models/gemini-3.9-flash", "object": "model", "created": 0, "owned_by": "google"},
                {"id": "models/gemini-embedding-001", "object": "model", "created": 0, "owned_by": "google"}]})
        return httpx.Response(404, json={"error": {"message": "model not found"}})
    with pytest.raises(LLMError) as e:
        _compat(GeminiLLM, handler).chat([{"role": "user", "content": "hi"}])
    assert "GEMINI_MODEL" in str(e.value) and "gemini-3.9-flash" in str(e.value) and "embedding" not in str(e.value)


def test_groq_now_uses_gpt_oss_with_reasoning(monkeypatch):
    from app.llm import GroqLLM, get_llm
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    monkeypatch.delenv("GROQ_MODEL", raising=False)
    llm = get_llm("groq")
    assert llm.model == "openai/gpt-oss-120b" and llm.reasoning
    seen = {}

    def handler(req):
        seen.update(url=str(req.url), body=_json.loads(req.content))
        return _completion({"role": "assistant", "content": "We take Aetna [1]."})
    assert _compat(GroqLLM, handler).chat([{"role": "user", "content": "aetna?"}]) == "We take Aetna [1]."
    assert seen["url"] == "https://api.groq.com/openai/v1/chat/completions"
    assert seen["body"]["reasoning_effort"] == "low" and "max_completion_tokens" in seen["body"]
    assert "name" not in _json.dumps([m for m in seen["body"]["messages"] if m["role"] == "tool"])


def test_rate_limit_is_a_friendly_message():
    import httpx

    from app.llm import GeminiLLM
    llm = _compat(GeminiLLM, lambda req: httpx.Response(429, json={"error": {"message": "Resource exhausted"}}))
    with pytest.raises(LLMError, match="busy right now"):
        llm.chat([{"role": "user", "content": "hi"}])


def test_timing_counts_hidden_retries():
    import httpx

    from app import timing
    from app.llm import GeminiLLM
    answers = [httpx.Response(429, json={"error": {"message": "slow down"}}, headers={"retry-after": "0"}),
               _completion({"role": "assistant", "content": "OK"})]
    llm = GeminiLLM()
    import openai
    llm._client = openai.OpenAI(api_key="x", base_url=GeminiLLM.BASE_URL, max_retries=2, http_client=httpx.Client(
        transport=httpx.MockTransport(lambda req: answers.pop(0)),
        event_hooks={"request": [timing.count_request], "response": [timing.note_response]}))
    timing.start()
    with timing.step("model: write answer"):
        assert llm.chat([{"role": "user", "content": "hi"}]) == "OK"
    t = timing.collect()[0]
    assert t["attempts"] == 2 and "rate-limited" in t["notes"][0]


def test_gemini_thought_signature_is_sent_back_unchanged():
    """Real error from gemini-3.5-flash: 'Function call is missing a thought_signature in functionCall parts'."""
    from app.llm import GeminiLLM, GroqLLM
    sent = []
    sig = {"extra_content": {"google": {"thought_signature": "CvcQAdHN2OekY10ClPFkYA=="}}}

    def handler(req):
        sent.append(_json.loads(req.content))
        if len(sent) == 1:
            return _completion({"role": "assistant", "content": None, "tool_calls": [
                {"id": "fc1", "type": "function", **sig,
                 "function": {"name": "check_availability", "arguments": "{\"visit_type\": \"evaluation\"}"}}]})
        return _completion({"role": "assistant", "content": "Here are some times."})
    llm = _compat(GeminiLLM, handler)
    first = llm.chat_tools([{"role": "user", "content": "book me"}], TOOLS)
    call = first.tool_calls[0]
    assert call.extra == sig
    history = [{"role": "user", "content": "book me"},
               {"role": "assistant", "content": "", "tool_calls": [
                   {"id": call.id, "name": call.name, "arguments": call.arguments, "extra": call.extra}]},
               {"role": "tool", "tool_call_id": call.id, "name": call.name, "content": "{\"slots\": []}"}]
    assert llm.chat_tools(history, TOOLS).text == "Here are some times."
    assert sent[1]["messages"][1]["tool_calls"][0]["extra_content"] == sig["extra_content"]

    # A call the model wrote as text (no signature) gets Gemini's documented placeholder...
    sent.clear()
    sent.append({})                                             # skip the tool-call answer
    history[1]["tool_calls"][0]["extra"] = {}
    llm.chat_tools(history, TOOLS)
    assert sent[1]["messages"][1]["tool_calls"][0]["extra_content"]["google"]["thought_signature"] == \
        "skip_thought_signature_validator"
    # ...but other providers never see Gemini fields.
    sent.clear()
    sent.append({})
    _compat(GroqLLM, handler).chat_tools(history, TOOLS)
    assert "extra_content" not in sent[1]["messages"][1]["tool_calls"][0]


def test_gemini_falls_back_to_the_lighter_model_when_the_main_one_stalls(monkeypatch):
    """Live demo logs: gemini-3.5-flash sometimes hung until the read timeout (one message took 104 s)."""
    import httpx

    from app import timing
    from app.llm import GeminiLLM
    monkeypatch.delenv("GEMINI_FALLBACK_MODEL", raising=False)
    asked = []

    def handler(req):
        model = _json.loads(req.content)["model"]
        asked.append(model)
        if model == "gemini-3.5-flash":
            raise httpx.ReadTimeout("The read operation timed out")
        return _completion({"role": "assistant", "content": "We take Aetna [1]."})
    llm = _compat(GeminiLLM, handler)
    assert llm.fallback_model == "gemini-3.5-flash-lite" and GeminiLLM.DEFAULT_TIMEOUT == 20
    timing.start()
    with timing.step("model: write answer"):
        assert llm.chat([{"role": "user", "content": "aetna?"}]) == "We take Aetna [1]."
    assert asked == ["gemini-3.5-flash", "gemini-3.5-flash-lite"]
    assert "fallback gemini-3.5-flash-lite" in timing.collect()[0]["notes"][0]

    monkeypatch.setenv("GEMINI_FALLBACK_MODEL", "none")             # can be switched off
    asked.clear()
    with pytest.raises(LLMError, match="took too long"):
        llm.chat([{"role": "user", "content": "aetna?"}])
    assert asked == ["gemini-3.5-flash"]


def test_no_fallback_when_the_network_is_down():
    import httpx

    from app.llm import GeminiLLM
    asked = []

    def handler(req):
        asked.append(1)
        raise httpx.ConnectTimeout("timed out")
    with pytest.raises(LLMError, match="Couldn't open a connection"):
        _compat(GeminiLLM, handler).chat([{"role": "user", "content": "hi"}])
    assert len(asked) == 1                                           # another model wouldn't help


def test_fallback_model_also_covers_quota_errors(monkeypatch):
    """Live demo: 'The assistant is busy right now (Gemini usage limit reached)'. Free quotas are per model."""
    import httpx

    from app.llm import GeminiLLM
    monkeypatch.delenv("GEMINI_FALLBACK_MODEL", raising=False)

    def handler(req):
        if _json.loads(req.content)["model"] == "gemini-3.5-flash":
            return httpx.Response(429, json={"error": {"message": "Quota exceeded for GenerateRequestsPerDay"}})
        return _completion({"role": "assistant", "content": "OK"})
    assert _compat(GeminiLLM, handler).chat([{"role": "user", "content": "hi"}]) == "OK"

    both_out = _compat(GeminiLLM, lambda req: httpx.Response(429, json={"error": {"message": "Quota exceeded"}}))
    with pytest.raises(LLMError, match="busy right now"):
        both_out.chat([{"role": "user", "content": "hi"}])


def test_typographic_spaces_are_normalised():
    """gpt-oss on Groq writes 'Oct\\u202f29 at 9:00\\u202fAM'; matching code and eval rules expect normal spaces."""
    from app.llm import GroqLLM, clean_text
    assert clean_text("Thursday, Oct 29 at 9:00 AM  ") == "Thursday, Oct 29 at 9:00 AM"
    llm = _compat(GroqLLM, lambda req: _completion({"role": "assistant", "content": "Open 7:00 AM to 7:00 PM"}))
    assert llm.chat([{"role": "user", "content": "hours?"}]) == "Open 7:00 AM to 7:00 PM"


def test_non_breaking_hyphens_are_normalised():
    from app.llm import clean_text
    assert clean_text("out‑of‑network, call (703) 555‑0142") == "out-of-network, call (703) 555-0142"
    assert clean_text("Monday–Thursday") == "Monday–Thursday"          # en dash in ranges is left alone
