"""Turn spoken-transcript sentences into impersonal reference prose.

This backs the offline organizer. Claude does a far better job on the same
task, so these are deliberately conservative, explicit rules: a sentence is
either rewritten by a rule that is safe, or dropped. Nothing is invented.

Order matters - rewrites run before the drop tests so that a phrase like
"let's say" becomes "suppose" (an example marker worth keeping) instead of
being discarded as first-person chatter.
"""

from __future__ import annotations

import re

# Sentences whose only job is presenting, framing, or addressing an audience.
DROP_PATTERNS = [
    r"\b(?:this|that|the|our|today'?s)\s+(?:video|session|presentation|talk|"
    r"episode|tutorial|lesson|course|webinar|demo)\b",
    r"\b(?:welcome|thanks for|thank you for|stay tuned|see you|subscribe|"
    r"hit the like|don'?t forget to|feel free to|any questions)\b",
    r"\b(?:hope|hoping)\s+(?:you|that you)\b",
    r"\b(?:we|i)(?:'ll| will| are going to|'re going to| shall)\s+"
    r"(?:explore|look|dive|cover|discuss|talk|see|start|begin|walk|examine|"
    r"wrap|move|turn|jump|go over|take a)\b",
    r"\b(?:as|like)\s+(?:we|you)\s+"
    r"(?:saw|said|mentioned|discussed|learned|noted|remember)\b",
    r"\b(?:to recap|recap what|wrapping up|wrap up|in summary, we|"
    r"before we|by the end)\b",
    r"\bthink of it this way\b",
    r"\b(?:let'?s|lets)\s+(?:get started|begin|start|move on|dive|take a closer|"
    r"turn|recap|wrap|look at how)\b",
    r"^(?:great|excellent|perfect|awesome|nice|good)\s*[.!]",
    r"\bhere'?s (?:what|how) (?:we|you)\b",
]
DROP_RE = [re.compile(p, re.I) for p in DROP_PATTERNS]

# Spoken disfluencies and hedges - removed wherever they appear.
DISFLUENCY = re.compile(
    r"(?:(?<=\s)|^)(?:uh+|um+|erm?|ah+|hmm+|mm+|like,|you know,?|i mean,?|"
    r"sort of|kind of|i guess,?|right\?)(?=[\s,]|$)",
    re.I,
)

# Auto-captions repeat words when a speaker stumbles: "from from", "the the".
STUTTER = re.compile(r"\b(\w+)(\s+\1\b)+", re.I)

# Leading discourse markers - stripped, possibly more than one deep.
LEADING_MARKERS = re.compile(
    r"^(?:so|now|well|okay|ok|all right|alright|and|but|then|again|"
    r"basically|essentially|actually|of course|in fact|you know|right|"
    r"first of all|first off|finally|lastly|next|anyway|however)\b[,:]?\s+",
    re.I,
)

# Safe phrase-level rewrites, applied in order.
REWRITES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(?:let'?s|let us)\s+(?:say|suppose)\b", re.I), "suppose"),
    (re.compile(r"\b(?:let'?s|let us)\s+assume\b", re.I), "assume"),
    (re.compile(r"\b(?:if|when)\s+(?:we|you)\s+(?:go|come|turn|refer|look)\s+"
                r"back to\s+(?:our|the|that|this)\s+", re.I), "in the "),
    (re.compile(r"\b(?:remember|recall)\s+(?:our|the|that)\s+"
                r"(\w+(?:\s+\w+)?)\s+analogy\b[.?]?", re.I),
     r"In the \1 analogy,"),
    (re.compile(r"\b(?:let'?s|let us)\s+(consider|imagine|examine|compare|"
                r"review)\b", re.I), r"\1"),
    (re.compile(r"\bthink of (?:it|this) (?:like|as)\b", re.I), "this is analogous to"),
    (re.compile(r"\byou can think of\b", re.I), "consider"),
    (re.compile(r"\bwe(?:'re| are) talking about\b", re.I), "this involves"),
    (re.compile(r"\b(?:as|like) (?:you|we) can see,?\s*", re.I), ""),
    (re.compile(r"\byou(?:'ll| will) (?:notice|see|find),?\s*", re.I), ""),
    (re.compile(r"\b(?:we|you)\s+(?:can|could)\s+", re.I), "it is possible to "),
    (re.compile(r"\b(?:we|you)\s+(?:need to|have to|must)\s+", re.I),
     "it is necessary to "),
    (re.compile(r"\b(?:we|you)\s+should\s+", re.I), "it is advisable to "),
    (re.compile(r"\bour\b", re.I), "the"),
    (re.compile(r"\byour\b", re.I), "the"),
]

# Anything still carrying a personal pronoun is framing we cannot safely rewrite.
PRONOUN_RE = re.compile(
    r"\b(?:i|me|my|mine|we|us|our|ours|you|your|yours|i'm|i've|i'll|i'd|"
    r"we're|we've|we'll|we'd|you're|you've|you'll|you'd|let'?s)\b",
    re.I,
)

MIN_WORDS = 5


def _tidy(sentence: str) -> str:
    sentence = re.sub(r"\s+", " ", sentence).strip()
    sentence = re.sub(r"\s+([,.;:!?])", r"\1", sentence)
    sentence = re.sub(r"([,;:])(?:\s*[,;:])+", r"\1", sentence)
    sentence = re.sub(r",\s*([.!?])", r"\1", sentence)
    sentence = re.sub(r"^[,;:]\s*", "", sentence)
    if sentence and sentence[0].islower():
        sentence = sentence[0].upper() + sentence[1:]
    return sentence


def impersonal(sentence: str) -> str | None:
    """Rewrite one sentence as reference prose, or None if it is small talk."""
    text = sentence.strip()
    if not text:
        return None

    text = DISFLUENCY.sub(" ", text)
    text = STUTTER.sub(r"\1", text)
    text = re.sub(r"\s+", " ", text).strip(" ,")

    for _ in range(3):  # markers stack: "So, now, let's ..."
        stripped = LEADING_MARKERS.sub("", text, count=1)
        if stripped == text:
            break
        text = stripped

    for pattern, replacement in REWRITES:
        text = pattern.sub(replacement, text)

    text = _tidy(text)
    if not text:
        return None
    if any(rx.search(text) for rx in DROP_RE):
        return None
    if PRONOUN_RE.search(text):
        return None
    if len(text.split()) < MIN_WORDS:
        return None
    return text


def to_reference_prose(sentences: list[str]) -> list[str]:
    """Apply `impersonal` across a list, dropping what cannot be salvaged."""
    return [out for s in sentences if (out := impersonal(s))]
