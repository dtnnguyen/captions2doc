"""Generate captions from a video's audio when it has none published.

This is local speech-to-text (Whisper), not a hosted service: no API key, no
network calls beyond the audio download, and nothing leaves the machine. It is
an optional extra because the model weights are large - install with:

    pip install '.[transcribe]'

Backends are tried in order of preference: faster-whisper (fastest, CTranslate2),
the reference openai-whisper package, then a `whisper` binary on PATH.
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import tempfile

from .captions import Cue

DEFAULT_MODEL = "base"
MODEL_SIZES = ("tiny", "base", "small", "medium", "large-v3")

INSTALL_HINT = (
    "No speech-to-text backend found. Install one with:\n"
    "    pip install 'video-captions-to-doc[transcribe]'\n"
    "(needs ffmpeg on PATH, which you already have if yt-dlp merges formats.)"
)


def available_backend() -> str | None:
    """Which local Whisper implementation is usable, if any."""
    try:
        import faster_whisper  # noqa: F401
        return "faster-whisper"
    except ImportError:
        pass
    try:
        import whisper  # noqa: F401
        return "openai-whisper"
    except ImportError:
        pass
    return "whisper-cli" if shutil.which("whisper") else None


def download_audio(url: str, out_dir: str) -> str:
    """Fetch the audio track only - far smaller and faster than the video."""
    import yt_dlp

    opts = {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(out_dir, "audio.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.extract_info(url, download=True)
    files = [f for f in glob.glob(os.path.join(out_dir, "audio.*"))]
    if not files:
        raise SystemExit("Could not download the audio track for transcription.")
    return files[0]


def _cues_from_faster_whisper(path: str, lang: str, model_size: str) -> list[Cue]:
    from faster_whisper import WhisperModel

    model = WhisperModel(model_size, device="auto", compute_type="int8")
    segments, _info = model.transcribe(path, language=lang or None, vad_filter=True)
    return [
        Cue(seg.start, seg.end, seg.text.strip())
        for seg in segments
        if seg.text and seg.text.strip()
    ]


def _cues_from_openai_whisper(path: str, lang: str, model_size: str) -> list[Cue]:
    import whisper

    model = whisper.load_model(model_size)
    result = model.transcribe(path, language=lang or None, verbose=False)
    return [
        Cue(float(seg["start"]), float(seg["end"]), seg["text"].strip())
        for seg in result.get("segments", [])
        if seg.get("text", "").strip()
    ]


def _cues_from_whisper_cli(path: str, lang: str, model_size: str) -> list[Cue]:
    with tempfile.TemporaryDirectory() as tmp:
        cmd = ["whisper", path, "--model", model_size, "--output_format", "json",
               "--output_dir", tmp, "--verbose", "False"]
        if lang:
            cmd += ["--language", lang]
        subprocess.run(cmd, check=True, capture_output=True)
        files = glob.glob(os.path.join(tmp, "*.json"))
        if not files:
            raise SystemExit("whisper produced no output.")
        with open(files[0], encoding="utf-8") as fh:
            data = json.load(fh)
    return [
        Cue(float(seg["start"]), float(seg["end"]), seg["text"].strip())
        for seg in data.get("segments", [])
        if seg.get("text", "").strip()
    ]


def transcribe_media(
    path: str, lang: str = "en", model_size: str = DEFAULT_MODEL
) -> list[Cue]:
    """Transcribe an audio or video file into cues."""
    backend = available_backend()
    if backend is None:
        raise SystemExit(INSTALL_HINT)
    if model_size not in MODEL_SIZES:
        raise SystemExit(
            f"Unknown model size {model_size!r}; choose from {', '.join(MODEL_SIZES)}."
        )
    handler = {
        "faster-whisper": _cues_from_faster_whisper,
        "openai-whisper": _cues_from_openai_whisper,
        "whisper-cli": _cues_from_whisper_cli,
    }[backend]
    print(f"   transcribing with {backend} ({model_size} model) - this takes a while")
    cues = handler(path, lang, model_size)
    if not cues:
        raise SystemExit("Transcription produced no speech.")
    return cues


def transcribe_url(
    url: str, lang: str = "en", model_size: str = DEFAULT_MODEL
) -> list[Cue]:
    """Download a video's audio and transcribe it."""
    if available_backend() is None:
        raise SystemExit(INSTALL_HINT)
    with tempfile.TemporaryDirectory() as tmp:
        print("   no published captions - downloading audio")
        audio = download_audio(url, tmp)
        return transcribe_media(audio, lang=lang, model_size=model_size)


def _vtt_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    ms = int(round((seconds - int(seconds)) * 1000))
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def cues_to_vtt(cues: list[Cue]) -> str:
    """Serialize cues as WebVTT so generated captions are reusable like any other."""
    out = ["WEBVTT", ""]
    for cue in cues:
        out.append(f"{_vtt_time(cue.start)} --> {_vtt_time(cue.end)}")
        out.append(cue.text)
        out.append("")
    return "\n".join(out)
