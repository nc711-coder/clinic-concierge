"""Send a sample event to EVENT_WEBHOOK_URL, to test the n8n workflow without making a booking.

    python -m app.notify              # a sample "appointment.booked"
    python -m app.notify handoff      # a sample "handoff.created"
    python -m app.notify error        # a sample "assistant.error" (the email you get when the chat fails)
"""
from __future__ import annotations

import os
import sys

from ingest.env import load_dotenv

SAMPLES = {
    "booked": ("appointment.booked", {
        "appointment_id": 0, "lead_id": 0, "when": "Thu, Oct 22 at 10:00 AM", "starts_at": "2026-10-22T10:00:00-04:00",
        "provider": "Dr. Marcus Bell", "visit_type": "evaluation", "visit_label": "initial evaluation",
        "name": "Test Patient", "phone": "(703) 555-0199", "email": "test.patient@example.com",
        "reason": "knee pain after running (TEST EVENT)"}),
    "handoff": ("handoff.created", {
        "handoff_id": 0, "session_id": None, "lead_id": 0, "reason": "asked for a person",
        "summary": "TEST EVENT: patient wants to talk about a bill.", "name": "Test Patient",
        "phone": "(703) 555-0199", "email": None}),
    "error": ("assistant.error", None),          # built in sample_error(), from the real alert code
}


def sample_error() -> dict:
    from app.alerts import Alerter, explain
    from app.llm import LLMError
    e = LLMError("TEST EVENT: Groq took too long to answer (ReadTimeout: The read operation timed out).")
    kind, headline, fix = explain(e)
    return Alerter.payload(e, kind, headline, fix, "do you take aetna?", "/chat", 2, "openai/gpt-oss-120b")


def main(argv=None) -> int:
    load_dotenv()
    from app.booking import notify
    which = (argv if argv is not None else sys.argv[1:]) or ["booked"]
    if which[0] not in SAMPLES:
        print(f"Unknown sample '{which[0]}'. Choose: {', '.join(SAMPLES)}")
        return 2
    url = os.environ.get("EVENT_WEBHOOK_URL")
    if not url:
        print("EVENT_WEBHOOK_URL is not set in .env, so there's nowhere to send the event.")
        return 2
    event, data = SAMPLES[which[0]]
    data = data if data is not None else sample_error()
    print(f"Sending {event} to {url} ...")
    status = notify(event, data, wait=True)
    if status is None:
        print("No answer. Is n8n running, and is the workflow active (or 'Listen for test event' clicked)?")
        return 1
    hints = {401: "the secret doesn't match: EVENT_WEBHOOK_SECRET in .env vs the n8n Header Auth credential",
             403: "the secret doesn't match: EVENT_WEBHOOK_SECRET in .env vs the n8n Header Auth credential",
             404: "n8n doesn't know this URL. Test URLs only work right after 'Listen for test event'; "
                  "for the production URL the workflow must be Active"}
    print(f"n8n answered HTTP {status}" + (f": {hints[status]}" if status in hints else " (OK)" if status < 300 else ""))
    return 0 if status < 300 else 1


if __name__ == "__main__":
    sys.exit(main())
