"""Turn a video's captions into an organized Markdown document and a PDF."""

from .captions import Captions, fetch_from_file, fetch_from_url
from .organize import build_markdown
from .pdf import write_pdf

__version__ = "1.0.0"
__all__ = [
    "Captions",
    "build_markdown",
    "fetch_from_file",
    "fetch_from_url",
    "write_pdf",
]
