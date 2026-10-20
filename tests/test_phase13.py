"""Phase 13 — per-task LLM routing + cost accounting, keyword-driven search,
weekly newsletter, admin pages."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from signal_tracker.auth import hash_password
from signal_tracker.collectors.base import BaseCollector, CollectedItem
from signal_tracker.config import UserProfile, get_settings, model_for
from signal_tracker.dashboard.app import build_app
from signal_tracker.notifier.email import DryRunSender
from signal_tracker.notifier.newsletter import (
    NewsletterBuilder,
    company_link,
    read_unsubscribe_token,
    render_newsletter,
    send_newsletters,
    unsubscribe_token,
)
from signal_tracker.pipeline import run_classification, run_collection
from signal_tracker.storage import Database, init_db
from signal_tracker.storage.models import (
    JobAgentScore,
    JobOffer,
    LlmUsage,
    RawItem,
    SearchRun,
    Signal,
    User,
    UserCV,
    UserKeyword,
)
from signal_tracker.utils.keywords import (
    KeywordMatcher,
    gdelt_queries_from_keywords,
    newsapi_queries_from_keywords,
)
from signal_tracker.utils.llm_usage import (
    estimate_cost,
    record_usage,
    set_usage_db,
    usage_summary,
)

# ---------------------------------------------------------------------------
# LLM routing
# ---------------------------------------------------------------------------


def test_model_for_falls_back_then_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_MODEL", "anthropic/claude-sonnet-5-5")
    monkeypatch.delenv("LLM_CHEAP_MODEL", raising=False)
    get_settings.cache_clear()
    assert model_for("classify") == "anthropic/claude-sonnet-5-5"
    assert model_for("extract") == "anthropic/claude-sonnet-5-5"

    monkeypatch.setenv("LLM_CHEAP_MODEL", "anthropic/claude-haiku-5-5")
    monkeypatch.setenv("LLM_MODEL_CLASSIFY", "gemini/gemini-2.5-flash-lite")
    get_settings.cache_clear()
    assert model_for("classify") == "gemini/gemini-2.5-flash-lite"
    assert model_for("extract") == "anthropic/claude-haiku-5-5"
    assert model_for("prefilter") == "anthropic/claude-haiku-5-5"
    assert model_for("write") == "anthropic/claude-sonnet-5-5"


def test_record_usage_and_summary(tmp_db: Database) -> None:
    set_usage_db(tmp_db)
    try:
        response = SimpleNamespace(
            model="claude-haiku-5-5",
            usage=SimpleNamespace(prompt_tokens=2_000, completion_tokens=500),
        )
        record_usage("classify", "anthropic/claude-haiku-5-5", response)
        record_usage("classify", "anthropic/claude-haiku-5-5", SimpleNamespace())  # no usage
        record_usage("write", "mystery/model-x", SimpleNamespace(
            model="mystery/model-x",
            usage={"prompt_tokens": 10, "completion_tokens": 10},
        ))
    finally:
        set_usage_db(None)
    with tmp_db.session() as s:
        rows = list(s.execute(select(LlmUsage)).scalars())
    assert len(rows) == 2
    summary = {r.task: r for r in usage_summary(tmp_db, days=30)}
    assert summary["classify"].calls == 1
    assert summary["classify"].input_tokens == 2_000
    assert summary["write"].unknown_cost_calls == 1
    # 2000 * 0.10 + 500 * 0.50 per 1M (or LiteLLM's own price if it knows the model)
    assert summary["classify"].cost_usd > 0


def test_estimate_cost_uses_cached_rate() -> None:
    full = estimate_cost("anthropic/claude-haiku-5-5", 1_000_000, 0)
    cached = estimate_cost("anthropic/claude-haiku-5-5", 1_000_000, 0, cached=1_000_000)
    assert full == pytest.approx(0.10)
    assert cached == pytest.approx(0.01)
    assert estimate_cost("unknown/model", 10, 10) is None


# ---------------------------------------------------------------------------
# Keywords
# ---------------------------------------------------------------------------


def test_keyword_matcher_is_accent_case_and_word_aware() -> None:
    m = KeywordMatcher(["Levée de fonds", "AI", "climate tech"])
    assert m.matches("Sweep annonce une LEVEE DE FONDS de 22 M€")
    assert m.matches("New AI startup")
    assert not m.matches("Said the email")  # "ai" inside a word does not count
    assert m.found("Climate Tech and AI") == ["ai", "climate tech"]
    assert KeywordMatcher([]).matches("anything")  # no terms → no filtering


def test_gdelt_and_newsapi_queries_from_keywords() -> None:
    kw = {
        "job_title": ["Data Engineer", "ML"],  # "ML" is too short for GDELT
        "field": ["climate tech", 'carbon "accounting"'],
        "other": ["CSRD"],
    }
    queries = gdelt_queries_from_keywords(kw)
    by_id = {q.id: q.query for q in queries}
    assert by_id["kw_job_title_0"].startswith('"Data Engineer" (hiring OR')
    assert by_id["kw_field_0"].startswith('("climate tech" OR "carbon accounting")')
    assert by_id["kw_other_0"] == '"CSRD"'
    news = newsapi_queries_from_keywords(kw)
    assert news and '"Data Engineer"' in news[0].query


class _Stub(BaseCollector):
    source_id = "stub"

    def __init__(self, items: list[CollectedItem]) -> None:
        self._items = items

    async def collect(self) -> AsyncIterator[CollectedItem]:
        for item in self._items:
            yield item


async def test_keyword_gate_skips_unrelated_articles_without_llm(
    monkeypatch: pytest.MonkeyPatch, tmp_db: Database, sample_profile: UserProfile,
) -> None:
    with tmp_db.session() as s:
        u = User(email="k@x", password_hash="x", is_active=True, is_approved=True)
        s.add(u)
        s.flush()
        s.add(UserKeyword(user_id=u.id, category="field", value="climate tech"))
        uid = u.id
    when = datetime(2026, 10, 1, tzinfo=UTC)
    items = [
        CollectedItem(source="rss:t", url="https://a/1", title="Sweep, climate tech startup",
                      content="Sweep hires.", published_at=when),
        CollectedItem(source="rss:t", url="https://a/2", title="PSG wins the derby",
                      content="Football results.", published_at=when),
    ]
    await run_collection(collectors=[_Stub(items)], db=tmp_db)
    calls: list[str] = []

    async def fake_classify(item: Any, *_a: Any, **_k: Any) -> Any:
        calls.append(item.url)
        raise RuntimeError("stop here")

    monkeypatch.setattr("signal_tracker.pipeline.classify", fake_classify)
    report = await run_classification(profile=sample_profile, db=tmp_db, user_id=uid)
    assert calls == ["https://a/1"]
    assert report.keyword_filtered == 1
    with tmp_db.session() as s:
        assert s.query(RawItem).filter_by(classified=True).count() == 1


# ---------------------------------------------------------------------------
# Dashboard: results search, accept-all, admin pages, newsletter
# ---------------------------------------------------------------------------


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    db = init_db(tmp_path / "p13.db")
    with db.session() as s:
        s.add(User(email="owner@example.com", password_hash=hash_password("password"),
                   is_active=True, is_owner=True, is_approved=True))
        s.add(User(email="member@example.com", password_hash=hash_password("password"),
                   is_active=True, is_approved=True))
    return db


def _login(db: Database, email: str = "owner@example.com") -> TestClient:
    c = TestClient(build_app(db=db))
    r = c.post("/login", data={"email": email, "password": "password"}, follow_redirects=False)
    assert r.status_code == 303
    return c


def _uid(db: Database, email: str = "owner@example.com") -> int:
    with db.session() as s:
        return int(s.execute(select(User.id).where(User.email == email)).scalar_one())


def _signal(db: Database, user_id: int, company: str, summary: str, score: float = 80) -> int:
    with db.session() as s:
        run = SearchRun(user_id=user_id, label="r", status="done")
        s.add(run)
        s.flush()
        raw = RawItem(source="rss", url=f"https://news/{company}", title=f"{company} news",
                      hash=f"h-{company}")
        s.add(raw)
        s.flush()
        sig = Signal(
            raw_item_id=raw.id, signal_type="funding", company_name=company,
            company_normalized=company.lower(), relevance_score=score, urgency_score=score,
            fit_with_profile_score=score, total_score=score, summary_fr=summary,
            recommended_action="contact", dedup_key=f"d-{company}", search_run_id=run.id,
        )
        s.add(sig)
        s.flush()
        return sig.id


def test_results_keyword_search(db: Database) -> None:
    c = _login(db)
    uid = _uid(db)
    _signal(db, uid, "Sweep", "Sweep lève 22 M€ pour sa plateforme carbone.")
    _signal(db, uid, "Mistral", "Mistral ouvre un bureau à Londres.")
    body = c.get("/results?q=carbone").text
    assert "Sweep" in body and "Mistral" not in body
    assert "Mistral" in c.get("/results").text


def test_accept_all_suggested_keywords(db: Database) -> None:
    c = _login(db)
    uid = _uid(db)
    with db.session() as s:
        s.add(UserCV(user_id=uid, text="cv", char_count=2, profile_json={
            "suggested_keywords": {
                "field": ["ESG data", "esg data"], "job_title": ["Data Engineer"],
            },
        }))
        s.add(UserKeyword(user_id=uid, category="job_title", value="data engineer"))
    r = c.post("/keywords/suggested/accept-all", follow_redirects=False)
    assert r.status_code == 303
    with db.session() as s:
        values = sorted(
            (k.category, k.value)
            for k in s.execute(select(UserKeyword).where(UserKeyword.user_id == uid)).scalars()
        )
    assert values == [("field", "ESG data"), ("job_title", "data engineer")]


def test_admin_pages_are_owner_only(db: Database) -> None:
    owner = _login(db)
    assert owner.get("/admin/usage").status_code == 200
    assert owner.get("/admin/newsletter").status_code == 200
    member = _login(db, "member@example.com")
    assert member.get("/admin/usage").status_code == 403
    assert member.get("/admin/newsletter").status_code == 403


def test_landing_renders_overview(db: Database) -> None:
    body = _login(db).get("/").text
    assert "Lancer la recherche" in body
    assert "Relances dues" in body
    assert "Newsletter hebdomadaire" in body


def test_company_link_prefers_proven_careers_board(tmp_db: Database) -> None:
    with tmp_db.session() as s:
        s.add(JobOffer(
            company_normalized="sweep", company_name="Sweep", ats="lever",
            ats_company_slug="sweep", external_id="1", title="DE", url="https://x",
            relevance_score=10, is_open=True, dedup_key="sweep|lever|1",
        ))
    link = company_link(tmp_db, "Sweep", "sweep")
    assert link.url == "https://jobs.lever.co/sweep" and link.verified
    fallback = company_link(tmp_db, "Unknown Co", "unknown co")
    assert not fallback.verified and fallback.url.startswith("https://www.google.com/search?q=")


def test_newsletter_content_and_unsubscribe_flow(db: Database) -> None:
    uid = _uid(db)
    _signal(db, uid, "Sweep", "Sweep lève 22 M€.", score=85)
    with db.session() as s:
        job = JobOffer(
            company_normalized="sweep", company_name="Sweep", ats="greenhouse",
            ats_company_slug="sweep", external_id="9", title="Senior Data Engineer",
            url="https://boards.greenhouse.io/sweep/jobs/9", relevance_score=40,
            is_open=True, dedup_key="sweep|greenhouse|9",
        )
        s.add(job)
        s.flush()
        s.add(JobAgentScore(user_id=uid, job_offer_id=job.id, agent_score=81,
                            agent_killer_angle="Ton expérience CSRD."))
    secret = "test-secret"
    data = NewsletterBuilder(db, base_url="https://signaltracker.fr", secret=secret).build(uid)
    assert [j.title for j in data.jobs] == ["Senior Data Engineer"]
    assert [s.company for s in data.signals] == ["Sweep"]
    assert data.signals[0].company_link.url == "https://boards.greenhouse.io/sweep"
    rendered = render_newsletter(data)
    assert "1 offre pour toi" in rendered.subject
    assert "https://signaltracker.fr/jobs/" in rendered.html
    assert "https://boards.greenhouse.io/sweep" in rendered.html
    assert data.unsubscribe_url and "/newsletter/unsubscribe?t=" in data.unsubscribe_url

    token = unsubscribe_token(uid, secret)
    assert read_unsubscribe_token(token, secret) == uid
    assert read_unsubscribe_token(token, "other-secret") is None


async def test_send_newsletters_skips_empty_and_opted_out(db: Database) -> None:
    owner, member = _uid(db), _uid(db, "member@example.com")
    _signal(db, owner, "Sweep", "Sweep lève 22 M€.", score=85)
    sender = DryRunSender()
    stats = await send_newsletters(
        db, sender=sender, from_email="tracker@x", base_url=None, secret=None,
    )
    assert stats == {"sent": 1, "skipped_empty": 1, "failed": 0}
    assert sender.sent[0]["to"] == "owner@example.com"
    with db.session() as s:
        m = s.get(User, member)
        assert m is not None
        m.newsletter_opt_in = False
        o = s.get(User, owner)
        assert o is not None and o.newsletter_last_sent_at is not None
    sender2 = DryRunSender()
    stats2 = await send_newsletters(
        db, sender=sender2, from_email="tracker@x", base_url=None, secret=None,
        skip_empty=False,
    )
    assert stats2["sent"] == 1 and [m["to"] for m in sender2.sent] == ["owner@example.com"]


def test_newsletter_routes(db: Database) -> None:
    c = _login(db)
    assert c.get("/newsletter/preview").status_code == 200
    r = c.post("/newsletter/toggle", data={"next": "/"}, follow_redirects=False)
    assert r.status_code == 303
    uid = _uid(db)
    with db.session() as s:
        u = s.get(User, uid)
        assert u is not None and u.newsletter_opt_in is False
        u.newsletter_opt_in = True
    # The public one-click unsubscribe uses the app's session secret, which
    # is random per boot in tests: a forged token is refused.
    anon = TestClient(build_app(db=db))
    assert anon.get("/newsletter/unsubscribe?t=forged").status_code == 400
