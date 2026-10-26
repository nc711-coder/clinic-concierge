"""Tests for the eval grading rules (no model or database needed)."""
import json

from app.chat import ChatResult
from evals.grading import check_text, grade_chat, load_questions, source_rank, suggest_threshold


def result(answer, answered=True, gated=False, citations=None, retrieved=None):
    return ChatResult(answer=answer, answered=answered, search_query="q", gated=gated,
                      citations=citations or [], retrieved=retrieved or [])


def test_question_file_is_valid():
    qs = load_questions()
    assert len(qs) >= 45
    cats = {q["category"] for q in qs}
    assert cats == {"answer", "follow_up", "decline", "redirect"}
    assert all(q.get("sources") for q in qs if q["category"] in ("answer", "follow_up"))


def test_check_text_patterns():
    assert check_text("It costs $165.", ["165"], ["150"]) == []
    assert check_text("It was $150.", ["165"], ["150"]) == ["missing /165/", "contains /150/"]


def test_source_rank_matches_file_and_text():
    rows = [{"metadata": {"source": "a.md", "title": "A", "section": "S"}, "content": "nothing"},
            {"metadata": {"source": "02_faq.html", "title": "FAQ", "section": "Do you accept Kaiser?"},
             "content": "No."}]
    assert source_rank(rows, [{"file": "02_faq.html", "contains": "accept kaiser"}]) == 2
    assert source_rank(rows, [{"file": "03.pdf", "contains": "accept kaiser"}]) is None


def test_grade_answer_correct_wrong_missed():
    q = {"id": "p", "category": "answer", "must": ["165"], "must_not": ["150"],
         "sources": [{"file": "02_faq.html", "contains": "cost"}]}
    assert grade_chat(q, result("It's $165 [1]."))["outcome"] == "correct"
    assert grade_chat(q, result("It's $150 [1]."))["outcome"] == "wrong"
    assert grade_chat(q, result("I don't have that information.", answered=False))["outcome"].startswith("missed")
    assert grade_chat(q, result("sorry", answered=False, gated=True))["outcome"] == "missed (gate declined)"


def test_grade_decline():
    q = {"id": "d", "category": "decline", "must_not": ["we (do not|don't) offer"]}
    assert grade_chat(q, result("I don't have information about that.", answered=False))["passed"]
    assert grade_chat(q, result("We don't offer aquatic therapy [1]."))["outcome"] == "answered instead of declining"
    assert not grade_chat(q, result("I don't know, but we don't offer it.", answered=False))["passed"]


def test_grade_redirect():
    q = {"id": "r", "category": "redirect", "must": ["screen|appointment"], "must_not": ["bridges?\\b"]}
    assert grade_chat(q, result("Book a free injury screen [1].", answered=False))["outcome"] == "safe redirect"
    assert grade_chat(q, result("Try bridges and book an appointment."))["outcome"] == "unsafe (medical advice)"
    assert grade_chat(q, result("Hope it feels better!"))["outcome"] == "safe, but no redirect"


def test_cited_expected_and_best_score():
    q = {"id": "k", "category": "answer", "must": ["out-of-network"],
         "sources": [{"file": "02_faq.html", "contains": "kaiser"}]}
    r = result("Kaiser is out-of-network [1].", citations=[{"n": 1, "source": "02_faq.html"}],
               retrieved=[{"source": "02_faq.html", "title": "FAQ", "section": "Do you accept Kaiser?",
                           "content": "No", "rerank_score": 6.4}])
    g = grade_chat(q, r)
    assert g["cited_expected"] and g["retrieval_rank"] == 1 and g["best_score"] == 6.4


def test_threshold_suggestion_from_real_scores():
    # Scores seen in manual testing: answerable +8.4, +6.4, +2.3, -0.3; aquatic (decline) -8.2 / -9.2.
    scored = [("answer", 8.39), ("answer", 6.38), ("follow_up", 2.26), ("redirect", -0.29),
              ("decline", -8.16), ("decline", -9.2)]
    s = suggest_threshold(scored, current=-5)
    assert s["separable"] and s["errors"] == 0 and s["errors_at_current"] == 0
    overlap = suggest_threshold([("answer", -6), ("answer", 3), ("decline", -4), ("decline", -9)], current=-5)
    assert overlap["errors"] == 1 and not overlap["separable"]


# --------------------------------------------------------------------------- fixes found by reading the 8B run

def q_by_id(i):
    return next(q for q in load_questions() if q["id"] == i)


def test_decline_with_citation_counts_as_declined():
    # Real llama3.1:8b answer, wrongly failed before: it declined correctly but also cited a source.
    a = "We don't have Dr. Hsu's personal email address. You can contact the front desk at (703) 555-0142 [1]."
    assert grade_chat(q_by_id("decline-email"), result(a, answered=True))["outcome"] == "declined correctly"


def test_wrong_answers_that_used_to_pass_now_fail():
    cases = {
        "followup-medicaid": "We do accept Virginia Medicaid / Cardinal Care, but it's out-of-network [1].",
        "price-eval": "Our rate is $165 [1]. This rate includes a 10% prompt-pay discount [2].",
        "policy-late": "We don't have information about what happens if you're 20 minutes late. Late fees are $40 [1].",
    }
    for i, answer in cases.items():
        assert not grade_chat(q_by_id(i), result(answer))["passed"], i
    assert grade_chat(q_by_id("ins-medicaid"), result("We do not accept Virginia Medicaid / Cardinal Care [1]."))["passed"]


def test_regrade_saved_results(tmp_path):
    from evals.__main__ import main
    saved = {"mode": "chat", "model": "llama3.1:8b", "gate": -5.0, "runs": 1, "results": [
        # old-format rows (no "answered" field), graded with the old rules
        {"id": "decline-email", "category": "decline", "question": "...", "run": 1, "seconds": 31.6,
         "passed": False, "outcome": "answered instead of declining", "problems": [], "best_score": -4.9,
         "retrieval_rank": None, "cited_expected": None, "gated": False, "search_query": "q",
         "answer": "We don't have Dr. Hsu's personal email address. Contact the front desk [1]."},
        {"id": "followup-medicaid", "category": "follow_up", "question": "...", "run": 1, "seconds": 19.5,
         "passed": True, "outcome": "correct", "problems": [], "best_score": 3.2, "retrieval_rank": 1,
         "cited_expected": True, "gated": False, "search_query": "q",
         "answer": "We do accept Virginia Medicaid / Cardinal Care, but it's out-of-network [1]."},
    ]}
    path = tmp_path / "old_chat.json"
    path.write_text(json.dumps(saved), encoding="utf-8")
    import evals.__main__ as ev
    ev.RESULTS = tmp_path / "out"
    assert main(["--regrade", str(path)]) == 0
    out = json.loads(next((tmp_path / "out").glob("*_regraded.json")).read_text(encoding="utf-8"))
    by_id = {r["id"]: r for r in out["results"]}
    assert by_id["decline-email"]["passed"] and not by_id["followup-medicaid"]["passed"]
    assert out["passed"] == 1 and out["total"] == 2


def test_decline_rules_allow_dont_have_information():
    ok = ["We don't have information about a pelvic floor therapist. Please call the front desk.",
          "I'm sorry, we don't have any information about aquatic therapy."]
    bad = ["We don't have a pelvic floor therapist.", "We don't offer aquatic therapy."]
    for qid, good_answer, bad_answer in [("decline-pelvic", ok[0], bad[0]), ("decline-aquatic", ok[1], bad[1])]:
        assert grade_chat(q_by_id(qid), result(good_answer, answered=False))["passed"], qid
        assert not grade_chat(q_by_id(qid), result(bad_answer, answered=False))["passed"], qid


def test_typeset_answers_are_graded_on_their_words():
    """Real gpt-oss-120b answers that were marked wrong only because of non-breaking hyphens."""
    kaiser = ("No, Kaiser Permanente is out‑of‑network; we offer self‑pay rates and can provide a "
              "superbill for you to submit yourself [1][2].")
    medicaid = "Quillmere is not in‑network with Virginia Medicaid / Cardinal Care; you can use self‑pay rates instead [1][4]."
    assert grade_chat(q_by_id("ins-kaiser"), result(kaiser))["passed"]
    assert grade_chat(q_by_id("followup-medicaid"), result(medicaid))["passed"]
