"""Caption extraction: pull a video's title and subtitles from a web link.

Uses yt-dlp, so anything yt-dlp supports (YouTube, Vimeo, etc.) works.
"""

from __future__ import annotations

import glob
import html
import os
import re
import tempfile
from dataclasses import dataclass

from .textutil import normalize_title


@dataclass
class Cue:
    start: float
    end: float
    text: str


@dataclass
class Captions:
    title: str
    url: str
    uploader: str
    duration: float
    cues: list[Cue]

    @property
    def plain_text(self) -> str:
        return "\n".join(c.text for c in self.cues)

    @property
    def timestamped_text(self) -> str:
        return "\n".join(f"[{fmt_ts(c.start)}] {c.text}" for c in self.cues)


def fmt_ts(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"


_TS_RE = re.compile(
    r"(\d{2,}):(\d{2}):(\d{2})[.,](\d{3})\s*-->\s*(\d{2,}):(\d{2}):(\d{2})[.,](\d{3})"
)
_TAG_RE = re.compile(r"<[^>]+>")


def _to_seconds(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def parse_vtt(raw: str) -> list[Cue]:
    """Parse WebVTT/SRT text into de-duplicated cues.

    YouTube auto-captions repeat the previous line in every cue to create the
    rolling-caption effect; those repeats are dropped here.
    """
    cues: list[Cue] = []
    block_lines: list[str] = []
    start = end = 0.0
    have_time = False

    def flush() -> None:
        nonlocal block_lines, have_time
        if have_time and block_lines:
            text = " ".join(block_lines).strip()
            if text:
                cues.append(Cue(start, end, text))
        block_lines = []
        have_time = False

    for line in raw.splitlines():
        line = line.rstrip()
        m = _TS_RE.search(line)
        if m:
            flush()
            start = _to_seconds(*m.group(1, 2, 3, 4))
            end = _to_seconds(*m.group(5, 6, 7, 8))
            have_time = True
            continue
        if not line:
            flush()
            continue
        if not have_time:
            continue
        cleaned = html.unescape(_TAG_RE.sub("", line)).strip()
        cleaned = re.sub(r"^>>+\s*", "", cleaned)      # speaker-change marker
        cleaned = re.sub(r"^-\s+(?=[A-Z])", "", cleaned)  # dashed speaker turn
        cleaned = re.sub(r"^\[[^\]]{1,30}\]\s*", "", cleaned)  # [MUSIC], [APPLAUSE]
        cleaned = re.sub(r"\s+", " ", cleaned)
        if cleaned:
            block_lines.append(cleaned)
    flush()

    return _dedupe(cues)


def _dedupe(cues: list[Cue]) -> list[Cue]:
    """Collapse rolling-caption repetition into a linear transcript."""
    out: list[Cue] = []
    seen_tail = ""
    for cue in cues:
        text = cue.text.strip()
        if not text:
            continue
        if out and text == out[-1].text:
            out[-1].end = cue.end
            continue
        # Rolling captions: the new cue often starts with the tail of the old one.
        if seen_tail and text.startswith(seen_tail):
            text = text[len(seen_tail):].strip()
        elif out:
            prev = out[-1].text
            overlap = _longest_overlap(prev, text)
            if overlap >= 12:
                text = text[overlap:].strip()
        if not text:
            out[-1].end = cue.end
            continue
        out.append(Cue(cue.start, cue.end, text))
        seen_tail = text
    return out


def _longest_overlap(prev: str, cur: str) -> int:
    """Length of the longest suffix of `prev` that is a prefix of `cur`."""
    limit = min(len(prev), len(cur))
    for size in range(limit, 0, -1):
        if prev.endswith(cur[:size]):
            return size
    return 0


def _safe_stem(title: str, max_len: int = 120) -> str:
    name = "".join("-" if ch in r'<>:"/\\|?*' else ch for ch in title)
    name = "".join(ch for ch in name if ch.isprintable())
    name = re.sub(r"\s+", " ", name).strip(" .-_")
    if len(name) > max_len:
        name = name[:max_len].rsplit(" ", 1)[0].strip()
    return name or "captions"


def _pick_subtitle_file(directory: str, langs: list[str]) -> str | None:
    files = sorted(glob.glob(os.path.join(directory, "*.vtt")))
    files += sorted(glob.glob(os.path.join(directory, "*.srt")))
    if not files:
        return None
    for lang in langs:
        for path in files:
            if f".{lang}." in os.path.basename(path):
                return path
    return files[0]


def fetch_from_url(
    url: str,
    lang: str = "en",
    save_dir: str | None = None,
    transcribe: bool = True,
    whisper_model: str = "base",
    force_transcribe: bool = False,
) -> Captions:
    """Download subtitles + metadata for `url` using yt-dlp.

    Published captions are preferred (manual over auto-generated). When the
    video has none - or `force_transcribe` is set - the audio is transcribed
    locally with Whisper instead. Either way the captions are kept in
    `save_dir` as a .vtt, named after the video title, so a rerun is free.
    """
    try:
        import yt_dlp
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(
            "yt-dlp is required. Install it with: pip install -r requirements.txt"
        ) from exc

    langs = [lang, f"{lang}-orig", f"{lang}.*", "en"]
    with tempfile.TemporaryDirectory() as tmp:
        opts = {
            "skip_download": True,
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": [lang, f"{lang}.*", "en", "en.*"],
            "subtitlesformat": "vtt/srt/best",
            "outtmpl": os.path.join(tmp, "captions.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)

        path = None if force_transcribe else _pick_subtitle_file(tmp, langs)
        if path is not None:
            with open(path, encoding="utf-8", errors="replace") as fh:
                raw = fh.read()
            cues = parse_vtt(raw)
            ext = os.path.splitext(path)[1]
        else:
            if not transcribe:
                raise SystemExit(
                    f"No captions available for this video (language '{lang}'). "
                    "Drop --no-transcribe to generate them from the audio."
                )
            from .transcribe import cues_to_vtt, transcribe_url

            cues = transcribe_url(url, lang=lang, model_size=whisper_model)
            raw = cues_to_vtt(cues)
            ext = ".vtt"

        if save_dir:
            os.makedirs(save_dir, exist_ok=True)
            title = normalize_title(info.get("title") or "captions")
            stem = _safe_stem(title)
            kept = os.path.join(save_dir, f"{stem}.{lang}{ext}")
            with open(kept, "w", encoding="utf-8") as fh:
                fh.write(raw)
            print(f"   saved captions to {kept}")

    if not cues:
        raise SystemExit("Captions were found but contained no usable text.")

    return Captions(
        title=normalize_title(info.get("title") or "video"),
        url=info.get("webpage_url") or url,
        uploader=(info.get("uploader") or info.get("channel") or "").strip(),
        duration=float(info.get("duration") or (cues[-1].end if cues else 0.0)),
        cues=cues,
    )


def fetch_from_file(
    path: str, title: str | None = None, url: str | None = None
) -> Captions:
    """Load captions from an already-downloaded .vtt/.srt file.

    When `url` is not given, a YouTube id embedded in the filename by yt-dlp
    (`Title [VIDEOID].en.vtt`) is turned back into a watch link so the document
    can still point at its source.
    """
    with open(path, encoding="utf-8", errors="replace") as fh:
        raw = fh.read()
    cues = parse_vtt(raw)
    if not cues:
        raise SystemExit(f"No usable captions found in {path}")
    if title is None:
        base = os.path.basename(path)
        base = re.sub(r"\.(vtt|srt)$", "", base, flags=re.I)
        base = re.sub(r"\.[A-Za-z]{2}(-[A-Za-z]+)?$", "", base)  # strip .en
        base = re.sub(r"\s*\[[A-Za-z0-9_-]{6,}\]\s*$", "", base)  # strip [videoid]
        title = normalize_title(base) or "captions"
    if not url:
        vid = video_id_from_filename(path)
        url = f"https://www.youtube.com/watch?v={vid}" if vid else ""
    return Captions(
        title=title, url=url, uploader="", duration=cues[-1].end, cues=cues
    )


def fetch_from_media(
    path: str,
    lang: str = "en",
    whisper_model: str = "base",
    title: str | None = None,
    url: str | None = None,
) -> Captions:
    """Transcribe a local audio or video file into captions."""
    from .transcribe import transcribe_media

    cues = transcribe_media(path, lang=lang, model_size=whisper_model)
    if title is None:
        title = normalize_title(os.path.splitext(os.path.basename(path))[0])
    return Captions(
        title=title or "recording",
        url=url or "",
        uploader="",
        duration=cues[-1].end,
        cues=cues,
    )


# ---------------------------------------------------------------------------
# Source links
# ---------------------------------------------------------------------------

_YOUTUBE_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


def video_id_from_filename(path: str) -> str | None:
    """Recover a YouTube id from yt-dlp's `Title [VIDEOID].en.vtt` naming."""
    base = os.path.basename(path)
    m = re.search(r"\[([A-Za-z0-9_-]{11})\](?=\.[^.]*(\.[^.]*)?$)", base)
    return m.group(1) if m and _YOUTUBE_ID.match(m.group(1)) else None


def timestamp_url(url: str, seconds: float) -> str | None:
    """A deep link to `seconds` into the video, or None if unsupported."""
    if not url:
        return None
    secs = max(0, int(seconds))
    low = url.lower()
    if "youtube.com" in low or "youtu.be" in low:
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}t={secs}s"
    if "vimeo.com" in low:
        return f"{url}#t={secs}s"
    return None
