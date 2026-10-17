from datetime import timedelta

import pytest
from sqlalchemy import select

from tests.test_pipeline import ingest, news
from vcdiscovery import db
from vcdiscovery.models import Citation, NewsArticle, ScoreOverride, SourceObservation, Startup, utcnow
from vcdiscovery.confidence import compute_confidence
from vcdiscovery.scoring import load_config, save_weights, score_startup, validate_weights


def config(settings):
    with db.session_scope() as s:
        return load_config(s, settings.load_json(settings.scoring_file))


def score_of(settings, name):
    with db.session_scope() as s:
        st = s.scalar(select(Startup).where(Startup.name == name))
        result = score_startup(s, st, config(settings))
        return {f["factor"]: f for f in result.factors}, result


def test_unscorable_factors_are_listed_not_invented(settings):
    ingest(settings, [news("Acme Robotics raises $12M Series A led by Example Ventures", "https://a.example/1",
                           when=utcnow() - timedelta(days=5))])
    factors, result = score_of(settings, "Acme Robotics")
    assert factors["product_differentiation"]["score"] is None
    assert factors["product_differentiation"]["status"] == "unscorable"
    assert factors["financial_evidence"]["score"] is None  # pre-revenue is not scored as zero
    assert factors["market_opportunity"]["score"] is None  # not enough market data in the database
    assert factors["funding_validation"]["score"] == 55  # single-source report 45 + named lead 10
    assert {m["factor"] for m in result.missing} >= {"product_differentiation", "financial_evidence", "market_opportunity"}
    assert result.coverage < 0.5 and result.rating == "Insufficient evidence"
    assert "not an investment recommendation" in result.thesis


def test_media_volume_is_not_rewarded(settings):
    when = utcnow() - timedelta(days=3)
    ingest(settings, [news("Quiet Co raises $2M seed", "https://a.example/q", when=when)])
    ingest(settings, [news("Loud Co raises $2M seed", "https://a.example/l", when=when)])
    ingest(settings, [news("Quiet Co partners with Example Bank", "https://a.example/q2", when=when)])
    ingest(settings, [news(f"Loud Co partners with Example Bank, story {i}", f"https://outlet{i}.example/l",
                           publisher=f"Outlet {i}", when=when) for i in range(10)])
    quiet, _ = score_of(settings, "Quiet Co")
    loud, _ = score_of(settings, "Loud Co")
    for factor in ("business_traction", "growth_momentum", "funding_validation"):
        assert quiet[factor]["score"] == loud[factor]["score"], factor


def test_rumour_only_funding_scores_low_and_is_a_risk(settings):
    ingest(settings, [news("Orbital reportedly in talks to raise $50M", "https://a.example/r", when=utcnow())])
    factors, result = score_of(settings, "Orbital")
    assert factors["funding_validation"]["score"] == 15
    assert any("rumoured" in r for r in result.risks)


def test_overrides_persist_and_weights_are_customisable(settings):
    ingest(settings, [news("Acme Robotics raises $12M Series A", "https://a.example/1", when=utcnow())])
    with db.session_scope() as s:
        st = s.scalar(select(Startup))
        s.add(ScoreOverride(startup_id=st.id, factor="product_differentiation", score=80, note="Two granted patents", analyst="me"))
    factors, first = score_of(settings, "Acme Robotics")
    assert factors["product_differentiation"]["status"] == "analyst_override"
    assert factors["product_differentiation"]["score"] == 80
    with db.session_scope() as s:
        weights = {k: 0 for k in first.weights}
        weights.update(product_differentiation=50, funding_validation=50)
        save_weights(s, weights)
    _, second = score_of(settings, "Acme Robotics")
    assert second.weights["product_differentiation"] == 50
    assert second.total_score == round((80 * 50 + 45 * 50) / 100, 1)
    assert second.coverage == 1.0


def test_invalid_weights_rejected():
    with pytest.raises(ValueError, match="add up to 100"):
        validate_weights({"market_opportunity": 50})
    with pytest.raises(ValueError, match="Unknown"):
        validate_weights({"vibes": 100})



def test_confidence_does_not_combine_sources_about_different_fields(settings):
    with db.session_scope() as s:
        startup = Startup(name="Evidence Test Co", normalized_name="evidence test co",
                          review_status="Needs Verification", flags=[], is_demo=False)
        s.add(startup)
        s.flush()
        for index, (field_name, value, origin) in enumerate([
            ("industry", "Robotics", "publisher-a"),
            ("founders", "Jane Example", "publisher-b"),
        ], start=1):
            article = NewsArticle(
                source_key=f"news-{index}", source_type="news", publisher=f"Outlet {index}",
                url=f"https://evidence{index}.example/story", title=f"Evidence item {index}",
                title_fingerprint=f"{index:064x}", community_metrics=None,
            )
            s.add(article)
            s.flush()
            observation = SourceObservation(
                article_id=article.id, revision_number=1, source_key=article.source_key,
                source_type=article.source_type, publisher=article.publisher, source_url=article.url,
                title=article.title, summary=None, published_at=None, fingerprint=f"{index:064x}",
                origin_group=origin, source_role="independent_reporter",
            )
            s.add(observation)
            s.flush()
            s.add(Citation(
                startup_id=startup.id, field_name=field_name, value_text=value,
                evidence_status="reported", is_estimate=False, source_type="news",
                publisher=article.publisher, source_url=article.url, article_id=article.id,
                source_observation_id=observation.id, is_current=True,
                dedupe_key=f"{startup.id}:{field_name}:{index}",
            ))
        s.flush()
        result = compute_confidence(s, startup)
        corroboration = next(p for p in result["components"] if p["component"] == "corroboration")
        assert corroboration["points"] == 0
        assert "field/value" in corroboration["explanation"]


def test_confidence_rewards_multiple_origins_supporting_same_field_value(settings):
    with db.session_scope() as s:
        startup = Startup(name="Claim Test Co", normalized_name="claim test co",
                          review_status="Needs Verification", flags=[], is_demo=False)
        s.add(startup)
        s.flush()
        for index, origin in enumerate(("outlet-a", "outlet-b"), start=1):
            article = NewsArticle(
                source_key=f"claim-news-{index}", source_type="news", publisher=f"Outlet {index}",
                url=f"https://claim{index}.example/story", title="Claim evidence",
                title_fingerprint=f"{index:064x}", community_metrics=None,
            )
            s.add(article)
            s.flush()
            observation = SourceObservation(
                article_id=article.id, revision_number=1, source_key=article.source_key,
                source_type=article.source_type, publisher=article.publisher, source_url=article.url,
                title=article.title, summary=None, published_at=None, fingerprint=f"{index:064x}",
                origin_group=origin, source_role="independent_reporter",
            )
            s.add(observation)
            s.flush()
            s.add(Citation(
                startup_id=startup.id, field_name="industry", value_text="Robotics",
                evidence_status="reported", is_estimate=False, source_type="news",
                publisher=article.publisher, source_url=article.url, article_id=article.id,
                source_observation_id=observation.id, is_current=True,
                dedupe_key=f"{startup.id}:industry:{index}",
            ))
        s.flush()
        result = compute_confidence(s, startup)
        corroboration = next(p for p in result["components"] if p["component"] == "corroboration")
        assert corroboration["points"] == 14
