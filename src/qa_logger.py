"""Pipe-delimited (question, answer) audit log.

Every form field the agent fills gets one row appended to ``logs/qa.csv``
in the form ``|question|answer|`` so the user can review what the agent
answered on their behalf across dry-run walk-throughs.

The file accumulates across iterations; callers don't need to clear it.
"""

import os

LOG_PATH = os.path.join(os.path.dirname(__file__), "..", "logs", "qa.csv")
HEADER = "|question-string|answer-string|\n"


def _normalize_answer(answer) -> str:
    """Turn whatever the agent decided into the canonical CSV string form."""
    if answer is None:
        return ""
    if isinstance(answer, bool):
        return "TRUE" if answer else "FALSE"
    if isinstance(answer, int):
        return str(answer)
    if isinstance(answer, float):
        return str(int(answer)) if answer.is_integer() else str(answer)
    s = str(answer).strip()
    low = s.lower()
    if low in ("yes", "true", "y"):
        return "TRUE"
    if low in ("no", "false", "n"):
        return "FALSE"
    return s


def _clean(text: str) -> str:
    """Strip separators that would break the pipe-row format."""
    if text is None:
        return ""
    return (
        str(text)
        .replace("|", " ")
        .replace("\r", " ")
        .replace("\n", " ")
        .strip()
    )


_NOISE_QUESTIONS = (
    "required fields are marked",
    "an asterisk",
)


def log(question: str, answer) -> None:
    """Append one ``|question|answer|`` row to ``logs/qa.csv``.

    Creates the file (with header) and the ``logs/`` dir on first call.
    Silently no-ops on I/O failure so logging never breaks a run.
    """
    q = _clean(question)
    a = _clean(_normalize_answer(answer))
    if not q:
        return
    q_low = q.lower()
    if any(noise in q_low for noise in _NOISE_QUESTIONS):
        return
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        is_new = not os.path.exists(LOG_PATH)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            if is_new:
                f.write(HEADER)
            f.write(f"|{q}|{a}|\n")
    except Exception as e:
        # Logging is best-effort; never break the form flow.
        print(f"  [qa_log] Error writing row: {e}")
