"""The comment sweep and the token refresher.

The sweep exists because real-time comment webhooks need App Review. It is also
the more reliable half of the design, so the tests that matter here are the ones
about it not losing or double-counting work across cycles.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from gateway import db, poller
from gateway.config import GatewaySettings
from gateway.graph import GraphClient
from gateway.metrics import Metrics
from tests.gateway_harness import ACCOUNT, FakeMeta, comment, settings

LINK = "https://github.com/DietrichGebert/ponytail"


@pytest.fixture
def cfg(tmp_path):
    return settings(tmp_path)


@pytest.fixture
def meta():
    return FakeMeta()


@pytest.fixture
async def graph(meta, cfg):
    async with meta.client() as client:
        yield GraphClient(client, cfg)


@pytest.fixture
def metrics():
    return Metrics()


@pytest.fixture
async def conn(cfg):
    connection = await db.connect(cfg.db_path)
    await db.upsert_account(connection, ig_user_id=ACCOUNT, access_token="tok")
    await db.register_post(
        connection, media_id="media-1", ig_user_id=ACCOUNT, keyword="send", link=LINK
    )
    yield connection
    await connection.close()


# --- Sweeping ---------------------------------------------------------------


async def test_only_matching_comments_get_a_reply(conn, graph, cfg, metrics, meta):
    meta.comments = [
        comment("c1", "SEND"),
        comment("c2", "nice video"),
        comment("c3", "send please"),
    ]

    sent = await poller.poll_once(conn, graph, cfg, metrics)

    assert sent == 2
    assert len(meta.sends) == 2


async def test_a_second_sweep_replies_to_nothing_twice(conn, graph, cfg, metrics, meta):
    meta.comments = [comment("c1", "send")]
    await poller.poll_once(conn, graph, cfg, metrics)
    meta.sends.clear()

    await poller.poll_once(conn, graph, cfg, metrics)

    assert meta.sends == []


async def test_a_new_comment_between_sweeps_is_picked_up(conn, graph, cfg, metrics, meta):
    meta.comments = [comment("c1", "send")]
    await poller.poll_once(conn, graph, cfg, metrics)
    meta.comments.append(comment("c2", "send"))
    meta.sends.clear()

    await poller.poll_once(conn, graph, cfg, metrics)

    assert len(meta.sends) == 1


async def test_a_post_past_the_reply_window_is_no_longer_polled(conn, graph, cfg, metrics, meta):
    """Meta refuses a private reply more than seven days after the comment.

    Polling past that only spends quota on replies that cannot be sent.
    """
    meta.comments = [comment("c1", "send")]
    stale = db.iso(db.now() - timedelta(days=cfg.post_ttl_days + 1))
    await conn.execute("UPDATE posts SET registered_at = ?", (stale,))
    await conn.commit()

    sent = await poller.poll_once(conn, graph, cfg, metrics)

    assert sent == 0
    assert meta.calls == []


async def test_a_paused_account_is_skipped_entirely(conn, graph, cfg, metrics, meta):
    meta.comments = [comment("c1", "send")]
    await db.set_account_flags(conn, ACCOUNT, active=False)

    sent = await poller.poll_once(conn, graph, cfg, metrics)

    assert sent == 0
    assert meta.calls == []


async def test_one_unreadable_post_does_not_stop_the_others(conn, graph, cfg, metrics, meta):
    """A deleted post, or one with comments turned off, is a normal event."""
    await db.register_post(
        conn, media_id="media-2", ig_user_id=ACCOUNT, keyword="send", link=LINK
    )
    calls = {"n": 0}
    original = graph.list_comments

    async def flaky(*, media_id: str, token: str, limit: int = 50):
        calls["n"] += 1
        if media_id == "media-1":
            from gateway.graph import GraphError

            raise GraphError("Unsupported get request", code=100)
        return await original(media_id=media_id, token=token, limit=limit)

    graph.list_comments = flaky
    meta.comments = [comment("c1", "send")]

    sent = await poller.poll_once(conn, graph, cfg, metrics)

    assert calls["n"] == 2
    assert sent == 1


async def test_a_sweep_records_when_it_last_succeeded(conn, graph, cfg, metrics):
    await poller.poll_once(conn, graph, cfg, metrics)

    row = await db.get_post(conn, "media-1")
    assert row["last_polled_at"] is not None
    assert metrics.registry.get_sample_value("reelsmith_poll_cycles_total") == 1


# --- Token refresh ----------------------------------------------------------


async def test_a_token_well_inside_its_life_is_left_alone(conn, graph, cfg, metrics, meta):
    await db.save_account_token(conn, ACCOUNT, "tok", 60 * 86_400)

    refreshed = await poller.refresh_tokens_once(conn, graph, cfg, metrics)

    assert refreshed == 0
    assert meta.calls == []


async def test_a_token_inside_the_margin_is_refreshed(conn, graph, cfg, metrics):
    await db.save_account_token(conn, ACCOUNT, "tok", 5 * 86_400)

    refreshed = await poller.refresh_tokens_once(conn, graph, cfg, metrics)

    assert refreshed == 1
    row = await db.get_account(conn, ACCOUNT)
    assert row["access_token"] == "fresher"


async def test_an_unknown_expiry_counts_as_due(conn, graph, cfg, metrics):
    """That is the state a hand-pasted token is in, and refreshing it is how it
    gets an expiry we can count down from."""
    refreshed = await poller.refresh_tokens_once(conn, graph, cfg, metrics)

    assert refreshed == 1


async def test_an_expired_token_is_not_retried_forever(conn, graph, cfg, metrics, meta):
    """Past expiry a refresh cannot work. Someone has to re-authorise."""
    past = db.iso(db.now() - timedelta(days=1))
    await conn.execute("UPDATE accounts SET token_expires_at = ?", (past,))
    await conn.commit()

    refreshed = await poller.refresh_tokens_once(conn, graph, cfg, metrics)

    assert refreshed == 0
    assert meta.calls == []


async def test_the_days_left_gauge_is_what_a_dashboard_would_alert_on(conn, graph, cfg, metrics):
    await db.save_account_token(conn, ACCOUNT, "tok", 40 * 86_400)

    await poller.refresh_tokens_once(conn, graph, cfg, metrics)

    days = metrics.registry.get_sample_value(
        "reelsmith_token_days_left", {"ig_user_id": ACCOUNT}
    )
    assert 39 < days < 41


# --- Schema -----------------------------------------------------------------


async def test_the_schema_migration_is_idempotent(cfg):
    conn = await db.connect(cfg.db_path)
    try:
        assert await db.migrate(conn) == db.SCHEMA_VERSION
        assert await db.migrate(conn) == db.SCHEMA_VERSION
    finally:
        await conn.close()


def test_settings_reject_a_trailing_slash_on_the_public_url(tmp_path):
    cfg = settings(tmp_path, public_base_url="https://gate.example.test/")
    assert cfg.public_base_url == "https://gate.example.test"


def test_the_graph_base_matches_the_publishers_shape():
    cfg = GatewaySettings(app_secret="x", verify_token="y", api_token="z", _env_file=None)
    assert cfg.graph_base == "https://graph.instagram.com/v23.0"
