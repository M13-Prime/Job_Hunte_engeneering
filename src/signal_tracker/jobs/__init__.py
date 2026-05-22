"""Module 2 — scrape job offers from companies discovered in signals.

Architecture:
- ``schemas.py``         : Pydantic models for JobPosting and per-company result.
- ``slug.py``            : heuristics that turn a company name into ATS slugs.
- ``relevance.py``       : score a posting against the user profile.
- ``backends/``          : one ATS adapter per provider (Greenhouse, Lever,
                           Workable, Ashby). All extend ``JobsBackend``.
- ``scraper.py``         : orchestrator that tries each (slug, backend) pair
                           with a shared httpx client, file cache and dedup.

The pipeline is independent of the signal classifier: run ``make jobs`` to
trigger it on its own schedule.
"""
