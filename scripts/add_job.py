"""Add one job posting from any URL, then optionally AI-score it.

    python scripts/add_job.py URL [--title T] [--company C] [--location L]
                             [--user-id N] [--no-ai]

Reads the page through Jina Reader, stores it as a manual JobOffer and runs
the deterministic prefilter. With a --user-id that has a CV, it also runs
the usual AI scoring so the offer shows up graded on the Jobs page.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from signal_tracker.config import get_settings, load_user_profile
from signal_tracker.jobs.manual import add_job_from_url
from signal_tracker.storage import init_db


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    profile = load_user_profile()
    db = init_db(settings.db_path)
    job = await add_job_from_url(
        db, args.url, profile,
        title=args.title, company=args.company, location=args.location,
    )
    verb = "ajoutée" if job.created else "mise à jour"
    print(f"Offre {verb} #{job.job_offer_id} — {job.title} @ {job.company}")
    print(f"  score heuristique : {job.relevance_score:.0f}/100")
    if job.matched:
        print(f"  correspondances   : {', '.join(job.matched)}")
    if job.dealbreaker:
        print(f"  DEALBREAKER       : {job.dealbreaker!r} (score forcé à 0)")

    if args.no_ai or args.user_id is None:
        return 0

    from signal_tracker.jobs.matching import load_cv_text, score_job_for_user

    cv_text = load_cv_text(db, args.user_id)
    if not cv_text.strip():
        print("  (pas de CV pour cet utilisateur : notation IA ignorée)")
        return 0
    score = await score_job_for_user(
        db, user_id=args.user_id, job_offer_id=job.job_offer_id,
        profile=profile, cv_text=cv_text,
    )
    print(f"  notation IA       : {score}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("--title")
    parser.add_argument("--company")
    parser.add_argument("--location")
    parser.add_argument("--user-id", type=int, dest="user_id")
    parser.add_argument("--no-ai", action="store_true", dest="no_ai")
    return asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
