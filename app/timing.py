"""Where does the time go in one message? A tiny stopwatch shown by `python -m app --debug` (and in the
API's debug output).

    timing.start()                       # at the start of a message
    with timing.step("model: write answer"):
        ...                              # recorded as ("model: write answer", seconds, attempts, notes)
    timing.collect()                     # -> list of steps

Model calls also record how many HTTP attempts they took and whether the provider said "slow down"
(HTTP 429). The OpenAI library retries those silently, which looks like a long pause.
"""
from __future__ import annotations

import contextvars
import time
from contextlib import contextmanager
from dataclasses import dataclass, field

_steps: contextvars.ContextVar[list | None] = contextvars.ContextVar("timing_steps", default=None)
_open: contextvars.ContextVar[list] = contextvars.ContextVar("timing_open", default=[])


@dataclass
class Step:
    label: str
    seconds: float = 0.0
    attempts: int = 0
    notes: list[str] = field(default_factory=list)


def start() -> None:
    _steps.set([])
    _open.set([])


@contextmanager
def step(label: str):
    s = Step(label)
    stack = _open.get()
    _open.set(stack + [s])
    t = time.perf_counter()
    try:
        yield s
    finally:
        s.seconds = time.perf_counter() - t
        _open.set(stack)
        steps = _steps.get()
        if steps is not None:
            steps.append(s)


def count_request(_request=None) -> None:
    """httpx event hook: one more HTTP attempt for the innermost open step."""
    stack = _open.get()
    if stack:
        stack[-1].attempts += 1


def note_response(response) -> None:
    """httpx event hook: remember when the provider rate-limited us (it will be retried after a wait)."""
    stack = _open.get()
    if stack and response.status_code == 429:
        stack[-1].notes.append("rate-limited (429), waited and retried")
    elif stack and response.status_code >= 500:
        stack[-1].notes.append(f"provider error {response.status_code}, retried")


def note(text: str) -> None:
    """Attach a remark to the innermost open step (e.g. "answered by the fallback model")."""
    stack = _open.get()
    if stack:
        stack[-1].notes.append(text)


def note_last(text: str) -> None:
    """Attach a remark to the step that just finished (e.g. why its output wasn't used)."""
    steps = _steps.get()
    if steps:
        steps[-1].notes.append(text)


def collect() -> list[dict]:
    return [{"step": s.label, "seconds": round(s.seconds, 2), "attempts": s.attempts, "notes": s.notes}
            for s in (_steps.get() or [])]
