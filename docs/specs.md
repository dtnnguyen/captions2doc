Application goal: Extract caption from videos to text should use no AI since it's using yt-dlp, which takes care of the video already has caption otherwise generates caption and text. Overview and summary will be generated and need no LLM. User can apply AI to the output markdown file to do their own analysis.


-----

**Input — get the captions**
- Take a web link and extract its captions (yt-dlp, so any site it supports).
- Prefer published captions; use manual subtitles over auto-generated ones.
- If the video has no captions, download the audio only and transcribe it locally with Whisper. No AI service, nothing leaves the machine.
- Also accept a local caption file (`--from-vtt`) or a local video/audio file to transcribe (`--from-media`).
- Collapse the rolling-caption repetition YouTube emits into a linear transcript; strip caption tags, speaker markers (`>>`, `[MUSIC]`), disfluencies and stutters.
- Save the captions into `input/` as a `.vtt` — downloaded or generated — so a rerun is free and they can be hand-edited first.

**Document — an info paper, not a talk**
- Convert the captions into a Markdown file, organized by topic rather than by timeline.
- Read like a reference paper: impersonal third person, no "I", "we", "our", "you", no addressing the reader.
- No small talk: greetings, introductions, thanks, sign-offs, promotion, "in this video", and announce-what's-next sentences are removed.
- Straight to the point, declarative present tense; state the subject as fact.
- Include an example block where the video actually works one through.
- Highlight important keywords in bold, scaled to the length of the transcript so emphasis stays scarce; list them in a Key Terms section.
- Structure: Overview → Summary → topic sections → Key Takeaways → Key Terms → Source.
- Add diagrams (Mermaid) where a picture carries the idea better than prose.
- Link back to the video: a source link, plus a per-topic timestamp deep link.
- `--summary` writes a short briefing note instead of the full paper.

**Output**
- Create a PDF alongside the Markdown, with the diagrams drawn as real vector graphics.
- Markdown file and pdf file have the same name as the video's title.
- Write both into `output/`.

**AI is optional**
- Everything above runs with plain Python — no API key, no cost.
- Overview and summary are extractive: real sentences scored by keyword density, de-duplicated so the bullets cover different ground.
- `--claude` opts in to rewriting the document with Claude instead (billed per use). Missing key falls back to the built-in organizer rather than failing.
- The Markdown output is the handoff point for the user's own AI analysis.

**Packaging**
- Installable package with a `captions2doc` command (`pip install -e .`), not a loose script.
- Unit tests covering caption parsing, prose rewriting, diagram parsing, summaries and PDF output.
