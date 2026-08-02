"""Where the token travels.

One test file for one property: a live Instagram token, with publishing and
messaging rights, must not appear in a URL. It did once, and the way it got out
was not a bug in this service at all. httpx logs a URL per request at INFO,
somebody turned this package's logging up, and the token was in the query
string of every call. Anything that writes URLs down would have done the same.

So the assertions here are about the request as it leaves, not about the reply.
"""

from __future__ import annotations

import pytest

from gateway.graph import GraphClient
from tests.gateway_harness import ACCOUNT, IGSID, FakeMeta, settings

TOKEN = "a-live-token"


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


async def test_the_token_rides_in_a_header(graph, meta):
    await graph.list_comments(media_id="media-1", token=TOKEN)

    request = meta.requests[-1]
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert "access_token" not in request.url.params
    # The header form is instead of, not as well as. Sending both is a second
    # copy of the secret in the place that leaks.
    assert TOKEN not in str(request.url)


async def test_no_call_but_the_refresh_puts_the_token_in_a_url(graph, meta):
    """The whole surface, so a new endpoint cannot quietly reintroduce it."""
    await graph.send_private_reply(
        ig_user_id=ACCOUNT, token=TOKEN, comment_id="c1", text="hi"
    )
    await graph.send_message(ig_user_id=ACCOUNT, token=TOKEN, igsid=IGSID, text="hi")
    await graph.list_comments(media_id="media-1", token=TOKEN)
    await graph.media_insights(media_id="media-1", token=TOKEN)
    await graph.get_profile(igsid=IGSID, token=TOKEN)
    await graph.subscribe_messages(token=TOKEN)
    await graph.me(token=TOKEN)

    # Eight for seven calls: `media_insights` asks a second time without the
    # Reels-only metrics when the first attempt is refused.
    assert len(meta.requests) == 8
    for request in meta.requests:
        assert TOKEN not in str(request.url), request.url.path
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"


async def test_the_refresh_is_the_one_documented_exception(graph, meta):
    """Meta documents `refresh_access_token` with the token as a query
    parameter and no header form. This test exists so that stays a decision
    somebody made rather than a call that got missed."""
    fresh, expires_in = await graph.refresh_token(token=TOKEN)
    assert (fresh, expires_in) == ("fresher", 5_184_000)

    request = meta.requests[-1]
    assert request.url.params["access_token"] == TOKEN
    assert "Authorization" not in request.headers


async def test_the_query_string_still_carries_what_is_not_secret(graph, meta):
    await graph.media_insights(media_id="media-1", token=TOKEN)

    metrics = meta.requests[-1].url.params["metric"]
    assert "views" in metrics and "reach" in metrics
