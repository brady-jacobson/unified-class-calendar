from __future__ import annotations

import re


def normalized_event_title(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def canonical_event_key(title: str, kind: str) -> str:
    """Return a conservative cross-source identity for dedupe/conflict checks."""
    normalized = normalized_event_title(title)
    if kind in {"available", "availability_end"}:
        return f"{kind}:{normalized}"
    if re.search(r"\b(?:hw|homework)\s+\d+\.\d+\b", title, re.I):
        return f"{kind}:{normalized}"
    if "fall-break" in normalized:
        return "break:fall"
    if "thanksgiving-break" in normalized:
        return "break:thanksgiving"
    if "last-day-of-classes" in normalized:
        return "term:last-day-of-classes"
    if "withdraw" in normalized:
        return "term:withdraw"

    section = re.search(r"section-?0?([1-9])", normalized)
    if "final-exam" in normalized and section:
        return f"exam:final:section-{section.group(1)}"
    if "final-exam" in normalized:
        return "exam:final"

    numbered_patterns = (
        (r"(?:exam|test)-?(?:number|no)?-?#?-?(\d+)", "exam"),
        (r"quiz-?#?-?(\d+)", "quiz"),
        (r"zy-?#?-?(\d+)", "due:zy"),
        (r"lab-?#?-?(\d+)", "due:lab"),
        (r"project-?#?-?(\d+)", "due:project"),
        (r"(?:hw|homework)-?#?-?(\d+)(?![\d.])", "due:hw"),
        (r"tips?-?#?-?(\d+)", "tips"),
    )
    for pattern, prefix in numbered_patterns:
        match = re.search(pattern, normalized)
        if match:
            return f"{prefix}:{match.group(1)}"
    return f"{kind}:{normalized}"
