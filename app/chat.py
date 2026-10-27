"""The chat pipeline: rewrite -> retrieve -> answer with citations.

1. Rewrite: the LLM turns the patient's latest message (plus recent conversation) into a clear search
   query. "what about Saturdays?" after a question about hours becomes "Saturday hours"; "if I miss my
   first appointment this year" becomes "missed appointment no-show fee first no-show waiver".
2. Retrieve: hybrid search for the rewritten query AND the original words, merged and reranked
   (ingest/retrieve.py). Superseded, expired and restricted documents are excluded.
3. Answer: the LLM answers only from the numbered sources and cites them as [1], [2]. If the sources
   don't cover the question, it says so and points to the front desk instead of guessing.
"""
from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass, field
from datetime import date

from ingest.retrieve import retrieve

from . import timing

from .guardrails import SAFE_REPLY, check_answer, check_negative_claims, check_question, split_sentences
from .sources import excerpt, source_url

CLINIC_NAME = os.environ.get("CLINIC_NAME", "Quillmere Physical Therapy & Sports Rehab")
CLINIC_PHONE = os.environ.get("CLINIC_PHONE", "(703) 555-0142")
TOP_K = int(os.environ.get("CHAT_TOP_K", "5"))
# If the best source's reranker score is below this, nothing relevant was found: answer "I don't have that
# information" without asking the LLM (which might otherwise invent a confident "no"). Observed scores:
# answerable questions +2 to +8 (an indirect one -2.7); "do you offer aquatic therapy?" (not in docs) -8.2.
# Tune with the eval set. Ignored when the reranker is off.
MIN_RERANK_SCORE = float(os.environ.get("CHAT_MIN_RERANK_SCORE", "-5"))
HISTORY_TURNS = 6          # recent messages the model sees (3 exchanges)
MAX_SOURCE_CHARS = 1200    # per source in the prompt

REWRITE_SYSTEM = f"""You turn a patient's latest message to {CLINIC_NAME} into a search query for the clinic's documents (FAQ, policies, prices, insurance list, staff bios).

Rules:
- Use the conversation to resolve words like "that", "it", "then", "what about...".
- Say what the patient is really asking about, using words a clinic policy or FAQ would use (for example: self-pay price, insurance accepted, cancellation fee, no-show, hours, referral, therapist specialty).
- If the message could be read two ways, include words for both readings.
- Output only the search query on one line. No quotes, no explanation."""

# A few worked examples (deliberately not taken from the eval questions).
REWRITE_EXAMPLES = [
    ("is it cheaper if i just pay cash", "self-pay price discount paying cash at time of service"),
    ("can my wife come in with me to my session?", "family member companion allowed in treatment session visitor policy"),
    ("Conversation so far:\nuser: what are your hours?\nassistant: We're open Monday to Friday, 7 AM to 6 PM.\n\n"
     "Latest message: ok and what about saturdays?", "Saturday hours open appointments"),
]

ANSWER_SYSTEM = """You are the website assistant for {clinic}. Today is {today}.

Answer the patient's question using ONLY the numbered sources in their message.
- Put the source number in square brackets after each fact, like [1] or [2][3].
- Keep it short: 1–4 sentences, friendly and plain. No headings.
- If the sources don't answer the question, say you don't have that information and suggest calling the front desk at {phone}. Don't guess, and don't use outside knowledge.
- A general rule applies to specific cases: a policy for missed appointments also covers a missed first appointment, and clinic hours cover every kind of visit. Use it, and cite it.
- Only state what the sources say. If a service, plan or policy isn't mentioned, say you don't have information about it. Never claim the clinic doesn't offer something unless a source says so.
- The front desk phone number comes from these instructions, not a source: don't put a citation after it.
- Never give medical advice, diagnose, or recommend exercises or treatments. For anything about symptoms or what to do for pain, suggest the free 15-minute injury screen or an evaluation.
- If sources disagree, prefer the one with the later effective date.
- Don't mention "sources" or "documents"; just answer and cite."""

NO_ANSWER = (f"I'm sorry, I don't have information about that. Please call our front desk at {CLINIC_PHONE} "
             f"and they'll be happy to help.")

CITE_REMINDER = ("Answer in 1–4 short sentences. End every sentence with the number of the source it came from, "
                 "like this: \"New patients are usually seen within 5 business days [3].\" "
                 "If the sources don't answer the question, say you don't have that information. "
                 "If something isn't mentioned, don't say the clinic doesn't offer it.")

_CITE = re.compile(r"\[(\d{1,2})\]")
_DECLINE = re.compile(r"(don.t|do not) have (that|this|any|enough)? ?(specific )?information|"
                      r"(don.t|do not) know|not (able|sure)|can.t (help|answer)|isn.t (in|covered)", re.I)
_WORD = re.compile(r"[a-z0-9$][a-z0-9$.%-]*[a-z0-9%]|\d", re.I)
_STOP = set("""a an and are as at be been but by can do does for from has have if in is it its of on or our
so that the their them then there these they this to was we were will with you your yours i my me""".split())
MIN_OVERLAP = 0.5   # share of a sentence's content words that must appear in a source to cite it


def _words(text: str) -> set[str]:
    return {w.lower().rstrip(".") for w in _WORD.findall(text)} - _STOP


def attribute_citations(answer: str, sources: list[str]) -> tuple[str, list[int]]:
    """Add [n] to each sentence that clearly comes from source n (by word overlap). Returns (text, used)."""
    source_words = [_words(s) for s in sources]
    out, used = [], []
    for sentence in split_sentences(answer):
        words = _words(sentence)
        best_n, best = None, 0.0
        for n, sw in enumerate(source_words, start=1):
            share = len(words & sw) / len(words) if words else 0.0
            if share > best:
                best_n, best = n, share
        if best_n and best >= MIN_OVERLAP and len(words) >= 3:
            sentence = re.sub(r"([.!?]?)$", lambda m: f" [{best_n}]{m.group(1)}", sentence, count=1)
            if best_n not in used:
                used.append(best_n)
        out.append(sentence)
    return " ".join(out), used


@dataclass
class ChatResult:
    answer: str
    answered: bool                  # False when the assistant said it doesn't know or nothing could be cited
    search_query: str               # the rewritten query that was searched
    citations: list[dict] = field(default_factory=list)    # sources the answer cited
    retrieved: list[dict] = field(default_factory=list)    # every source shown to the model (for debugging)
    gated: bool = False             # True when the relevance gate declined before asking the LLM
    guardrail: str | None = None    # set when the medical-advice guardrail replaced the answer


class ChatEngine:
    def __init__(self, conn, embedder, reranker, llm, top_k: int = TOP_K, min_score: float = MIN_RERANK_SCORE):
        self.conn, self.embedder, self.reranker, self.llm, self.top_k = conn, embedder, reranker, llm, top_k
        self.min_score = min_score
        self._db_lock = threading.Lock()    # one database connection shared by API worker threads

    # ---------------------------------------------------------------- step 1
    def rewrite(self, message: str, history: list[dict]) -> str:
        return self._rewrite(message, history)[0]

    def _rewrite(self, message: str, history: list[dict]) -> tuple[str, str | None]:
        """(query to search and rank by, extra search words or None)."""
        messages = [{"role": "system", "content": REWRITE_SYSTEM}]
        for question, query in REWRITE_EXAMPLES:
            messages += [{"role": "user", "content": question}, {"role": "assistant", "content": query}]
        recent = "\n".join(f"{m['role']}: {m['content'][:300]}" for m in history[-HISTORY_TURNS:])
        prompt = f"Conversation so far:\n{recent}\n\nLatest message: {message}" if recent else message
        messages.append({"role": "user", "content": prompt})

        with timing.step("model: rewrite question for search"):
            raw = self.llm.chat(messages, temperature=0.0, max_tokens=60)
        lines = raw.strip().splitlines()[0:1]
        query = lines[0].strip().strip('"').strip() if lines else ""
        if len(query) > 200:
            # gpt-oss often starts with good search words and then loops ("...cancellation feemissed first
            # appointment missed..."). Measured on the eval: ranking by the trimmed words did worse (retrieval
            # 98% -> 83%) than ranking by the patient's own words. So: rank by the question, and use the
            # trimmed start only as an extra search, to widen what's found.
            seen, words = set(), []
            for w in re.findall(r"[\w$'-]+", query):
                if w.lower() not in seen:
                    seen.add(w.lower())
                    words.append(w)
            hint = " ".join(words[:16])
            timing.note_last(f"rewrite looped ({len(query)} chars): searched the question, plus {hint!r}")
            return message, hint
        # Models sometimes answer instead of rewriting; fall back to the original words, and say why in --debug.
        problem = ("it returned nothing" if not query else
                   "it declined" if query.lower().startswith(("i'm sorry", "sorry", "as an ai")) else
                   "it repeated the question" if query.lower().rstrip("?. ") == message.lower().rstrip("?. ") else None)
        if problem:
            timing.note_last(f"rewrite not used: {problem}; got {raw.strip()[:80]!r}")
            return message, None
        return query, None

    # ---------------------------------------------------------------- step 2
    def search(self, query: str, original: str, extra: str | None = None) -> list[dict]:
        extras = [original] + ([extra] if extra else [])
        with self._db_lock, timing.step("search + rerank (on this computer)"):
            return retrieve(self.conn, self.embedder, self.reranker, query, k=self.top_k,
                            extra_queries=extras, rerank_query=query, include_restricted=False)

    # ---------------------------------------------------------------- step 3
    @staticmethod
    def format_sources(rows: list[dict]) -> str:
        blocks = []
        for i, r in enumerate(rows, start=1):
            m = r["metadata"]
            label = m.get("title", m.get("source", ""))
            if m.get("section"):
                label += f" — {m['section']}"
            if m.get("effective_date"):
                label += f" (effective {m['effective_date']})"
            blocks.append(f"[{i}] {label}\n{r['content'][:MAX_SOURCE_CHARS]}")
        return "\n\n".join(blocks)

    def answer(self, message: str, history: list[dict] | None = None) -> ChatResult:
        history = [m for m in (history or []) if m.get("role") in ("user", "assistant") and m.get("content")]
        message = message.strip()
        if check_question(message):
            # Asking for treatment advice or a diagnosis: answer safely without involving the model.
            return ChatResult(SAFE_REPLY, answered=False, search_query="", guardrail="medical question")
        query, extra = self._rewrite(message, history)
        rows = self.search(query, message, extra)
        if not rows:
            return ChatResult(NO_ANSWER, answered=False, search_query=query)
        best = rows[0].get("rerank_score")
        if best is not None and best < self.min_score:
            return ChatResult(NO_ANSWER, answered=False, search_query=query,
                              retrieved=self._retrieved(rows), gated=True)

        system = ANSWER_SYSTEM.format(clinic=CLINIC_NAME, today=date.today().strftime("%A, %B %d, %Y"),
                                      phone=CLINIC_PHONE)
        messages = [{"role": "system", "content": system}]
        messages += [{"role": m["role"], "content": m["content"]} for m in history[-HISTORY_TURNS:]]
        messages.append({"role": "user",
                         "content": f"Sources:\n\n{self.format_sources(rows)}\n\nQuestion: {message}\n\n{CITE_REMINDER}"})
        with timing.step("model: write answer"):
            text = self.llm.chat(messages, temperature=0.1, max_tokens=350)
        advice = check_answer(text)
        if advice:
            # The model gave medical advice despite the prompt: replace it before the patient sees it.
            return ChatResult(SAFE_REPLY, answered=False, search_query=query, retrieved=self._retrieved(rows),
                              guardrail=f"medical advice in answer: {advice!r}")
        # "We don't offer X" is only allowed if X appears in the sources; otherwise the model invented the "no".
        text, invented = check_negative_claims(text, [r.get("embed_text") or r["content"] for r in rows])
        note = f"removed unsupported claim: {invented[0]!r}" if invented else None
        if invented and not text:
            return ChatResult(NO_ANSWER, answered=False, search_query=query, retrieved=self._retrieved(rows),
                              guardrail=note)

        # Keep only citations that point at a real source; drop invented numbers like [9].
        used = []
        for n in _CITE.findall(text):
            n = int(n)
            if 1 <= n <= len(rows) and n not in used:
                used.append(n)
        text = _CITE.sub(lambda m: m.group(0) if 1 <= int(m.group(1)) <= len(rows) else "", text).strip()

        declined = bool(_DECLINE.search(text))
        inferred = False
        if not used and not declined:
            # The model answered but skipped the [n] markers (common with small models): attribute each
            # sentence to the source it overlaps most, and mark those citations as inferred.
            text, used = attribute_citations(text, [r["content"] for r in rows])
            inferred = bool(used)

        used = sorted(used)
        citations = [{"n": n, "source": rows[n - 1]["metadata"].get("source"),
                      "title": rows[n - 1]["metadata"].get("title"),
                      "section": rows[n - 1]["metadata"].get("section"),
                      "excerpt": excerpt(rows[n - 1]["content"]),
                      "url": source_url(rows[n - 1]["metadata"]),
                      "inferred": inferred} for n in used]
        return ChatResult(text, answered=bool(citations) and not declined, search_query=query,
                          citations=citations, retrieved=self._retrieved(rows), guardrail=note)

    @staticmethod
    def _retrieved(rows: list[dict]) -> list[dict]:
        return [{"n": i, "source": r["metadata"].get("source"), "title": r["metadata"].get("title"),
                 "section": r["metadata"].get("section"),
                 "rerank_score": r.get("rerank_score"), "search_rank": r.get("search_rank"),
                 "content": r["content"]} for i, r in enumerate(rows, start=1)]
