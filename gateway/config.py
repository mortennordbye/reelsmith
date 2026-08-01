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
    # Login path, no Facebook Page anywhere in it.
    graph_host: str = "https://graph.instagram.com"
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
