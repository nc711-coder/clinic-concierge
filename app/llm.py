"""Language model providers for the chat. Pick one with LLM_PROVIDER in .env:

    ollama     (default) free, runs on your PC through Ollama (https://ollama.com). No API key.
               OLLAMA_MODEL  default llama3.2:3b (~2 GB, CPU). With 16 GB+ RAM, llama3.1:8b answers better.
               OLLAMA_URL    default http://localhost:11434
    openai     OpenAI API. Needs OPENAI_API_KEY.     OPENAI_CHAT_MODEL  default gpt-5-mini
    anthropic  Claude API. Needs ANTHROPIC_API_KEY.  ANTHROPIC_MODEL    default claude-haiku-5-5

Every provider has the same two methods, so the rest of the app doesn't care which one runs:
    chat(messages, temperature=, max_tokens=) -> str   messages are {"role": "system"|"user"|"assistant", ...}
    check()                                            fail early with a clear message (missing key, ...)
"""
from __future__ import annotations

import logging
import os

import httpx

DEFAULT_MODEL = "llama3.2:3b"


log = logging.getLogger("clinic_concierge")


# Some models (gpt-oss) write typographic spaces and hyphens, e.g. "9:00\u202fAM", "out\u2011of\u2011network". They look normal but break exact-text
# matching (booking times, eval rules), so replies are normalised to ordinary spaces on arrival.
_ODD_SPACES = str.maketrans({"\u202f": " ", "\u00a0": " ", "\u2009": " ", "\u2007": " ", "\u200a": " ",
                             "\u2010": "-", "\u2011": "-"})       # (non-breaking) hyphens -> "-" as well


def clean_text(text: str | None) -> str:
    return (text or "").translate(_ODD_SPACES).strip()


class LLMError(RuntimeError):
    """Raised with a message a person can act on (Ollama not running, model not pulled, ...)."""


class OllamaLLM:
    def __init__(self, model: str | None = None, url: str | None = None, timeout: float = 180.0):
        self.model = model or os.environ.get("OLLAMA_MODEL") or DEFAULT_MODEL
        self.url = (url or os.environ.get("OLLAMA_URL") or "http://localhost:11434").rstrip("/")
        from . import timing
        self.client = httpx.Client(timeout=timeout, event_hooks={"request": [timing.count_request],
                                                                 "response": [timing.note_response]})

    def chat(self, messages: list[dict], *, temperature: float = 0.0, max_tokens: int = 400) -> str:
        """Send a list of {"role": "system"|"user"|"assistant", "content": str} and return the reply text."""
        try:
            resp = self.client.post(f"{self.url}/api/chat", json={
                "model": self.model,
                "messages": messages,
                "stream": False,
                # Ollama defaults to a 4k-token context window; retrieved sources can exceed that.
                "options": {"temperature": temperature, "num_ctx": 8192, "num_predict": max_tokens},
            })
        except httpx.ConnectError:
            raise LLMError(f"Can't reach Ollama at {self.url}. Is the Ollama app running? "
                           f"Start it, or run `ollama serve` in another terminal.")
        except httpx.TimeoutException:
            raise LLMError("Ollama took too long to answer. The first request after starting loads the model "
                           "into memory and can be slow; try again, or use a smaller OLLAMA_MODEL.")
        if resp.status_code == 404:
            raise LLMError(f"Ollama doesn't have the model '{self.model}' yet. Run: ollama pull {self.model}")
        if resp.status_code >= 400:
            raise LLMError(f"Ollama returned {resp.status_code}: {resp.text[:300]}")
        return resp.json()["message"]["content"].strip()

    def check(self) -> None:
        """Fail early, with a clear message, if Ollama isn't running or the model isn't pulled."""
        try:
            tags = self.client.get(f"{self.url}/api/tags", timeout=5).json()
        except httpx.HTTPError:
            raise LLMError(f"Can't reach Ollama at {self.url}. Install it from ollama.com and make sure it's running.")
        names = {m.get("name", "") for m in tags.get("models", [])}
        if self.model not in names and f"{self.model}:latest" not in names:
            have = ", ".join(sorted(names)) or "none"
            raise LLMError(f"Model '{self.model}' isn't downloaded (you have: {have}). Run: ollama pull {self.model}")


# --------------------------------------------------------------------------- OpenAI

def _broken_tool_call(e) -> bool:
    text = f"{getattr(e, 'code', '')} {getattr(e, 'message', '')} {getattr(e, 'body', '')}".lower()
    return "tool_use_failed" in text or "failed to call a function" in text or "tool call validation" in text


def _root_cause(e: BaseException) -> str:
    """The innermost error behind a connection failure (DNS, SSL certificate, refused, IPv6...), which is what
    tells you how to fix it."""
    while e.__cause__ or e.__context__:
        e = e.__cause__ or e.__context__
    return f"{type(e).__name__}: {e}"[:300]


class OpenAILLM:
    """OpenAI, and (as subclasses below) any provider with an OpenAI-compatible API: Gemini, Groq."""
    DEFAULT_MODEL = "gpt-5-mini"
    NAME, KEY_ENV, MODEL_ENV, BASE_URL = "OpenAI", "OPENAI_API_KEY", "OPENAI_CHAT_MODEL", None
    EFFORT_ENV = "OPENAI_REASONING_EFFORT"      # how hard a reasoning model thinks: low is plenty here
    TOKENS_PARAM = "max_completion_tokens"      # name of the answer-length limit this API expects
    TOOL_RESULT_NAME = False                    # whether tool results must repeat the function name
    SIGNED_TOOL_CALLS = False                   # whether every tool call sent back needs a thought signature
    DEFAULT_TIMEOUT, MAX_RETRIES = 45, 2        # per attempt (LLM_TIMEOUT overrides) and retries per model
    FALLBACK_ENV, DEFAULT_FALLBACK = None, None # second model to try when the first one stalls

    def __init__(self, model: str | None = None, client=None):
        self.model = model or os.environ.get(self.MODEL_ENV) or self.DEFAULT_MODEL
        self._client = client

    @property
    def client(self):
        if self._client is None:
            import httpx
            from openai import DefaultHttpxClient, OpenAI

            from . import timing
            extra = {}
            if os.environ.get("FORCE_IPV4", "").lower() in ("1", "true", "yes"):
                # Some home networks advertise IPv6 but can't route it, so connections hang. Binding to
                # 0.0.0.0 makes every connection use IPv4.
                extra["transport"] = httpx.HTTPTransport(local_address="0.0.0.0")
            # Count attempts and note "slow down" (429) answers, so --debug can show hidden retries.
            http_client = DefaultHttpxClient(event_hooks={"request": [timing.count_request],
                                                          "response": [timing.note_response]}, **extra)
            # LLM_TIMEOUT: seconds to wait for one attempt before retrying (a stuck request on a busy free
            # tier is better retried sooner than waited out).
            self._client = OpenAI(api_key=os.environ.get(self.KEY_ENV), base_url=self.BASE_URL,
                                  max_retries=self.MAX_RETRIES, http_client=http_client,
                                  timeout=float(os.environ.get("LLM_TIMEOUT") or self.DEFAULT_TIMEOUT))
        return self._client

    @property
    def reasoning(self) -> bool:
        # Reasoning models (gpt-5 family, o-series) reject `temperature` and spend part of the token budget
        # thinking before they answer.
        return self.model.startswith(("gpt-5", "o1", "o3", "o4"))

    def _sampling(self, max_tokens: int, temperature: float = 0.0) -> dict:
        if self.reasoning:                       # thinking counts against the limit: leave room to answer
            return {self.TOKENS_PARAM: max(max_tokens * 5, 2000),
                    "reasoning_effort": os.environ.get(self.EFFORT_ENV, "low")}
        return {self.TOKENS_PARAM: max_tokens, "temperature": temperature}

    def chat(self, messages: list[dict], *, temperature: float = 0.0, max_tokens: int = 400) -> str:
        kwargs = {"model": self.model, "messages": messages, **self._sampling(max_tokens, temperature)}
        resp = self._create(kwargs)
        text = clean_text(resp.choices[0].message.content)
        if not text and resp.choices[0].finish_reason == "length":
            raise LLMError(f"The {self.NAME} model used its whole token budget thinking and returned no answer. "
                           f"Set {self.EFFORT_ENV}=low (or minimal) in .env, or use a non-reasoning model.")
        return text

    @property
    def fallback_model(self) -> str | None:
        """A second model to try once when the main one stalls or is overloaded (GEMINI_FALLBACK_MODEL=none to turn
        it off). Free tiers occasionally hang on a request; a lighter model usually answers in seconds."""
        name = os.environ.get(self.FALLBACK_ENV) if self.FALLBACK_ENV else None
        name = self.DEFAULT_FALLBACK if name is None else name
        return None if not name or name.lower() == "none" or name == self.model else name

    def _create(self, kwargs):
        """One API call (plus the fallback model if the main one stalls), errors turned into plain English."""
        import openai

        from . import timing
        try:
            return self.client.chat.completions.create(**kwargs)
        except (openai.APITimeoutError, openai.InternalServerError, openai.RateLimitError) as e:
            # Stalled, overloaded, or out of quota. Free-tier quotas are counted per model, so the
            # fallback model usually still has some left when the main one has run out.
            fallback = self.fallback_model
            never_connected = "Connect" in _root_cause(e).split(":")[0]
            if not fallback or never_connected:
                raise self._friendly(e) from e
            log.warning("%s %s didn't answer (%s); trying %s", self.NAME, self.model, type(e).__name__, fallback)
            why = "out of quota" if isinstance(e, openai.RateLimitError) else "stalled"
            timing.note(f"{self.model} {why}, answered by fallback {fallback}")
            try:
                return self.client.chat.completions.create(**{**kwargs, "model": fallback})
            except Exception as e2:
                raise self._friendly(e2) from e2
        except openai.BadRequestError as e:
            if not _broken_tool_call(e):
                raise self._friendly(e) from e
            # The model wrote a tool call the provider couldn't parse (Groq: "tool_use_failed"). It's random,
            # so the same request usually works the second time; then the fallback model gets a go.
            log.warning("%s %s wrote a broken tool call; retrying", self.NAME, self.model)
            timing.note(f"{self.model} wrote a broken tool call, retried")
            for model in [self.model] + ([self.fallback_model] if self.fallback_model else []):
                try:
                    return self.client.chat.completions.create(**{**kwargs, "model": model})
                except openai.BadRequestError as e2:
                    if not _broken_tool_call(e2):
                        raise self._friendly(e2) from e2
                    e = e2
                except Exception as e2:
                    raise self._friendly(e2) from e2
            raise LLMError(f"{self.NAME} kept writing broken tool calls ({str(e.message)[:200]}). Usually random; "
                           f"if it keeps happening, try another model with `python -m app.llmcheck`.") from e
        except Exception as e:
            raise self._friendly(e) from e

    def _friendly(self, e: Exception) -> Exception:
        import openai
        if isinstance(e, openai.AuthenticationError):
            return LLMError(f"{self.NAME} rejected the API key. Check {self.KEY_ENV} in .env.")
        if isinstance(e, openai.NotFoundError):
            return LLMError(f"{self.NAME} won't run the model '{self.model}' for this key: it may be retired, or not "
                            f"available on your plan ({self.NAME} said: {str(e.message)[:160]}). Set {self.MODEL_ENV} "
                            f"in .env to one of: {self._available_models()}")
        if isinstance(e, openai.RateLimitError):
            return LLMError(f"The assistant is busy right now ({self.NAME} usage limit reached). Please try again in "
                            f"a minute, or call the clinic. [{e.message}]")
        if isinstance(e, openai.BadRequestError):
            return LLMError(f"{self.NAME} rejected the request: {e.message}")
        if isinstance(e, openai.APITimeoutError):
            cause = _root_cause(e)
            if "Connect" in cause.split(":")[0]:          # the connection never opened: a network problem
                return LLMError(f"Couldn't open a connection to the {self.NAME} API ({cause}). This is usually the "
                                f"network: a firewall/antivirus, or IPv6 trouble (try FORCE_IPV4=1 in .env).")
            return LLMError(f"{self.NAME} took too long to answer ({cause}). Its servers may be busy: try again, "
                            f"or try a lighter model (python -m app.llmcheck lists them).")
        if isinstance(e, openai.InternalServerError):
            return LLMError(f"{self.NAME} had a server error ({e.status_code}): {str(e.message)[:160]}. Try again shortly.")
        if isinstance(e, openai.APIConnectionError):
            return LLMError(f"Can't reach the {self.NAME} API: {_root_cause(e)}")
        return e

    def _available_models(self) -> str:
        try:
            ids = sorted(m.id.removeprefix("models/") for m in self.client.models.list())
        except Exception:
            return "(couldn't list them; check the provider's model page)"
        skip = ("embed", "whisper", "tts", "image", "guard", "aqa", "imagen", "veo", "audio", "vision-", "live",
                "transcribe", "translate", "computer-use", "research", "antigravity", "customtools")
        chat = [i for i in ids if not any(w in i for w in skip)] or ids
        chat.sort(key=lambda i: ("preview" in i, "flash" not in i, i))     # stable "flash" models first
        return ", ".join(chat[:30])

    def check(self) -> None:
        if not os.environ.get(self.KEY_ENV):
            raise LLMError(f"LLM_PROVIDER={self.NAME.lower()} but {self.KEY_ENV} is not set in .env.")


class GeminiLLM(OpenAILLM):
    """Google Gemini through its OpenAI-compatible endpoint. Free tier with a key from aistudio.google.com
    (no card); used for the live demo. Gemini 3 models always think a little; reasoning_effort sets how much."""
    # Picked with `python -m app.llmcheck` (Oct 2026): fastest of the free models that passed every step, including
    # the two-round tool exchange. 3.8-flash and 3.1-flash-lite often timed out on the free tier; 2.5 isn't
    # available to new free keys. Use exact names, not "-latest" aliases, so behaviour doesn't change silently.
    DEFAULT_MODEL = "gemini-3.5-flash"
    NAME, KEY_ENV, MODEL_ENV = "Gemini", "GEMINI_API_KEY", "GEMINI_MODEL"
    BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
    EFFORT_ENV = "GEMINI_REASONING_EFFORT"
    TOKENS_PARAM = "max_tokens"
    TOOL_RESULT_NAME = True
    SIGNED_TOOL_CALLS = True
    # The free tier usually answers in 1-5 s but occasionally hangs: give up after 20 s, retry once, then let the
    # lighter model answer (it passed every `python -m app.llmcheck` step too).
    DEFAULT_TIMEOUT, MAX_RETRIES = 20, 1
    FALLBACK_ENV, DEFAULT_FALLBACK = "GEMINI_FALLBACK_MODEL", "gemini-3.5-flash-lite"

    @property
    def reasoning(self) -> bool:
        return True


class GroqLLM(OpenAILLM):
    """Groq runs open models very fast behind an OpenAI-compatible API. Used for the live demo on the paid
    Developer plan (~$0.15 / $0.60 per million tokens for gpt-oss-120b, about a tenth of a cent per message).
    The free tier's Llama models were removed in August 2026."""
    DEFAULT_MODEL = "openai/gpt-oss-120b"
    NAME, KEY_ENV, MODEL_ENV, BASE_URL = "Groq", "GROQ_API_KEY", "GROQ_MODEL", "https://api.groq.com/openai/v1"
    EFFORT_ENV = "GROQ_REASONING_EFFORT"
    # Groq is fast (answers in a second or two), so a stuck request can be abandoned sooner; the smaller
    # gpt-oss model answers if the main one stalls or hits a limit.
    DEFAULT_TIMEOUT, MAX_RETRIES = 30, 1
    FALLBACK_ENV, DEFAULT_FALLBACK = "GROQ_FALLBACK_MODEL", "openai/gpt-oss-20b"

    @property
    def reasoning(self) -> bool:
        return self.model.startswith(("openai/gpt-oss", "qwen"))


# --------------------------------------------------------------------------- Anthropic (Claude)

class AnthropicLLM:
    DEFAULT_MODEL = "claude-haiku-5-5"

    def __init__(self, model: str | None = None, client=None):
        self.model = model or os.environ.get("ANTHROPIC_MODEL") or self.DEFAULT_MODEL
        self._client = client

    @property
    def client(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic(max_retries=3, timeout=60)
        return self._client

    def chat(self, messages: list[dict], *, temperature: float = 0.0, max_tokens: int = 400) -> str:
        import anthropic

        # Claude takes the system prompt as its own field, not as a message, and the conversation must
        # alternate user/assistant starting with a user turn.
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        turns: list[dict] = []
        for m in messages:
            if m["role"] == "system":
                continue
            if turns and turns[-1]["role"] == m["role"]:          # merge two same-role turns in a row
                turns[-1]["content"] += "\n\n" + m["content"]
            else:
                turns.append({"role": m["role"], "content": m["content"]})
        if turns and turns[0]["role"] == "assistant":
            turns.insert(0, {"role": "user", "content": "(conversation continues)"})
        # No `temperature`: current Claude models and SDKs don't take it, so it's not sent.
        kwargs = {"model": self.model, "max_tokens": max_tokens, "messages": turns}
        if system:
            kwargs["system"] = system
        try:
            resp = self.client.messages.create(**kwargs)
        except anthropic.AuthenticationError:
            raise LLMError("Anthropic rejected the API key. Check ANTHROPIC_API_KEY in .env.")
        except anthropic.NotFoundError:
            raise LLMError(f"Anthropic doesn't recognise the model '{self.model}'. Check ANTHROPIC_MODEL in .env.")
        except anthropic.RateLimitError:
            raise LLMError("Anthropic rate limit reached. Wait a minute or check your plan's limits.")
        except anthropic.BadRequestError as e:
            raise LLMError(f"Anthropic rejected the request: {e.message}")
        except anthropic.APIConnectionError:
            raise LLMError("Can't reach the Anthropic API. Check your internet connection.")
        return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()

    def check(self) -> None:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise LLMError("LLM_PROVIDER=anthropic but ANTHROPIC_API_KEY is not set in .env.")


# --------------------------------------------------------------------------- choose one

PROVIDERS = {"ollama": OllamaLLM, "openai": OpenAILLM, "gemini": GeminiLLM, "groq": GroqLLM, "anthropic": AnthropicLLM,
             "claude": AnthropicLLM}


def get_llm(provider: str | None = None):
    name = (provider or os.environ.get("LLM_PROVIDER") or "ollama").strip().lower()
    if name not in PROVIDERS:
        raise LLMError(f"Unknown LLM_PROVIDER '{name}'. Use ollama, gemini, groq, openai or anthropic.")
    return PROVIDERS[name]()


# =============================================================================================
# Tool calling
#
# One shared message format for every provider:
#   {"role": "system" | "user", "content": str}
#   {"role": "assistant", "content": str, "tool_calls": [{"id": str, "name": str, "arguments": dict}]}
#   {"role": "tool", "tool_call_id": str, "name": str, "content": str}      (content = JSON result)
# Each provider converts to and from its own format in chat_tools().
# =============================================================================================

import itertools
import json as _json
from dataclasses import dataclass, field

_ids = itertools.count(1)


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict
    # Provider-specific fields that must be sent back unchanged with this call in the next round,
    # e.g. Gemini 3's {"extra_content": {"google": {"thought_signature": "..."}}}.
    extra: dict = field(default_factory=dict)


@dataclass
class LLMReply:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)


def _parse_args(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        out = _json.loads(raw or "{}")
        return out if isinstance(out, dict) else {}
    except (TypeError, ValueError):
        return {}


def _ollama_chat_tools(self, messages, tools, *, max_tokens: int = 500) -> LLMReply:
    out = []
    for m in messages:
        if m["role"] == "assistant" and m.get("tool_calls"):
            out.append({"role": "assistant", "content": m.get("content") or "",
                        "tool_calls": [{"function": {"name": c["name"], "arguments": c["arguments"]}}
                                       for c in m["tool_calls"]]})
        elif m["role"] == "tool":
            out.append({"role": "tool", "content": m["content"], "tool_name": m["name"]})
        else:
            out.append({"role": m["role"], "content": m["content"]})
    body = {"model": self.model, "messages": out, "stream": False,
            "tools": [{"type": "function", "function": t} for t in tools],
            "options": {"temperature": 0.0, "num_ctx": 8192, "num_predict": max_tokens}}
    try:
        resp = self.client.post(f"{self.url}/api/chat", json=body)
    except httpx.ConnectError:
        raise LLMError(f"Can't reach Ollama at {self.url}. Is the Ollama app running?")
    except httpx.TimeoutException:
        raise LLMError("Ollama took too long to answer. Try again, or use a smaller OLLAMA_MODEL.")
    if resp.status_code == 404:
        raise LLMError(f"Ollama doesn't have the model '{self.model}' yet. Run: ollama pull {self.model}")
    if resp.status_code == 400 and "does not support tools" in resp.text:
        raise LLMError(f"The Ollama model '{self.model}' can't call tools. Use llama3.1:8b or llama3.2:3b.")
    if resp.status_code >= 400:
        raise LLMError(f"Ollama returned {resp.status_code}: {resp.text[:300]}")
    msg = resp.json()["message"]
    calls = [ToolCall(id=c.get("id") or f"call_{next(_ids)}", name=c["function"]["name"],
                      arguments=_parse_args(c["function"].get("arguments")))
             for c in msg.get("tool_calls") or []]
    return LLMReply(text=(msg.get("content") or "").strip(), tool_calls=calls)


def _openai_chat_tools(self, messages, tools, *, max_tokens: int = 500) -> LLMReply:

    out = []
    for m in messages:
        if m["role"] == "assistant" and m.get("tool_calls"):
            calls = [{"id": c["id"], "type": "function",
                      "function": {"name": c["name"], "arguments": _json.dumps(c["arguments"])}, **(c.get("extra") or {})}
                     for c in m["tool_calls"]]
            if self.SIGNED_TOOL_CALLS and not any(c.get("extra") for c in m["tool_calls"]):
                # A call the model didn't make itself (e.g. one written as JSON text) has no thought signature;
                # Gemini accepts this documented placeholder instead.
                calls[0]["extra_content"] = {"google": {"thought_signature": "skip_thought_signature_validator"}}
            out.append({"role": "assistant", "content": m.get("content") or None, "tool_calls": calls})
        elif m["role"] == "tool":
            msg = {"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]}
            if self.TOOL_RESULT_NAME and m.get("name"):
                msg["name"] = m["name"]
            out.append(msg)
        else:
            out.append({"role": m["role"], "content": m["content"]})
    kwargs = {"model": self.model, "messages": out, "tools": [{"type": "function", "function": t} for t in tools],
              **self._sampling(max_tokens)}
    resp = self._create(kwargs)
    msg = resp.choices[0].message
    calls = [ToolCall(id=c.id or f"call_{i}", name=c.function.name, arguments=_parse_args(c.function.arguments),
                      extra={k: v for k, v in (c.model_extra or {}).items() if v is not None})
             for i, c in enumerate(msg.tool_calls or [])]
    return LLMReply(text=clean_text(msg.content), tool_calls=calls)


def _anthropic_messages(messages) -> tuple[str, list[dict]]:
    """Shared format -> (system, messages) for Claude: tool calls are content blocks, results go in a user turn,
    turns alternate user/assistant."""
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    turns: list[dict] = []

    def add(role, blocks):
        if turns and turns[-1]["role"] == role:
            turns[-1]["content"].extend(blocks)
        else:
            turns.append({"role": role, "content": list(blocks)})

    for m in messages:
        if m["role"] == "system":
            continue
        if m["role"] == "tool":
            add("user", [{"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"]}])
        elif m["role"] == "assistant":
            blocks = [{"type": "text", "text": m["content"]}] if m.get("content") else []
            blocks += [{"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["arguments"]}
                       for c in m.get("tool_calls") or []]
            add("assistant", blocks or [{"type": "text", "text": "(no reply)"}])
        else:
            add("user", [{"type": "text", "text": m["content"]}])
    if turns and turns[0]["role"] == "assistant":
        turns.insert(0, {"role": "user", "content": [{"type": "text", "text": "(conversation continues)"}]})
    return system, turns


def _anthropic_chat_tools(self, messages, tools, *, max_tokens: int = 500) -> LLMReply:
    import anthropic

    system, turns = _anthropic_messages(messages)
    kwargs = {"model": self.model, "max_tokens": max_tokens, "messages": turns,
              "tools": [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}
                        for t in tools]}
    if system:
        kwargs["system"] = system
    try:
        resp = self.client.messages.create(**kwargs)
    except anthropic.AuthenticationError:
        raise LLMError("Anthropic rejected the API key. Check ANTHROPIC_API_KEY in .env.")
    except anthropic.BadRequestError as e:
        raise LLMError(f"Anthropic rejected the request: {e.message}")
    except anthropic.APIConnectionError:
        raise LLMError("Can't reach the Anthropic API. Check your internet connection.")
    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
    calls = [ToolCall(id=b.id, name=b.name, arguments=dict(b.input or {}))
             for b in resp.content if getattr(b, "type", None) == "tool_use"]
    return LLMReply(text=text, tool_calls=calls)


OllamaLLM.chat_tools = _ollama_chat_tools
OpenAILLM.chat_tools = _openai_chat_tools               # GroqLLM inherits it
AnthropicLLM.chat_tools = _anthropic_chat_tools
