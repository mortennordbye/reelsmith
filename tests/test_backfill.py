"""Bringing a Reel that skipped the pipeline into the feedback loop.

The join here is a caption rather than a repo name, and a caption is the one
field a human can edit after the fact. So the tests that matter are the ones
about being wrong: a body that two run folders claim, a caption edited in the
app, a media that is not a Reel at all. Every one of them has to end as "not
matched" rather than as a confident pairing, because a wrong pairing puts a
rejected draft's hook next to a real post's numbers, and nothing downstream can
tell.

The other half is that registering a backfill must never arm the comment
poller. A private reply to a comment left days ago is the failure this whole
path is designed around.
"""

from __future__ import annotations

import json

import httpx
import pytest

from config import Settings
from pipeline import backfill

BODY = (
    "Ponytail is a rules file that makes your coding agent reach for the browser "
    "before it writes anything new."
)


@pytest.fixture
def cfg() -> Settings:
    return Settings(
        github_token="x",
        ig_user_id="17841400000000000",
        gateway_url="https://gate.example.test",
        gateway_token="test-token",
        _env_file=None,
    )


@pytest.fixture
def build(tmp_path):
    return tmp_path / "build"


def run_folder(
    build,
    day: str,
    slug: str,
    *,
    repo: str,
    caption: str,
    hook: str = "a hook",
    url: str | None = None,
) -> None:
    folder = build / day / slug
    folder.mkdir(parents=True)
    (folder / "repo.json").write_text(
        json.dumps({"full_name": repo, "url": url or f"https://github.com/{repo}"})
    )
    (folder / "script.json").write_text(json.dumps({"hook": hook}))
    (folder / "caption.txt").write_text(caption)


def media(
    media_id: str = "18000000000000001",
    *,
    caption: str,
    timestamp: str = "2026-07-31T18:04:11+0000",
    product: str | None = "REELS",
) -> dict:
    item = {
        "id": media_id,
        "caption": caption,
        "timestamp": timestamp,
        "permalink": f"https://www.instagram.com/reel/{media_id}/",
    }
    if product is not None:
        item["media_product_type"] = product
    return item


# ---------------------------------------------------------------------------
# The join
# ---------------------------------------------------------------------------


def test_matches_a_post_on_its_body_paragraph(cfg, build):
    run_folder(build, "2026-07-31", "dietrichgebert-ponytail", repo="dietrichgebert/ponytail",
               caption=f"{BODY}\n\n#claudecode #devtools", hook="Your agent rebuilds the browser")

    found = backfill.plan(cfg, build_dir=build, media=[media(caption=f"{BODY}\n\n#claudecode")])

    assert len(found.matched) == 1
    match = found.matched[0]
    assert match.repo_full_name == "dietrichgebert/ponytail"
    assert match.link == "https://github.com/dietrichgebert/ponytail"
    assert match.hook == "Your agent rebuilds the browser"
    assert match.published_at.isoformat() == "2026-07-31T18:04:11+00:00"
    assert not found.unmatched


def test_the_hashtag_block_and_the_comment_ask_do_not_have_to_agree(cfg, build):
    """The gateway inserts the ask above the hashtags on one path and not the
    other, so only the body is written once and never touched again."""
    run_folder(build, "2026-08-01", "nousresearch-hermes-agent", repo="NousResearch/hermes-agent",
               caption=f"{BODY}\n\n#opensource #llm")

    live = media(caption=f"{BODY}\n\nComment HERMES if you want the link.\n\n#opensource #llm")
    found = backfill.plan(cfg, build_dir=build, media=[live])

    assert [m.repo_full_name for m in found.matched] == ["NousResearch/hermes-agent"]


def test_whitespace_and_case_survive_a_phone_keyboard(cfg, build):
    run_folder(build, "2026-07-31", "a-b", repo="a/b", caption=f"{BODY}\n\n#tag")

    mangled = BODY.replace(" ", "  ").upper()
    found = backfill.plan(cfg, build_dir=build, media=[media(caption=f"{mangled}\n\n#tag")])

    assert len(found.matched) == 1


def test_a_body_two_run_folders_claim_matches_neither(cfg, build):
    """The `.v2` bug from `_runs_by_repo`, arriving through a different door.

    Two folders for one repo is the documented way to force a regeneration, and
    if both kept the same caption there is nothing here that can say which one
    shipped. Reporting it beats guessing right half the time.
    """
    run_folder(build, "2026-07-31", "dietrichgebert-ponytail", repo="dietrichgebert/ponytail",
               caption=f"{BODY}\n\n#tag", hook="the one that shipped")
    run_folder(build, "2026-07-31", "dietrichgebert-ponytail.v2", repo="dietrichgebert/ponytail",
               caption=f"{BODY}\n\n#tag", hook="a rejected draft")

    found = backfill.plan(cfg, build_dir=build, media=[media(caption=BODY)])

    assert not found.matched
    assert len(found.ambiguous) == 1


def test_a_caption_edited_after_posting_is_reported_not_guessed(cfg, build):
    run_folder(build, "2026-07-31", "a-b", repo="a/b", caption=f"{BODY}\n\n#tag")

    found = backfill.plan(cfg, build_dir=build, media=[media(caption="Something else entirely")])

    assert not found.matched
    assert [m["id"] for m in found.unmatched] == ["18000000000000001"]


def test_photos_and_stories_are_skipped(cfg, build):
    run_folder(build, "2026-07-31", "a-b", repo="a/b", caption=f"{BODY}\n\n#tag")

    found = backfill.plan(
        cfg, build_dir=build, media=[media(caption=BODY, product="FEED")]
    )

    assert not found.matched
    assert not found.unmatched


def test_a_run_folder_without_a_repo_url_is_not_registerable(cfg, build):
    folder = build / "2026-07-31" / "a-b"
    folder.mkdir(parents=True)
    (folder / "repo.json").write_text(json.dumps({"full_name": "a/b"}))
    (folder / "caption.txt").write_text(f"{BODY}\n\n#tag")

    found = backfill.plan(cfg, build_dir=build, media=[media(caption=BODY)])

    assert not found.matched
    assert len(found.unmatched) == 1


def test_a_missing_build_directory_is_empty_rather_than_an_error(cfg, tmp_path):
    found = backfill.plan(cfg, build_dir=tmp_path / "nope", media=[media(caption=BODY)])

    assert not found.matched
    assert len(found.unmatched) == 1


# ---------------------------------------------------------------------------
# Registering
# ---------------------------------------------------------------------------


def recording_gateway() -> tuple[httpx.Client, list[dict]]:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        # What a gateway on schema v8 or later answers. The wording is load
        # bearing: it is how the caller tells "measured" from "watched".
        return httpx.Response(200, json={"detail": f"measuring {body['media_id']}"})

    return httpx.Client(transport=httpx.MockTransport(handler)), seen


def test_registering_a_backfill_never_arms_the_comment_poller(cfg, build):
    run_folder(build, "2026-07-31", "dietrichgebert-ponytail", repo="dietrichgebert/ponytail",
               caption=f"{BODY}\n\n#tag")
    found = backfill.plan(cfg, build_dir=build, media=[media(caption=BODY)])

    client, seen = recording_gateway()
    done = backfill.apply(cfg, found.matched, client=client)

    assert len(done) == 1
    assert seen[0]["poll_comments"] is False
    assert seen[0]["media_id"] == "18000000000000001"


def test_the_real_publish_date_goes_with_it(cfg, build):
    """Otherwise the post is dated the moment somebody noticed it was missing,
    which is what `registered_at` would have said."""
    run_folder(build, "2026-07-31", "a-b", repo="a/b", caption=f"{BODY}\n\n#tag")
    found = backfill.plan(cfg, build_dir=build, media=[media(caption=BODY)])

    client, seen = recording_gateway()
    backfill.apply(cfg, found.matched, client=client)

    assert seen[0]["published_at"] == "2026-07-31T18:04:11+00:00"


def two_matched(cfg, build):
    run_folder(build, "2026-07-31", "a-b", repo="a/b", caption=f"{BODY}\n\n#tag")
    run_folder(build, "2026-07-31", "c-d", repo="c/d", caption="Another body\n\n#tag")
    found = backfill.plan(
        cfg,
        build_dir=build,
        media=[
            media("18000000000000001", caption=BODY),
            media("18000000000000002", caption="Another body"),
        ],
    )
    assert len(found.matched) == 2
    return found


def test_the_first_refusal_stops_the_rest(cfg, build):
    """One post to go and inspect, rather than a whole account."""
    found = two_matched(cfg, build)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body["media_id"])
        return httpx.Response(500, json={})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    done = backfill.apply(cfg, found.matched, client=client)

    assert done == []
    assert seen == ["18000000000000001"], "the second must never have been attempted"


def test_a_gateway_too_old_to_know_the_flag_is_caught_by_its_own_answer(cfg, build):
    """The deploy-skew case, and the reason the reply is read at all.

    Pydantic drops a field the model does not declare, so an old gateway takes
    the registration, ignores `poll_comments` and arms its poller. It cannot be
    prevented from here, only noticed, and the wording of the reply is the only
    evidence that says which of the two things it did.
    """
    found = two_matched(cfg, build)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"detail": "watching 18000000000000001"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    done = backfill.apply(cfg, found.matched, client=client)

    assert done == []
