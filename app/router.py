"""Decide who handles a message. Rules, not an LLM call: instant, free and testable.

    answer   questions about the clinic -> RAG pipeline (app/chat.py), with citations and guardrails
    booking  wants an appointment, or is mid-booking giving details -> booking agent (app/agent.py)
    handoff  asks for a person -> front desk, straight away
"""
from __future__ import annotations

import re

_HANDOFF = re.compile(r"""
    \b(talk|speak|chat)\s+(to|with)\s+(a\s+|an\s+|the\s+|your\s+)?(real\s+|actual\s+|live\s+)?
        (person|human|someone|somebody|staff|front\s+desk|receptionist|manager|representative|people)\b
  | \b(real|actual|live)\s+(person|human)\b | \brepresentative\b | \boperator\b
  | \b(call|ring)\s+me(\s+back)?\b | \bhave\s+(someone|somebody)\s+(call|contact)\b
""", re.I | re.X)

_BOOKING = re.compile(r"""
    \b(book|schedule|reschedule|make|set\s+up|get|arrange|cancel)\s+(me\s+)?(in\s+)?(for\s+)?(an?\s+|my\s+|the\s+)?
        (appointment|appt|visit|evaluation|eval|session|follow[\s-]?up|time|slot|consult\w*|screen(ing)?)\b
  | \b(i'?d|i\s+would|i|we)\s+(like|want|need|wanna)\s+to\s+(book|schedule|come\s+in|see|make|get)\b
  | \b(can|could|may)\s+i\s+(book|schedule|come\s+in|get\s+in|make)\b
  | \bbook(ing)?\s+(an?\s+|my\s+)?(appointment|visit|evaluation|session)\b
  | \b(any|have|got)\s+(any\s+)?(openings?|availability|available\s+(times?|slots?|appointments?)|slots?|time\s+slots?)\b
  | \bsign\s+me\s+up\b | \bget\s+(me\s+)?in\s+(to\s+see|with)\b | \bbook\s+(me|us)\b | \bbook\s+(with|for)\b
  # "I want an appointment on Thursday", "I need a follow-up", "we'd like to get a visit"
  # (not "do I need an evaluation ...?", which is a question about the clinic)
  | (?<!\bdo\s)(?<!\bwill\s)(?<!\bwould\s)(?<!\bshould\s)(?<!\bmust\s)
    \b(i|we)(\s+would|\s+really|'d|’d)?\s+(want|need|like)\s+(to\s+(get|have|make|set\s+up)\s+)?
        (an?|another|a\s+new|one\s+more|my\s+next|some)\s+
        (appointments?|appts?|visits?|evaluations?|evals?|sessions?|follow[\s-]?ups?|time\s+slots?|consult\w*)\b
  | \b(appointment|appt|visit|evaluation|session|follow[\s-]?up)\s+(on|for|this|next)\s+
        (mon|tue|wed|thu|fri|sat|sun|tomorrow|today|morning|afternoon|evening|week)
  # Asking about the calendar: only the booking tools know it, the documents don't.
  | \b(next|earliest|soonest|first)\s+(available|open|opening|free)\b
  | \bwhen\s+(can|could|will)\s+(i|we|you)\s+(come\s+in|get\s+in|be\s+seen|book|see\s+me|fit\s+me)\b
  | \bwhen('?s|\s+is|\s+are)\s+(the\s+)?(next\s+)?(available|opening|openings|open\s+(slot|time|appointment)s?)\b
  | \b(what|which)\s+(times?|days?|slots?|appointments?)\s+(are|do\s+you\s+have)\s+(available|open|free)\b
  | \b(any|what)\s+(openings?|availability)\b | \bwhat('?s|\s+is)\s+(your\s+)?availability\b
""", re.I | re.X)

# Inside a booking, these mean the patient is still talking about the appointment.
_BOOKING_DETAIL = re.compile(r"""
    \b(mon|tues?|wed(nes)?|thu(rs)?|fri|sat(ur)?|sun)(day)?s?\b | \b(morning|afternoon|evening|tonight|tomorrow|today|
        next\s+week|this\s+week|weekend)\b | \b\d{1,2}(:\d\d)?\s*(am|pm|a\.m\.|p\.m\.)\b | \b\d{1,2}/\d{1,2}\b
  | \b(first|second|third|last|earliest|that|this)\s+(one|time|slot|option)\b | \bdr\.?\s+\w+ | \bwith\s+(dr|doctor)\b
  | \b(new|returning|existing)\s+patient\b | \bbeen\s+(there|here|before)\b | \b(never|first\s+time)\b
  | @ | \d{3}\D?\d{3}\D?\d{4}
""", re.I | re.X)

_QUESTION = re.compile(r"\?\s*$|^\s*(what|which|who|whom|where|when|why|how|do|does|did|is|are|can|could|will|"
                       r"would|should|may|am)\b", re.I)


def route(message: str, state: dict) -> str:
    text = message.strip()
    if _HANDOFF.search(text):
        return "handoff"
    if _BOOKING.search(text):
        return "booking"
    if state.get("mode") == "booking":
        # Mid-booking: details go to the agent, but a clinic question ("do you take Aetna?") goes to the
        # evaluated RAG pipeline. The booking continues on the next message.
        if _QUESTION.search(text) and not _BOOKING_DETAIL.search(text):
            return "answer"
        return "booking"
    return "answer"


_YES = re.compile(r"^\s*(yes|yeah|yep|yup|ya|y|sure|ok(ay)?|correct|confirm(ed)?|please\s+do|please|book\s+it|"
                  r"sounds\s+good|that\s+works|perfect|great|go\s+ahead|do\s+it|absolutely|definitely)\b", re.I)
_NO = re.compile(r"^\s*(no|nope|nah|n|not|don'?t|wait|hold\s+on|cancel|stop)\b|\b(instead|different|another|change|"
                 r"other|actually|wrong|rather)\b", re.I)


# --------------------------------------------------------------------------- new or returning patient

_NEW_PATIENT = re.compile(r"""
    \b(new\s+patient|first\s+(time|visit|appointment)|never\s+(been|come|visited)|haven'?t\s+been|not\s+been|
       initial\s+(eval\w*|visit|appointment)|evaluation|eval|assessment)\b
""", re.I | re.X)
_RETURNING = re.compile(r"""
    \b(returning|existing|current)\s+patient\b | \bfollow[\s-]?ups?\b
  | \b(i'?ve|i\s+have|we'?ve)\s+(been|come)\s+(to\s+(you|your|the)|in\s+before|coming|seeing|a\s+patient)
  | \bbeen\s+(there|here|before)\b | \bbeen\s+in\s+before\b | \b(came|come)\s+(in\s+)?before\b | \bmy\s+(next|regular|usual)\s+(visit|session|appointment)\b
  | \bcontinu\w+\s+(my\s+)?(treatment|therapy|sessions?|care)\b | \balready\s+(a\s+patient|seeing|started)\b
""", re.I | re.X)
_EXISTING_APPT = re.compile(r"\b(cancel\w*|reschedul\w*|move|change|push\s+back)\b", re.I)
_PLAIN_YES = re.compile(r"^\s*(yes|yeah|yep|yup|ya|y|sure|i\s+have|correct)\b", re.I)
_PLAIN_NO = re.compile(r"^\s*(no|nope|nah|n|never|not\s+yet)\b", re.I)
_NEGATED = re.compile(r"\b(not|never|n'?t|haven'?t|havent|didn'?t|didnt)\b", re.I)
_JUST_YES = re.compile(r"^\s*(yes|yeah|yep|yup|sure|ok(ay)?|sounds\s+good|that\s+works)(\s+please)?[\s.!]*$", re.I)


_NO_TIMES = re.compile(r"""
    \b(don'?t|do\s+not)\s+have\s+any\s+(open|available|openings?|availability|slots?|times?|appointments?)\b
  | \bno\s+(open|available|free)\s+(times?|slots?|appointments?|spots?)\b | \bno\s+(openings?|availability)\b
  | \b(fully|all)\s+booked\b | \bnothing\s+(is\s+)?(open|available)\b | \b(aren'?t|are\s+not)\s+any\s+(open|available)
""", re.I | re.X)
_FRUSTRATED = re.compile(r"""
    \b(inconvenient|frustrat\w*|annoy\w*|ridiculous|useless|terrible|awful|unacceptable|waste\s+of\s+time|
       not\s+helpful|unhelpful|this\s+(is|isn'?t)\s+(bad|stupid|working)|doesn'?t\s+work|ugh+)\b
""", re.I | re.X)


def says_no_times(reply: str) -> bool:
    """The reply tells the patient nothing is available."""
    return bool(_NO_TIMES.search(reply or ""))


_NO_PREFERENCE = re.compile(r"""
    ^\s*(not\s+really|no(\s+(real\s+)?preference)?|nope|nah|none|any(\s*time|\s+day|\s+time|thing)?|
          whenever|whatever|either|flexible|i'?m\s+flexible|(it\s+)?doesn'?t\s+matter|i\s+don'?t\s+(care|mind)|
          (the\s+)?(earliest|soonest|first\s+available|next\s+available)(\s+(one|time|slot))?|asap|as\s+soon\s+as\s+possible)
    ([\s,]+(really|works|is\s+fine|is\s+good|works\s+for\s+me|please|thanks|thank\s+you|i'?m\s+flexible))*[\s.!]*$
""", re.I | re.X)
_WHEN_WORDS = re.compile(r"\b(mon|tue|wed|thu|fri|sat|sun)[a-z]*\b|\b(morning|afternoon|evening|today|tomorrow|"
                         r"week|weekend|am|pm|noon|\d{1,2}(:\d\d)?)\b", re.I)


def no_preference(message: str) -> bool:
    """'not really', 'any time', 'whenever', 'the earliest' in reply to "any preferred day or time?"."""
    return bool(_NO_PREFERENCE.match(message)) and not _WHEN_WORDS.search(message) and len(message.split()) <= 8


def frustrated(message: str) -> bool:
    return bool(_FRUSTRATED.search(message))


_DECLINE_CALLBACK = re.compile(r"""
    ^\s*(no|nope|nah)\b | \bno\s+thanks?\b | \bnever\s*mind\b | \bnvm\b | \b(i'?ll|i\s+will)\s+call\b
  | \b(don'?t|do\s+not|rather\s+not)\s+(want|give|share|call)\b | \bnot\s+(now|right\s+now)\b
""", re.I | re.X)


def declines_callback(message: str) -> bool:
    """'no thanks', 'never mind', 'I'll call them myself' in reply to "what's your phone number?"."""
    return bool(_DECLINE_CALLBACK.search(message))


def just_yes(message: str) -> bool:
    """A bare "yes" / "sure" with no choice in it."""
    return bool(_JUST_YES.match(message))


def visit_type_hint(message: str) -> str | None:
    """'evaluation' (new patient), 'follow_up' (returning) or None if the message doesn't say."""
    if _NEW_PATIENT.search(message):          # first: "I've never been here before" also says "been here before"
        return "evaluation"
    if _RETURNING.search(message):
        return "follow_up"
    return None


def been_before_reply(message: str) -> str | None:
    """The answer to "Have you been to our clinic before?": 'yes' = returning, 'no' = new."""
    hint = visit_type_hint(message)
    if hint:
        return hint
    if _PLAIN_NO.search(message) or _NEGATED.search(message):     # "I have not", "nope", "I haven't"
        return "evaluation"
    if _PLAIN_YES.search(message):
        return "follow_up"
    return None


def about_existing_appointment(message: str) -> bool:
    """Cancelling or moving an appointment they already have: no new-or-returning question needed."""
    return bool(_EXISTING_APPT.search(message))


def confirmation_reply(message: str) -> str:
    """'yes', 'no' (or a change of plan), or 'unclear' for a reply to "Shall I book it?"."""
    if _NO.search(message):
        return "no"
    if _YES.search(message):
        return "yes"
    return "unclear"
