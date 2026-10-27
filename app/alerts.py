"""Tell the owner when a visitor got an error instead of an answer, and why.

When the chat fails ("The assistant isn't available right now"), an `assistant.error` event goes to the same
n8n webhook as bookings, and n8n emails it. The email says what broke in plain words, the likely fix, the
visitor's message and the command that shows the server log.

    ALERT_EVERY_MINUTES=30    at most one email per kind of problem in this window (default 30). A Groq outage
                              would otherwise send an email for every message. The next email says how many
                              failures happened in between.

Nothing is sent when EVENT_WEBHOOK_URL isn't set (local use). Sending never raises: an alert must not turn
one error into two.
"""
from __future__ import annotations

import logging
import os
import threading
import time
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo

log = logging.getLogger("clinic_concierge")

# (words in the error, kind, headline, likely fix). First match wins.
KNOWN = [
    ("rejected the API key", "api_key", "The AI provider rejected the API key",
     "Check GROQ_API_KEY in deploy.env.yaml (a key may have been deleted or rotated in the Groq console), then "
     "redeploy."),
    ("won't run the model", "model", "The AI model isn't available any more",
     "The model was probably retired. The error lists the models the key can use: set GROQ_MODEL in "
     "deploy.env.yaml to one of them, test with `python -m app.llmcheck`, then redeploy."),
    ("busy right now", "rate_limit", "The AI provider's usage limit was reached",
     "Check the Groq console (Settings > Limits and Billing): the plan may have dropped to the free tier, a "
     "spending limit was hit, or traffic spiked."),
    ("took too long", "timeout", "The AI provider stalled",
     "Usually temporary: both the main and the fallback model timed out. Check https://groqstatus.com. If it "
     "keeps happening, compare models with `python -m app.llmcheck`."),
    ("server error", "provider_down", "The AI provider had a server error",
     "Usually a short outage on the provider's side. Check https://groqstatus.com; nothing to change in the app."),
    ("broken tool calls", "tool_call", "The AI model kept writing invalid tool calls",
     "The model's request to a booking tool was malformed three times in a row (main model, retry, fallback). "
     "Usually random. If it repeats, test the model with `python -m app.llmcheck` or switch GROQ_MODEL."),
    ("rejected the request", "bad_request", "The AI provider rejected the request",
     "The error below says what it didn't accept. If a model setting changed, test with `python -m app.llmcheck`."),
    ("connection", "network", "The server couldn't reach the AI provider",
     "A network problem between Cloud Run and the provider. Usually temporary; check https://groqstatus.com."),
    ("can't reach", "network", "The server couldn't reach the AI provider",
     "A network problem between Cloud Run and the provider. Usually temporary; check https://groqstatus.com."),
]
DATABASE = ("database", "The database couldn't be reached",
            "Supabase pauses free projects after a week without activity. Open the Supabase dashboard and click "
            "Restore project. If it isn't paused, check DATABASE_URL (it must be the Session pooler address).")
OTHER = ("bug", "The app hit an unexpected error",
         "This is a bug or a setup problem, not the AI provider. The details below and the server log show where "
         "it happened.")

LOGS_COMMAND = ("gcloud run services logs read {service} --region {region} --limit 50")


def explain(e: BaseException) -> tuple[str, str, str]:
    """(kind, headline, likely fix) for an error, in plain words."""
    text = str(e).lower()
    module = type(e).__module__ or ""
    if module.startswith(("psycopg", "psycopg_pool")) or "could not translate host" in text or \
            "server closed the connection" in text:
        return DATABASE
    if type(e).__name__ == "LLMError":
        for words, kind, headline, fix in KNOWN:
            if words.lower() in text:
                return kind, headline, fix
        return "llm", "The AI provider returned an error", "See the details below."
    return OTHER


class Alerter:
    def __init__(self, every_minutes: float | None = None, clock=time.time, send=None):
        minutes = every_minutes if every_minutes is not None else float(os.environ.get("ALERT_EVERY_MINUTES") or 30)
        self.window = minutes * 60
        self.clock = clock
        self._send = send
        self.last_sent: dict[str, float] = {}
        self.skipped: dict[str, int] = {}
        self.lock = threading.Lock()

    def report(self, e: BaseException, visitor_message: str = "", where: str = "/chat", model: str = "") -> bool:
        """Email the owner about this failure, unless the same kind was emailed recently. True if sent."""
        try:
            kind, headline, fix = explain(e)
            now = self.clock()
            with self.lock:
                if now - self.last_sent.get(kind, -1e12) < self.window:
                    self.skipped[kind] = self.skipped.get(kind, 0) + 1
                    return False
                self.last_sent[kind] = now
                skipped = self.skipped.pop(kind, 0)
            payload = self.payload(e, kind, headline, fix, visitor_message, where, skipped, model)
            (self._send or _notify)("assistant.error", payload)
            return True
        except Exception:                                       # never let an alert cause a second failure
            log.exception("could not send the error alert")
            return False

    @staticmethod
    def payload(e, kind, headline, fix, visitor_message, where, skipped, model="") -> dict:
        tz = ZoneInfo(os.environ.get("CLINIC_TZ", "America/New_York"))
        service = os.environ.get("K_SERVICE", "clinic-concierge")
        tb = "".join(traceback.format_exception(type(e), e, e.__traceback__)[-6:]) if e.__traceback__ else ""
        return {
            "kind": kind,
            "headline": headline,
            "fix": fix,
            "error": f"{type(e).__name__}: {e}"[:1500],
            "traceback": tb[-2500:],
            "visitor_message": (visitor_message or "")[:300],
            "where": where,
            "when": datetime.now(tz).strftime("%a %b %d, %Y at %I:%M %p %Z"),
            "model": model or os.environ.get("LLM_PROVIDER", ""),
            "version": os.environ.get("K_REVISION", "local"),
            "similar_failures_not_emailed": skipped,
            "logs_command": LOGS_COMMAND.format(service=service, region=os.environ.get("CLOUD_RUN_REGION", "us-west1")),
        }


def _notify(event: str, payload: dict) -> None:
    from .booking import notify
    notify(event, payload)


alerter = Alerter()
