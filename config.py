"""Central configuration.

Everything tunable lives here so no stage has to reach for an env var directly.
Import `get_settings()` rather than constructing `Settings` yourself -- it is
cached, so the `.env` file is read exactly once per process.
"""

from __future__ import annotations

import shutil
from datetime import date
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).parent.resolve()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Credentials -------------------------------------------------------
    # Script generation uses the Claude Code CLI's existing subscription auth,
    # so there is no ANTHROPIC_API_KEY anywhere in here.
    github_token: str = Field(default="", description="GitHub PAT, no scopes needed")

    # --- Instagram publishing ----------------------------------------------
    # Optional: everything except `--post` and `--publish` runs without these.
    # Setup is docs/instagram-api-setup.md; the short version is a Meta app in
    # development mode with your own account added as a tester, which needs no
    # App Review.
    ig_user_id: str = ""
    # The seed token only. The live one lives in data/ig_token.json, because a
    # refreshed token has to be written back somewhere and rewriting .env from
    # a cron job is a good way to lose the rest of the file.
    ig_access_token: str = ""
    # graph.instagram.com is the Instagram Login path. The Facebook Login path
    # is graph.facebook.com with a different permission set; only the host
    # changes here, so it is a setting rather than a fork in the code.
    ig_graph_host: str = "https://graph.instagram.com"
    ig_api_version: str = "v23.0"
    # Meta suggests polling a container once a minute for no more than five.
    # Ours are 30-45s of 1080x1920, which in practice finish inside a minute,
    # so poll faster and keep the ceiling.
    ig_poll_interval_s: int = 8
    ig_publish_timeout_s: int = 300
    ig_upload_timeout_s: int = 600
    # Refresh when the token has less than this left. Long-lived tokens last 60
    # days and can be refreshed any time after they are 24 hours old, so a wide
    # margin costs nothing and a narrow one risks a missed cron.
    ig_refresh_margin_days: int = 15

    # --- DM gateway (optional) ---------------------------------------------
    # The self-hosted service that answers comments and DMs, and hosts the
    # cover image Meta fetches. Leaving gateway_url empty disables both calls,
    # which is the normal state for anyone who does not run it. Everything the
    # pipeline asks of it is best effort: see pipeline/gateway.py.
    gateway_url: str = ""
    gateway_token: str = ""
    # What the video tells people to comment. Per-post on the gateway side, so
    # this is only the default the pipeline registers with.
    gateway_keyword: str = "send"

    # --- Topic selection ---------------------------------------------------
    min_stars_breakout: int = 400
    min_stars_established: int = 2000
    breakout_window_days: int = 90
    pushed_window_days: int = 7
    repo_cooldown_days: int = 30
    candidates_per_query: int = 50

    # --- Script generation (Claude Code CLI) -------------------------------
    claude_model: str = "opus"
    claude_effort: str = "high"
    claude_research: bool = True
    claude_timeout_s: int = 420
    max_script_words: int = 80
    # One source for both the prompt and the VideoScript validator. Past ~60 the
    # hook wraps to a third line on a 1080-wide frame and stops reading as a
    # headline. Raise it only if you also loosen the type size in the renderer.
    max_hook_chars: int = 60
    readme_char_budget: int = 12_000

    # --- Voiceover ---------------------------------------------------------
    # "edge"   -- Microsoft Edge voices. Free, keyless, but a network call, and
    #             its only natural-sounding English voices (Andrew, Brian, Ava,
    #             Emma) are the ones in every AI video on the internet.
    # "kokoro" -- Apache-2.0, fully local, 54 voices, far less recognisable.
    # "chatterbox" -- my own cloned voice. The only option here that no other
    #             account can be using, which is the whole point.
    tts_backend: str = "chatterbox"

    # edge backend
    tts_voice: str = "en-US-AndrewMultilingualNeural"
    # Measured: this voice at +0% reads ~150 wpm, so an 80-word script lands at
    # ~32s -- inside the 30-45s target. Raising the rate pushes you under 30s;
    # if you want a longer video, raise max_script_words instead.
    tts_rate: str = "+0%"

    # kokoro backend
    kokoro_voice: str = "am_michael"
    # 1.0 reads like documentary narration, which loses viewers on a platform
    # where the thumb is already moving. Past ~1.35 it starts sounding rushed
    # rather than energetic, and Whisper's word alignment gets less reliable.
    kokoro_speed: float = 1.15
    kokoro_lang: str = "en-us"

    # chatterbox backend -- my cloned voice, run out of process. See
    # tools/chatterbox/synth.py for why it cannot share this interpreter.
    #
    # These two numbers were picked by ear from a four-preset sweep against the
    # Ponytail script, audible in tools/chatterbox/out/. Re-audition with
    # `tools/chatterbox/.venv/bin/python tools/chatterbox/clone.py --sweep`
    # before changing them; they are not independent and guessing goes badly.
    #   exaggeration -- emotional intensity. 0.5 is neutral, past ~0.7 it acts
    #     rather than reads.
    #   cfg_weight   -- pull toward the reference. 0.3 reads calmer and slower
    #     than the 0.5 default, which suits a reference read at Reels pace.
    chatterbox_exaggeration: float = 0.5
    chatterbox_cfg_weight: float = 0.3
    # "mps" on Apple silicon, "cpu" everywhere else. Roughly 35s of compute for
    # 25s of audio once warm; the first call of a process pays a much larger
    # one-off for MPS kernel compilation.
    chatterbox_device: str = "mps"
    # Generous because the first call in a cold process pays for MPS kernel
    # compilation, which took ~200s more than a warm one when measured. A run
    # that hangs past this is broken, not slow.
    chatterbox_timeout_s: int = 900
    # Recording of my voice the clone is built from. Gitignored, so a fresh
    # checkout has to re-record it (tools/chatterbox/ref/RECORD-THIS.txt).
    chatterbox_ref: Path = ROOT / "tools/chatterbox/ref/morten.wav"

    # --- Captions ----------------------------------------------------------
    whisper_model: str = "small.en"
    whisper_compute_type: str = "int8"

    # --- Video -------------------------------------------------------------
    fps: int = 30
    width: int = 1080
    height: int = 1920

    @field_validator("claude_effort")
    @classmethod
    def _valid_effort(cls, v: str) -> str:
        allowed = {"low", "medium", "high", "xhigh", "max"}
        if v not in allowed:
            raise ValueError(f"claude_effort must be one of {sorted(allowed)}, got {v!r}")
        return v

    @field_validator("tts_backend")
    @classmethod
    def _valid_backend(cls, v: str) -> str:
        allowed = {"edge", "kokoro", "chatterbox"}
        if v not in allowed:
            raise ValueError(f"tts_backend must be one of {sorted(allowed)}, got {v!r}")
        return v

    # --- Derived paths -----------------------------------------------------
    @property
    def data_dir(self) -> Path:
        p = ROOT / "data"
        p.mkdir(exist_ok=True)
        return p

    @property
    def build_dir(self) -> Path:
        p = ROOT / "build"
        p.mkdir(exist_ok=True)
        return p

    @property
    def video_dir(self) -> Path:
        return ROOT / "video"

    @property
    def models_dir(self) -> Path:
        p = ROOT / "models"
        p.mkdir(exist_ok=True)
        return p

    @property
    def kokoro_model_path(self) -> Path:
        return self.models_dir / "kokoro-v1.0.onnx"

    @property
    def kokoro_voices_path(self) -> Path:
        return self.models_dir / "voices-v1.0.bin"

    @property
    def chatterbox_python(self) -> Path:
        """The isolated interpreter, not the one running this."""
        return ROOT / "tools/chatterbox/.venv/bin/python"

    @property
    def chatterbox_worker(self) -> Path:
        return ROOT / "tools/chatterbox/synth.py"

    @property
    def star_history_path(self) -> Path:
        return self.data_dir / "star_history.json"

    @property
    def used_repos_path(self) -> Path:
        return self.data_dir / "used_repos.json"

    @property
    def ig_token_path(self) -> Path:
        """Where the live Instagram token lives. Gitignored with the rest of data/."""
        return self.data_dir / "ig_token.json"

    @property
    def ig_graph_base(self) -> str:
        return f"{self.ig_graph_host.rstrip('/')}/{self.ig_api_version}"

    def run_dir(self, slug: str, on: date | None = None) -> Path:
        """Per-run artifact folder: build/2026-07-30/owner-repo/.

        Nested rather than flat (`build/2026-07-30-owner-repo/`) so a day's
        runs group together. Once you are posting daily and re-running the odd
        repo, a flat build/ turns into an unsortable wall of directories.
        """
        stamp = (on or date.today()).isoformat()
        p = self.build_dir / stamp / slug
        p.mkdir(parents=True, exist_ok=True)
        return p


class ConfigError(RuntimeError):
    """Raised at startup for a problem the user must fix before anything runs."""


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def resolve_claude_cli() -> str:
    """Locate the Claude Code binary, failing loudly and early.

    Discovering this mid-pipeline -- after we have already spent API calls on
    scraping -- is a much worse experience than failing on startup.
    """
    path = shutil.which("claude")
    if not path:
        raise ConfigError(
            "The 'claude' CLI was not found on PATH.\n"
            "This project uses Claude Code's headless mode for script generation "
            "instead of a paid API key.\n"
            "Install it from https://claude.com/product/claude-code and make sure "
            "`claude --version` works in this shell."
        )
    return path


def require_instagram(settings: Settings) -> None:
    """Fail before a render if publishing is asked for but not configured.

    Checked up front rather than at the end, because the alternative is
    discovering it after Remotion has spent ten minutes on an MP4.
    """
    missing = [] if settings.ig_user_id else ["IG_USER_ID"]
    # A stored token counts: after the first --refresh-token the .env line is
    # stale by design, and demanding it back would be a lie about what is needed.
    if not settings.ig_access_token and not settings.ig_token_path.exists():
        missing.append("IG_ACCESS_TOKEN")
    if missing:
        raise ConfigError(
            f"Instagram publishing is not configured ({', '.join(missing)} missing).\n"
            "Setup is docs/instagram-api-setup.md: a Meta app in development mode, your "
            "own account added as an Instagram tester, then a long-lived token.\n"
            "No App Review is needed to publish to an account that holds a role on "
            "your own app."
        )


def require_github_token(settings: Settings) -> str:
    if not settings.github_token or settings.github_token == "ghp_replace_me":
        raise ConfigError(
            "GITHUB_TOKEN is not set.\n"
            "Unauthenticated GitHub allows only 10 search requests/minute and 60 core "
            "requests/hour, which is not enough for one pipeline run.\n"
            "Create a classic token (no scopes needed for public repos) at "
            "https://github.com/settings/tokens and put it in .env"
        )
    return settings.github_token
