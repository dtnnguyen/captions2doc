#!/usr/bin/env python3
"""Video captions -> organized Markdown + PDF.

Folder layout:
    input/   caption sources (*.vtt, *.srt) - downloads are saved here too
    output/  generated documents (*.md, *.pdf, optional *.transcript.txt)

Usage:
    captions2doc                          # convert every file in ./input
    captions2doc "https://youtu.be/..."   # download captions, then convert
    captions2doc --from-vtt captions.en.vtt
"""

from __future__ import annotations

import argparse
import glob
import os
import re
import sys
import unicodedata

from . import captions as caption_extract
from .captions import Captions
from .organize import MODEL, build_markdown
from .pdf import write_pdf
from .transcribe import DEFAULT_MODEL as DEFAULT_WHISPER
from .transcribe import MODEL_SIZES

# A console using a legacy code page (Windows cp1252) cannot print every title;
# degrade those characters rather than crashing on the progress output.
# Line buffering keeps progress (stdout) and errors (stderr) in the order they
# happened when the run is piped to a file - block-buffered stdout would other-
# wise flush after the unbuffered error that refers to it.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(errors="backslashreplace", line_buffering=True)
        except (ValueError, OSError):    # pragma: no cover - already unusable
            pass

HERE = os.getcwd()
INPUT_DIR = os.path.join(HERE, "input")
OUTPUT_DIR = os.path.join(HERE, "output")

INVALID = r'<>:"/\\|?*'
CAPTION_EXTS = (".vtt", ".srt")

# Device names Windows refuses as filenames, whatever the extension.
RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def display(path: str) -> str:
    """Relative path when it lives under the project, absolute otherwise."""
    path = os.path.abspath(path)
    return os.path.relpath(path, HERE) if path.startswith(HERE + os.sep) else path


def ensure_dir(path: str, label: str) -> None:
    """Create a folder, explaining the conflict rather than leaking an errno."""
    if os.path.isdir(path):
        return
    if os.path.exists(path):
        raise RuntimeError(
            f"{label} path is a file, not a folder: {display(path)} - "
            "move or rename it, or pass a different path"
        )
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(
            f"cannot create {label} folder {display(path)}: {exc.strerror}"
        ) from exc


def safe_filename(title: str, max_len: int = 120) -> str:
    name = unicodedata.normalize("NFC", title)
    name = "".join("-" if ch in INVALID else ch for ch in name)
    name = "".join(ch for ch in name if ch.isprintable())
    name = re.sub(r"\s+", " ", name).strip(" .-_")
    name = "".join(ch for ch in name if ord(ch) >= 32)   # control chars
    if len(name) > max_len:
        name = name[:max_len].rsplit(" ", 1)[0].strip()
    name = name.rstrip(" .")            # Windows drops trailing dots and spaces
    if name.upper() in RESERVED or name.upper().split(".")[0] in RESERVED:
        name = f"{name}-video"
    return name or "video-captions"


def resolve_input(path: str, indir: str) -> str:
    """Accept a bare filename (looked up in the input folder) or any real path."""
    if os.path.isfile(path):
        return path
    candidate = os.path.join(indir, path)
    if os.path.isfile(candidate):
        return candidate
    raise SystemExit(f"Caption file not found: {path} (also looked in {indir}/)")


def list_input_files(indir: str) -> list[str]:
    files: list[str] = []
    for ext in CAPTION_EXTS:
        files += glob.glob(os.path.join(indir, f"*{ext}"))
    return sorted(files)


def write_documents(captions: Captions, args: argparse.Namespace) -> None:
    words = len(captions.plain_text.split())
    print(f"   {captions.title!r} - {len(captions.cues)} cues, {words:,} words")

    what = "Summarizing" if args.summary else "Organizing into topics"
    print(f"-> {what}" + (f" with {args.model}" if args.claude else ""))
    markdown = build_markdown(
        captions,
        use_llm=args.claude,
        model=args.model,
        diagrams=not args.no_diagrams,
        brief=args.summary,
    )

    ensure_dir(args.outdir, "output")
    stem = safe_filename(captions.title)
    if args.summary:
        stem += " (summary)"

    md_path = os.path.join(args.outdir, f"{stem}.md")
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(markdown)
    print(f"   wrote {display(md_path)}")

    if args.save_transcript:
        txt_path = os.path.join(args.outdir, f"{stem}.transcript.txt")
        with open(txt_path, "w", encoding="utf-8") as fh:
            fh.write(captions.timestamped_text + "\n")
        print(f"   wrote {display(txt_path)}")

    if not args.no_pdf:
        pdf_path = os.path.join(args.outdir, f"{stem}.pdf")
        write_pdf(markdown, pdf_path, title=captions.title)
        print(f"   wrote {display(pdf_path)}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Extract a video's captions and turn them into an organized "
                    "Markdown file (with Mermaid diagrams) and a matching PDF. "
                    "Runs entirely offline; pass --claude to rewrite with AI. "
                    "With no arguments, every caption file in ./input is converted.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("url", nargs="?", help="Web link to the video")
    p.add_argument("--from-vtt", metavar="FILE",
                   help="Caption file to convert (bare names resolve inside input/)")
    p.add_argument("--from-media", metavar="FILE",
                   help="Local audio/video file to transcribe locally, then convert")
    p.add_argument("-i", "--indir", default=INPUT_DIR,
                   help="Folder holding caption files (default: ./input)")
    p.add_argument("-o", "--outdir", default=OUTPUT_DIR,
                   help="Folder for generated documents (default: ./output)")
    p.add_argument("-l", "--lang", default="en", help="Caption language (default: en)")
    p.add_argument("--title", help="Override the video title used for filenames")
    p.add_argument("--url", dest="source_url", metavar="LINK",
                   help="Video link to reference from the document when converting "
                        "a local caption file (recovered from the filename for "
                        "yt-dlp downloads)")
    p.add_argument("--claude", action="store_true",
                   help="Rewrite the document with Claude instead of the built-in "
                        "organizer (needs ANTHROPIC_API_KEY; billed per use)")
    p.add_argument("--model", default=MODEL,
                   help=f"Claude model, with --claude (default: {MODEL})")
    p.add_argument("--no-llm", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--no-pdf", action="store_true", help="Write only the Markdown file")
    p.add_argument("--no-diagrams", action="store_true",
                   help="Do not ask for / generate Mermaid diagrams")
    p.add_argument("-s", "--summary", action="store_true",
                   help="Write a short briefing note (overview, summary bullets, "
                        "key terms) instead of the full topic-by-topic paper")
    p.add_argument("--no-keep-vtt", action="store_true",
                   help="Do not save downloaded captions into input/")
    p.add_argument("--transcribe", action="store_true",
                   help="Transcribe the audio locally even if captions exist")
    p.add_argument("--no-transcribe", action="store_true",
                   help="Fail instead of transcribing when a video has no captions")
    p.add_argument("--whisper-model", default=DEFAULT_WHISPER, choices=MODEL_SIZES,
                   help=f"Local speech-to-text model size (default: {DEFAULT_WHISPER})")
    p.add_argument("--save-transcript", action="store_true",
                   help="Also write the raw de-duplicated transcript as .txt")
    args = p.parse_args(argv)

    given = [bool(args.url), bool(args.from_vtt), bool(args.from_media)]
    if sum(given) > 1:
        p.error("give only one of: a URL, --from-vtt, or --from-media")

    indir = os.path.abspath(args.indir)
    args.outdir = os.path.abspath(args.outdir)
    try:
        ensure_dir(indir, "input")
    except RuntimeError as exc:
        raise SystemExit(f"captions2doc: {exc}") from exc

    if args.url:
        print(f"-> Fetching captions for {args.url}")
        captions = caption_extract.fetch_from_url(
            args.url,
            lang=args.lang,
            save_dir=None if args.no_keep_vtt else indir,
            transcribe=not args.no_transcribe,
            whisper_model=args.whisper_model,
            force_transcribe=args.transcribe,
        )
        if args.title:
            captions.title = args.title
        if args.source_url:
            captions.url = args.source_url
        write_documents(captions, args)
        return 0

    if args.from_media:
        media = resolve_input(args.from_media, indir)
        print(f"-> Transcribing {display(media)}")
        captions = caption_extract.fetch_from_media(
            media, lang=args.lang, whisper_model=args.whisper_model,
            title=args.title, url=args.source_url,
        )
        if not args.no_keep_vtt:
            from .transcribe import cues_to_vtt

            ensure_dir(indir, "input")
            kept = os.path.join(indir, f"{safe_filename(captions.title)}.{args.lang}.vtt")
            with open(kept, "w", encoding="utf-8") as fh:
                fh.write(cues_to_vtt(captions.cues))
            print(f"   saved captions to {display(kept)}")
        write_documents(captions, args)
        return 0

    if args.from_vtt:
        sources = [resolve_input(args.from_vtt, indir)]
    else:
        sources = list_input_files(indir)
        if not sources:
            p.error(
                f"no .vtt/.srt files in {display(indir)}/ - "
                "pass a video URL or drop caption files there"
            )
        print(f"-> {len(sources)} caption file(s) in {display(indir)}/")

    failures = 0
    for path in sources:
        print(f"-> Reading {display(path)}")
        try:
            captions = caption_extract.fetch_from_file(
                path, title=args.title, url=args.source_url
            )
            write_documents(captions, args)
        except Exception as exc:  # noqa: BLE001 - keep batch going
            failures += 1
            print(f"   ! failed: {exc}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
