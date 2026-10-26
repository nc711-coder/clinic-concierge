"""Medical-advice guardrail, enforced in code so it doesn't depend on the model following the prompt.

The prompt already tells the model not to give medical advice, but in evals llama3.2:3b still listed
"bridges" and other exercises for low back pain. So:

1. check_question(): a message *asking for* treatment advice ("what exercises should I do", "should I
   put ice on it", "is my rotator cuff torn?") skips the model and gets SAFE_REPLY.
2. check_answer(): any answer that *gives* advice anyway (named exercises, sets/reps, medication,
   "apply ice", "sounds like a sprain") is replaced with SAFE_REPLY before the patient sees it.

Patterns favour safety over helpfulness: a false positive costs a redirect to the front desk, a false
negative is medical advice from a website bot.
"""
from __future__ import annotations

import os
import re

CLINIC_PHONE = os.environ.get("CLINIC_PHONE", "(703) 555-0142")

SAFE_REPLY = ("I can't give medical advice, recommend exercises or diagnose an injury, but our physical "
              "therapists can help. You can book a free 15-minute injury screen or a full evaluation by "
              f"calling our front desk at {CLINIC_PHONE}.")

# --- questions that ask for advice -------------------------------------------------------------
_ASKS_FOR_ADVICE = re.compile(r"""
    \b(what|which|any|good|best|recommend\w*)\b[^?.]{0,25}\b(exercises?|stretch(es|ing)?|workouts?|
        treatments?|medicines?|medications?|pills?|painkillers?|remed(y|ies)|home\ (care|remedies))\b
  | \bshould\ i\ (do|use|take|put|try|apply|ice|heat|stretch|rest|wrap|brace|tape|keep\ running|stop\ running)\b
  | \bhow\ (do|can|should)\ i\ (treat|fix|heal|cure|stretch|strengthen|rehab|ease|relieve|get\ rid\ of)\b
  | \b(is|are)\ (it|my\ [\w\ ]{1,25}?)\ (torn|broken|sprained|strained|fractured|dislocated|herniated)\b
  | \bdo\ i\ have\ (a|an)?\ ?[\w\ ]{0,20}?(tear|sprain|strain|fracture|tendinitis|tendonitis|
        bursitis|sciatica|concussion|arthritis|herniat\w*)\b
  | \b(ice|heat)\ or\ (ice|heat)\b
""", re.I | re.X)

# Questions about the clinic's own process mention "exercises" too; let those through to the docs.
_ABOUT_THE_CLINIC = re.compile(r"\b(first (visit|appointment)|evaluation|session|appointment|you (do|offer|use)|"
                               r"do you|does (the|your) (therapist|clinic))\b", re.I)

# --- answers that give advice ------------------------------------------------------------------
_GIVES_ADVICE = re.compile(r"""
    \b(glute\ )?bridges?\b | \bpelvic\ tilts?\b | \bbird[\ -]dogs?\b | \bcat[\ -]cow\b | \bknee[\ -]to[\ -]chest\b
  | \bclamshells?\b | \bsquats?\b | \blunges?\b | \bplanks?\b | \b(straight\ )?leg\ raises?\b | \bwall\ sits?\b
  | \bcalf\ raises?\b | \bheel\ slides?\b | \bhamstring\ stretch(es)?\b | \bquad(ricep)?s?\ stretch(es)?\b
  | \bfoam\ rolling\b | \bchild'?s\ pose\b | \bstep[\ -]ups?\b
  | \b\d+\s*(-|to)?\s*\d*\s*(reps|repetitions|sets)\b | \bhold\b[\w\ ]{0,25}?\b\d+\s*(-\s*\d+\s*)?seconds\b
  | \b(ibuprofen|advil|motrin|tylenol|acetaminophen|naproxen|aleve|aspirin|nsaids?)\b
  | \b(apply|use|put)\ (an?\ )?(ice|heat|cold|warm)(\ pack|\ compress)?\b | \bR\.?I\.?C\.?E\.?\b
  | \b(sounds\ like|you\ (may|might|probably|likely)\ have|it\ ('?s|is)\ (likely|probably)|could\ be)\s+
        (a\s+|an\s+)?(\w+\s+){0,3}?(tear|torn|strain|sprain|tendinitis|tendonitis|bursitis|fracture|sciatica|
        runner'?s\s+knee|patellofemoral|it\s+band|shin\s+splints|plantar\s+fasciitis|injury)\b
""", re.I | re.X)


def check_question(message: str) -> bool:
    """True if the patient is asking for treatment advice or a diagnosis."""
    return bool(_ASKS_FOR_ADVICE.search(message)) and not _ABOUT_THE_CLINIC.search(message)


def check_answer(answer: str) -> str | None:
    """Return the advice-like phrase found in an answer, or None if it's clean."""
    m = _GIVES_ADVICE.search(answer)
    return m.group(0) if m else None


# =============================================================================================
# Grounding check for "no" claims
#
# Small models turn "the sources don't mention X" into "we don't offer X" (in evals: "we don't have a
# pelvic floor therapist" - the documents never say that). A "no" is only allowed when the thing being
# denied actually appears in the sources the model was given; otherwise that sentence is replaced
# with "I don't have information about that."
# =============================================================================================

_SUBJECT = r"(?:we|the\s+clinic|quillmere(?:\s+pt)?|our\s+(?:clinic|office|practice|team))"
_NEG = r"(?:do\s+not|don['’]t|does\s+not|doesn['’]t|cannot|can['’]t|are\s+not\s+able\s+to|aren['’]t\s+able\s+to)"
_ADVERB = r"(?:\s+(?:currently|presently|yet|really|actually|unfortunately))?"
_VERB = (r"(?:offer|have|provide|accept|take|treat|do|perform|see|support|carry|allow|permit|work\s+with|"
         r"employ|staff|bill)")
_OBJ = r"(?P<obj>[^.?!;:\[\]\n]{1,80})"

_NEGATIVE_CLAIMS = [
    re.compile(rf"\b{_SUBJECT}{_ADVERB}\s+{_NEG}{_ADVERB}\s+{_VERB}\b\s*{_OBJ}", re.I),
    re.compile(rf"\b{_SUBJECT}\s+have\s+no\s+{_OBJ}", re.I),
    re.compile(rf"(?P<obj>\b[\w'’ /-]{{2,60}}?)\s+(?:is|are)\s+not\s+(?:currently\s+)?(?:offered|available|provided)\b", re.I),
    re.compile(rf"(?P<obj>\b[\w'’ /-]{{2,60}}?)\s+(?:isn['’]t|aren['’]t)\s+(?:currently\s+)?(?:offered|available|provided)\b", re.I),
    re.compile(rf"(?P<obj>\b[\w'’ /-]{{2,60}}?)\s+(?:is\s+not|isn['’]t)\s+something\s+we\s+(?:offer|provide|do)\b", re.I),
]

# "we don't have information / access / details" is a decline, not a claim about the clinic.
_DECLINE_OBJECT = re.compile(r"^(?:any\s+|that\s+|this\s+|enough\s+|specific\s+|more\s+|further\s+|the\s+)*"
                             r"(?:information|info|details?|access|records?\s+of|a\s+record|data|knowledge|"
                             r"way\s+to|ability)\b", re.I)
# Where the denied thing ends: "we don't offer X, but Y" / "X so Y" / "X at this time".
_OBJ_END = re.compile(r"\s+(?:but|however|although|though|so|and\s+we|and\s+our|which|because|at\s+this\s+time|"
                      r"right\s+now|at\s+the\s+moment|at\s+our\s+clinic|here)\b.*$", re.I)

_GENERIC = set("""a an the any our your their this that these those such some its it all
currently specific specialized specialised dedicated separate kind kinds type types form forms
service services program programs option options therapy therapies therapist therapists specialist
specialists provider providers treatment treatments appointment appointments patient patients clinic
office practice staff team care plan plans insurance coverage session sessions visit visits dr mr mrs ms
for of on in at to with by as or and from about who which that's
""".split())


# Sentence boundaries, but not after "Dr." / "Mr." / "e.g." etc. ("Dr. Hsu" is not two sentences).
_SENTENCE_END = re.compile(r"(?<!\bDr\.)(?<!\bMr\.)(?<!\bMs\.)(?<!\bSt\.)(?<!\bMrs\.)(?<!\be\.g\.)(?<!\bi\.e\.)"
                           r"(?<=[.!?])\s+")
# "we don't have X, but Dr. Y treats Z" -> keep "Dr. Y treats Z"
_CONTRAST = re.compile(r"[,;]?\s+(?:but|however|although|though)\s*,?\s+", re.I)


_ABBREV = re.compile(r"\b(Dr|Mr|Mrs|Ms|St)\.")
_ABBREV_BACK = re.compile(r"\b(Dr|Mr|Mrs|Ms|St)\b(?!\.)")


def split_sentences(text: str) -> list[str]:
    return [s for s in _SENTENCE_END.split(text.strip()) if s]


def _stem(word: str) -> str:
    w = word.lower().strip("'’")
    if len(w) <= 4:
        return w.rstrip("s") if len(w) > 3 else w
    return w[:max(4, len(w) - 2)]


def _key_words(obj: str) -> list[str]:
    obj = _OBJ_END.sub("", obj)
    words = [re.sub(r"['’]s$", "", w) for w in re.findall(r"[a-z0-9][a-z0-9'’-]*", obj.lower())[:8]]
    return [w for w in words if w not in _GENERIC and len(w) >= 3]


def _grounded(keys: list[str], sources: list[str]) -> bool:
    """True if one source mentions all the key words, with the first two together as a phrase
    ("pelvic floor", "initial evaluations"), not just somewhere in the text."""
    stems = [_stem(k) for k in keys]
    for src in sources:
        if not all(re.search(r"\b" + re.escape(st), src) for st in stems):
            continue
        if len(stems) < 2:
            return True
        phrase = (r"\b" + re.escape(stems[0]) + r"[\w'’-]*[\s/-]+(?:[\w'’-]+\s+)?" + re.escape(stems[1]))
        if re.search(phrase, src):
            return True
    return False


def check_negative_claims(answer: str, sources: list[str]) -> tuple[str, list[str]]:
    """Replace sentences that deny something the sources never mention.

    Returns (new_answer, unsupported_claims). new_answer is "" if nothing useful is left.
    """
    sources = [s.lower() for s in sources]
    kept, removed = [], []
    for original in split_sentences(answer):
        sentence = _ABBREV.sub(r"\1", original)              # "Dr. Hsu" -> "Dr Hsu" so the object isn't cut at "."
        unsupported, claim_end = None, 0
        for pattern in _NEGATIVE_CLAIMS:
            for m in pattern.finditer(sentence):
                obj = m.group("obj").strip()
                if _DECLINE_OBJECT.match(obj):
                    continue                                   # "we don't have information about ..." is fine
                keys = _key_words(obj)
                # For "X is not offered" the object can run back over earlier words; keep the last few.
                if pattern.pattern.lstrip().startswith("(?P<obj>"):
                    keys = keys[-3:]
                if not keys or not _grounded(keys, sources):
                    unsupported = _OBJ_END.sub("", m.group(0)).strip(" ,;")
                    claim_end = m.start("obj")
                    break
            if unsupported:
                break
        if unsupported:
            removed.append(unsupported)
            contrast = _CONTRAST.search(sentence, claim_end)
            if contrast:                                       # keep the true half after "but ..."
                tail = sentence[contrast.end():].strip()
                if tail:
                    kept.append(_ABBREV_BACK.sub(r"\1.", tail[0].upper() + tail[1:]))
        else:
            kept.append(original)
    if not removed:
        return answer, []
    rest = " ".join(kept).strip()
    if len(re.sub(r"\[\d+\]", "", rest).strip()) < 15:
        return "", removed
    return f"I don't have information about that specifically. {rest}", removed
