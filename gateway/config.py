"""Gateway configuration.

Deliberately not `config.Settings`. That one reads the repo's `.env` and
resolves paths under the checkout, and the container has neither a checkout nor
a dotenv. Everything here comes from the environment, which in the cluster is an
ExternalSecret and in local dev is the same `.env` the pipeline uses (the
`GATEWAY_` prefix keeps the two from colliding).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class GatewaySettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GATEWAY_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Secrets -----------------------------------------------------------
    # The app secret signs every webhook delivery. Without it the POST route
    # cannot tell Meta from anyone who found the URL, so it refuses to serve.
    app_secret: str = ""
    # Echoed back to Meta during the subscription handshake. Any random string,
    # as long as the dashboard and this agree.
    verify_token: str = ""
    # What the Mac presents on /api/*. One token, one client.
    api_token: str = ""

    # --- Meta API ----------------------------------------------------------
    # Same host and versioning story as pipeline/publisher.py: the Instagram
    # Login path. **This host is Instagram's and stays Instagram's.** The Page
    # path is a different login, a different permission set and a different
    # token, and it names `graph.facebook.com` as a constant inside
    # `gateway/facebook.py` rather than reading this. One setting serving two
    # login paths is a setting that is wrong for one of them.
    graph_host: str = "https://graph.instagram.com"
    # Shared, because it is genuinely the same Graph API version on both hosts.
    api_version: str = "v23.0"
    graph_timeout_s: float = 20.0

    # --- Behaviour ---------------------------------------------------------
    # How long someone waits between commenting and the DM arriving. Meta's own
    # messaging policy expects an automated experience to respond within 30
    # seconds, and a 60 second sweep misses that on a bad draw: worst case is a
    # full interval, so 60 meant up to a minute.
    #
    # At 20s the worst case is 20 seconds and the average is 10. The cost is
    # reads: one call per watched post per sweep, and posts age out after
    # `post_ttl_days`, so the ceiling is 7 posts x 180 sweeps/hour = 1260 reads
    # an hour. Private replies, the genuinely limited action at 750/hour, are
    # unaffected because they only happen once per comment.
    poll_interval_s: int = 20
    # How long a post stays in the poller's sweep. Meta refuses a private reply
    # more than seven days after the comment, so polling past that only burns
    # quota.
    post_ttl_days: int = 7
    default_keyword: str = "send"
    # The messaging window. After each inbound message the account may reply for
    # 24 hours and not a minute longer.
    message_window_h: int = 24
    # A follow that never arrives should not turn into an open-ended stream of
    # reminders. After this many nudges the conversation goes quiet and waits.
    max_nudges: int = 3
    # Matches the pipeline's margin. Long-lived tokens last 60 days, refresh is
    # free, and a missed cron is the only real failure mode.
    token_refresh_margin_days: int = 15
    token_refresh_interval_s: int = 86_400

    # --- TikTok ---------------------------------------------------------------
    # Off by default, like the scheduler and for the same reason: publishing to
    # a third real account should be a decision rather than something gained by
    # upgrading. See GATEWAY_TIKTOK_* in .env.example.
    tiktok_enabled: bool = False
    # Daily, and unlike the Meta margin this is not a "when it is nearly due"
    # check. TikTok's access token lasts 24 hours, so a daily pass is the
    # minimum that keeps one alive at all; the refresh token's own year is
    # extended as a side effect. Anything slower and the account is dead between
    # passes.
    tiktok_refresh_interval_s: int = 86_400
    # Direct Post or the inbox. Off until the audit lands, which
    # docs/tiktok-api-setup.md argues will probably be never: the audit reviews
    # a posting screen this repo does not have and rejects apps "designed for
    # private/personal use only". The unaudited path forces SELF_ONLY on a
    # private account, so the inbox is the only thing that actually publishes
    # before then.
    tiktok_direct_post: bool = False
    # Has to be one of the options `creator_info` returns or the post fails
    # `privacy_level_option_mismatch`. SELF_ONLY until audited, so the pre-audit
    # behaviour is chosen rather than discovered, exactly as
    # `youtube_privacy_status` was.
    tiktok_privacy_level: str = "SELF_ONLY"
    # The same question `containsSyntheticMedia` asks on YouTube, and answered
    # the same way for the same reason: the voice is a clone of a real person
    # reading a script that person commissioned. They move together or not at
    # all, so changing one without the other is the bug to look for.
    tiktok_is_aigc: bool = False
    tiktok_publish_timeout_s: int = 300
    tiktok_poll_interval_s: int = 5

    # --- Insights ------------------------------------------------------------
    # Reads how published Reels are doing. Read only against Meta: it creates
    # nothing, publishes nothing and messages nobody, which is why it is on by
    # default where the scheduler is not.
    insights_enabled: bool = True
    # Meta updates these roughly daily and a Reel keeps moving for days, so
    # anything faster spends Graph calls to redraw the same numbers. Six hours
    # gives four chances to catch the day rather than one.
    insights_interval_s: int = 21_600
    # Past this a Reel has stopped moving, and re-reading every post ever made
    # would grow a Graph call a day forever.
    insights_max_age_days: int = 30

    # --- Backups -----------------------------------------------------------
    # On by default and for the same reason insights are: it only reads. The
    # cost of it running unasked is some disk; the cost of it not running is
    # `comments_handled`, which cannot be rebuilt and whose loss re-DMs every
    # commenter still inside Meta's seven day reply window.
    backup_enabled: bool = True
    # Six hours, matching the insights sweep. The queue and the conversations
    # move slowly enough that losing a few hours of either is recoverable by
    # hand, and a copy is a full database rather than a delta.
    backup_interval_s: int = 21_600
    # Roughly three and a half days at six hours. Long enough that damage done
    # on a Friday is still recoverable on a Monday.
    backup_keep: int = 14

    # --- Scheduled publishing ----------------------------------------------
    # The whole queue is off unless this is on. A gateway that only answers
    # comments should not grow the ability to post to the feed by upgrading.
    scheduler_enabled: bool = False
    # How often the due check runs. A slot's jitter is minutes wide, so a minute
    # of granularity is plenty and the tick is nearly free: one query against a
    # table with tens of rows.
    scheduler_interval_s: int = 60
    # How late a missed slot may still fire. Longer than this and the day is
    # written off, because a pod that was down all night should not publish at
    # breakfast to an audience that is not there.
    scheduler_grace_minutes: int = 90
    # A publish that keeps failing stops asking. Each attempt costs an upload
    # fetch on Meta's side and the failure is nearly always structural.
    max_publish_attempts: int = 3
    # Meta transcodes an 8 MB Reel in about 35 seconds. The ceiling is generous
    # because the container stays valid for 24 hours either way.
    publish_timeout_s: int = 600
    publish_poll_interval_s: float = 5.0
    # Where the thumbnail comes from when no cover was uploaded. 90 frames at
    # 30fps, the same moment `cover.png` is rendered from in the pipeline, so
    # the fallback loses the hook band and nothing else.
    cover_thumb_offset_ms: int = 3000
    # Defaults for a slot created without them, and what the admin UI prefills.
    default_timezone: str = "UTC"
    default_jitter_minutes: int = 15

    # The schedule itself, declared rather than clicked. One slot per line, so
    # it drops into a ConfigMap as a block string:
    #
    #   GATEWAY_SLOTS: |
    #     18:00 Europe/Oslo jitter=15
    #     08:30 Europe/Oslo jitter=20 days=6,7
    #
    # Applied at startup and owned by config from then on: these rows are
    # replaced on every boot, so editing one in the admin UI would be undone by
    # the next rollout and the UI says so rather than letting it look sticky.
    # Slots added in the UI are a separate set and survive.
    slots: str = ""
    # Which account a config-declared slot belongs to. One account is the
    # normal case; with several, declare slots in the UI instead.
    slots_account: str = ""

    # --- Admin UI ----------------------------------------------------------
    # Off by default, and that default is deliberate. This service is publicly
    # reachable by necessity: Meta fetches `/media/*` and posts to `/webhook`
    # from its own servers, so there is no network boundary to hide behind. An
    # admin panel that publishes to a real account cannot default to on.
    admin_enabled: bool = False
    # The panel's own password. Set this, or `admin_trust_proxy_auth` when
    # something in front is already authenticating. With neither, a panel that
    # is switched on refuses to start rather than serving itself to the
    # internet.
    admin_token: str = ""
    # Say explicitly that forward-auth (Authentik at Traefik, here) protects
    # /admin. An opt-in rather than an assumption, because "I thought the
    # ingress was doing it" is how these get exposed.
    admin_trust_proxy_auth: bool = False
    # Session length for the panel's cookie.
    admin_session_hours: int = 12

    # What this service's own loggers emit. The uvicorn access log is held at
    # WARNING regardless, because health checks every ten seconds bury
    # everything worth reading.
    log_level: str = "INFO"

    # --- YouTube -----------------------------------------------------------
    # No enable flag, deliberately. A channel that is not registered publishes
    # nothing, `scheduler_enabled` already gates all publishing, and
    # `accounts.active` is the per-destination kill switch. A fourth switch
    # would be a fourth thing to check when nothing goes out.
    #
    # What a published Short goes out as.
    #
    # The documentation says uploads from an API project created after 28 July
    # 2020 and not yet audited are locked to private whatever this asks for.
    # That is not true of this project, measured rather than assumed on
    # 2026-08-16: three uploads asking for private, unlisted and public each
    # kept the value they asked for, with no rejection reason. So the audit
    # gates quota and standing, not publishing.
    #
    # Left at private as the safe default for a fresh deployment rather than
    # because anything forces it. The scheduler still logs if a request comes
    # back downgraded, since that is what the lock would look like if it ever
    # does apply here.
    youtube_privacy_status: str = "private"
    # A declaration, not a default, and answered the same way on both platforms
    # that ask. **Revisited on 2026-08-26 against TikTok's `is_aigc`, which is
    # the same question**, and kept at False for a reason rather than because a
    # value had to be sent.
    #
    # What these fields ask is whether the content is synthetic media: generated
    # or meaningfully altered so that it depicts something that did not happen.
    # Here the voice is a clone of a real person, reading a script that person
    # commissioned, about a repository that exists, over a screenshot of that
    # repository's own README. Nothing depicts anything that did not happen. A
    # cloned voice reading its owner's words is closer to a person using a
    # microphone than to a fabricated likeness, and the label exists for the
    # second thing.
    #
    # That is a judgement and it is worth re-reading if the format ever gains a
    # face, a person who did not consent, or a claim the video acts out rather
    # than reports. **The two flags move together or not at all**, and one
    # changed without the other is the bug to look for. See PROFILE.md.
    youtube_synthetic_media: bool = False
    # Still deliberately absent: GATEWAY_YOUTUBE_ENABLED.
    #
    # `docs/youtube-publishing-plan.md` names it and it was never built. It
    # would be a second gate on something `scheduler_enabled` already gates
    # completely, because publishing to YouTube still happens only when a slot
    # fires, and a slot fires only when the scheduler is on.
    #
    # The flag below is the narrower thing and its name says which. A flag earns
    # its place when something runs without a slot, and as of the analytics
    # sweep exactly one YouTube thing does.
    #
    # On by default, for the same reason `insights_enabled` is: it only reads,
    # it asks Google nothing on a deployment with no YouTube account, and the
    # cost of it running unasked is a report nobody opens. Off is the switch for
    # a channel whose refresh token has died, where the alternative is a sweep
    # spending a failed token mint every six hours and a `graph_errors` count
    # that says nothing about Meta.
    youtube_insights_enabled: bool = True
    # Shorts are 10 to 20 MB and go up in seconds on any real connection. The
    # ceiling is for a stalled transfer, not a slow one.
    youtube_upload_timeout_s: int = 600

    # --- Facebook -------------------------------------------------------------
    # **No GATEWAY_FACEBOOK_ENABLED, and that is the same decision YouTube's
    # absence records.** Publishing here runs only when a slot fires, and a slot
    # only fires when the scheduler is on, so a flag would gate something that
    # is already gated. TikTok earned one because its token refresher runs
    # without a slot; nothing on this path does, because a Page token does not
    # rotate and there is no refresher to run.
    #
    # The insights sweep is the one Facebook thing that runs without a slot, so
    # it is the one thing with a flag, exactly as on YouTube. On by default: it
    # only reads, a deployment with no Page asks Meta nothing, and off is the
    # switch for a Page whose token has died, where the alternative is a failed
    # call every six hours and a `graph_errors` count that says nothing about
    # what actually broke.
    facebook_insights_enabled: bool = True
    # Meta fetches the video rather than being handed it, so this is a ceiling
    # on somebody else's download of a 10 to 20 MB file, not on an upload from
    # here. Wide, because the failure it guards against is a stall.
    facebook_upload_timeout_s: int = 600
    # `finish` returning success means the request was accepted, not that a
    # Reel is visible. Transcoding follows and can fail on its own, so the
    # publisher waits for `publish_status` the way every other path waits for
    # its platform to say the post exists.
    facebook_publish_timeout_s: int = 300
    facebook_poll_interval_s: int = 5

    # --- Storage -----------------------------------------------------------
    db_path: Path = Path("gateway/dev.sqlite3")
    covers_dir: Path = Path("gateway/covers")
    # Where Meta and the browser reach this service. Used to build the cover
    # URL handed back to the pipeline, so it has to be the external name.
    public_base_url: str = "http://localhost:8000"

    @field_validator("public_base_url")
    @classmethod
    def _no_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @property
    def backup_dir(self) -> Path:
        """Beside the database, on the same volume, on purpose.

        A copy here answers "the file is wrong", which is the failure with no
        other remedy. It does not answer "the volume is gone", and pretending
        otherwise by putting it somewhere that merely looks separate would be
        worse than being clear about what it covers.
        """
        return self.db_path.parent / "backups"

    @property
    def graph_base(self) -> str:
        return f"{self.graph_host.rstrip('/')}/{self.api_version}"

    @property
    def message_window_s(self) -> int:
        return self.message_window_h * 3600


class GatewayConfigError(RuntimeError):
    """A configuration problem that has to be fixed before serving."""


# Enough that guessing is not the attack. `openssl rand -hex 24` gives 48.
MIN_ADMIN_TOKEN_CHARS = 24


def require_secrets(cfg: GatewaySettings) -> None:
    """Fail at startup rather than on the first request.

    An unsigned webhook route and a bearer route with an empty token are both
    open doors, and both would look fine in a smoke test.
    """
    missing = [
        name
        for name, value in (
            ("GATEWAY_APP_SECRET", cfg.app_secret),
            ("GATEWAY_VERIFY_TOKEN", cfg.verify_token),
            ("GATEWAY_API_TOKEN", cfg.api_token),
        )
        if not value
    ]
    if missing:
        raise GatewayConfigError(f"Not set: {', '.join(missing)}")

    require_admin_auth(cfg)


def require_admin_auth(cfg: GatewaySettings) -> None:
    """Refuse to serve an unauthenticated control panel.

    Checked at startup rather than per request, and it fails the boot on
    purpose. The panel can publish to a real Instagram account, flip the kill
    switch and rewrite captions, and this service has to be publicly reachable
    for Meta to fetch media from it. A crashlooping pod is a much better
    outcome than a control panel someone finds.
    """
    if not cfg.admin_enabled:
        return
    if cfg.admin_token:
        # A short token on a route reachable from the internet is a password
        # someone will guess. Refused here rather than warned about, because a
        # warning in a startup log is a thing nobody reads.
        if len(cfg.admin_token) < MIN_ADMIN_TOKEN_CHARS:
            raise GatewayConfigError(
                f"GATEWAY_ADMIN_TOKEN is shorter than {MIN_ADMIN_TOKEN_CHARS} characters. "
                f"Generate one with `openssl rand -hex 24`."
            )
        return
    if cfg.admin_trust_proxy_auth:
        return
    raise GatewayConfigError(
        "GATEWAY_ADMIN_ENABLED is on with no authentication. Set "
        "GATEWAY_ADMIN_TOKEN, or GATEWAY_ADMIN_TRUST_PROXY_AUTH=true if "
        "forward-auth already protects /admin."
    )


@lru_cache(maxsize=1)
def get_gateway_settings() -> GatewaySettings:
    return GatewaySettings()
