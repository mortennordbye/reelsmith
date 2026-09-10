"""Discovery for the second niche, and the measurement that shaped it.

The encyclopedia's revision feed was the proposed source and it is not enough
on its own: 89 revisions in three months, 69 substantive, 18 about a person and
10 about a person dead long enough to quote freely, which is one subject every
nine days against a format that wants one a night. So the feed is a signal and
Wikidata is the catalogue, which is the same division account 1 makes between
Hacker News and the GitHub search.

Nothing here touches the network. The fixtures are trimmed captures of the real
answers, because the shape of those answers is the thing worth pinning.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from config import Settings
from pipeline import subjects
from pipeline.models import SubjectCandidate
from sources import sep
from sources import wikimedia as wm

FEED = """
<div id="content"><h1>What's New</h1>
<ul>
<li> <a href="entries/montaigne/"><strong>Michel de Montaigne</strong></a>
(Marc Foglia and Emiliano Ferrari) [REVISED: <em>August 8, 2026</em>]
<div class="small">Changes to: Main text, Bibliography</div></li>
<li> <a href="entries/real-essence/"><strong>Locke on Real Essence</strong></a>
(Jan-Erik Jones) [REVISED: <em>September 1, 2026</em>]
<div class="small">Changes to: Bibliography</div></li>
<li> <a href="entries/indexicals/"><strong>Indexicals</strong></a>
(David Braun) [NEW: <em>September 3, 2026</em>]</li>
</ul></div>
"""


def test_the_feed_parses_into_revisions():
    revisions = list(sep.parse(FEED))

    assert [r.slug for r in revisions] == ["montaigne", "real-essence", "indexicals"]
    assert revisions[0].revised == date(2026, 8, 8)
    assert revisions[0].authors == ("Marc Foglia", "Emiliano Ferrari")
    assert revisions[2].is_new


def test_a_bibliography_change_is_not_news():
    """An author adding a paper somebody else wrote is not the entry saying
    something different, and the format is about what changed."""
    montaigne, locke, indexicals = sep.parse(FEED)

    assert montaigne.substantive
    assert not locke.substantive
    assert indexicals.substantive


def test_velocity_is_the_recent_window_against_its_own_baseline():
    """The same shape as star velocity and for the same reason: a big number
    is a famous subject, and the ranking is about what moved."""
    flat = [100] * 60
    assert wm.velocity(flat) == pytest.approx(1.0)

    spiking = [100] * 53 + [300] * 7
    assert wm.velocity(spiking) == pytest.approx(3.0)


def test_velocity_is_zero_when_there_is_not_enough_series():
    """A new article, or an API that answered with a week. Zero rather than a
    number computed from too little, which would rank noise at the top."""
    assert wm.velocity([100] * 8) == 0.0


def test_only_public_domain_artefacts_are_usable():
    """Narrower than what Commons allows on purpose. A CC-BY file needs
    attribution, and attribution in a caption on four platforms is a promise
    this account cannot keep, so the licence filter is how it never owes one."""
    pd = wm.Artefact(title="a", url="u", width=2000, height=3000, licence="Public domain")
    cc = wm.Artefact(title="b", url="u", width=2000, height=3000, licence="CC BY-SA 4.0")
    small = wm.Artefact(title="c", url="u", width=800, height=1200, licence="Public domain")

    assert pd.usable
    assert not cc.usable
    assert not small.usable


def test_a_person_dead_long_enough_is_quotable():
    alive_recently = wm.Person(qid="Q1", label="x", article="x", died=date.today().year - 10)
    long_gone = wm.Person(qid="Q2", label="y", article="y", died=1592)

    assert not alive_recently.public_domain
    assert long_gone.public_domain


def test_the_score_splits_into_the_parts_that_made_it():
    """Stored rather than recomputed, for `RepoCandidate`'s reason: the weights
    are config and have changed twice, so a score with no breakdown is a number
    nobody can argue with later."""
    scored = subjects.score(
        SubjectCandidate(
            qid="Q41568",
            name="Michel de Montaigne",
            article="Michel de Montaigne",
            views_recent=20_000,
            velocity=2.0,
            usable_artefacts=4,
            sep_slug="montaigne",
            sep_revised=date.today(),
        )
    )

    assert set(scored.score_breakdown) == {"velocity", "attention", "encyclopedia", "artefacts"}
    assert scored.score == pytest.approx(1.0)
    assert scored.score_breakdown["velocity"] == pytest.approx(0.5)


def test_an_old_revision_counts_for_less_than_a_new_one():
    def with_revision(days: int) -> float:
        return subjects.score(
            SubjectCandidate(
                qid="Q1",
                name="x",
                article="x",
                sep_revised=date.today() - timedelta(days=days),
            )
        ).score_breakdown["encyclopedia"]

    assert with_revision(0) > with_revision(45) > with_revision(89)
    assert with_revision(120) == 0.0


def test_the_cooldown_key_says_which_catalogue_it_came_from():
    """One store per account holds both niches' keys, and a bare Q41568 beside
    astral-sh/uv says nothing about what it is."""
    candidate = SubjectCandidate(qid="Q41568", name="Michel de Montaigne", article="M")

    assert candidate.key == "wikidata:Q41568"


@pytest.fixture
def cfg(tmp_path, monkeypatch) -> Settings:
    monkeypatch.setattr(Settings, "data_dir", property(lambda self: tmp_path))
    return Settings(account="thewholequote", _env_file=None)


def test_the_pool_is_people_rather_than_statements(cfg, monkeypatch):
    """A person with three occupations and two portraits is that many rows even
    under DISTINCT, and the label service answers with a bare id often enough
    that an episode could have been about "Q9061"."""
    rows = [
        {
            "p": {"value": "http://www.wikidata.org/entity/Q9061"},
            "pLabel": {"value": "Q9061"},
            "article": {"value": "https://en.wikipedia.org/wiki/Karl_Marx"},
            "died": {"value": "+1883-03-14T00:00:00Z"},
            "born": {"value": "+1818-05-05T00:00:00Z"},
            "links": {"value": "312"},
        },
    ] * 3
    monkeypatch.setattr(subjects, "_ask", lambda query, client: rows)

    pool = subjects.fetch_pool(cfg, refresh=True)

    assert len(pool) == 1
    assert pool[0]["name"] == "Karl Marx"
    assert pool[0]["died"] == 1883


def test_a_dead_query_service_falls_back_to_the_cache(cfg, monkeypatch):
    """A stale catalogue of people who died before 1931 is worth more than a
    night that ranks nobody."""
    (cfg.data_dir / "subject_pool.json").write_text(json.dumps([{"qid": "Q1", "name": "x"}]))

    def refuse(query, client):
        raise ValueError("502")

    monkeypatch.setattr(subjects, "_ask", refuse)
    monkeypatch.setattr(subjects.time, "sleep", lambda s: None)

    assert subjects.fetch_pool(cfg, refresh=True) == [{"qid": "Q1", "name": "x"}]


def test_covered_subjects_cost_no_request(cfg, monkeypatch):
    """Dropped before the pageviews call, for the reason discovery drops
    covered repos before enrichment."""
    monkeypatch.setattr(
        subjects,
        "fetch_pool",
        lambda cfg, **kw: [
            {"qid": "Q1", "name": "One", "article": "One", "links": 90},
            {"qid": "Q2", "name": "Two", "article": "Two", "links": 80},
        ],
    )
    monkeypatch.setattr(subjects.sep, "fetch", lambda client=None: [])
    asked: list[str] = []

    def views(article, client=None, days=60):
        asked.append(article)
        return [100] * 53 + [200] * 7

    monkeypatch.setattr(subjects.wm, "pageviews", views)

    ranked = subjects.rank(cfg, enrich_top=0, covered={"wikidata:Q1"})

    assert asked == ["Two"]
    assert [c.name for c in ranked] == ["Two"]
