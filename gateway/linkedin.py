"""Publishing a post to a personal LinkedIn profile.

The fifth destination and the first that is not video. What gets published is
text, optionally with an article card pointing at a blog entry, so a queue row
for LinkedIn carries an empty `video_name` and its picture, if any, in
`cover_name`.

**The credential is the account row**, as on Facebook. The self-serve *Share on
LinkedIn* product mints a 60 day access token and no refresh token, so there is
nothing to rotate and nothing to refresh: when the token runs out, the consent
trip is walked again. The panel counts down to that, because nothing else can.

Two calls, and only the second one creates anything a person can see:

| phase | endpoint |
|---|---|
| thumbnail | `POST /rest/images?action=initializeUpload`, then `PUT` the bytes |
| post | `POST /rest/posts` |

**The line between a retry that is safe and one that is not sits at the post.**
An uploaded image that no post references is invisible, so a failure anywhere
before `POST /rest/posts` is safe to retry. Once that call has been sent the
post may exist whatever came back, and a duplicate under somebody's own name is
worse than a missed slot, so that failure is terminal. The same line
`facebook.PublishError` draws, one step later because there is no upload
session to stand in for the post.

**The API does not scrape the link card.** The web composer fetches a URL's
Open Graph tags and builds the card; the Posts API deliberately does not, so an
article post sends its own title and thumbnail. That is why the thumbnail is
uploaded rather than left to LinkedIn.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

API = "https://api.linkedin.com"

# LinkedIn refuses commentary past this with FIELD_LENGTH_TOO_LONG. Checked
# here so a draft that is too long fails at the row with a sentence rather than
# at the slot with an error code.
MAX_COMMENTARY = 3000

# The characters LinkedIn's "little" text format treats as markup. Unescaped,
# a parenthesis or a bracket can swallow the rest of the post, which is a
# failure nobody sees until the post is live and half missing. `#` is left out
# on purpose: escaping it would turn every hashtag into plain text.
_LITTLE_RESERVED = "\\|{}@[]()<>*_~"


class PublishError(RuntimeError):
    """A publish that did not complete.

    `post_sent` is what the caller acts on. False means `POST /rest/posts` was
    never sent and a retry is provably safe. True means a post may exist.
    """

    def __init__(self, message: str, *, post_sent: bool = False, status: int = 0):
        super().__init__(message)
        self.post_sent = post_sent
        self.status = status


@dataclass(frozen=True)
class PublishResult:
    post_urn: str
    permalink: str


def person_urn(person_id: str) -> str:
    """The author URN for an account key.

    The account key is the OpenID `sub`, which is the id part alone, so the
    row reads the same way a Page id or a channel id does.
    """
    return person_id if person_id.startswith("urn:") else f"urn:li:person:{person_id}"


def permalink(post_urn: str) -> str:
    return f"https://www.linkedin.com/feed/update/{post_urn}/"


def escape_commentary(text: str) -> str:
    """Escape the little-format markup characters, leaving hashtags alone."""
    return "".join(f"\\{ch}" if ch in _LITTLE_RESERVED else ch for ch in text)


def _headers(token: str, version: str) -> dict[str, str]:
    # The token in a header, never in a URL, which is the rule every other
    # platform module here keeps.
    return {
        "Authorization": f"Bearer {token}",
        "LinkedIn-Version": version,
        "X-Restli-Protocol-Version": "2.0.0",
    }


def _error(response: httpx.Response) -> str:
    """LinkedIn's error envelope, `{"message", "status", "serviceErrorCode"}`."""
    try:
        payload = response.json() if response.content else {}
    except ValueError:
        payload = {}
    if isinstance(payload, dict) and payload.get("message"):
        return f"{response.status_code} {payload['message']}"
    return f"{response.status_code} {response.text[:400]}"


async def upload_image(
    http: httpx.AsyncClient,
    *,
    token: str,
    person_id: str,
    payload: bytes,
    content_type: str,
    api_version: str,
) -> str:
    """Upload one image and return its `urn:li:image:...`.

    Every failure here leaves nothing anybody can see, so all of them are
    raised with `post_sent=False`.
    """
    try:
        response = await http.post(
            f"{API}/rest/images?action=initializeUpload",
            headers=_headers(token, api_version),
            json={"initializeUploadRequest": {"owner": person_urn(person_id)}},
        )
    except httpx.HTTPError as exc:
        raise PublishError(f"image initializeUpload failed: {exc}") from exc
    if response.status_code >= 400:
        raise PublishError(
            f"image initializeUpload refused: {_error(response)}", status=response.status_code
        )
    value = (response.json() or {}).get("value") or {}
    upload_url, image = value.get("uploadUrl"), value.get("image")
    if not upload_url or not image:
        raise PublishError("image initializeUpload returned no uploadUrl or image urn")

    try:
        put = await http.put(
            upload_url,
            headers={"Authorization": f"Bearer {token}", "Content-Type": content_type},
            content=payload,
        )
    except httpx.HTTPError as exc:
        raise PublishError(f"image upload failed: {exc}") from exc
    if put.status_code >= 400:
        raise PublishError(f"image upload refused: {_error(put)}", status=put.status_code)
    return str(image)


def post_body(
    *,
    person_id: str,
    commentary: str,
    link: str = "",
    title: str = "",
    thumbnail: str = "",
    image: str = "",
) -> dict:
    """The `POST /rest/posts` body for one of the three shapes this sends.

    A link makes an article post with its own card. An image without a link is
    an image post. Neither is text alone. Both at once is an article whose
    thumbnail is that image, because LinkedIn shows one or the other and the
    card is what carries a reader to the blog.
    """
    body: dict = {
        "author": person_urn(person_id),
        "commentary": escape_commentary(commentary),
        "visibility": "PUBLIC",
        "distribution": {
            "feedDistribution": "MAIN_FEED",
            "targetEntities": [],
            "thirdPartyDistributionChannels": [],
        },
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": False,
    }
    if link:
        article: dict = {"source": link, "title": title or link}
        if thumbnail or image:
            article["thumbnail"] = thumbnail or image
        body["content"] = {"article": article}
    elif image:
        body["content"] = {"media": {"id": image}}
    return body


async def create_post(
    http: httpx.AsyncClient, *, token: str, body: dict, api_version: str
) -> PublishResult:
    """Send the post. Every failure past the send is `post_sent=True`.

    That includes a timeout and a 5xx, since neither says the post was not
    made, and it excludes a 4xx refusal of the body itself, which LinkedIn
    answers before creating anything.
    """
    try:
        response = await http.post(
            f"{API}/rest/posts", headers=_headers(token, api_version), json=body
        )
    except httpx.TimeoutException as exc:
        raise PublishError(f"post timed out after sending: {exc}", post_sent=True) from exc
    except httpx.HTTPError as exc:
        # A connection that never opened sent nothing. Anything else may have.
        sent = not isinstance(exc, httpx.ConnectError)
        raise PublishError(f"post failed: {exc}", post_sent=sent) from exc

    if response.status_code >= 500:
        raise PublishError(
            f"post failed: {_error(response)}", post_sent=True, status=response.status_code
        )
    if response.status_code >= 400:
        raise PublishError(
            f"post refused: {_error(response)}", post_sent=False, status=response.status_code
        )
    urn = response.headers.get("x-restli-id") or ""
    if not urn:
        # Created, and LinkedIn did not say as what. Live, so not retryable,
        # and with no id for the insights join, so worth a person's look.
        raise PublishError("post created but x-restli-id was missing", post_sent=True)
    return PublishResult(post_urn=urn, permalink=permalink(urn))

