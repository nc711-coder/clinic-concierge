"""Score the assistant against evals/questions.jsonl.

    python -m evals                    # retrieval only (no LLM, ~1 min): vector vs hybrid vs hybrid + reranker
    python -m evals --chat             # full chat through Ollama: grades every answer (slow on CPU)
    python -m evals --chat --runs 3    # ask each question 3 times to see how stable answers are
    python -m evals --chat --only ins-kaiser decline-aquatic     # just some questions
    python -m evals --chat --category decline redirect
    python -m evals --regrade evals/results/2026-10-20_1126_chat.json   # re-score a saved run, no model needed

Each run prints a summary and saves a Markdown report + raw JSON under evals/results/.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from ingest.env import load_dotenv

from .grading import grade_chat, load_questions, regrade, source_rank, suggest_threshold

RESULTS = Path(__file__).resolve().parent / "results"


def pct(n, d):
    return f"{100 * n / d:.0f}%" if d else "–"


def short(text, n=110):
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


# ----------------------------------------------------------------------------- retrieval mode

def run_retrieval(questions, k: int):
    from ingest import store
    from ingest.embed import get_embedder
    from ingest.rerank import get_reranker
    from ingest.retrieve import retrieve

    embedder = get_embedder(os.environ.get("EMBEDDER", "local"))
    reranker = get_reranker()
    conn = store.connect(os.environ["DATABASE_URL"], embedder.dims)
    store.check_dims(conn, embedder.dims)

    configs = [("vector only", False, None), ("hybrid", True, None)]
    if reranker is not None:
        configs.append(("hybrid + rerank", True, reranker))
    qs = [q for q in questions if q["category"] == "answer" and q.get("sources")]
    print(f"\nRetrieval eval: {len(qs)} answerable questions, top {k}. "
          f"(Follow-ups need query rewriting, so they're scored in --chat mode.)\n")

    rows_out, summary = [], []
    for name, hybrid, rr in configs:
        ranks = []
        t0 = time.time()
        for q in qs:
            rows = retrieve(conn, embedder, rr, q["question"], k=10, hybrid=hybrid, include_restricted=False)
            r = source_rank(rows, q["sources"])
            ranks.append(r)
            rows_out.append({"config": name, "id": q["id"], "rank": r})
        n = len(ranks)
        hit1 = sum(r == 1 for r in ranks)
        hitk = sum(r is not None and r <= k for r in ranks)
        mrr = sum(1 / r for r in ranks if r) / n
        summary.append({"config": name, "hit@1": hit1, f"hit@{k}": hitk, "n": n, "mrr": round(mrr, 3),
                        "seconds": round(time.time() - t0, 1)})
        print(f"  {name:<16} hit@1 {pct(hit1, n):>4}   hit@{k} {pct(hitk, n):>4}   MRR {mrr:.2f}   "
              f"({time.time() - t0:.0f}s)")

    best = summary[-1]["config"]
    misses = [r for r in rows_out if r["config"] == best and (r["rank"] is None or r["rank"] > k)]
    if misses:
        print(f"\n  Missed by {best} (right source not in top {k}):")
        by_id = {q["id"]: q for q in qs}
        for r in misses:
            print(f"    {r['id']:<22} {short(by_id[r['id']]['question'], 70)}")

    md = [f"# Retrieval eval — {datetime.now():%Y-%m-%d %H:%M}", "",
          f"{len(qs)} answerable questions. A hit means a chunk holding the answer is in the top results.", "",
          f"| Config | Hit@1 | Hit@{k} | MRR |", "| --- | --- | --- | --- |"]
    md += [f"| {s['config']} | {pct(s['hit@1'], s['n'])} | {pct(s[f'hit@{k}'], s['n'])} | {s['mrr']:.2f} |"
           for s in summary]
    md += ["", f"## Per question (rank of the first correct source; – = not in top 10)", "",
           "| Question | " + " | ".join(c[0] for c in configs) + " |", "| --- |" + " --- |" * len(configs)]
    for q in qs:
        cells = [next(r["rank"] for r in rows_out if r["config"] == c[0] and r["id"] == q["id"]) for c in configs]
        md.append(f"| {q['id']} | " + " | ".join(str(c) if c else "–" for c in cells) + " |")
    return {"mode": "retrieval", "k": k, "summary": summary, "rows": rows_out}, md


# ----------------------------------------------------------------------------- chat mode

def run_chat(questions, runs: int, pause: float | None = None):
    from app.llm import LLMError
    from app.setup import build_engine

    # Measure one model: a fallback model quietly answering some questions would mix two models' results.
    os.environ["GEMINI_FALLBACK_MODEL"] = os.environ["GROQ_FALLBACK_MODEL"] = "none"
    engine = build_engine()
    hosted = os.environ.get("LLM_PROVIDER", "ollama").lower() not in ("ollama", "")
    pause = (6.0 if hosted else 0.0) if pause is None else pause      # free tiers allow ~10 calls a minute
    print(f"\nChat eval: {len(questions)} questions x {runs} run(s), model {engine.llm.model}, "
          f"gate {engine.min_score:+.1f}" + (f", {pause:.0f}s pause between questions" if pause else "") +
          ". First answer is slow while the model loads.\n")
    results = []
    stopped = None
    for q in questions:
        for run in range(1, runs + 1):
            if stopped:
                break
            if pause and results:
                time.sleep(pause)
            t0 = time.time()
            r = None
            for attempt in range(3):
                try:
                    r = engine.answer(q["question"], q.get("history", []))
                    break
                except LLMError as e:
                    per_day = "PerDay" in str(e) or "per day" in str(e).lower()
                    if "busy right now" in str(e) and attempt < 2 and not per_day:   # a daily cap won't clear in 60 s
                        print(f"  ...usage limit reached, waiting 60 s ({q['id']})")
                        time.sleep(60)
                        continue
                    stopped = f"{q['id']}: {e}"
                    break
            if r is None:
                break
            g = grade_chat(q, r)
            g.update(id=q["id"], category=q["category"], question=q["question"], run=run,
                     seconds=round(time.time() - t0, 1))
            results.append(g)
            print_row(g)

    if stopped:
        print(f"\n  Stopped early after {len(results)} answers, the model kept failing:\n  {stopped}\n"
              f"  The partial results below are saved; re-run the rest later with --only <ids>.")
    return summarize(results, engine.llm.model, engine.min_score, engine.top_k, runs)


def print_row(g):
    mark = "PASS" if g["passed"] else "FAIL"
    score = f"{g['best_score']:+.1f}" if g.get("best_score") is not None else "  – "
    print(f"  {mark}  {g['id']:<22} {score}  {g['outcome']:<30} {g.get('seconds', 0):>5.1f}s"
          + (f"   {'; '.join(g['problems'])}" if g["problems"] and not g["passed"] else ""))


def summarize(results, model, gate_cut, top_k, runs, title="Chat eval"):
    # ---- summary
    by_cat = defaultdict(list)
    for g in results:
        by_cat[g["category"]].append(g)
    total_pass = sum(g["passed"] for g in results)
    print(f"\n  Overall: {total_pass}/{len(results)} passed ({pct(total_pass, len(results))})")
    cat_rows = []
    for cat in ("answer", "follow_up", "decline", "redirect"):
        gs = by_cat.get(cat, [])
        if gs:
            p = sum(g["passed"] for g in gs)
            outcomes = Counter(g["outcome"] for g in gs)
            cat_rows.append((cat, p, len(gs), outcomes))
            print(f"    {cat:<10} {p}/{len(gs)} ({pct(p, len(gs))})   " +
                  ", ".join(f"{o}: {n}" for o, n in outcomes.most_common()))

    answerable = [g for g in results if g["category"] in ("answer", "follow_up")]
    hit = sum(g["retrieval_rank"] is not None for g in answerable)
    cited = [g for g in answerable if g["cited_expected"] is not None]
    cited_ok = sum(g["cited_expected"] for g in cited)
    print(f"    retrieval: right source among the {top_k} given to the model: {pct(hit, len(answerable))}")
    print(f"    citations: cited a correct source in {pct(cited_ok, len(cited))} of answers with citations")

    # A decline question that got past the gate and was declined by the model anyway did no harm, so it
    # doesn't count against a cutoff. Gated declines still count: we don't know what the model would have said.
    handled = lambda g: g["category"] == "decline" and g["passed"] and not g.get("gated")
    gate = suggest_threshold([(g["category"], g["best_score"]) for g in results if not handled(g)], gate_cut)
    if gate.get("errors") is not None:
        print(f"    gate: current {gate_cut:+.1f} makes {gate['errors_at_current']} mistake(s); "
              f"best cutoff {gate['threshold']:+.2f} makes {gate['errors']}. Lowest answerable score "
              f"{gate['lowest_answerable']:+.2f}, highest decline score {gate['highest_decline']:+.2f}.")
        if gate["threshold"] != gate_cut:
            print(f"          To use it, set CHAT_MIN_RERANK_SCORE={gate['threshold']} in .env")

    if runs > 1:
        flaky = [i for i in {g["id"] for g in results}
                 if len({g["passed"] for g in results if g["id"] == i}) > 1]
        print(f"    stability: {len(flaky)} question(s) passed on some runs and failed on others"
              + (f": {', '.join(sorted(flaky))}" if flaky else ""))

    secs = [g["seconds"] for g in results] or [0.0]
    print(f"    speed: {sum(secs) / len(secs):.1f}s per answer on average")

    # ---- report
    md = [f"# {title} — {datetime.now():%Y-%m-%d %H:%M}", "",
          f"Model `{model}`, relevance gate {gate_cut:+.1f}, {runs} run(s) per question.", "",
          f"**Overall: {total_pass}/{len(results)} passed ({pct(total_pass, len(results))})**", "",
          "| Category | Passed | Outcomes |", "| --- | --- | --- |"]
    md += [f"| {c} | {p}/{n} ({pct(p, n)}) | " + ", ".join(f"{o}: {k}" for o, k in oc.most_common()) + " |"
           for c, p, n, oc in cat_rows]
    md += ["", f"- Right source given to the model: {pct(hit, len(answerable))} of answerable questions",
           f"- Cited a correct source: {pct(cited_ok, len(cited))} of answers with citations",
           f"- Average time per answer: {sum(secs) / len(secs):.1f}s"]
    if gate.get("errors") is not None:
        md.append(f"- Relevance gate: best cutoff {gate['threshold']:+.2f} ({gate['errors']} mistakes; "
                  f"{gate['errors_at_current']} at the current {gate_cut:+.1f})")
    md += ["", "## Every answer", "", "| | Question | Outcome | Score | Answer |", "| --- | --- | --- | --- | --- |"]
    for g in results:
        score = f"{g['best_score']:+.1f}" if g["best_score"] is not None else "–"
        md.append(f"| {'✅' if g['passed'] else '❌'} | {g['id']} | {g['outcome']} | {score} | "
                  f"{short(g['answer'], 160).replace('|', '/')} |")
    return {"mode": "chat", "model": model, "gate": gate_cut, "runs": runs, "top_k": top_k,
            "passed": total_pass, "total": len(results), "threshold_analysis": gate, "results": results}, md



def run_regrade(path: Path, questions: list[dict]):
    saved = json.loads(path.read_text(encoding="utf-8"))
    if saved.get("mode") != "chat":
        raise SystemExit(f"{path} isn't a chat eval result.")
    by_id = {q["id"]: q for q in questions}
    results, changed = [], []
    for old in saved["results"]:
        q = by_id.get(old["id"])
        if q is None:
            continue                        # question removed from the set since that run
        new = regrade(q, old)
        results.append(new)
        if new["passed"] != old["passed"]:
            changed.append((old, new))
    print(f"\nRe-graded {len(results)} saved answers from {path.name} with the current rules "
          f"(model {saved.get('model')}, gate {saved.get('gate', 0):+.1f}). No model calls.\n")
    for g in results:
        print_row(g)
    if changed:
        print(f"\n  {len(changed)} result(s) changed:")
        for old, new in changed:
            print(f"    {old['id']:<22} {old['outcome']}  ->  {new['outcome']}")
    return summarize(results, saved.get("model"), saved.get("gate", 0), saved.get("top_k", 5),
                     saved.get("runs", 1), title=f"Chat eval (re-graded {path.name})")


# ----------------------------------------------------------------------------- entry point

def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    load_dotenv()
    p = argparse.ArgumentParser(prog="python -m evals", description="Score Clinic Concierge on the eval set.")
    p.add_argument("--chat", action="store_true", help="full chat with LLM_PROVIDER's model (default: retrieval only)")
    p.add_argument("--runs", type=int, default=1, help="times to ask each question (chat mode)")
    p.add_argument("--only", nargs="+", help="question ids to run")
    p.add_argument("--category", nargs="+", choices=["answer", "follow_up", "decline", "redirect"])
    p.add_argument("-k", type=int, default=5, help="top-k for retrieval hits")
    p.add_argument("--regrade", metavar="RESULT_JSON", help="re-score a saved chat run with the current rules")
    p.add_argument("--pause", type=float, help="seconds between questions in chat mode (default: 6 for hosted "
                                               "models, to stay under free-tier per-minute limits; 0 for Ollama)")
    args = p.parse_args(argv)

    questions = load_questions()
    if args.regrade:
        data, md = run_regrade(Path(args.regrade), questions)
        return save(data, md, suffix="regraded")
    if not os.environ.get("DATABASE_URL"):
        print("DATABASE_URL is not set (put it in .env).")
        return 2
    if args.only:
        unknown = set(args.only) - {q["id"] for q in questions}
        if unknown:
            print(f"Unknown question ids: {', '.join(sorted(unknown))}")
            return 2
        questions = [q for q in questions if q["id"] in args.only]
    if args.category:
        questions = [q for q in questions if q["category"] in args.category]

    data, md = run_chat(questions, args.runs, args.pause) if args.chat else run_retrieval(questions, args.k)
    return save(data, md)


def save(data, md, suffix=None) -> int:
    RESULTS.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    base = RESULTS / f"{stamp}_{data['mode']}{'_' + suffix if suffix else ''}"
    base.with_suffix(".md").write_text("\n".join(md) + "\n", encoding="utf-8")
    base.with_suffix(".json").write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    print(f"\n  Report: {base.with_suffix('.md')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
