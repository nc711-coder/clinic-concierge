"""HTTP API.

    uvicorn app.main:app --reload            # then open http://127.0.0.1:8000/docs to try it

POST /chat   {"message": "I'd like to book an evaluation", "session_id": null}
          -> {"session_id": "...", "reply": "...", "route": "booking", "citations": [], "appointment": null,
              "awaiting_confirmation": false, "handoff_id": null, ...}
             Send the returned session_id with the next message; the server remembers the conversation
             and any booking in progress.
POST /ask    {"message": "do you take kaiser?", "history": [...]}  -> questions only, no booking, no memory
GET  /health -> {"status": "ok", "model": "llama3.1:8b"}
GET  /            the demo clinic website (web/index.html) with the chat widget
GET  /widget.js   the widget, for embedding on any site: <script src="https://HOST/widget.js" defer></script>
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ingest.env import load_dotenv

from .llm import LLMError

load_dotenv()
log = logging.getLogger("clinic_concierge")
WEB = Path(__file__).resolve().parent.parent / "web"

concierge = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global concierge
    if concierge is None:               # tests inject their own before startup
        from .setup import build_concierge
        concierge = build_concierge()
    yield


app = FastAPI(title="Clinic Concierge", version="0.3.0", lifespan=lifespan)

# Browsers only let a page on site A call an API on site B if B says A is allowed (CORS). The demo site is
# served from this same server, so it needs nothing; ALLOWED_ORIGINS lists other sites that embed the widget.
_origins = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "*").split(",") if o.strip()]
app.add_middleware(CORSMiddleware, allow_origins=_origins, allow_methods=["GET", "POST"],
                   allow_headers=["Content-Type"])


class Message(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    content: str = Field(max_length=4000)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    session_id: str | None = None
    debug: bool = False                 # include search details and tool calls


class AskRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    history: list[Message] = Field(default_factory=list, max_length=40)
    debug: bool = False


# --------------------------------------------------------------------------- limits for a public demo
#
# A public link means strangers (and bots) can use your free model quota. Both limits are off unless set:
#   MESSAGES_PER_HOUR_PER_VISITOR=40   one visitor (IP address) can send at most this many messages per hour
#   MESSAGES_PER_DAY=300               all visitors together, per day (protects the Groq daily request limit)
# Counts live in memory, which is right for a single server (Cloud Run with --max-instances 1).

class RateLimiter:
    def __init__(self, per_hour: int = 0, per_day: int = 0, clock=time.time):
        self.per_hour, self.per_day, self.clock = per_hour, per_day, clock
        self.recent: dict[str, deque] = defaultdict(deque)
        self.day, self.today_count = None, 0
        self.lock = threading.Lock()

    def check(self, visitor: str) -> str | None:
        """None if allowed (and counted), otherwise the message to show."""
        now = self.clock()
        with self.lock:
            day = int(now // 86400)
            if day != self.day:
                self.day, self.today_count = day, 0
            if self.per_day and self.today_count >= self.per_day:
                return ("The demo assistant has reached its message limit for today. Please try again tomorrow, "
                        "or call the clinic.")
            q = self.recent[visitor]
            while q and q[0] < now - 3600:
                q.popleft()
            if self.per_hour and len(q) >= self.per_hour:
                return "You've sent a lot of messages in the last hour. Please wait a little and try again."
            q.append(now)
            self.today_count += 1
            if len(self.recent) > 10000:                     # forget idle visitors
                for k in [k for k, v in self.recent.items() if not v or v[-1] < now - 3600]:
                    del self.recent[k]
        return None


limiter = RateLimiter(int(os.environ.get("MESSAGES_PER_HOUR_PER_VISITOR") or 0),
                      int(os.environ.get("MESSAGES_PER_DAY") or 0))


def visitor_id(request: Request) -> str:
    # Behind Cloud Run's load balancer the visitor's address is the first entry in X-Forwarded-For.
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() or (request.client.host if request.client else "unknown")


def model_unavailable(e: LLMError) -> HTTPException:
    """Visitors get a plain message; the technical reason ("check GROQ_API_KEY") goes to the server log."""
    log.error("model call failed: %s", e)
    text = str(e)
    if text.startswith("The assistant is busy"):
        return HTTPException(status_code=503, detail=text.split(" [")[0])
    return HTTPException(status_code=503, detail="The assistant isn't available right now. Please try again in a "
                                                 "few minutes.")


@app.get("/health")
def health():
    return {"status": "ok", "model": getattr(concierge.engine.llm, "model", None)}


@app.post("/chat")
def chat(req: ChatRequest, request: Request):
    blocked = limiter.check(visitor_id(request))
    if blocked:
        raise HTTPException(status_code=429, detail=blocked)
    try:
        result = concierge.handle(req.message, req.session_id)
    except LLMError as e:
        raise model_unavailable(e)
    out = asdict(result)
    if not req.debug:
        out.pop("debug")
    return out


@app.post("/ask")
def ask(req: AskRequest, request: Request):
    blocked = limiter.check(visitor_id(request))
    if blocked:
        raise HTTPException(status_code=429, detail=blocked)
    try:
        result = concierge.engine.answer(req.message, [m.model_dump() for m in req.history])
    except LLMError as e:
        raise model_unavailable(e)
    out = asdict(result)
    if not req.debug:
        out.pop("retrieved")
    return out


# Last, so the API routes above win: everything else is a file from web/ ("/" -> index.html).
app.mount("/", StaticFiles(directory=WEB, html=True), name="web")
