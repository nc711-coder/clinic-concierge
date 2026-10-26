"""Chat in the terminal:

    python -m app            # 'reset' starts a new conversation, 'quit' exits
    python -m app --debug    # also show routing, search details and tool calls
"""
from __future__ import annotations

import argparse
import sys

from .llm import LLMError
from .setup import build_concierge


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):          # Windows consoles: print "–", "’" etc. safely
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(prog="python -m app", description="Chat with the clinic assistant.")
    p.add_argument("--debug", action="store_true", help="show routing, sources and tool calls")
    args = p.parse_args(argv)

    try:
        concierge = build_concierge()
    except LLMError as e:
        print(f"\n{e}")
        return 2
    print(f"\nClinic Concierge (model: {concierge.engine.llm.model}). Ask a question or book an appointment.\n"
          f"'reset' starts over, 'quit' exits.\n")
    session_id = None
    while True:
        try:
            message = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not message:
            continue
        if message.lower() in ("quit", "exit", "q"):
            return 0
        if message.lower() == "reset":
            session_id = None
            print("(new conversation)\n")
            continue
        try:
            r = concierge.handle(message, session_id)
        except LLMError as e:
            print(f"\n{e}\n")
            continue
        session_id = r.session_id
        if args.debug:
            print(f"\n  route: {r.route}")
            d = r.debug
            if d.get("search_query"):
                print(f"  search query: {d['search_query']}")
                for s in d.get("retrieved", []):
                    score = f"{s['rerank_score']:+.2f}" if s.get("rerank_score") is not None else "  -  "
                    print(f"  [{s['n']}] {score}  {s['source']} — {s.get('section') or s.get('title') or ''}")
            for call in d.get("tool_calls", []):
                who = " (by code)" if call.get("by") == "code" else ""
                print(f"  tool{who} {call['name']}({call['arguments']}) -> {str(call['result'])[:200]}")
            if d.get("timings") is not None:
                print(f"  time: {d.get('total_seconds', 0):.1f}s total")
                for t in d["timings"]:
                    extra = []
                    if t["attempts"] > 1:
                        extra.append(f"{t['attempts']} attempts")
                    extra += dict.fromkeys(t["notes"])                    # each note once
                    print(f"    {t['seconds']:5.1f}s  {t['step']}" + (f"   ({'; '.join(extra)})" if extra else ""))
        print(f"\nAssistant: {r.reply}")
        for c in r.citations:
            print(f"   [{c['n']}] {c['title']}" + (f" — {c['section']}" if c.get("section") else "")
                  + ("   (citation added by matching; the model left it out)" if c.get("inferred") else ""))
        if r.guardrail:
            print(f"   (safety guardrail: {r.guardrail})")
        if r.appointment:
            print(f"   (appointment #{r.appointment['appointment_id']} saved)")
        if r.handoff_id:
            print(f"   (front desk handoff #{r.handoff_id} created)")
        print()


if __name__ == "__main__":
    sys.exit(main())
