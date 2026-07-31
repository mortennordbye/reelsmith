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
    # Meta allows 750 private replies per hour per account. One sweep a minute
    # over a handful of posts is not close to it, and the reply window is seven
    # days, so an hour of downtime costs nothing.
    poll_interval_s: int = 60
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


@lru_cache(maxsize=1)
def get_gateway_settings() -> GatewaySettings:
    return GatewaySettings()
