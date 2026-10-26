"""Check the configured AI provider step by step, with timings. Useful when a new provider or model misbehaves.

    python -m app.llmcheck            # uses LLM_PROVIDER from .env
    python -m app.llmcheck gemini     # or name one
    $env:GEMINI_MODEL="gemini-2.5-flash"; python -m app.llmcheck     # PowerShell: try another model
                                                                      # (settings in the window beat .env)

Runs: list models -> a tiny chat (plain) -> the same with the app's settings -> a tool call.
Prints what each step sent, how long it took, and the error if any. Never prints your API key.
"""
from __future__ import annotations

import os
import sys
import time

from ingest.env import load_dotenv


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    load_dotenv()
    from app.llm import OpenAILLM, _root_cause, get_llm

    args = argv if argv is not None else sys.argv[1:]
    llm = get_llm(args[0] if args else None)
    print(f"Provider: {type(llm).__name__}   model: {llm.model}")
    if not isinstance(llm, OpenAILLM):
        print("This check is for OpenAI-compatible providers (openai, gemini, groq).")
        return 2
    from openai import OpenAI
    client = OpenAI(api_key=os.environ.get(llm.KEY_ENV), base_url=llm.BASE_URL, max_retries=0, timeout=45)

    def step(title, fn):
        t = time.time()
        try:
            out = fn()
            print(f"  OK    {time.time() - t:5.1f}s  {title}: {out}")
            return True
        except Exception as e:                                   # show everything; this is a diagnostic
            print(f"  FAIL  {time.time() - t:5.1f}s  {title}: {type(e).__name__}: {str(e)[:300]}")
            print(f"        cause: {_root_cause(e)}")
            return False

    def models():
        ids = sorted(m.id.removeprefix("models/") for m in client.models.list())
        chat = [i for i in ids if any(w in i for w in ("flash", "pro", "gpt", "llama", "qwen", "gemma"))
                and not any(w in i for w in ("embed", "image", "tts", "audio", "live", "veo"))]
        here = "YES" if llm.model in ids else "NO  <- set the model name in .env to one of the others"
        return f"current model listed? {here}\n        chat models: {', '.join(chat[:25])}"

    msgs = [{"role": "user", "content": "Reply with exactly: OK"}]

    def plain():
        r = client.chat.completions.create(model=llm.model, messages=msgs)
        return repr((r.choices[0].message.content or "")[:60])

    def with_app_settings():
        settings = llm._sampling(400)
        r = client.chat.completions.create(model=llm.model, messages=msgs, **settings)
        return f"{settings} -> {(r.choices[0].message.content or '')[:60]!r}"

    def tool_call():
        tools = [{"type": "function", "function": {
            "name": "check_availability", "description": "Find open appointment times",
            "parameters": {"type": "object", "properties": {"day": {"type": "string"}}, "required": ["day"]}}}]
        r = client.chat.completions.create(
            model=llm.model, tools=tools, **llm._sampling(400),
            messages=[{"role": "user", "content": "Find me a time on Thursday. Use the tool."}])
        calls = r.choices[0].message.tool_calls or []
        return f"{len(calls)} tool call(s): " + ", ".join(f"{c.function.name}({c.function.arguments})" for c in calls)

    def two_rounds():
        """What the booking agent does: model calls a tool, gets the result, then answers (via the app's code)."""
        spec = [{"name": "check_availability", "description": "Find open appointment times",
                 "parameters": {"type": "object", "properties": {"day": {"type": "string"}}, "required": ["day"]}}]
        history = [{"role": "user", "content": "Find me a time on Thursday. Use the tool."}]
        first = llm.chat_tools(history, spec)
        if not first.tool_calls:
            return f"no tool call in round 1 (said: {first.text[:80]!r})"
        c = first.tool_calls[0]
        history += [{"role": "assistant", "content": first.text,
                     "tool_calls": [{"id": c.id, "name": c.name, "arguments": c.arguments, "extra": c.extra}]},
                    {"role": "tool", "tool_call_id": c.id, "name": c.name,
                     "content": '{"slots": [{"slot_id": 1, "when": "Thu, Oct 29 at 9:00 AM"}]}'}]
        second = llm.chat_tools(history, spec)
        return f"round 2 answered: {second.text[:80]!r}"

    results = [step("list models", models), step("tiny chat, no settings", plain),
               step("tiny chat, app settings", with_app_settings), step("tool call", tool_call),
               step("tool call + result, 2 rounds (booking agent)", two_rounds)]
    print("All good." if all(results) else "Paste this output (it contains no keys) to get it fixed.")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
