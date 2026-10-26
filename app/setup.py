"""Build a ChatEngine from .env settings. Shared by the API server and the terminal chat."""
from __future__ import annotations

import os

from ingest.env import load_dotenv


def build_engine(llm=None, embedder=None, reranker="default", database_url: str | None = None):
    load_dotenv()                           # before importing modules that read settings at import time
    from ingest import store
    from ingest.embed import get_embedder
    from ingest.rerank import get_reranker

    from .chat import ChatEngine
    from .llm import get_llm

    database_url = database_url or os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL is not set (put it in .env).")
    if llm is None:
        llm = get_llm()
        llm.check()                         # clear message now, instead of a failure on the first question
    embedder = embedder or get_embedder(os.environ.get("EMBEDDER", "local"))
    if reranker == "default":
        reranker = get_reranker()
    conn = store.connect(database_url, embedder.dims)
    store.check_dims(conn, embedder.dims)
    return ChatEngine(conn, embedder, reranker, llm)


def build_concierge(llm=None, embedder=None, reranker="default", database_url: str | None = None):
    """The full assistant: answers, booking agent, handoffs and session memory."""
    load_dotenv()
    from datetime import date, timedelta

    from . import booking
    from .agent import BookingAgent
    from .chat import CLINIC_NAME, CLINIC_PHONE
    from .concierge import Concierge

    engine = build_engine(llm=llm, embedder=embedder, reranker=reranker, database_url=database_url)
    # A second connection for bookings and sessions, so they don't wait behind searches.
    import psycopg
    from psycopg.rows import dict_row
    conn = psycopg.connect(database_url or os.environ["DATABASE_URL"], row_factory=dict_row, autocommit=True,
                           prepare_threshold=None)
    schedule = booking.load_schedule()
    booking.apply_schema(conn)
    # Keep at least two weeks of calendar ahead. A long-running demo (or one that starts up again after weeks
    # asleep) tops itself up; existing slots and bookings are never touched.
    last = conn.execute("select max(starts_at) last from slots").fetchone()["last"]
    if last is None or last.date() < date.today() + timedelta(days=14):
        out = booking.seed_slots(conn, schedule, date.today(), days=21)
        if out["created"]:
            print(f"Added {out['created']} appointment slots to cover the next 21 days.")
    agent = BookingAgent(engine.llm, schedule, CLINIC_NAME, CLINIC_PHONE)
    return Concierge(conn, engine, agent, schedule)
