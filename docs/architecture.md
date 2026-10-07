# Architecture

How `captions2doc` turns a video link into a Markdown paper and a PDF.

See [output-format.md](output-format.md) for what the generated document looks
like, and the [README](../README.md) to install and run it.

The pipeline has three stages, and every stage has an offline path. Nothing
leaves the machine unless you pass `--claude`.

```mermaid
flowchart LR
    A[Video link<br/>or caption file] --> B[captions.py<br/>acquire + parse]
    B --> C[organize.py<br/>build Markdown]
    C --> D[pdf.py<br/>render PDF]
    B -.->|saves captions| E[("input/")]
    C --> F[("output/*.md")]
    D --> G[("output/*.pdf")]
```

## Modules

| Module | Role |
|---|---|
| `cli.py` | Argument parsing, `input/` → `output/` resolution, folder setup, batch mode, filename sanitization. |
| `captions.py` | `yt-dlp` fetches metadata + subtitles (manual preferred, auto-generated as fallback) and keeps a copy in `input/`. Parses WebVTT/SRT, collapses YouTube's rolling-caption repetition into a linear transcript, and builds source / timestamp deep links. |
| `transcribe.py` | Local Whisper speech-to-text for videos with no published captions, and WebVTT serialization of the result. |
| `organize.py` | Builds the document. The offline organizer segments the transcript, labels topics, scores sentences and surfaces examples. With `--claude`, sends the timestamped transcript to Claude instead (`claude-opus-5`, adaptive thinking, streamed) and falls back to the offline path on any API problem. |
| `prose.py` | Rule-based rewriting of spoken sentences into impersonal reference prose: drops small talk, strips disfluencies and stutters, depersonalizes what it can. Backs the offline organizer. |
| `diagrams.py` | Mermaid parser + ReportLab vector renderer, for diagrams in the PDF. |
| `pdf.py` | Renders the Markdown: headings, bullet/numbered lists, pipe tables, fenced code, blockquotes, rules, inline bold/italic/code/links, diagrams, page numbers, running footer. |
| `textutil.py` | Folds fullwidth punctuation, smart quotes and emoji into what the PDF core fonts can actually draw. |

## Layout

```
src/video_captions/      the package (modules above); entry point video_captions.cli:main
input/                   caption sources (*.vtt, *.srt), including downloaded captions (ignored by git)
output/                  generated *.md / *.pdf / *.transcript.txt (ignored by git)
docs/
  architecture.md        this file
  output-format.md       rules for the generated document
  specs.md               the app's goal and requirements (input, output, document rules)
tests/test_pipeline.py   plain unittest suite, no pytest
.github/workflows/
  ci.yml                 tests and a wheel build on every push
  release.yml            on a v* tag: build, check the version, smoke-test, publish a Release
pyproject.toml           package metadata, dependencies and the [transcribe] / [mermaid] extras
```

## Stage 1 — where the captions come from

`cli.main` accepts exactly one of a URL, `--from-vtt`, or `--from-media`; with
none of them it converts every caption file in `input/` as a batch.

```mermaid
flowchart TD
    A[captions2doc invoked] --> B{Which input?}
    B -->|URL| C[fetch_from_url]
    B -->|--from-vtt| D[fetch_from_file]
    B -->|--from-media| E[fetch_from_media]
    B -->|nothing| F[list_input_files]
    F --> G{Any .vtt / .srt?}
    G -->|no| H[Exit 2<br/>drop files or pass a URL]
    G -->|yes| I[Batch: one document per file]
    I --> D
    C --> J[Captions object]
    D --> J
    E --> J
```

A batch keeps going when one file fails: the error is reported per file and the
exit code is 1 if any failed.

### Acquiring captions from a URL

Published captions are always preferred over transcription — it is faster, more
accurate, and free. Whisper is the fallback, not the default.

```mermaid
flowchart TD
    A[fetch_from_url] --> B[yt-dlp: metadata + subtitles]
    B --> C{Subtitle track found?}
    C -->|manual| D[Use manual subtitles]
    C -->|auto-generated| E[Use auto subtitles]
    C -->|none| F{--no-transcribe?}
    F -->|yes| G[Exit: no captions available]
    F -->|no| H[transcribe.py<br/>download audio + Whisper]
    D --> I[parse_vtt]
    E --> I
    H --> J[cues_to_vtt]
    J --> I
    I --> K[Collapse rolling repetition<br/>strip tags, markers, disfluencies]
    K --> L[Save .vtt to input/]
    L --> M[Captions object]
```

`--transcribe` forces the Whisper path even when captions exist. The saved
`.vtt` is what makes a rerun free, and it can be hand-edited before rerunning.

## Stage 2 — building the document

**The offline organizer is the default.** Claude is opt-in via `--claude`, and
even then a failure degrades to the offline path rather than aborting the run.

```mermaid
flowchart TD
    A[build_markdown] --> B{--claude passed?}
    B -->|no| C[organize_heuristically]
    B -->|yes| D[estimate_input_cost<br/>count_tokens, free]
    D --> E[organize_with_claude<br/>streamed, adaptive thinking]
    E --> F{Succeeded?}
    F -->|no| G[Warn on stderr]
    G --> C
    F -->|yes| H[Markdown body]
    C --> H
    H --> I[Prepend title, channel,<br/>duration, source link]
    I --> J[output/TITLE.md]
```

### Inside the offline organizer

No model, no network. Sentences are scored by keyword density and de-duplicated
so the bullets cover different ground.

```mermaid
flowchart LR
    A[Cues] --> B[_sentences]
    B --> C[prose.impersonal<br/>strip small talk + disfluencies]
    C --> D[_segment<br/>group into topics]
    D --> E[_keywords<br/>scale emphasis to length]
    E --> F[Overview, Summary,<br/>topics, Takeaways, Key Terms]
```

Emphasis is scaled to transcript length — roughly one key term per 250 words,
capped at 12, and none at all under 120 words — so bold stays scarce enough to
mean something.

## Stage 3 — rendering the PDF

Markdown diagrams are plain ` ```mermaid ` blocks, which GitHub, VS Code and
Obsidian render natively. The PDF has no browser, so each diagram walks a
three-step fallback chain and a diagram can never break the document.

```mermaid
flowchart TD
    A[mermaid_to_flowable] --> B{mmdc on PATH?}
    B -->|yes| C[render_with_mmdc<br/>every diagram type]
    C --> D[PNG flowable]
    B -->|no| E[parse_mermaid]
    E --> F{Supported form?}
    F -->|flowchart TD/LR<br/>mindmap| G[graph_to_drawing<br/>vector graphics]
    F -->|anything else| H[Fall back to<br/>source in a code block]
    G --> I{Layout raised?}
    I -->|yes| H
    I -->|no| J[Vector flowable]
```

The built-in renderer does layered layout with barycentre-ordered nodes to
reduce edge crossings, and supports rectangle / rounded / stadium / diamond /
circle shapes, solid and dashed edges, and edge labels.

## Cost model

One `--claude` run is a **single** request — no per-section calls, no retry
loop. Before it fires, `estimate_input_cost` prints the input token count and
price using the free `count_tokens` endpoint. Output cannot be priced ahead of
generation and is the larger share of the bill, so the estimate says so rather
than implying it is the total.

Everything else — yt-dlp, Whisper, the offline organizer, Mermaid parsing, PDF
rendering — is local compute and costs nothing.

## Folder handling

`input/` is created eagerly at startup; `output/` is created lazily, only once a
document is about to be written, so an aborted run leaves no empty folder
behind. Both go through `ensure_dir`, which reports a path conflict in words
rather than leaking an errno.

## Design trade-offs

`captions2doc` deliberately favours **fast to deploy and simple to update** over a heavier
design: one `pip` / `pipx` install, offline by default, and every optional piece degrades
gracefully instead of being required.

| Choice | What it buys | What it gives up |
|---|---|---|
| Offline extractive organizer by default; Claude only with `--claude` | Free, private, no API key; works with no network once captions are saved | Bullets are real sentences selected from the transcript, not prose rewritten for the purpose |
| `yt-dlp` for links and captions | Any site yt-dlp supports, with no API keys or scraping code of our own | Relies on yt-dlp keeping up with site changes; when a site changes, update yt-dlp |
| Published captions first, Whisper only as a fallback | Fast and free for most videos | Auto-generated captions can be poor; `--transcribe` trades speed for accuracy |
| Built-in Mermaid → ReportLab renderer instead of requiring mermaid-cli | No Node.js or headless browser to install | Only flowchart TD/LR and mindmap are drawn; other diagram types appear as source in a code block |
| ReportLab with the PDF core fonts | Pure Python and cross-platform; no LaTeX or HTML-to-PDF engine | Emoji, smart quotes and fullwidth punctuation are folded to plain equivalents by `textutil.py` |
| One Claude request per run | Predictable cost, estimated before it is sent | No per-section refinement or retry; on any failure it falls back to the offline organizer |
| Caption files saved in `input/` as the cache | Reruns are free, and captions can be hand-edited before converting | `input/` grows with every new link; `--no-keep-vtt` skips saving, or delete old files by hand |

These are easy to revisit one at a time if a limit starts to hurt.
