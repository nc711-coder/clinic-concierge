"""Chat pipeline tests with a scripted stand-in for the LLM (no Ollama needed)."""
import os
import re
from pathlib import Path

import pytest

from app import chat as chat_mod
from app.chat import NO_ANSWER, ChatEngine
from ingest.embed import HashEmbedder


class FakeLLM:
    """Rewrites with a lookup table; answers by citing the first source that shares a keyword."""

    model = "fake"

    def __init__(self, rewrites=None, answer=None):
        self.rewrites = rewrites or {}
        self.fixed_answer = answer
        self.calls = []

    def chat(self, messages, temperature=0.0, max_tokens=400):
        self.calls.append(messages)
        if messages[0]["content"] == chat_mod.REWRITE_SYSTEM:
            latest = messages[-1]["content"].split("Latest message: ")[-1]
            return self.rewrites.get(latest, latest)
        if self.fixed_answer is not None:
            return self.fixed_answer
        prompt = messages[-1]["content"]
        question = prompt.split("Question: ")[-1].lower()
        for n, block in re.findall(r"\[(\d+)\] (.+?)(?=\n\n\[\d+\] |\n\nQuestion: )", prompt, re.S):
            if any(w in block.lower() for w in re.findall(r"[a-z]{6,}", question)):
                return f"Here's what I found [{n}]."
        return "I don't have that information. Please call the front desk."


def fake_rows(*texts):
    return [{"document_id": f"d{i}", "chunk_index": 0, "content": t,
             "metadata": {"source": f"doc{i}.md", "title": f"Doc {i}", "section": f"Sec {i}",
                          "effective_date": "2026-01-01"},
             "rerank_score": 1.0 - i, "search_rank": i} for i, t in enumerate(texts, start=1)]


def engine_with(rows, llm):
    e = ChatEngine(conn=None, embedder=None, reranker=None, llm=llm)
    e.search = lambda query, original, extra=None: rows
    return e


def test_answer_cites_and_reports_sources():
    llm = FakeLLM(answer="We don't take Kaiser [2]. Self-pay is available [2][9].")
    r = engine_with(fake_rows("Aetna is in-network", "Kaiser is out-of-network"), llm).answer("do you take kaiser?")
    assert r.answered and [c["n"] for c in r.citations] == [2]
    assert "[9]" not in r.answer                                  # invented citation removed
    assert r.citations[0]["source"] == "doc2.md"


def test_no_citation_means_not_answered():
    llm = FakeLLM(answer="I don't have that information. Please call the front desk.")
    r = engine_with(fake_rows("Our hours are 7 to 7"), llm).answer("do you do aquatic therapy?")
    assert not r.answered and r.citations == []


def test_nothing_retrieved_skips_the_llm_answer():
    llm = FakeLLM()
    r = engine_with([], llm).answer("anything?")
    assert r.answer == NO_ANSWER and not r.answered
    assert len(llm.calls) == 1                                   # only the rewrite call


def test_rewrite_uses_history_and_falls_back_on_bad_output():
    llm = FakeLLM(rewrites={"what about saturdays?": "Saturday hours"})
    e = engine_with([], llm)
    history = [{"role": "user", "content": "what are your hours?"}, {"role": "assistant", "content": "7 to 7."}]
    assert e.rewrite("what about saturdays?", history) == "Saturday hours"
    assert "what are your hours?" in llm.calls[-1][-1]["content"]
    bad = FakeLLM(rewrites={"hi": "I'm sorry, I can't help with that."})
    assert engine_with([], bad).rewrite("hi", []) == "hi"


def test_prompt_numbers_sources_and_includes_history():
    llm = FakeLLM()
    history = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "Hi! How can I help?"}]
    engine_with(fake_rows("Kaiser is out-of-network"), llm).answer("kaiser?", history)
    answer_call = llm.calls[-1]
    assert answer_call[1] == {"role": "user", "content": "hello"}
    assert answer_call[-1]["content"].startswith("Sources:\n\n[1] Doc 1 — Sec 1 (effective 2026-01-01)")


def test_api_ask_endpoint(monkeypatch):
    """/ask is the stateless questions-only endpoint (the RAG pipeline on its own)."""
    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    from app import main
    engine = engine_with(fake_rows("Kaiser is out-of-network"), FakeLLM(answer="No, Kaiser is out-of-network [1]."))
    monkeypatch.setattr(main, "concierge", SimpleNamespace(engine=engine))
    with TestClient(main.app) as client:
        r = client.post("/ask", json={"message": "do you take kaiser?", "history": []})
        assert r.status_code == 200
        body = r.json()
        assert body["answered"] and body["citations"][0]["source"] == "doc1.md"
        assert "retrieved" not in body
        assert client.post("/ask", json={"message": "x", "debug": True}).json()["retrieved"]
        assert client.post("/ask", json={"message": ""}).status_code == 422


# --------------------------------------------------------------------------- against the real index

DB = os.environ.get("TEST_DATABASE_URL")


class OverlapReranker:
    def score(self, question, texts):
        words = {w[:6] for w in re.findall(r"[a-z]{5,}", question.lower())}
        return [float(sum(w in t.lower() for w in words)) for t in texts]


@pytest.fixture(scope="module")
def db_engine_factory():
    if not DB:
        pytest.skip("set TEST_DATABASE_URL to run database tests")
    from ingest import store
    from ingest.pipeline import run
    root = Path(__file__).resolve().parent.parent
    run(root / "data" / "raw", root / "data" / "manifest.json", embedder=HashEmbedder(), database_url=DB)
    conn = store.connect(DB, HashEmbedder().dims)
    return lambda llm: ChatEngine(conn, HashEmbedder(), OverlapReranker(), llm)


def test_rewrite_finds_waiver_for_ambiguous_question(db_engine_factory):
    q = "what happens if I miss my first appointment this year?"
    llm = FakeLLM(rewrites={q: "missed appointment no-show fee first no-show of the year courtesy waiver"})
    r = db_engine_factory(llm).answer(q)
    assert r.search_query.startswith("missed appointment")
    assert any("courtesy waiver" in s["content"] for s in r.retrieved[:3])


def test_restricted_handout_never_reaches_the_model(db_engine_factory):
    q = "what exercises should I do for low back pain?"
    r = db_engine_factory(FakeLLM()).answer(q)
    assert all(s["source"] != "12_home_exercise_handout_low_back.pdf" for s in r.retrieved)


# --------------------------------------------------------------------------- Ollama client, with a fake HTTP server

def test_ollama_client_request_and_errors():
    import httpx

    from app.llm import LLMError, OllamaLLM

    seen = {}

    def handler(request):
        seen["body"] = request.read().decode()
        if "missing-model" in seen["body"]:
            return httpx.Response(404, json={"error": "model not found"})
        return httpx.Response(200, json={"message": {"role": "assistant", "content": "  hi there  "}})

    llm = OllamaLLM(model="llama3.2:3b", url="http://ollama.test")
    llm.client = httpx.Client(transport=httpx.MockTransport(handler))
    assert llm.chat([{"role": "user", "content": "hello"}]) == "hi there"
    assert '"num_ctx": 8192' in seen["body"] or '"num_ctx":8192' in seen["body"]

    llm.model = "missing-model"
    with pytest.raises(LLMError, match="ollama pull missing-model"):
        llm.chat([{"role": "user", "content": "hello"}])


def test_ollama_not_running_message():
    from app.llm import LLMError, OllamaLLM
    llm = OllamaLLM(url="http://127.0.0.1:9")        # nothing listens on port 9
    with pytest.raises(LLMError, match="Is the Ollama app running"):
        llm.chat([{"role": "user", "content": "hello"}])


# --------------------------------------------------------------------------- small models that skip [n] markers

def test_uncited_answer_gets_inferred_citations():
    """Real llama3.2:3b output for the missed-appointment question: correct, but no [n] markers."""
    answer = ("If you miss your first appointment this year, you will receive a courtesy waiver, which will "
              "automatically waive the late cancellation fee for that appointment. This waiver resets on "
              "January 1st and does not carry over to the next year.")
    rows = fake_rows("A fee of $40 will be charged for each late cancellation and each no-show.",
                     "Each patient receives one courtesy waiver per calendar year. The first late cancellation "
                     "or no-show in a calendar year will be waived automatically. The courtesy waiver resets on "
                     "January 1 and does not carry over to the next year.")
    r = engine_with(rows, FakeLLM(answer=answer)).answer("what happens if I miss my first appointment this year?")
    assert r.answered
    assert [c["n"] for c in r.citations] == [2] and r.citations[0]["inferred"]
    assert r.answer.count("[2]") == 2


def test_decline_is_not_answered_even_if_it_cites():
    r = engine_with(fake_rows("Our hours are 7 to 7"),
                    FakeLLM(answer="I don't have information about aquatic therapy [1].")).answer("aquatic therapy?")
    assert not r.answered


def test_unrelated_sentence_is_not_cited():
    from app.chat import attribute_citations
    text, used = attribute_citations("Thanks for asking! Have a great day.", ["Kaiser is out-of-network."])
    assert used == [] and "[" not in text


def test_citation_reminder_sits_after_the_question():
    llm = FakeLLM(answer="ok [1].")
    engine_with(fake_rows("x"), llm).answer("hours?")
    last = llm.calls[-1][-1]["content"]
    assert last.index("Question: hours?") < last.index("End every sentence with the number")


# --------------------------------------------------------------------------- relevance gate

def test_low_relevance_is_declined_without_asking_the_llm():
    """Real case: 'do you offer aquatic therapy?' best score -8.16, and the model invented 'we do not offer it'."""
    rows = fake_rows("Orthopedic physical therapy for back and neck pain.")
    rows[0]["rerank_score"] = -8.16
    llm = FakeLLM(answer="We do not offer aquatic therapy [1].")
    r = engine_with(rows, llm).answer("do you offer aquatic therapy?")
    assert r.gated and not r.answered and r.answer == NO_ANSWER
    assert len(llm.calls) == 1                                    # rewrite only; no answer call
    assert r.retrieved                                            # still visible in --debug


def test_relevant_enough_passes_the_gate():
    rows = fake_rows("Orthopedic physical therapy for knee pain.")
    rows[0]["rerank_score"] = -2.7                                # the knee-pain question scored this
    r = engine_with(rows, FakeLLM(answer="Book a free injury screen [1].")).answer("my knee hurts")
    assert not r.gated and r.answered


def test_no_reranker_means_no_gate():
    rows = fake_rows("x")
    rows[0].pop("rerank_score")
    assert not engine_with(rows, FakeLLM(answer="ok [1].")).answer("q").gated


def test_citations_listed_in_order_and_prompt_forbids_invented_no():
    llm = FakeLLM(answer="Not in-network [3]. Self-pay works [1].")
    r = engine_with(fake_rows("a", "b", "c"), llm).answer("medicaid?")
    assert [c["n"] for c in r.citations] == [1, 3]
    assert "Never claim the clinic doesn't offer something" in llm.calls[-1][0]["content"]


def test_runaway_rewrite_keeps_its_useful_start():
    """Real gpt-oss-120b rewrite: a good start, then the same words looped for 1,217 characters."""
    from app import timing
    looped = "first appointment missed no-show policy cancellation fee" + "missed first appointment no-show " * 40

    class Looping:
        model = "fake"

        def chat(self, messages, **kw):
            return looped

    timing.start()
    question = "What happens if I miss my first appointment?"
    query, extra = engine_with([], Looping())._rewrite(question, [])
    # Ranked by the patient's own words (measured better on the eval); the loop's start only widens the search.
    assert query == question
    assert extra.startswith("first appointment missed no-show policy cancellation") and len(extra.split()) <= 16
    assert "looped" in timing.collect()[0]["notes"][0]


def test_looped_rewrite_words_are_searched_but_not_used_for_ranking():
    seen = {}

    class Looping:
        model = "fake"

        def chat(self, messages, **kw):
            return ("first appointment missed no-show policy" + " missed appointment" * 80) if len(seen) == 0 \
                else "We waive it once a year [1]."

    e = ChatEngine(conn=None, embedder=None, reranker=None, llm=Looping())
    e.search = lambda query, original, extra=None: seen.update(query=query, extra=extra) or []
    e.answer("What happens if I miss my first appointment?")
    assert seen["query"] == "What happens if I miss my first appointment?" and "no-show" in seen["extra"]
