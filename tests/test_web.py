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
