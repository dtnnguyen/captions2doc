"""Text normalization shared by the document and diagram renderers.

The PDF core fonts (Helvetica, Courier) only cover Latin-1. Anything outside
it - fullwidth punctuation from video titles, smart quotes, em dashes, emoji -
renders as a black box, so it is folded to a close ASCII equivalent first.
"""

from __future__ import annotations

import unicodedata

REPLACEMENTS = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"',
    "–": "-", "—": "-", "‒": "-", "―": "-", "−": "-",
    "…": "...", "•": "-", " ": " ", "​": "",
    "→": "->", "←": "<-", "⇒": "=>", "≠": "!=",
    "≤": "<=", "≥": ">=", "×": "x",
}


def normalize_title(text: str) -> str:
    """Fold fullwidth/compatibility characters so titles read normally.

    yt-dlp replaces filesystem-hostile characters with fullwidth look-alikes
    (`:` becomes `：`); NFKC turns those back into plain ASCII.
    """
    return unicodedata.normalize("NFKC", text).strip()


def latin1_safe(text: str) -> str:
    """Make `text` renderable by the PDF core fonts."""
    text = "".join(REPLACEMENTS.get(ch, ch) for ch in text)
    text = unicodedata.normalize("NFKC", text)
    out: list[str] = []
    for ch in text:
        if ch in "\n\t" or ch.encode("latin-1", "ignore"):
            out.append(ch)
            continue
        decomposed = unicodedata.normalize("NFKD", ch)
        stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
        out.append(stripped if stripped.encode("latin-1", "ignore") else "")
    return "".join(out)
