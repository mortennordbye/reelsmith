"""The contract between the Mac and the gateway.

Same discipline as `pipeline/models.py`: if the pipeline and the gateway
disagree about a field, the disagreement should surface as a validation error
naming the field, not as a row with an empty column in it.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

# The account key arrives under either name for as long as a render host may be
# older than this image. `account_id` is what it is called everywhere now; a
# body still saying `ig_user_id` is a pipeline that has not been pulled yet, and
# refusing it would turn a rename with no behaviour into a broken publish. F10.
#
# The gateway image deploys itself and the render host is pulled by hand, so the
# side that lags is the one sending the old name. That asymmetry is the whole
# reason the alias is here rather than on the other side.
_ACCOUNT_KEY = AliasChoices("account_id", "ig_user_id")
_ACCEPTS_BOTH = ConfigDict(populate_by_name=True)


class PostRegistration(BaseModel):
    """A published Reel the poller should start watching."""

    model_config = _ACCEPTS_BOTH

    media_id: str = Field(min_length=1)
    account_id: str = Field(min_length=1, validation_alias=_ACCOUNT_KEY)
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

    model_config = _ACCEPTS_BOTH

    account_id: str = Field(min_length=1, validation_alias=_ACCOUNT_KEY)
    access_token: str = Field(min_length=1)
    username: str = ""
    # Meta hands this back with the long-lived token. Optional because a token
    # pasted by hand does not come with one, and an unknown expiry is treated as
    # due for refresh rather than as an error.
    expires_in: int | None = None
    # Which original account this destination belongs to, which is the
    # pipeline's `--account <name>`. Left off, the gateway derives it from the
    # username, which is right while one identity holds the same handle
    # everywhere. Say it explicitly when a second identity is registered, or
    # when its handle differs across platforms: this is what groups the three
    # rows into one account in the panel, and getting it wrong costs a board in
    # the wrong group rather than a post to the wrong place.
    brand: str = ""
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
    # Which original account this destination belongs to, which is the
    # pipeline's `--account <name>`. Left off, the gateway derives it from the
    # username, which is right while one identity holds the same handle
    # everywhere. Say it explicitly when a second identity is registered, or
    # when its handle differs across platforms: this is what groups the three
    # rows into one account in the panel, and getting it wrong costs a board in
    # the wrong group rather than a post to the wrong place.
    brand: str = ""

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


class TikTokAccountRegistration(BaseModel):
    """A TikTok account this gateway publishes to.

    Its own model for the same reason the YouTube one is: three platforms with
    three credential shapes, and one model covering all of them would be mostly
    optional and would validate none of them.

    The difference from YouTube's is the expiry. Google's refresh token has no
    clock; this one lasts a year, rotates on every use, and is rewritten by the
    refresher loop from the moment it is stored. `refresh_expires_in` is what
    the OAuth response returned, so the first deadline is recorded rather than
    guessed.
    """

    open_id: str = Field(min_length=1)
    client_key: str = Field(min_length=1)
    client_secret: str = Field(min_length=1)
    refresh_token: str = Field(min_length=1)
    # 31,536,000 seconds when it comes straight from the token endpoint. Left
    # off it is stored blank, which the refresher reads as "no deadline known"
    # and refreshes anyway, since it refreshes daily regardless.
    refresh_expires_in: int | None = None
    # The @handle, for the admin UI. Same column as an Instagram username.
    username: str = ""
    # Which original account this destination belongs to, which is the
    # pipeline's `--account <name>`. Left off, the gateway derives it from the
    # username, which is right while one identity holds the same handle
    # everywhere. Say it explicitly when a second identity is registered, or
    # when its handle differs across platforms: this is what groups the three
    # rows into one account in the panel, and getting it wrong costs a board in
    # the wrong group rather than a post to the wrong place.
    brand: str = ""

    @field_validator("open_id")
    @classmethod
    def _not_a_handle(cls, v: str) -> str:
        """Catch the handle-instead-of-open-id paste.

        There is no prefix to check the way `UC` guards a channel id, so this
        catches only the obvious mistake. An `@handle` or a URL would otherwise
        register cleanly and fail at the first publish, weeks later and nowhere
        near the cause.
        """
        v = v.strip()
        if v.startswith("@") or "/" in v:
            raise ValueError(
                "open_id must be the open id from the OAuth response, not a handle or URL"
            )
        return v


class FacebookAccountRegistration(BaseModel):
    """A Facebook Page this gateway publishes Reels to.

    **The one destination whose credentials are the account row.** A Page
    access token is a token plus an expiry, which is the shape `accounts` was
    built around, so unlike YouTube and TikTok there is no second table and no
    second write. What makes it a separate model from `AccountRegistration` is
    not the fields, which nearly match, but what the route does with them: no
    `subscribed_apps` call, because a Page here publishes and never answers
    messages, and the keyword mechanic is Instagram's alone.

    The token wanted here is a **long-lived Page token**, derived from a
    long-lived user token. Those do not expire on a clock, which is why
    `expires_in` is optional and why nothing in this service refreshes one. A
    short-lived Page token registers just as cleanly and stops working in an
    hour, which is the mistake this model cannot catch.
    """

    page_id: str = Field(min_length=1)
    access_token: str = Field(min_length=1)
    # Meta hands this back with a user token and usually not with a Page one.
    # Absent means "no clock known", which is the normal and correct state for
    # a long-lived Page token rather than something to warn about.
    expires_in: int | None = None
    # The Page name, for the admin UI. Same column as an Instagram username.
    username: str = ""
    # Which original account this destination belongs to, which is the
    # pipeline's `--account <name>`. Left off, the gateway derives it from the
    # username, which is right while one identity holds the same handle
    # everywhere. Say it explicitly when a second identity is registered, or
    # when its handle differs across platforms: this is what groups the rows
    # into one account in the panel, and getting it wrong costs a board in the
    # wrong group rather than a post to the wrong place.
    brand: str = ""

    @field_validator("page_id")
    @classmethod
    def _looks_like_a_page_id(cls, v: str) -> str:
        """Catch the vanity-name paste, which is the likely mistake.

        A Page id is digits. `facebook.com/thenightlybuild` and the vanity name
        on its own both address the Page perfectly well in a browser and
        neither works on `/{page-id}/video_reels`, so without this they would
        register cleanly and fail at the first publish, days later and nowhere
        near the cause.
        """
        v = v.strip()
        if not v.isdigit():
            raise ValueError("page_id must be the numeric Page id, not a name or URL")
        return v


class QueueSubmission(BaseModel):
    """A rendered Reel the gateway should publish on the next due slot.

    The files are uploaded through `/api/media` first, and what arrives here is
    the stored filename rather than a URL: the public hostname has changed once
    already, and a URL baked into a row that sits for a week would rot with it.
    """

    model_config = _ACCEPTS_BOTH

    account_id: str = Field(min_length=1, validation_alias=_ACCOUNT_KEY)
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

    model_config = _ACCEPTS_BOTH

    repo_full_name: str = Field(min_length=1)
    # Blank when the Mac has no account configured. See the v9 migration.
    account_id: str = Field(default="", validation_alias=_ACCOUNT_KEY)
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
