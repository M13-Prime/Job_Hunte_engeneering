"""Weekly newsletter, one per user (Phase 13).

Unlike the morning digest (one recipient, every new signal), the
newsletter is personal and editorial: for each approved user who did
not opt out, the week's best signals from *their* searches, the new
offers that match *their* CV, where their applications stand, and the
follow-ups that are due — each item linking back into the tool and out
to the company's own website or careers page.

Nothing here calls an LLM: it only reads what the tool already computed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote_plus

from itsdangerous import BadSignature, URLSafeSerializer
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import and_, desc, func, or_, select

from signal_tracker.config import get_settings
from signal_tracker.jobs.filters import offer_passes, user_default_filter
from signal_tracker.notifier.email import EmailSenderProtocol
from signal_tracker.storage import Database
from signal_tracker.storage.models import (
    Application,
    Company,
    JobAgentScore,
    JobOffer,
    Preparation,
    RawItem,
    SearchRun,
    Signal,
    User,
    WatchlistEntry,
)

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

# Public careers boards of the ATS the scraper knows. A posting scraped
# from one of these proves the board exists, so the link is never invented.
ATS_BOARDS: dict[str, str] = {
    "greenhouse": "https://boards.greenhouse.io/{slug}",
    "lever": "https://jobs.lever.co/{slug}",
    "ashby": "https://jobs.ashbyhq.com/{slug}",
    "workable": "https://apply.workable.com/{slug}/",
    "smartrecruiters": "https://jobs.smartrecruiters.com/{slug}",
    "recruitee": "https://{slug}.recruitee.com/",
    "teamtailor": "https://{slug}.teamtailor.com/jobs",
    "personio": "https://{slug}.jobs.personio.de/",
}

STATUS_LABELS = {
    "saved": "À postuler", "applied": "Postulée", "interview": "Entretien",
    "offer": "Offre", "rejected": "Refus",
}


@dataclass(slots=True)
class CompanyLink:
    url: str
    label: str  # "Site", "Page carrières" or "Site web" (first search result)
    verified: bool


@dataclass(slots=True)
class NewsSignal:
    id: int
    company: str
    score: float
    signal_type: str
    summary: str
    angle: str | None
    source_url: str | None
    company_link: CompanyLink


@dataclass(slots=True)
class NewsJob:
    id: int
    title: str
    company: str
    location: str | None
    score: float
    angle: str | None
    url: str
    company_link: CompanyLink


@dataclass(slots=True)
class NewsFollowup:
    company: str
    title: str
    status: str
    applied_at: datetime | None


@dataclass(slots=True)
class NewsletterData:
    user_email: str
    generated_at: datetime
    period_days: int
    base_url: str | None
    unsubscribe_url: str | None
    signals: list[NewsSignal] = field(default_factory=list)
    jobs: list[NewsJob] = field(default_factory=list)
    followups: list[NewsFollowup] = field(default_factory=list)
    pipeline: dict[str, int] = field(default_factory=dict)
    signals_total: int = 0
    jobs_total: int = 0

    @property
    def is_empty(self) -> bool:
        return not (self.signals or self.jobs or self.followups or any(self.pipeline.values()))

    @property
    def period_start(self) -> datetime:
        return self.generated_at - timedelta(days=self.period_days)

    def app_url(self, path: str) -> str | None:
        return f"{self.base_url.rstrip('/')}{path}" if self.base_url else None


@dataclass(slots=True)
class RenderedNewsletter:
    subject: str
    html: str
    text: str


# ---------------------------------------------------------------------------
# Unsubscribe tokens
# ---------------------------------------------------------------------------

def _serializer(secret: str) -> URLSafeSerializer:
    return URLSafeSerializer(secret, salt="newsletter-unsubscribe")


def unsubscribe_token(user_id: int, secret: str) -> str:
    return str(_serializer(secret).dumps({"u": user_id}))


def read_unsubscribe_token(token: str, secret: str) -> int | None:
    try:
        data = _serializer(secret).loads(token)
    except BadSignature:
        return None
    uid = data.get("u") if isinstance(data, dict) else None
    return int(uid) if isinstance(uid, int) else None


# ---------------------------------------------------------------------------
# Company links
# ---------------------------------------------------------------------------

def website_search_url(company_name: str) -> str:
    """DuckDuckGo "\\" query: jumps straight to the first result, which for a
    company name + "site officiel" is almost always its own website."""
    return f"https://duckduckgo.com/?q={quote_plus(chr(92) + company_name + ' site officiel')}"


def company_link(
    db: Database, company_name: str, company_normalized: str,
    *, prefer_website: bool = False,
) -> CompanyLink:
    """Best real link for a company, never an invented URL.

    Order: the company's known website > a careers board proven by a scraped
    posting > a careers page the preparation report gave with high/medium
    confidence > the first web result for the company's official site.
    With ``prefer_website`` the careers pages are skipped.
    """
    with db.session() as s:
        company = s.execute(
            select(Company).where(Company.normalized_name == company_normalized).limit(1)
        ).scalar_one_or_none()
        if company is not None and company.url:
            return CompanyLink(company.url, "Site", True)
        if prefer_website:
            return CompanyLink(website_search_url(company_name), "Site web", False)
        job = s.execute(
            select(JobOffer)
            .where(JobOffer.company_normalized == company_normalized)
            .order_by(desc(JobOffer.collected_at)).limit(1)
        ).scalar_one_or_none()
        if job is not None and job.ats in ATS_BOARDS and job.ats_company_slug:
            return CompanyLink(
                ATS_BOARDS[job.ats].format(slug=job.ats_company_slug), "Page carrières", True,
            )
        preps = s.execute(
            select(Preparation.report)
            .join(Signal, Preparation.signal_id == Signal.id)
            .where(and_(
                Signal.company_normalized == company_normalized,
                Preparation.status == "done",
            ))
            .order_by(desc(Preparation.created_at)).limit(3)
        ).scalars()
        for report in preps:
            intel = (report or {}).get("company_intel") or {}
            url = intel.get("career_page_url")
            if (
                isinstance(url, str) and url.startswith(("http://", "https://"))
                and intel.get("career_page_confidence") in ("high", "medium")
            ):
                return CompanyLink(url, "Page carrières", True)
    return CompanyLink(website_search_url(company_name), "Site web", False)


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------

class NewsletterBuilder:
    def __init__(
        self,
        db: Database,
        *,
        base_url: str | None,
        secret: str | None,
        period_days: int = 7,
        job_threshold: float | None = None,
        max_signals: int | None = None,
        max_jobs: int | None = None,
        hide_ghost: bool | None = None,
    ) -> None:
        settings = get_settings()
        self.db = db
        self.base_url = base_url.rstrip("/") if base_url else None
        self.secret = secret
        self.period_days = period_days
        self.job_threshold = (
            settings.newsletter_job_min_score if job_threshold is None else job_threshold
        )
        self.max_signals = settings.newsletter_max_signals if max_signals is None else max_signals
        self.max_jobs = settings.newsletter_max_jobs if max_jobs is None else max_jobs
        self.hide_ghost = settings.newsletter_hide_ghost if hide_ghost is None else hide_ghost

    def build(self, user_id: int) -> NewsletterData:
        now = datetime.now(UTC).replace(tzinfo=None)
        since = now - timedelta(days=self.period_days)
        with self.db.session() as s:
            user = s.get(User, user_id)
            if user is None:
                raise ValueError(f"user {user_id} not found")
            email = user.email
        unsub = (
            f"{self.base_url}/newsletter/unsubscribe?t={unsubscribe_token(user_id, self.secret)}"
            if self.base_url and self.secret else None
        )
        data = NewsletterData(
            user_email=email, generated_at=now, period_days=self.period_days,
            base_url=self.base_url, unsubscribe_url=unsub,
        )
        self._signals(data, user_id, since)
        self._jobs(data, user_id, since)
        self._applications(data, user_id, now)
        return data

    def _signals(self, data: NewsletterData, user_id: int, since: datetime) -> None:
        run_ids = select(SearchRun.id).where(SearchRun.user_id == user_id)
        with self.db.session() as s:
            rows = list(s.execute(
                select(Signal, RawItem)
                .join(RawItem, Signal.raw_item_id == RawItem.id)
                .where(and_(
                    Signal.search_run_id.in_(run_ids),
                    Signal.created_at >= since,
                ))
                .order_by(desc(Signal.total_score))
            ).all())
            watch = {
                n for (n,) in s.execute(
                    select(WatchlistEntry.normalized_name)
                    .where(WatchlistEntry.user_id == user_id)
                ).all()
            }
        data.signals_total = len(rows)
        seen: set[str] = set()
        # Watchlist companies first, then by score; one item per company.
        rows.sort(key=lambda r: (r[0].company_normalized not in watch, -r[0].total_score))
        for sig, raw in rows:
            if sig.company_normalized in seen or sig.total_score < 50:
                continue
            seen.add(sig.company_normalized)
            data.signals.append(NewsSignal(
                id=sig.id, company=sig.company_name, score=float(sig.total_score),
                signal_type=sig.signal_type, summary=sig.summary_fr,
                angle=sig.suggested_angle, source_url=raw.url if raw else None,
                company_link=company_link(self.db, sig.company_name, sig.company_normalized),
            ))
            if len(data.signals) >= self.max_signals:
                break

    def _jobs(self, data: NewsletterData, user_id: int, since: datetime) -> None:
        """Best-matching open offers found OR scored this week, one per title."""
        from signal_tracker.jobs.ghost import ghost_flags, title_key

        with self.db.session() as s:
            rows = list(s.execute(
                select(JobOffer, JobAgentScore)
                .join(JobAgentScore, JobAgentScore.job_offer_id == JobOffer.id)
                .where(and_(
                    JobAgentScore.user_id == user_id,
                    JobAgentScore.agent_score >= self.job_threshold,
                    JobOffer.is_open.is_(True),
                    or_(JobOffer.collected_at >= since, JobAgentScore.processed_at >= since),
                ))
                .order_by(desc(JobAgentScore.agent_score))
            ).all())
            flt = user_default_filter(s.get(User, user_id))
            rows = [r for r in rows if offer_passes(r[0], flt)]
            if self.hide_ghost and rows:
                ghosts = ghost_flags(s, [j for j, _ in rows])
                rows = [r for r in rows if not ghosts.get(r[0].id)]
        seen: set[tuple[str, str]] = set()
        unique = []
        for job, score in rows:
            key = (job.company_normalized, title_key(job.title))
            if key not in seen:
                seen.add(key)
                unique.append((job, score))
        rows = unique
        data.jobs_total = len(rows)
        for job, score in rows[: self.max_jobs]:
            data.jobs.append(NewsJob(
                id=job.id, title=job.title, company=job.company_name,
                location=job.location, score=float(score.agent_score),
                angle=score.agent_killer_angle or score.agent_fit_reasoning,
                url=job.url,
                company_link=company_link(self.db, job.company_name, job.company_normalized),
            ))

    def _applications(self, data: NewsletterData, user_id: int, now: datetime) -> None:
        with self.db.session() as s:
            counts: dict[str, int] = {
                str(status): int(n)
                for status, n in s.execute(
                    select(Application.status, func.count(Application.id))
                    .where(Application.user_id == user_id)
                    .group_by(Application.status)
                ).all()
            }
            due = list(s.execute(
                select(Application).where(and_(
                    Application.user_id == user_id,
                    Application.status.in_(("applied", "interview")),
                    Application.next_followup_at.is_not(None),
                    Application.next_followup_at <= now,
                )).order_by(Application.next_followup_at)
            ).scalars())
        data.pipeline = {k: int(counts.get(k, 0)) for k in STATUS_LABELS}
        data.followups = [
            NewsFollowup(a.company_name, a.title, a.status, a.applied_at) for a in due
        ]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _subject(data: NewsletterData) -> str:
    parts = []
    if data.jobs_total:
        parts.append(f"{data.jobs_total} offre{'s' if data.jobs_total > 1 else ''} pour toi")
    if data.signals_total:
        parts.append(
            f"{data.signals_total} {'signaux' if data.signals_total > 1 else 'signal'} "
            "de recrutement"
        )
    if data.followups:
        parts.append(f"{len(data.followups)} relance{'s' if len(data.followups) > 1 else ''}")
    head = "Ta semaine Signal Tracker"
    return f"{head} : {', '.join(parts)}" if parts else f"{head} : calme plat"


def render_newsletter(data: NewsletterData) -> RenderedNewsletter:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    subject = _subject(data)
    ctx = {"d": data, "labels": STATUS_LABELS, "subject": subject}
    return RenderedNewsletter(
        subject=subject,
        html=env.get_template("newsletter.html.j2").render(**ctx),
        text=env.get_template("newsletter.txt.j2").render(**ctx),
    )


def recipients(db: Database) -> list[tuple[int, str]]:
    """Approved, active users who did not opt out."""
    with db.session() as s:
        return [
            (int(uid), str(email))
            for uid, email in s.execute(
                select(User.id, User.email).where(and_(
                    User.is_active.is_(True),
                    User.is_approved.is_(True),
                    User.newsletter_opt_in.is_(True),
                )).order_by(User.id)
            ).all()
        ]


def mark_sent(db: Database, user_id: int) -> None:
    with db.session() as s:
        user = s.get(User, user_id)
        if user is not None:
            user.newsletter_last_sent_at = datetime.now(UTC).replace(tzinfo=None)


async def send_newsletters(
    db: Database,
    *,
    sender: EmailSenderProtocol,
    from_email: str,
    base_url: str | None,
    secret: str | None,
    period_days: int = 7,
    job_threshold: float | None = None,
    only_user_id: int | None = None,
    skip_empty: bool = True,
) -> dict[str, int]:
    """Build + send one newsletter per recipient. Returns counters."""
    builder = NewsletterBuilder(
        db, base_url=base_url, secret=secret,
        period_days=period_days, job_threshold=job_threshold,
    )
    stats = {"sent": 0, "skipped_empty": 0, "failed": 0}
    for uid, email in recipients(db):
        if only_user_id is not None and uid != only_user_id:
            continue
        data = builder.build(uid)
        if skip_empty and data.is_empty:
            stats["skipped_empty"] += 1
            continue
        rendered = render_newsletter(data)
        try:
            await sender.send(
                to=email, subject=rendered.subject, html=rendered.html,
                text=rendered.text, sender=from_email,
            )
        except Exception:
            stats["failed"] += 1
            continue
        mark_sent(db, uid)
        stats["sent"] += 1
    return stats


__all__ = [
    "ATS_BOARDS",
    "CompanyLink",
    "NewsletterBuilder",
    "NewsletterData",
    "company_link",
    "read_unsubscribe_token",
    "recipients",
    "render_newsletter",
    "send_newsletters",
    "unsubscribe_token",
    "website_search_url",
]
