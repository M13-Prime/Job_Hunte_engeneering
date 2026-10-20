"""Phase 12 — application tracker + tailored CV / cover letter.

- ``schemas.py`` : TailoredCV / TailoredDocuments Pydantic models.
- ``tailor.py``  : one LLM call → CV rewritten for a posting + cover letter,
                   using only facts present in the source CV.
- ``render.py``  : Markdown / plain-text export of the tailored documents.
- ``tracker.py`` : status transitions (kanban) and follow-up dates.
"""
