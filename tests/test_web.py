"""The demo site, the widget file and CORS (no database or model needed)."""
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app import main
from app.concierge import TurnResult
from app.tools import pick_offered_slot


class Fake:
    engine = SimpleNamespace(llm=SimpleNamespace(model="fake"))

    def handle(self, message, session_id=None):
        return TurnResult("s1", "hello", "answer")


def client(monkeypatch):
    monkeypatch.setattr(main, "concierge", Fake())
    return TestClient(main.app)


def test_site_and_widget_are_served(monkeypatch):
    with client(monkeypatch) as c:
        page = c.get("/")
        assert page.status_code == 200 and "widget.js" in page.text and "fictional clinic" in page.text
        js = c.get("/widget.js")
        assert js.status_code == 200 and "javascript" in js.headers["content-type"]
        assert "attachShadow" in js.text and "innerHTML = icon" in js.text
        assert c.get("/health").json()["status"] == "ok"            # API routes still win over the site


def test_widget_can_be_embedded_on_another_site(monkeypatch):
    with client(monkeypatch) as c:
        pre = c.options("/chat", headers={"Origin": "https://some-clinic.example", "Access-Control-Request-Method": "POST",
                                          "Access-Control-Request-Headers": "content-type"})
        assert pre.status_code == 200 and pre.headers["access-control-allow-origin"] in ("*", "https://some-clinic.example")
        r = c.post("/chat", json={"message": "hi"}, headers={"Origin": "https://some-clinic.example"})
        assert r.json()["reply"] == "hello" and "debug" not in r.json()


def test_option_number_from_the_widget_buttons():
    shown = [{"slot_id": 1, "when": "Thu, Oct 22 at 10:00 AM"}, {"slot_id": 2, "when": "Thu, Oct 22 at 11:00 AM"}]
    assert pick_offered_slot("2", shown, shown)["slot_id"] == 2
    assert pick_offered_slot("option 1", shown, shown)["slot_id"] == 1
    assert pick_offered_slot("3", shown, shown) is None                 # not on the list
    assert pick_offered_slot("2 people", shown, shown) is None


# --------------------------------------------------------------------------- public-demo limits

def test_rate_limiter_per_visitor_and_per_day():
    from app.main import RateLimiter
    now = [1_000_000.0]
    rl = RateLimiter(per_hour=2, per_day=3, clock=lambda: now[0])
    assert rl.check("a") is None and rl.check("a") is None
    assert "last hour" in rl.check("a")                        # 3rd message from the same visitor
    assert rl.check("b") is None                                # someone else is fine...
    assert "for today" in rl.check("c")                        # ...until the daily total (3) is used
    now[0] += 86400                                             # next day
    assert rl.check("c") is None


def test_limits_are_off_by_default_and_return_429_when_on(monkeypatch):
    from app.main import RateLimiter
    assert RateLimiter().check("x") is None and all(RateLimiter().check("x") is None for _ in range(100))
    monkeypatch.setattr(main, "limiter", RateLimiter(per_hour=1))
    with client(monkeypatch) as c:
        h = {"X-Forwarded-For": "203.0.113.7, 10.0.0.1"}         # Cloud Run puts the visitor first
        assert c.post("/chat", json={"message": "hi"}, headers=h).status_code == 200
        r = c.post("/chat", json={"message": "hi again"}, headers=h)
        assert r.status_code == 429 and "last hour" in r.json()["detail"]
        assert c.post("/chat", json={"message": "hi"}, headers={"X-Forwarded-For": "198.51.100.2"}).status_code == 200


def test_model_errors_are_plain_for_visitors(monkeypatch):
    from app.llm import LLMError

    class Broken(Fake):
        def __init__(self, msg):
            self.msg = msg

        def handle(self, message, session_id=None):
            raise LLMError(self.msg)

    monkeypatch.setattr(main, "concierge", Broken("Groq rejected the API key. Check GROQ_API_KEY in .env."))
    with TestClient(main.app) as c:
        r = c.post("/chat", json={"message": "hi"})
        assert r.status_code == 503 and "GROQ_API_KEY" not in r.text and "isn't available right now" in r.text
    monkeypatch.setattr(main, "concierge", Broken("The assistant is busy right now (Groq usage limit reached). "
                                                  "Please try again in a minute, or call the clinic. [Rate limit]"))
    with TestClient(main.app) as c:
        assert c.post("/chat", json={"message": "hi"}).json()["detail"].endswith("or call the clinic.")


# --------------------------------------------------------------------------- clickable citations

def test_citation_carries_excerpt_and_link_only_for_public_documents():
    from app.sources import excerpt, path_for, source_url
    assert source_url({"source": "03_fee_schedule_2026.pdf", "page_start": 2}) == "/sources/03_fee_schedule_2026.pdf#page=2"
    assert source_url({"source": "02_faq.html", "page_start": None}) == "/sources/02_faq.html"
    assert source_url({"source": "10_price_list_2025_OUTDATED.pdf"}) is None          # superseded
    assert source_url({"source": "12_home_exercise_handout_low_back.pdf"}) is None    # restricted
    assert path_for("../.env") is None and path_for("manifest.json") is None
    long = "word " * 400
    assert len(excerpt(long)) <= 701 and excerpt(long).endswith("…") and excerpt("short  text") == "short text"


def test_source_documents_are_served_safely(monkeypatch):
    with client(monkeypatch) as c:
        r = c.get("/sources/03_fee_schedule_2026.pdf")
        assert r.status_code == 200 and r.headers["content-type"] == "application/pdf" and r.content[:4] == b"%PDF"
        assert r.headers["content-disposition"].startswith("inline")
        html = c.get("/sources/02_faq.html")
        assert html.status_code == 200 and html.headers["content-security-policy"] == "sandbox"
        assert c.get("/sources/12_home_exercise_handout_low_back.pdf").status_code == 404
        assert c.get("/sources/10_price_list_2025_OUTDATED.pdf").status_code == 404
        assert c.get("/sources/..%2F..%2F.env").status_code == 404


def test_widget_makes_citations_clickable(monkeypatch):
    with client(monkeypatch) as c:
        js = c.get("/widget.js").text
        assert "Open the full document" in js and 'class: "cite"' in js and "aria-expanded" in js


# --------------------------------------------------------------------------- failure alerts

def test_alert_explains_the_failure_and_is_throttled():
    from app.alerts import Alerter, explain
    from app.llm import LLMError
    assert explain(LLMError("Groq rejected the API key. Check GROQ_API_KEY in .env."))[0] == "api_key"
    assert explain(LLMError("Groq took too long to answer (ReadTimeout)"))[0] == "timeout"
    assert explain(LLMError("The assistant is busy right now (Groq usage limit reached)."))[0] == "rate_limit"
    assert explain(RuntimeError("boom"))[0] == "bug"

    class PgError(Exception):
        pass
    PgError.__module__ = "psycopg.errors"
    assert explain(PgError("connection refused"))[0] == "database"

    sent, now = [], [0.0]
    a = Alerter(every_minutes=30, clock=lambda: now[0], send=lambda ev, data: sent.append((ev, data)))
    timeout = LLMError("Groq took too long to answer (ReadTimeout)")
    assert a.report(timeout, "do you take aetna?", model="openai/gpt-oss-120b")
    assert not a.report(timeout, "hello") and not a.report(timeout, "hello")      # same kind, within 30 min
    assert a.report(RuntimeError("boom"), "hi")                                     # a different kind still goes
    now[0] += 31 * 60
    assert a.report(timeout, "again")
    event, data = sent[0]
    assert event == "assistant.error" and data["kind"] == "timeout" and data["visitor_message"] == "do you take aetna?"
    assert "groqstatus" in data["fix"] and data["model"] == "openai/gpt-oss-120b" and "logs read" in data["logs_command"]
    assert sent[2][1]["similar_failures_not_emailed"] == 2


def test_failures_email_the_owner_and_stay_plain_for_visitors(monkeypatch):
    from app.alerts import Alerter
    from app.llm import LLMError
    sent = []
    monkeypatch.setattr(main, "alerter", Alerter(send=lambda ev, data: sent.append(data)))

    class Broken(Fake):
        def __init__(self, exc):
            self.exc = exc

        def handle(self, message, session_id=None):
            raise self.exc

    monkeypatch.setattr(main, "concierge", Broken(LLMError("Groq rejected the API key. Check GROQ_API_KEY in .env.")))
    with TestClient(main.app) as c:
        r = c.post("/chat", json={"message": "what are your hours?"})
        assert r.status_code == 503 and "GROQ_API_KEY" not in r.text
    monkeypatch.setattr(main, "concierge", Broken(KeyError("slot_id")))
    with TestClient(main.app, raise_server_exceptions=False) as c:
        r = c.post("/chat", json={"message": "book me"})
        assert r.status_code == 500 and "Something went wrong on our side" in r.json()["detail"]
        assert "slot_id" not in r.text
    assert [d["kind"] for d in sent] == ["api_key", "bug"]
    assert sent[0]["visitor_message"] == "what are your hours?" and "KeyError" in sent[1]["error"]


def test_workflow_has_an_alert_branch():
    import json
    from pathlib import Path
    wf = json.loads((Path(__file__).resolve().parent.parent / "automation" / "n8n_clinic_events.json").read_text())
    names = {n["name"] for n in wf["nodes"]}
    assert {"Alert email", "Email the owner (alert)"} <= names
    assert len(wf["connections"]["Which event?"]["main"]) == 3
