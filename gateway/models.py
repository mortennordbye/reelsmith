"""The contract between the Mac and the gateway.

Same discipline as `pipeline/models.py`: if the pipeline and the gateway
disagree about a field, the disagreement should surface as a validation error
naming the field, not as a row with an empty column in it.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class PostRegistration(BaseModel):
    """A published Reel the poller should start watching."""

    media_id: str = Field(min_length=1)
    ig_user_id: str = Field(min_length=1)
    link: str = Field(min_length=1)
    # What the video told people to comment. Per post, because a video about a
    # repo may well ask for something better than "send".
    keyword: str = "send"
    # When it actually went out, for a post being registered after the fact.
    # The publisher leaves this off, because it registers seconds after the
    # media id exists and `registered_at` says the same thing.
    published_at: datetime | None = None
    # False registers a post to be measured without arming the comment poller.
    # A backfilled Reel is days old, and a private reply to a comment its author
    # has forgotten reads as a bot rather than as an answer.
    poll_comments: bool = True

    @field_validator("link")
    @classmethod
    def _http_only(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError("link must be an http or https URL")
        return v

    @field_validator("keyword")
    @classmethod
    def _one_word(cls, v: str) -> str:
        v = v.strip()
        if not v or len(v.split()) != 1:
            raise ValueError("keyword must be a single word")
        return v


class AccountRegistration(BaseModel):
    """An Instagram account this gateway answers for."""

    ig_user_id: str = Field(min_length=1)
    access_token: str = Field(min_length=1)
    username: str = ""
    # Meta hands this back with the long-lived token. Optional because a token
    # pasted by hand does not come with one, and an unknown expiry is treated as
    # due for refresh rather than as an error.
    expires_in: int | None = None
    # Whether to call subscribed_apps. Off in tests and when the subscription is
    # already known good.
    subscribe: bool = True


class YouTubeAccountRegistration(BaseModel):
    """A YouTube channel this gateway publishes to.

    A separate model from `AccountRegistration` rather than a union with it,
    because almost nothing matches: no long-lived token, no expiry, no messages
    subscription, and three credential fields Meta has no equivalent of. One
    model covering both would be half optional and would validate neither.

    The one-time browser authorisation happens wherever it is convenient. What
    arrives here is its result, and this is where it lives from then on: the
    refresh token belongs in the cluster secret rather than in a file beside
    the renderer, because publishing happens here.
    """

    channel_id: str = Field(min_length=1)
    client_id: str = Field(min_length=1)
    client_secret: str = Field(min_length=1)
    refresh_token: str = Field(min_length=1)
    # The @handle, for the admin UI. Same column as an Instagram username.
    username: str = ""

    @field_validator("channel_id")
    @classmethod
    def _looks_like_a_channel_id(cls, v: str) -> str:
        """Catch the handle-instead-of-id paste, which is the likely mistake.

        Channel ids are `UC` and 22 more characters. Pasting `@handle` or a
        channel URL here would otherwise register cleanly and fail at the first
        publish, weeks later and nowhere near the cause.
        """
        v = v.strip()
        if not v.startswith("UC"):
            raise ValueError("channel_id must be the UC... channel id, not a handle or URL")
        return v


class QueueSubmission(BaseModel):
    """A rendered Reel the gateway should publish on the next due slot.

    The files are uploaded through `/api/media` first, and what arrives here is
    the stored filename rather than a URL: the public hostname has changed once
    already, and a URL baked into a row that sits for a week would rot with it.
    """

    ig_user_id: str = Field(min_length=1)
    video_name: str = Field(min_length=1)
    cover_name: str | None = None
    caption: str = ""
    # Required by YouTube, meaningless on Instagram, and built on the Mac where
    # the hook and the wording rules live. Capped at what YouTube accepts so a
    # too-long title is a validation error naming the field rather than a
    # rejected upload days later.
    title: str = Field(default="", max_length=100)
    keyword: str = "send"
    link: str = Field(min_length=1)
    repo_full_name: str | None = None
    # The checkout and settings that wrote this script, from
    # `pipeline/results.py`. Optional, because a client that does not send one
    # is saying the video exists and nothing recorded what made it, which is
    # different from claiming it was made by the current code.
    recipe: str = Field(default="", max_length=120)
    # What was on screen for the first three seconds, which is the thing
    # `skip_rate` scores. Capped well above the scriptwriter's own 60 character
    # limit, because a cap that rejects is a queue refusing a rendered video
    # over a label.
    hook: str = Field(default="", max_length=300)
    # Off by default. See db.QUEUE_DRAFT for why arming is a separate act.
    approved: bool = False
    # Pins this post to a wall-clock time instead of the next free slot.
    slot_override: datetime | None = None

    @field_validator("link")
    @classmethod
    def _http_only(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError("link must be an http or https URL")
        return v

    @field_validator("keyword")
    @classmethod
    def _one_word(cls, v: str) -> str:
        v = v.strip()
        if not v or len(v.split()) != 1:
            raise ValueError("keyword must be a single word")
        return v

    @field_validator("video_name", "cover_name")
    @classmethod
    def _basename_only(cls, v: str | None) -> str | None:
        """No separators, so a queued name can never walk out of covers_dir.

        The serving routes rebuild from the basename too, but a row that cannot
        hold a traversal in the first place is one fewer thing to get right.
        """
        if v is None:
            return None
        if "/" in v or "\\" in v or v in {".", ".."}:
            raise ValueError("must be a bare filename")
        return v


class Queued(BaseModel):
    """What the Mac gets back after pushing a post."""

    id: int
    state: str
    detail: str = ""


class CoverUploaded(BaseModel):
    """Where Meta can fetch the cover from."""

    name: str
    url: str


class Registered(BaseModel):
    ok: bool = True
    detail: str = ""


class RenderedRepo(BaseModel):
    """A repo whose Reel has been built but not committed to a slot.

    Weaker than `QueueSubmission` on purpose, and it carries no media, because
    nothing is uploaded at render time. It exists so the next discovery pass
    does not rebuild a video that is already on disk.
    """

    repo_full_name: str = Field(min_length=1)
    # Blank when the Mac has no account configured. See the v9 migration.
    ig_user_id: str = ""
    # `2026-08-08/firecrawl-anydoc`, so a person reading the row can find the
    # video it is talking about.
    run_folder: str = ""
    # What the scorer gave it and why, from `score_candidates`. The ranking has
    # never left the machine that ran it, so nothing could answer why discovery
    # keeps landing on the same corner of GitHub.
    score: float = 0.0
    # The components as the pipeline already writes them, not columns. The
    # weights are config and have changed twice, so a fixed set of fields would
    # need a migration each time and still not say what the weights were on the
    # day. Capped because it arrives over the network and nothing reads inside
    # it.
    score_breakdown: dict[str, float] = Field(default_factory=dict, max_length=20)
