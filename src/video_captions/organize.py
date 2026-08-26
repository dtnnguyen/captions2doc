"""Turn a raw caption transcript into a topic-organized Markdown document.

Primary path: Claude (Anthropic API) reorganizes the transcript into topics,
key points, and worked examples. Fallback path: an offline heuristic that
segments the transcript and pulls out example passages, used when no API
credentials are available or when --no-llm is passed.
"""

from __future__ import annotations

import re
import sys
from collections import Counter

from .captions import Captions, fmt_ts, timestamp_url
from .prose import impersonal

MODEL = "claude-opus-5"

SYSTEM_PROMPT = """\
You are a technical editor. You are given the raw caption transcript of a talk. \
Rewrite it as an informational reference paper in Markdown - a document that \
explains the subject, not a record of someone presenting it.

Voice - this matters most:
- Write impersonally, in the third person. Never use "I", "we", "our", "us", \
"you", "your", "let's", or "my". Do not address the reader.
- Never mention the video, the session, the speaker, the slides, or the audience. \
Drop "in this video", "as mentioned earlier", "we'll now look at", "as you can see".
- State the subject matter directly as fact: "The control plane computes routes", \
not "The speaker explains that the control plane computes routes" and not \
"Now let's look at how the control plane computes routes".
- Cut all small talk: greetings, introductions, thanks, sign-offs, promotion, \
recaps of what was "covered", and transitions between topics.
- Declarative present tense. Straight to the point - no throat-clearing sentences \
whose only job is to announce what comes next.

Structure:
- Organize by TOPIC, not by timeline. Merge related material that is scattered \
across the transcript into one section.
- Use `##` for major topics and `###` for sub-topics; write headings as noun \
phrases. Do not emit an H1 title (the caller adds it).
- Lead with a short "## Overview" section: 2-4 sentences defining the subject and \
its scope.
- Follow it with a "## Summary" section: 5-8 bullets carrying the substance a reader \
would need if they read nothing else - the actual mechanisms, distinctions and \
conclusions, not a list of the topics covered. Do not restate the Overview's wording.
- Under each topic, use prose and bullet points to capture the substance: \
definitions, mechanisms, comparisons, trade-offs, numbers, and named technologies.
- Include an "**Example:**" block wherever the transcript works through a concrete \
example, scenario, analogy, or walkthrough - rewritten impersonally. Present it \
clearly (steps, a small table, or a fenced code block if it is code, config or \
commands). Do NOT invent examples that are not in the transcript.
- Add a "## Key Takeaways" section at the end with the most important points.

Emphasis:
- Bold the important keywords so the document can be skimmed: technical terms on \
their first meaningful mention, named technologies and protocols, standards, and \
the numbers that matter. Write **forwarding table**, not a bolded whole clause.
- Bold sparingly - at most a handful per section. Emphasis everywhere is emphasis \
nowhere. Never bold inside a heading, and never bold a full sentence.
- After "## Key Takeaways", add a "## Key Terms" section: a bullet per important \
term as `- **Term** - one-line definition`. Define only terms the transcript \
actually explains or uses substantively; no more than about ten.

Accuracy:
- Preserve technical accuracy. Never add facts that are not in the transcript. \
Fix obvious speech-to-text errors in technical terms only when the intent is clear.
- Output Markdown only - no preamble, no explanation of what you did.
"""

BRIEF_SYSTEM_PROMPT = """\
You are a technical editor. You are given the raw caption transcript of a talk. \
Condense it into a short briefing note in Markdown - what the subject is and what \
matters about it, for a reader who will not read anything longer.

Voice - this matters most:
- Write impersonally, in the third person. Never use "I", "we", "our", "us", \
"you", "your", "let's", or "my". Do not address the reader.
- Never mention the video, the session, the speaker, or the audience.
- State the subject matter directly as fact, in declarative present tense.
- No greetings, sign-offs, promotion, or transitions.

Structure - exactly this, nothing else:
- "## Overview": 2-4 sentences defining the subject and its scope.
- "## Summary": 6-10 bullets carrying the substance - mechanisms, distinctions, \
trade-offs, named technologies, numbers. Each bullet stands on its own.
- "## Key Terms": a bullet per important term as `- **Term** - one-line \
definition`, at most eight. Omit this section if the subject has no jargon.
- Do not emit an H1 title (the caller adds it). No other sections, no diagrams.

Emphasis:
- Bold the important keywords - technical terms, named technologies, protocols, \
standards, and the numbers that matter. Bold sparingly, never a whole sentence.

Accuracy:
- Never add facts that are not in the transcript. Fix obvious speech-to-text errors \
in technical terms only when the intent is clear.
- Output Markdown only - no preamble, no explanation of what you did.
"""

TIMESTAMP_PROMPT = """\

Source links:
- The video is at {url} and deep links to a moment work like {example}.
- End every `##` section with its own line in exactly this form, pointing at the \
earliest transcript timestamp that section draws on:
  `[Watch from M:SS]({url_sep}t=SECONDSs)`
  where SECONDS is that timestamp in whole seconds. Use the timestamps given in the \
transcript - never guess one.
- Do not put timestamps anywhere else in the document.
"""

DIAGRAM_PROMPT = """\

Diagrams:
- Add Mermaid diagrams wherever a picture carries the idea better than prose: an \
architecture or layering, a flow of data or control, a decision, a lifecycle, a \
comparison, or a topic hierarchy. Aim for one diagram after the Overview that maps \
the whole subject, plus a diagram in any major section that genuinely benefits. Do \
not add a diagram to a section that is purely definitional.
- Only these forms are supported - anything else will not render:
  - `flowchart TD` (top-down) or `flowchart LR` (left-right)
  - `mindmap` (indentation-based hierarchy)
- Node syntax: `A[Rectangle]`, `B(Rounded)`, `C{Decision}`, `D((Circle))`. \
Edges: `A --> B`, `A -->|label| B`, `A -- label --> B`, `A -.-> B` for a weak or \
indirect relationship.
- Keep node IDs short and alphanumeric. Keep each label under about 40 characters \
and avoid parentheses, quotes, colons and pipes inside labels - they break parsing. \
Write `BGP and OSPF`, not `Protocols (BGP, OSPF)`.
- Keep a diagram to at most about 12 nodes; split a bigger idea into two diagrams.
- Every diagram must reflect what the video actually said. Put it directly under the \
heading it illustrates, in a fenced ```mermaid block.
"""


def _user_prompt(captions: Captions) -> str:
    meta = [f"Video title: {captions.title}"]
    if captions.uploader:
        meta.append(f"Channel: {captions.uploader}")
    if captions.url:
        meta.append(f"URL: {captions.url}")
    if captions.duration:
        meta.append(f"Duration: {fmt_ts(captions.duration)}")
    return (
        "\n".join(meta)
        + "\n\nTranscript (timestamps are for your reference only; do not "
        "include them in the output):\n\n"
        + captions.timestamped_text
    )


# Input / output dollars per million tokens, for the cost estimate printed
# before a billed call. Unlisted models fall back to Opus-tier rates.
PRICING = {
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
DEFAULT_PRICING = (5.0, 25.0)


def _system_prompt(captions: Captions, diagrams: bool, brief: bool) -> str:
    """Assemble the system prompt for a request."""
    if brief:
        return BRIEF_SYSTEM_PROMPT
    system = SYSTEM_PROMPT + (DIAGRAM_PROMPT if diagrams else "")
    example = timestamp_url(captions.url, 125)
    if example:
        sep = "&" if "?" in captions.url else "?"
        system += TIMESTAMP_PROMPT.format(
            url=captions.url, example=example, url_sep=captions.url + sep
        )
    return system


def estimate_input_cost(
    client,
    captions: Captions,
    model: str = MODEL,
    diagrams: bool = True,
    brief: bool = False,
) -> tuple[int, float]:
    """Return (input tokens, dollars) for the request, without running it.

    Token counting is a free endpoint, so this costs nothing. Only the input
    side can be priced up front - the output, which is the larger share of the
    bill, is not knowable before generation.
    """
    counted = client.messages.count_tokens(
        model=model,
        system=_system_prompt(captions, diagrams and not brief, brief),
        messages=[{"role": "user", "content": _user_prompt(captions)}],
    )
    rate_in, _ = PRICING.get(model, DEFAULT_PRICING)
    return counted.input_tokens, counted.input_tokens * rate_in / 1_000_000


def organize_with_claude(
    captions: Captions,
    model: str = MODEL,
    diagrams: bool = True,
    brief: bool = False,
) -> str:
    import anthropic

    client = anthropic.Anthropic()
    if brief:
        diagrams = False
    system = _system_prompt(captions, diagrams, brief)

    try:
        tokens, dollars = estimate_input_cost(
            client, captions, model=model, diagrams=diagrams, brief=brief
        )
        print(
            f"   {tokens:,} input tokens ~ ${dollars:.3f}; output billed on top",
            file=sys.stderr,
        )
    except Exception as exc:  # noqa: BLE001 - an estimate must never block the run
        print(f"   (could not estimate cost: {exc})", file=sys.stderr)

    with client.messages.stream(
        model=model,
        max_tokens=64000,
        system=system,
        thinking={"type": "adaptive"},
        output_config={"effort": "high"},
        messages=[{"role": "user", "content": _user_prompt(captions)}],
    ) as stream:
        message = stream.get_final_message()

    if message.stop_reason == "refusal":
        raise RuntimeError(
            "Claude declined this request "
            f"({getattr(message.stop_details, 'category', 'unknown')})."
        )
    body = "".join(b.text for b in message.content if b.type == "text").strip()
    if not body:
        raise RuntimeError("Claude returned an empty document.")
    return body


# --------------------------------------------------------------------------
# Offline fallback
# --------------------------------------------------------------------------

STOPWORDS = set(
    """a an the and or but so if then than that this these those there here it its
    is are was were be been being am do does did doing have has had having i you he
    she we they me him her us them my your our their his hers of to in on at for
    with without from into onto over under about as by can could will would shall
    should may might must not no nor very really just also too now well okay ok
    let lets going gonna get got go goes went come comes said say says like what
    when where which who whom how why all any both each few more most other some
    such only own same s t don now up down out off again further once because
    while before after between through during above below to's what's it's we're
    they're that's you're i'm we'll you'll thing things kind sort lot bit way ways
    right actually basically essentially something anything everything one two
    first second next last see look think know want need make makes made use used
    using uses talk talking mean means video session today work works working
    together needs needed operates operate happens happen handle handles matters
    focus part parts point points example examples different important critical
    complete whole various single entire actual takes take taken comes coming
    based include includes including key big small good better best
    he she they him her them his hers theirs its itself themselves
    across around within without along among between toward towards through
    advisable likely unlikely able really truly simply clearly obviously
    uh um erm ah hmm mm okay yeah yep nope""".split()
)

# A bigram whose second word is one of these reads as a verb phrase, not a term.
_WEAK_TAIL = set(
    """work works working operates operate needs handles handled happens matters
    means makes helps allows requires uses used comes goes takes gets stays
    often responsible able ready aware sure available possible necessary
    follows follow provides provide offers gives sends moves decides determines
    looks keeps runs exchanges updates installs sits lives depends""".split()
)

EXAMPLE_MARKERS = (
    "for example",
    "for instance",
    "let's say",
    "lets say",
    "imagine",
    "consider a",
    "consider the case",
    "as an example",
    "think of it like",
    "think of this like",
    "a good analogy",
    "analogy",
    "picture this",
    "suppose",
    "in practice",
    "real world example",
    "real-world example",
    "case in point",
    "walk through",
    "let's walk",
)

SECTION_TARGET_WORDS = 320


def _sentences(captions: Captions) -> list[tuple[float, str]]:
    """Regroup cues into sentences, keeping each sentence's start time."""
    out: list[tuple[float, str]] = []
    buf: list[str] = []
    start = captions.cues[0].start if captions.cues else 0.0
    for cue in captions.cues:
        if not buf:
            start = cue.start
        buf.append(cue.text)
        joined = " ".join(buf)
        while True:
            m = re.search(r"[.!?](\s|$)", joined)
            if not m:
                break
            sentence, joined = joined[: m.end()].strip(), joined[m.end():].strip()
            if sentence:
                out.append((start, sentence))
            start = cue.end
            if not joined:
                break
        buf = [joined] if joined else []
    if buf and " ".join(buf).strip():
        out.append((start, " ".join(buf).strip()))
    return out


def _keywords(text: str, count: int) -> list[str]:
    words = re.findall(r"[A-Za-z][A-Za-z0-9'-]+", text.lower())
    freq = Counter(
        w for w in words
        if w not in STOPWORDS and len(w) > 2 and "'" not in w
    )
    bigrams = Counter()
    tokens = [w for w in words if len(w) > 2]
    for a, b in zip(tokens, tokens[1:]):
        if a in STOPWORDS or b in STOPWORDS or b in _WEAK_TAIL or b.endswith("ly"):
            continue
        if "'" in a or "'" in b:
            continue
        bigrams[f"{a} {b}"] += 1
    picked: list[str] = []
    for phrase, n in bigrams.most_common(count):
        if n >= 3:                      # a real term recurs; a verb phrase rarely does
            picked.append(phrase)
    for word, _ in freq.most_common(count * 4):
        if len(picked) >= count:
            break
        if any(word in p for p in picked):
            continue
        if any(_same_stem(word, p) for p in picked):   # "state" vs "states"
            continue
        picked.append(word)
    return picked[:count]


def _same_stem(a: str, b: str) -> bool:
    """Crude singular/plural match so a term is not listed twice."""
    return a.rstrip("s") == b.rstrip("s") or a.rstrip("es") == b.rstrip("es")


def _titlecase(phrase: str) -> str:
    small = {"of", "and", "the", "in", "on", "vs", "a", "an", "to", "for"}
    parts = phrase.split()
    return " ".join(
        p if (i and p in small) else (p.upper() if len(p) <= 3 and p.isupper() else p.capitalize())
        for i, p in enumerate(parts)
    )


def _segment(sentences: list[tuple[float, str]]) -> list[list[tuple[float, str]]]:
    sections: list[list[tuple[float, str]]] = []
    current: list[tuple[float, str]] = []
    words = 0
    for start, sentence in sentences:
        current.append((start, sentence))
        words += len(sentence.split())
        if words >= SECTION_TARGET_WORDS:
            sections.append(current)
            current, words = [], 0
    if current:
        if sections and words < SECTION_TARGET_WORDS // 3:
            sections[-1].extend(current)
        else:
            sections.append(current)
    return sections



FILLER_RE = re.compile(
    r"^(and\s+)?(welcome|hello|hi\b|thanks|thank you|so\b|now\b|okay|all right|alright|"
    r"let's get started|first, let's|next, let's|now let's|let's take a closer|"
    r"in this (video|session)|today we|before we (wrap|finish)|that's all|"
    r"don't forget|please (like|subscribe)|see you)",
    re.I,
)


def _is_filler(sentence: str) -> bool:
    return bool(FILLER_RE.match(sentence.strip()))


def _paragraphs(sentences: list[str], per: int = 4) -> list[str]:
    return [
        " ".join(sentences[i : i + per]) for i in range(0, len(sentences), per)
    ]


def _unique_heading(candidate: str, used: set[str], body: str) -> str:
    if candidate.lower() not in used:
        used.add(candidate.lower())
        return candidate
    for alt in _keywords(body, 6)[1:]:
        alt = _titlecase(alt)
        if alt.lower() not in used:
            used.add(alt.lower())
            return alt
    n = 2
    while f"{candidate} ({n})".lower() in used:
        n += 1
    used.add(f"{candidate} ({n})".lower())
    return f"{candidate} ({n})"


def _mermaid_escape(label: str) -> str:
    """Strip characters that break the Mermaid subset we render."""
    label = re.sub(r"[()\[\]{}|\"\'`:;]", " ", label)
    return re.sub(r"\s+", " ", label).strip()[:40]


def _topic_mindmap(title: str, headings: list[str]) -> str:
    """A topic-hierarchy diagram built from the section headings."""
    root = re.split(r"[:\u2013-]", title, maxsplit=1)[0].strip() or title
    lines = ["```mermaid", "mindmap", f"  root(({_mermaid_escape(root)}))"]
    lines += [f"    {_mermaid_escape(h)}" for h in headings if h]
    lines.append("```")
    return "\n".join(lines)


def _highlight(text: str, terms: list[str], used: set[str]) -> str:
    """Bold the first mention of each key term so it stands out when scanning."""
    for term in terms:
        if term in used:
            continue
        pattern = re.compile(rf"(?<![\w*]){re.escape(term)}(?![\w*])", re.I)
        m = pattern.search(text)
        if m:
            text = f"{text[:m.start()]}**{m.group(0)}**{text[m.end():]}"
            used.add(term)
    return text


def organize_heuristically(
    captions: Captions, diagrams: bool = True, brief: bool = False
) -> str:
    """Offline organizer: segment, label, and surface examples. No LLM."""
    spoken = _sentences(captions)
    sentences = [(t, out) for t, s in spoken if (out := impersonal(s))]
    if not sentences:                      # nothing survived the rewrite rules
        sentences = spoken
    sections = _segment(sentences)
    all_text = " ".join(s for _, s in sentences)

    total_words = len(all_text.split())
    # Emphasis has to stay scarce to mean anything: a handful of terms for a
    # short transcript, up to a dozen for a long one, none for a fragment.
    term_count = 0 if total_words < 120 else min(12, max(4, total_words // 250))
    key_terms = _keywords(all_text, term_count) if term_count else []
    lines: list[str] = []
    if key_terms:                       # no terms worth listing -> no Overview
        subjects = key_terms[:6]
        lines.append("## Overview")
        lines.append("")
        lines.append(
            "Subjects covered: "
            + ", ".join(f"**{t}**" for t in subjects[:-1])
            + (f", and **{subjects[-1]}**." if len(subjects) > 1
               else f"**{subjects[0]}**.")
        )
        lines.append("")

    summary = _summary_bullets(
        [s for _, s in sentences], 10 if brief else 7, key_terms
    )
    if summary:
        lines.append("## Summary")
        lines.append("")
        lines.extend(f"- {b}" for b in summary)
        lines.append("")

    if brief:
        if key_terms:
            lines.append("## Key Terms")
            lines.append("")
            lines.extend(f"- **{_titlecase(t)}**" for t in key_terms[:8])
            lines.append("")
        if captions.url:
            lines.append("## Source")
            lines.append("")
            lines.append(f"[{captions.title}]({captions.url})")
        return "\n".join(lines).strip()

    takeaways: list[str] = []
    used_headings: set[str] = set()
    headings: list[str] = []
    overview_at = len(lines)
    for section in sections:
        body = " ".join(s for _, s in section)
        kws = _keywords(body, 4)
        heading = _unique_heading(
            _titlecase(kws[0]) if kws else "Discussion", used_headings, body
        )
        headings.append(heading)
        start = section[0][0]
        lines.append(f"## {heading}")
        jump = timestamp_url(captions.url, start)
        lines.append(
            f"*Starts at [{fmt_ts(start)}]({jump})*" if jump
            else f"*Starts at {fmt_ts(start)}*"
        )
        lines.append("")

        examples = [s for _, s in section if _is_example(s)]
        prose = [s for _, s in section if not _is_example(s)]
        highlighted: set[str] = set()       # bold each term once per section
        for para in _paragraphs(prose):
            lines.append(_highlight(para, key_terms, highlighted))
            lines.append("")
        bullets = _key_sentences(prose, 3)
        if bullets:
            lines.append("**Key points**")
            lines.append("")
            lines.extend(f"- {b}" for b in bullets)
            lines.append("")
            takeaways.append(bullets[0])
        if examples:
            lines.append(f"**Example:** {' '.join(examples)}")
            lines.append("")

    if diagrams and headings:
        lines[overview_at:overview_at] = [
            _topic_mindmap(captions.title, headings),
            "",
        ]

    if takeaways:
        lines.append("## Key Takeaways")
        lines.append("")
        lines.extend(f"- {t}" for t in takeaways[:8])
        lines.append("")

    if key_terms:
        lines.append("## Key Terms")
        lines.append("")
        lines.extend(f"- **{_titlecase(t)}**" for t in key_terms[:10])
        lines.append("")

    if captions.url:
        lines.append("## Source")
        lines.append("")
        lines.append(f"[{captions.title}]({captions.url})")
        lines.append("")
    return "\n".join(lines).strip()


def _is_example(sentence: str) -> bool:
    low = sentence.lower()
    return any(marker in low for marker in EXAMPLE_MARKERS)


def _key_sentences(
    sentences: list[str], count: int, by_score: bool = False
) -> list[str]:
    """Top `count` sentences by keyword density, in document order by default."""
    if not sentences:
        return []
    freq = Counter(
        w
        for w in re.findall(r"[a-z][a-z0-9'-]+", " ".join(sentences).lower())
        if w not in STOPWORDS and len(w) > 2
    )
    scored = []
    for i, sentence in enumerate(sentences):
        words = [w for w in re.findall(r"[a-z][a-z0-9'-]+", sentence.lower())]
        if len(words) < 8 or len(words) > 45 or _is_filler(sentence):
            continue
        score = sum(freq[w] for w in words) / len(words)
        scored.append((score, i, sentence))
    scored.sort(key=lambda x: (-x[0], x[1]))
    top = scored[:count]
    if not by_score:
        top.sort(key=lambda x: x[1])
    return [s for _, _, s in top]


def _summary_bullets(sentences: list[str], count: int, terms: list[str]) -> list[str]:
    """Pick the sentences that carry the most of the document's own vocabulary.

    Candidates are scored by keyword density, then greedily selected while
    skipping any sentence whose content words mostly repeat one already chosen,
    so the bullets cover different ground instead of restating each other.
    """
    ranked = _key_sentences(sentences, len(sentences), by_score=True)
    order = {s: i for i, s in enumerate(sentences)}
    picked: list[str] = []
    seen_words: list[set[str]] = []
    for sentence in ranked:
        words = {
            w for w in re.findall(r"[a-z][a-z0-9'-]+", sentence.lower())
            if w not in STOPWORDS and len(w) > 3
        }
        if not words:
            continue
        if any(len(words & prev) / len(words) > 0.5 for prev in seen_words):
            continue
        picked.append(sentence)
        seen_words.append(words)
        if len(picked) >= count:
            break
    highlighted: set[str] = set()
    return [
        _highlight(s, terms, highlighted)
        for s in sorted(picked, key=lambda s: order.get(s, 0))
    ]


def build_markdown(
    captions: Captions,
    use_llm: bool = True,
    model: str = MODEL,
    diagrams: bool = True,
    brief: bool = False,
) -> str:
    """Return the full Markdown document (title + body).

    `brief` produces a short briefing note - overview, summary bullets and key
    terms - instead of the full topic-by-topic paper.
    """
    body = None
    if use_llm:
        try:
            body = organize_with_claude(
                captions, model=model, diagrams=diagrams, brief=brief
            )
        except Exception as exc:  # noqa: BLE001 - fall back on any API problem
            print(f"  ! Claude unavailable ({exc}); using offline organizer.", file=sys.stderr)
    if body is None:
        body = organize_heuristically(captions, diagrams=diagrams, brief=brief)

    header = [f"# {captions.title}", ""]
    meta = []
    if captions.uploader:
        meta.append(f"**Channel:** {captions.uploader}")
    if captions.duration:
        meta.append(f"**Duration:** {fmt_ts(captions.duration)}")
    if captions.url:
        meta.append(f"**Source:** [{captions.title}]({captions.url})")
    if meta:
        header.append("  \n".join(meta))
        header.append("")
        header.append("")
    return "\n".join(header) + body + "\n"
