"""High-level orchestration: collect -> dedup -> classify -> persist."""

from __future__ import annotations

from collections.abc import AsyncIterable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select

from signal_tracker.agents.source_picker import (
    PickerSelection,
    load_source_registry,
    materialize_sources,
)
from signal_tracker.classifier.feedback import load_feedback_examples
from signal_tracker.classifier.llm import classify, prefilter
from signal_tracker.classifier.schemas import ClassificationResult, ClassifierInput
from signal_tracker.collectors.base import BaseCollector, CollectedItem
from signal_tracker.collectors.france_travail import FranceTravailCollector
from signal_tracker.collectors.gdelt import GdeltCollector, GdeltQuery
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
    # Two-stage prefilter metrics (zero when the prefilter is disabled).
    prefiltered_out: int = 0  # cheap "no" → skipped expensive call


def _filter_feeds_by_language(
    feeds: list[FeedConfig], languages: set[str],
) -> list[FeedConfig]:
    """Keep feeds whose language is in `languages`, plus feeds with no
    language tag (we don't have enough info to exclude them).

    Returns the input unchanged when `languages` is empty (= no filter).
    """
    if not languages:
        return feeds
    return [f for f in feeds if (f.language or "").lower() in languages or not f.language]


def _augment_gdelt_queries(
    queries: list[GdeltQuery], languages: set[str],
) -> list[GdeltQuery]:
    """Prefix each query with a sourcelang clause so GDELT only returns
    articles in the requested languages.

    Returns the input unchanged when `languages` is empty.
    """
    from signal_tracker.utils.geo import languages_to_gdelt_clause
    clause = languages_to_gdelt_clause(languages)
    if not clause:
        return queries
    return [
        GdeltQuery(
            id=q.id,
            query=f"{q.query} {clause}".strip(),
            timespan=q.timespan,
            max_records=q.max_records,
        )
        for q in queries
    ]


def build_default_collectors(
    language_filter: set[str] | None = None,
) -> list[BaseCollector]:
    """Instantiate collectors enabled in ``config/sources.yaml``.

    Each section is gated on either a presence check (RSS feeds list) or an
    explicit ``enabled: true`` flag. API keys come from the runtime settings
    (.env), so a missing key skips the collector with a warning.
    """
    sources = load_sources()
    settings = get_settings()
    collectors: list[BaseCollector] = []
    languages = language_filter or set()

    rss_feeds = [FeedConfig.from_dict(raw) for raw in sources.get("rss", [])]
    rss_feeds = _filter_feeds_by_language(rss_feeds, languages)
    if rss_feeds:
        collectors.append(RSSCollector(rss_feeds))

    gdelt_cfg = sources.get("gdelt") or {}
    if gdelt_cfg.get("enabled"):
        gdelt_queries = GdeltCollector.queries_from_yaml(gdelt_cfg)
        gdelt_queries = _augment_gdelt_queries(gdelt_queries, languages)
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


def build_collectors_for_selection(
    selection: PickerSelection,
    language_filter: set[str] | None = None,
) -> list[BaseCollector]:
    """Build collectors restricted to what the dynamic source picker chose.

    Used per search-run (Phase 10 feature 3) when the LLM picker selects
    a subset of curated domains + drafts a few extra GDELT queries. The
    RSS + GDELT collectors are built from the materialized union; the
    other collectors (NewsAPI / Pappers / France Travail) still come
    from the static config because they aren't domain-tagged.

    When `language_filter` is provided (e.g. {"fr"} for a France-only
    user), RSS feeds outside those languages are dropped and GDELT
    queries are prefixed with a sourcelang clause — so we never make
    the network round-trip for content we'd discard at /results anyway.
    """
    registry = load_source_registry()
    feeds, gdelt_queries = materialize_sources(selection, registry)
    languages = language_filter or set()
    feeds = _filter_feeds_by_language(feeds, languages)
    gdelt_queries = _augment_gdelt_queries(gdelt_queries, languages)
    collectors: list[BaseCollector] = []
    if feeds:
        collectors.append(RSSCollector(feeds))
    if gdelt_queries:
        collectors.append(GdeltCollector(gdelt_queries))

    # Static-config collectors (NewsAPI / Pappers / France Travail) stay
    # global because they're API-key gated and not domain-tagged.
    sources = load_sources()
    settings = get_settings()

    newsapi_cfg = sources.get("newsapi") or {}
    if newsapi_cfg.get("enabled") and settings.newsapi_key:
        newsapi_queries = NewsApiCollector.queries_from_yaml(newsapi_cfg)
        if newsapi_queries:
            collectors.append(NewsApiCollector(settings.newsapi_key, newsapi_queries))

    pappers_cfg = sources.get("pappers") or {}
    if pappers_cfg.get("enabled") and settings.pappers_api_key:
        watchlist = PappersCollector.watchlist_from_yaml(pappers_cfg)
        if watchlist:
            collectors.append(PappersCollector(settings.pappers_api_key, watchlist))

    ft_cfg = sources.get("france_travail") or {}
    if (
        ft_cfg.get("enabled")
        and settings.france_travail_client_id
        and settings.france_travail_client_secret
    ):
        ft_config = FranceTravailCollector.from_yaml(ft_cfg)
        if ft_config.rome_codes:
            collectors.append(FranceTravailCollector(
                client_id=settings.france_travail_client_id,
                client_secret=settings.france_travail_client_secret,
                config=ft_config,
            ))
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
    """Run all configured collectors and persist new raw items.

    Phase 11.2 — collectors run concurrently via asyncio.gather. The 4-5
    collectors are independent (different APIs, different network
    endpoints) so there's nothing to lose by overlapping their HTTP
    waits. Each `_ingest` opens its own SQLAlchemy session, and asyncio
    is single-threaded, so the DB writes are still serialized at the
    event-loop level — safe for both Postgres (prod) and SQLite (dev).
    """
    import asyncio as _asyncio
    settings = get_settings()
    if db is None:
        db = init_db(resolve_db_url(settings))
    if collectors is None:
        collectors = build_default_collectors()

    report = CollectionReport()

    async def _one(collector: BaseCollector) -> None:
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

    await _asyncio.gather(*[_one(c) for c in collectors], return_exceptions=False)
    return report


def _isoweek_bucket(when: datetime | None) -> str:
    when = when or datetime.now(tz=UTC)
    iso = when.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _store_signal(
    session: object,
    raw: RawItem,
    result: ClassificationResult,
    search_run_id: int | None = None,
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
            hq_country=result.hq_country,
            active_countries=result.active_countries or None,
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
            search_run_id=search_run_id,
        )
    )
    return True


async def run_classification(
    profile: UserProfile | None = None,
    db: Database | None = None,
    limit: int | None = None,
    search_run_id: int | None = None,
    user_id: int | None = None,
) -> ClassificationReport:
    """Classify all unclassified raw_items in the DB.

    When ``search_run_id`` is set, signals created during this run are tagged
    with it so the dashboard can flag them as new for that search. When
    ``user_id`` is set (Phase 8 multi-tenant), only keywords belonging to
    that user are injected into the classifier prompt.
    """
    settings = get_settings()
    if db is None:
        db = init_db(resolve_db_url(settings))
    if profile is None:
        profile = load_user_profile()

    report = ClassificationReport()

    # Load runtime user keywords from the dashboard-managed table. Scope
    # to the launching user when we have one — keeps each user's chase
    # independent.
    user_keywords: dict[str, list[str]] = {}
    with db.session() as _kw_session:
        from signal_tracker.storage.models import UserKeyword as _UK
        kw_stmt = select(_UK)
        if user_id is not None:
            kw_stmt = kw_stmt.where(_UK.user_id == user_id)
        for kw in _kw_session.execute(kw_stmt).scalars():
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
    prefilter_enabled = bool(
        settings.llm_prefilter_enabled and settings.llm_cheap_model
    )

    with db.session() as session:
        stmt = select(RawItem).where(RawItem.classified.is_(False)).order_by(RawItem.id)
        if limit is not None:
            stmt = stmt.limit(limit)
        backlog: list[RawItem] = list(session.execute(stmt).scalars())

    import asyncio as _asyncio  # local import to keep top-level imports tight

    # Phase 11.2 — parallel classifier. asyncio.gather under a Semaphore
    # gives us 1 → N concurrency with a single knob (llm_concurrency).
    # When concurrency == 1 the behavior matches the old sequential loop
    # (used by tests + dev where back-pressure matters).
    concurrency = max(1, int(settings.llm_concurrency))
    sem = _asyncio.Semaphore(concurrency)
    # Fail-fast signal: any task that catches a credentials error flips
    # this. Other tasks check it at their gate and exit cheaply. We can't
    # cancel siblings cleanly from inside a coroutine in all cases, but
    # the gate at the top keeps the blast radius to one in-flight call
    # per concurrency slot.
    aborted = {"flag": False}

    async def _process_one(raw: RawItem) -> None:
        async with sem:
            if aborted["flag"]:
                return
            # Skip the rate-limit sleep when running concurrently — the
            # semaphore already does back-pressure, and stacking sleeps
            # on top would just serialize what we're trying to parallelize.
            if concurrency == 1 and rate_limit > 0:
                await _asyncio.sleep(rate_limit)
            item = ClassifierInput(
                source=raw.source,
                url=raw.url,
                title=raw.title,
                content=raw.content,
                published_at=raw.published_at,
            )

            # Stage 1: cheap prefilter. "no" → mark classified, skip the
            # expensive call entirely. "yes"/"maybe"/failure → fall through.
            if prefilter_enabled:
                verdict = await prefilter(item, profile)
                if verdict == "no":
                    report.prefiltered_out += 1
                    with db.session() as session:
                        row = session.get(RawItem, raw.id)
                        if row is not None:
                            row.classified = True
                    return

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
                lowered = message.lower()
                if "authenticationerror" in lowered or (
                    "missing" in lowered and "api key" in lowered
                ):
                    aborted["flag"] = True
                    logger.error(
                        "pipeline.aborted_credentials_missing",
                        extra={"processed_before_abort": report.processed},
                    )
                return

            report.processed += 1
            with db.session() as session:
                row = session.get(RawItem, raw.id)
                if row is None:
                    return
                row.classified = True
                if result.is_relevant:
                    report.relevant += 1
                    if _store_signal(session, row, result, search_run_id=search_run_id):
                        report.signals_created += 1
                    else:
                        report.signals_deduped += 1

    await _asyncio.gather(*(_process_one(raw) for raw in backlog))
    return report


__all__ = [
    "ClassificationReport",
    "CollectionReport",
    "build_default_collectors",
    "run_classification",
    "run_collection",
]
