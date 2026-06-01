"""Bootstrap CLI: create / promote the owner account and claim orphan data.

Phase 8 migration from single-user to multi-tenant. On the VM:

    docker compose -f docker-compose.lite.yml exec dashboard \\
        python scripts/create_owner.py --email you@example.com

You'll be prompted for the password interactively (won't echo). Once the
owner exists, any rows that still have user_id IS NULL (legacy data from
the single-user era) are reassigned to the owner.

Idempotent: rerunning with the same email just updates the password (with
--password) or no-ops.
"""

from __future__ import annotations

import argparse
import getpass
import sys

from sqlalchemy import select, update

from signal_tracker.auth.password import hash_password
from signal_tracker.config import get_settings, resolve_db_url
from signal_tracker.storage import init_db
from signal_tracker.storage.models import (
    Preparation,
    SearchRun,
    Signal,
    SignalFeedback,
    User,
    UserCV,
    UserKeyword,
    WatchlistEntry,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Create or promote the owner user.")
    parser.add_argument("--email", required=True, help="Owner email")
    parser.add_argument(
        "--password",
        help="Owner password. If omitted, you'll be prompted (recommended).",
    )
    parser.add_argument(
        "--no-claim",
        action="store_true",
        help="Skip claiming orphan rows (default: claim NULL user_id rows for the owner).",
    )
    args = parser.parse_args()

    password = args.password or getpass.getpass(f"Password for {args.email}: ")
    if not password:
        print("error: empty password", file=sys.stderr)
        return 2
    if len(password) < 6:
        print("error: password must be at least 6 characters", file=sys.stderr)
        return 2

    db = init_db(resolve_db_url(get_settings()))
    with db.session() as session:
        user = session.execute(
            select(User).where(User.email == args.email)
        ).scalar_one_or_none()
        if user is None:
            user = User(
                email=args.email,
                password_hash=hash_password(password),
                is_active=True,
                is_owner=True,
            )
            session.add(user)
            session.flush()
            print(f"created owner user id={user.id} email={user.email}")
        else:
            user.password_hash = hash_password(password)
            user.is_active = True
            user.is_owner = True
            print(f"updated existing user id={user.id} email={user.email}")
        owner_id = user.id

    # Claim orphan rows (user_id IS NULL) for the owner. Also migrate the
    # legacy Signal.user_feedback column into SignalFeedback rows.
    if not args.no_claim:
        with db.session() as session:
            claimed = {}
            for model in (SearchRun, UserKeyword, WatchlistEntry, UserCV, Preparation):
                result = session.execute(
                    update(model)
                    .where(model.user_id.is_(None))
                    .values(user_id=owner_id)
                )
                claimed[model.__tablename__] = result.rowcount or 0  # type: ignore[attr-defined]

            # Migrate Signal.user_feedback -> SignalFeedback (owner)
            existing_fb_signals = {
                sid for (sid,) in session.execute(
                    select(SignalFeedback.signal_id).where(
                        SignalFeedback.user_id == owner_id
                    )
                ).all()
            }
            fb_migrated = 0
            for sig in session.execute(
                select(Signal).where(Signal.user_feedback.is_not(None))
            ).scalars():
                if sig.id in existing_fb_signals:
                    continue
                session.add(
                    SignalFeedback(
                        user_id=owner_id, signal_id=sig.id, action=sig.user_feedback
                    )
                )
                fb_migrated += 1
            print("claimed orphan rows:")
            for k, v in claimed.items():
                print(f"  {k}: {v}")
            print(f"  signal_feedback (migrated from Signal.user_feedback): {fb_migrated}")

    print("done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
