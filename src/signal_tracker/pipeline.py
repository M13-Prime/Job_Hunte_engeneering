"""High-level orchestration: collect -> dedup -> classify -> persist."""

from __future__ import annotations

from collections.abc import AsyncIterable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select

from signal_tracker.classifier.feedback import load_feedback_examples
from signal_tracker.classifier.llm import classify
from signal_tracker.classifier.schemas import ClassificationResult, ClassifierInput
from signal_tracker.collectors.base import BaseCollector, CollectedItem
from signal_tracker.collectors.france_travail import FranceTravailCollector
from signal_tracker.collectors.gdelt import GdeltCollector
from signal_tracker.collectors.newsapi import NewsApiCollector
from signal_tracker.collectors.pappers import PappersCollector
from signal_tracker.collectors.rss import FeedConfig, RSSCollector
from signal_tracker.config import (
    UserProfile,
    get_settings,
    load_sources,
    load_user_profile,
    resolve_db_url,
)
from signal_tracker.storage import Database, init_db
from signal_tracker.storage.models import RawItem, Signal
from signal_tracker.utils.dedup import raw_item_hash, signal_dedup_key
from signal_tracker.utils.logging import get_logger
from signal_tracker.utils.normalize import normalize_company_name

logger = get_logger(__name__)


@dataclass(slots=True)
class CollectionReport:
    fetched: int = 0
    new: int = 0
    duplicates: int = 0


@dataclass(slots=True)
class ClassificationReport:
    processed: int = 0
    relevant: int = 0
    signals_created: int = 0
    signals_deduped: int = 0
    errors: int = 0


def build_default_collectors() -> list[BaseCollector]:
    """Instantiate collectors enabled in ``config/sources.yaml``.

    Each section is gated on either a presence check (RSS feeds list) or an
    explicit ``enabled: true`` flag. API keys come from the runtime settings
    (.env), so a missing key skips the collector with a warning.
    """
    sources = load_sources()
    settings = get_settings()
    collectors: list[BaseCollector] = []

    rss_feeds = [FeedConfig.from_dict(raw) for raw in sources.get("rss", [])]
    if rss_feeds:
        collectors.append(RSSCollector(rss_feeds))

    gdelt_cfg = sources.get("gdelt") or {}
    if gdelt_cfg.get("enabled"):
        gdelt_queries = GdeltCollector.queries_from_yaml(gdelt_cfg)
        if gdelt_queries:
            collectors.append(GdeltCollector(gdelt_queries))
        else:
            logger.warning("pipeline.gdelt_no_queries")

    newsapi_cfg = sources.get("newsapi") or {}
    if newsapi_cfg.get("enabled"):
        if not settings.newsapi_key:
            logger.warning("pipeline.newsapi_skipped reason=missing_NEWSAPI_KEY")
        else:
            newsapi_queries = NewsApiCollector.queries_from_yaml(newsapi_cfg)
            if newsapi_queries:
                collectors.append(
                    NewsApiCollector(settings.newsapi_key, newsapi_queries)
                )

    pappers_cfg = sources.get("pappers") or {}
    if pappers_cfg.get("enabled"):
        if not settings.pappers_api_key:
            logger.warning("pipeline.pappers_skipped reason=missing_PAPPERS_API_KEY")
        else:
            watchlist = PappersCollector.watchlist_from_yaml(pappers_cfg)
            if watchlist:
                collectors.append(
                    PappersCollector(settings.pappers_api_key, watchlist)
                )

    ft_cfg = sources.get("france_travail") or {}
    if ft_cfg.get("enabled"):
        if not (settings.france_travail_client_id and settings.france_travail_client_secret):
            logger.warning(
                "pipeline.france_travail_skipped "
                "reason=missing_FRANCE_TRAVAIL_CLIENT_ID/SECRET"
            )
        else:
            ft_config = FranceTravailCollector.from_yaml(ft_cfg)
            if ft_config.rome_codes:
                collectors.append(
                    FranceTravailCollector(
                        client_id=settings.france_travail_client_id,
                        client_secret=settings.france_travail_client_secret,
                        config=ft_config,
                    )
                )

    return collectors


async def _ingest(
    db: Database,
    items: AsyncIterable[CollectedItem],
    report: CollectionReport,
) -> None:
    with db.session() as session:
        async for item in items:
            report.fetched += 1
            h = raw_item_hash(item.source, item.url)
            already = session.execute(
                select(RawItem.id).where(RawItem.hash == h)
            ).first()
            if already is not None:
                report.duplicates += 1
                continue
            session.add(
                RawItem(
                    source=item.source,
                    url=item.url,
                    title=item.title,
                    content=item.content,
                    published_at=item.published_at,
                    hash=h,
                    classified=False,
                )
            )
            report.new += 1


async def run_collection(
    collectors: list[BaseCollector] | None = None,
    db: Database | None = None,
) -> CollectionReport:
    """Run all configured collectors and persist new raw items."""
    settings = get_settings()
    if db is None:
        db = init_db(resolve_db_url(settings))
    if collectors is None:
        collectors = build_default_collectors()

    report = CollectionReport()
    for collector in collectors:
        await _ingest(db, collector.collect(), report)
        logger.info(
            "pipeline.collector_done",
            extra={
                "collector": collector.source_id,
                "fetched": report.fetched,
                "new": report.new,
                "duplicates": report.duplicates,
            },
        )
    return report


def _isoweek_bucket(when: datetime | None) -> str:
    when = when or datetime.now(tz=UTC)
    iso = when.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _store_signal(
    session: object,
    raw: RawItem,
    result: ClassificationResult,
) -> bool:
    """
    Persist a Signal row when ``is_relevant`` is True.

    Returns True if a new row was inserted, False if a same-week duplicate
    already exists.
    """
    from sqlalchemy.orm import Session  # local import to avoid module-level dep

    assert isinstance(session, Session)

    normalized = result.company_normalized or normalize_company_name(result.company_name)
    dedup = signal_dedup_key(
        company_normalized=normalized,
        signal_type=result.signal_type,
        week_bucket=_isoweek_bucket(raw.published_at or raw.collected_at),
    )
    existing = session.execute(
        select(Signal.id).where(Signal.dedup_key == dedup)
    ).first()
    if existing is not None:
        return False

    session.add(
        Signal(
            raw_item_id=raw.id,
            signal_type=result.signal_type,
            company_name=result.company_name,
            company_normalized=normalized,
            key_persons=[p.model_dump() for p in result.key_persons],
            relevance_score=result.relevance_score,
            urgency_score=result.urgency_score,
            fit_with_profile_score=result.fit_with_profile_score,
            total_score=result.total_score,
            summary_fr=result.summary_fr,
            suggested_angle=result.suggested_angle,
            recommended_action=result.recommended_action,
            target_contact=(
                result.target_contact.model_dump() if result.target_contact else None
            ),
            dedup_key=dedup,
        )
    )
    return True


async def run_classification(
    profile: UserProfile | None = None,
    db: Database | None = None,
    limit: int | None = None,
) -> ClassificationReport:
    """Classify all unclassified raw_items in the DB."""
    settings = get_settings()
    if db is None:
        db = init_db(resolve_db_url(settings))
    if profile is None:
        profile = load_user_profile()

    report = ClassificationReport()

    # Load runtime user keywords from the dashboard-managed table.
    user_keywords: dict[str, list[str]] = {}
    with db.session() as _kw_session:
        from signal_tracker.storage.models import UserKeyword as _UK
        for kw in _kw_session.execute(select(_UK)).scalars():
            user_keywords.setdefault(kw.category, []).append(kw.value)
    if user_keywords:
        logger.info(
            "pipeline.user_keywords_loaded",
            extra={"counts": {k: len(v) for k, v in user_keywords.items()}},
        )

    # Load dynamic few-shot examples from past user feedback (Phase 4).
    feedback_examples = load_feedback_examples(db)
    if feedback_examples:
        logger.info(
            "pipeline.feedback_examples_loaded",
            extra={"count": len(feedback_examples)},
        )

    rate_limit = max(0.0, float(settings.llm_rate_limit_seconds))

    with db.session() as session:
        stmt = select(RawItem).where(RawItem.classified.is_(False)).order_by(RawItem.id)
        if limit is not None:
            stmt = stmt.limit(limit)
        backlog: list[RawItem] = list(session.execute(stmt).scalars())

    import asyncio as _asyncio  # local import to keep top-level imports tight

    for index, raw in enumerate(backlog):
        if index > 0 and rate_limit > 0:
            await _asyncio.sleep(rate_limit)
        item = ClassifierInput(
            source=raw.source,
            url=raw.url,
            title=raw.title,
            content=raw.content,
            published_at=raw.published_at,
        )
        try:
            result = await classify(
                item,
                profile,
                extra_examples=feedback_examples,
                user_keywords=user_keywords or None,
            )
        except Exception as exc:
            report.errors += 1
            message = str(exc)[:200]
            logger.error(
                "pipeline.classify_error",
                extra={"raw_item_id": raw.id, "error": message},
            )
            # Fail-fast on credential errors: no point in burning through the
            # whole backlog with the same broken key.
            lowered = message.lower()
            if "authenticationerror" in lowered or (
                "missing" in lowered and "api key" in lowered
            ):
                logger.error(
                    "pipeline.aborted_credentials_missing",
                    extra={"processed_before_abort": report.processed},
                )
                break
            continue

        report.processed += 1
        with db.session() as session:
            # Re-attach: load a fresh ORM row in this session.
            row = session.get(RawItem, raw.id)
            if row is None:
                continue
            row.classified = True
            if result.is_relevant:
                report.relevant += 1
                if _store_signal(session, row, result):
                    report.signals_created += 1
                else:
                    report.signals_deduped += 1

    return report


__all__ = [
    "ClassificationReport",
    "CollectionReport",
    "build_default_collectors",
    "run_classification",
    "run_collection",
]
