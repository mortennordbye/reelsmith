"""The contract between the Mac and the gateway.

Same discipline as `pipeline/models.py`: if the pipeline and the gateway
disagree about a field, the disagreement should surface as a validation error
naming the field, not as a row with an empty column in it.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class PostRegistration(BaseModel):
    """A published Reel the poller should start watching."""

    media_id: str = Field(min_length=1)
    ig_user_id: str = Field(min_length=1)
    link: str = Field(min_length=1)
    # What the video told people to comment. Per post, because a video about a
    # repo may well ask for something better than "send".
    keyword: str = "send"

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


class CoverUploaded(BaseModel):
    """Where Meta can fetch the cover from."""

    name: str
    url: str


class Registered(BaseModel):
    ok: bool = True
    detail: str = ""
