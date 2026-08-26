# The generated document

What `captions2doc` produces: how the prose is rewritten, how the document links back
to the video, and how diagrams are drawn.

See [architecture.md](architecture.md) for how the code is put together, and the
[README](../README.md) to install and run it.

## Reference paper, not a transcript

The document reads as an informational paper about the subject, not a record of
someone talking about it:

- **No first or second person.** No "I", "we", "our", "you" - and no addressing the
  reader.
- **No small talk.** Greetings, introductions, thanks, sign-offs, promotion, "in this
  video", "as we saw earlier", and sentences whose only job is announcing what comes
  next are all removed.
- **Straight to the point.** Declarative present tense, subject stated as fact:
  *"The control plane computes routes"*, not *"Now let's look at how the control plane
  computes routes"*.
- **Key terms highlighted** in bold on first mention so the document can be skimmed,
  plus a `## Key Terms` section at the end. In the PDF, highlighted terms are drawn in
  the accent colour.

By default this is done offline by `prose.py`, which applies an explicit
rule table - drop conversational sentences, strip discourse markers ("So,", "Now,",
"Basically,"), rewrite safe frames (*"we can add capacity"* -> *"it is possible to add
capacity"*, *"let's say"* -> *"suppose"*, *"think of it like"* -> *"this is analogous
to"*) - and drops anything still carrying a personal pronoun rather than inventing a
paraphrase. With `--claude` the same result comes from rewriting instead.

## Source links

Every document points back at the video it came from:

- a **Source** link under the title,
- a **jump link on each topic** (`[Watch from 4:12](...&t=252s)`) so a section in your
  notes takes you to that moment in the video,
- a **Source** section at the end.

Deep links are generated for YouTube (`&t=252s`) and Vimeo (`#t=252s`); other hosts get
the plain link. When you convert a local caption file, the link is recovered from
yt-dlp's `Title [VIDEOID].en.vtt` filename, or you can supply it with `--url`.

## Diagrams

The offline organizer emits a `mindmap` of the topic headings. With `--claude`, a
diagram is added wherever a picture carries the idea better than prose - an
architecture, a flow, a decision, a lifecycle, a topic hierarchy. `--no-diagrams`
turns both off.

In the Markdown these are plain ```` ```mermaid ```` blocks, so GitHub, VS Code,
Obsidian and Claude render them natively. The PDF has no browser available, so
`diagrams.py` parses the diagram and draws it as **native vector graphics** with
ReportLab: layered layout, barycentre-ordered nodes to reduce edge crossings, rectangle
/ rounded / stadium / diamond / circle shapes, solid and dashed edges, and edge labels.

Supported forms: `flowchart TD`, `flowchart LR`, and `mindmap`. If
[mermaid-cli](https://github.com/mermaid-js/mermaid-cli) (`mmdc`) is on `PATH` it is
used instead and every diagram type works. Anything unsupported degrades to its source
text in a code block rather than breaking the document.
