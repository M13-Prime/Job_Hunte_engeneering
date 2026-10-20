"""Plain exports of the tailored documents (Markdown CV, text letter)."""

from __future__ import annotations

import re

from signal_tracker.applications.schemas import TailoredCV

_LABELS = {
    "fr": {
        "summary": "Profil", "skills": "Compétences", "experience": "Expérience",
        "education": "Formation", "languages": "Langues",
        "certifications": "Certifications",
    },
    "en": {
        "summary": "Summary", "skills": "Skills", "experience": "Experience",
        "education": "Education", "languages": "Languages",
        "certifications": "Certifications",
    },
}


def section_labels(language: str) -> dict[str, str]:
    return _LABELS.get(language, _LABELS["fr"])


def cv_to_markdown(cv: TailoredCV, language: str = "fr") -> str:
    labels = section_labels(language)
    lines: list[str] = []
    if cv.name:
        lines.append(f"# {cv.name}")
    lines.append(f"**{cv.headline}**")
    if cv.contact_line:
        lines.append(cv.contact_line)
    lines += ["", f"## {labels['summary']}", cv.summary]
    if cv.skills:
        lines += ["", f"## {labels['skills']}", " · ".join(cv.skills)]
    if cv.experiences:
        lines += ["", f"## {labels['experience']}"]
        for exp in cv.experiences:
            head = f"### {exp.title} — {exp.company}"
            meta = " · ".join(p for p in (exp.period, exp.location) if p)
            lines += ["", head]
            if meta:
                lines.append(f"*{meta}*")
            lines += [f"- {b}" for b in exp.bullets]
    for key, items in (
        ("education", cv.education),
        ("languages", cv.languages),
        ("certifications", cv.certifications),
    ):
        if items:
            lines += ["", f"## {labels[key]}", *(f"- {i}" for i in items)]
    return "\n".join(lines).strip() + "\n"


def safe_filename(*parts: str) -> str:
    """ASCII-only, filesystem-safe slug for Content-Disposition."""
    raw = "-".join(p for p in parts if p)
    slug = re.sub(r"[^A-Za-z0-9]+", "-", raw).strip("-").lower()
    return slug[:80] or "document"


__all__ = ["cv_to_markdown", "safe_filename", "section_labels"]
