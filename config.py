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
    # The only one. Script generation uses the Claude Code CLI's existing
    # subscription auth, so there is no ANTHROPIC_API_KEY anywhere in here.
    github_token: str = Field(default="", description="GitHub PAT, no scopes needed")

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
    tts_backend: str = "kokoro"

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
    def star_history_path(self) -> Path:
        return self.data_dir / "star_history.json"

    @property
    def used_repos_path(self) -> Path:
        return self.data_dir / "used_repos.json"

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
