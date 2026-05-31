"""CV text extraction. Accepts PDF bytes or raw text."""

from __future__ import annotations

import io


class CVExtractionError(ValueError):
    """Raised when we cannot extract usable text from the uploaded file."""


def extract_text_from_pdf(data: bytes) -> str:
    """Best-effort plain-text extraction from a PDF.

    Uses pypdf (already added to deps). If the PDF is image-only (scanned),
    we return whatever is found (often empty) and the caller can ask the user
    to paste their CV instead.
    """
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover
        raise CVExtractionError(
            "pypdf not installed — add it to dependencies"
        ) from exc

    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception as exc:
        raise CVExtractionError(f"Invalid PDF file: {exc}") from exc

    parts: list[str] = []
    for page in reader.pages:
        try:
            parts.append(page.extract_text() or "")
        except Exception:
            continue
    text = "\n".join(parts).strip()
    return text


def extract_text(filename: str, data: bytes) -> str:
    """Dispatch by extension. Falls back to UTF-8 decode for .txt."""
    name = (filename or "").lower()
    if name.endswith(".pdf"):
        return extract_text_from_pdf(data)
    # Treat .txt and unknown as raw text
    try:
        return data.decode("utf-8", errors="ignore").strip()
    except Exception as exc:
        raise CVExtractionError(f"Could not decode {filename!r}: {exc}") from exc
