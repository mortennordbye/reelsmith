"""Central configuration, and which account a run belongs to.

Everything tunable lives here so no stage has to reach for an env var directly.
Import `get_settings()` rather than constructing `Settings` yourself -- it is
cached, so the `.env` file is read exactly once per process.

## Accounts

One checkout serves several accounts. An account is a directory,
`accounts/<name>/`, holding the machine readable half of an identity:

    accounts/nightlybuild/.env      the per account overrides
    accounts/nightlybuild/data/     the cooldown store, star history, token
    accounts/nightlybuild/ref/      the voice recording the clone is built from

and the run folders it produces live under `build/<name>/<date>/<slug>/`.

The editorial half stays in one root `PROFILE.md`, which already carries its
shared rules at the top and one section per account. Splitting it per directory
would either duplicate the shared half, which is the drift this repo refuses
everywhere else, or invent an include mechanism for a markdown file.

**Nothing resolves an account by counting.** `--account` or `REELSMITH_ACCOUNT`,
and neither one means the run fails naming what it found. The gateway's
resolve-when-there-is-exactly-one is precisely what deleted a working schedule
the first time a second account existed (F0 in docs/multi-destination-audit.md),
and repeating that pattern here would be repeating a known bug on purpose. A run
that fails at startup costs a night; a run that posts account 1's video to
account 2 cannot be taken back.

The split between what an account owns and what the checkout owns was measured
rather than guessed, and it is F4 in the same document. Credentials, the
editorial register, the voice, the cooldown store and the build subtree are per
account. The GitHub token, the model knobs, the validators, the frame size, the
Remotion project and the gateway URL are not.
"""

from __future__ import annotations

import logging
import shutil
import sys
from datetime import date
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger(__name__)

ROOT = Path(__file__).parent.resolve()
ACCOUNTS_DIR = ROOT / "accounts"

# Where the single account layout kept things, and where an account that has
# not been migrated yet still has them. Both are read only fallbacks: nothing
# writes to either once an account is selected and its directory exists.
LEGACY_DATA_DIR = ROOT / "data"
LEGACY_VOICE_REF = ROOT / "tools/chatterbox/ref/morten.wav"


def _default_torch_device() -> str:
    """The torch backend this machine actually has.

    Only Apple silicon has Metal. Deciding here rather than defaulting to one
    and overriding in `.env` on every other host keeps the failure off a
    machine that has no way to know it was meant to set the variable.
    """
    return "mps" if sys.platform == "darwin" else "cpu"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # So `Settings(account="x")` works alongside REELSMITH_ACCOUNT. The
        # alias exists because ACCOUNT, which is what pydantic would derive,
        # is far too general a name to put in a shared host environment.
        populate_by_name=True,
    )

    # --- Which account this run belongs to ---------------------------------
    # Empty is the checkout with no account selected: the global half of these
    # settings is still correct, and every per account path falls back to the
    # single account layout this repo had before `accounts/` existed. That is
    # what keeps `pipeline/models.py` importable, since it reads the validator
    # limits at import time, long before a CLI flag has been parsed.
    #
    # It is *not* a default account. `main.py` refuses to do per account work
    # without one, and says which accounts it can see.
    account: str = Field(default="", validation_alias="REELSMITH_ACCOUNT")

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

    # --- YouTube (optional) -------------------------------------------------
    # Setup is docs/youtube-api-setup.md. Written by
    # scripts/youtube_authorise.py and read by scripts/youtube_upload.py.
    #
    # All four live here, refresh token included, and that is the difference
    # from the Instagram block above. `ig_access_token` is only a seed because
    # a Meta token is refreshed every 60 days and the live one has to be
    # written back somewhere, which is what data/ig_token.json is for. Google
    # refresh tokens do not rotate and do not expire on a clock, so nothing
    # ever writes back and there is nothing for a second file to hold.
    youtube_client_id: str = ""
    youtube_client_secret: str = ""
    youtube_refresh_token: str = ""
    # The UC... channel id, not the @handle. Read back at authorisation time
    # rather than pasted, because the two are easy to confuse and the mistake
    # does not surface until the first upload.
    youtube_channel_id: str = ""

    # --- TikTok (optional) --------------------------------------------------
    # The open id only, and nothing else. The credentials live on the gateway,
    # which is what publishes, so no TikTok secret reaches the machine that
    # renders. Same shape as the YouTube channel id above and for the same
    # reason: this host needs to know where a row is going, not how to get in.
    #
    # Without it the fan-out skips TikTok silently and only the Reel and the
    # Short are queued, which is the behaviour every render had before this.
    tiktok_open_id: str = ""

    # --- Facebook (optional) ------------------------------------------------
    # The numeric Page id only, and nothing else. The Page access token lives
    # on the gateway, which is what publishes, so no Facebook secret reaches
    # the machine that renders. Same shape as the YouTube channel id and the
    # TikTok open id above and for the same reason: this host needs to know
    # where a row is going, not how to get in.
    #
    # The numeric id, not the vanity name. `facebook.com/thenightlybuild`
    # addresses the Page perfectly well in a browser and not at all on
    # `/{page-id}/video_reels`, and the gateway refuses a non-numeric one at
    # registration rather than at the first publish.
    #
    # Without it the fan-out skips Facebook silently and queues the rest, which
    # is the behaviour every render had before this.
    facebook_page_id: str = ""

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
    # How many *usable* candidates a query has to yield, and how deep it may
    # page to find them. Two numbers rather than one because the cooldown list
    # eats a stars-sorted result set from the top: the repos we featured last
    # week are still the highest-starred things the query matches, so they sit
    # in front of everything else every night until their 30 days expire.
    #
    # A fixed window of 50 results is therefore not a fixed window of 50
    # candidates, and on 2026-08-17 and 2026-08-18 it was a window of zero.
    # 60 repos were inside their cooldown and 54 of them fell in the top 150 by
    # stars, so discovery filtered everything it looked at and the batch died at
    # ranking. The pool was never thin -- the two queries matched about 9,900
    # repos that morning -- the window into it was.
    #
    # So discovery pages until it has `candidate_target` survivors instead. The
    # window now widens by exactly as much as the cooldown list grows, and costs
    # nothing on a night when it does not have to: a normal night still stops on
    # the first page. `candidate_search_cap` is the ceiling on that widening and
    # the thing to raise if the log ever says a query hit it; GitHub itself
    # refuses to read one query past 1000 results, which is the real ceiling.
    candidate_target: int = 50
    candidate_search_cap: int = 400

    # --- Script generation (Claude Code CLI) -------------------------------
    claude_model: str = "opus"
    claude_effort: str = "high"
    claude_research: bool = True
    claude_timeout_s: int = 420
    # Measured on six real runs on 2026-08-01: the cloned voice reads 165 to
    # 190 words per minute, call it 170, and the appended ask adds about seven
    # words of audio on top of this budget. So a script written to the ceiling
    # lands at (words + 7) / 170 minutes, which is about 31 seconds here.
    #
    # This was briefly 100, on the reasoning that a stated 30 to 45 second
    # target was unreachable from 80. Retention data pulled the same day killed
    # that: average watch across the first seven posts was 2.9 to 8.4 seconds
    # on videos of 24 to 30 seconds, so the extra eight seconds would have been
    # watched by almost nobody while pushing completion further below the
    # roughly 30 percent where non-follower distribution reportedly stops.
    #
    # **Length is not the lever here, and raising this will not make it one.**
    # The account loses 64 to 80 percent of viewers inside the first three
    # seconds. Fix the opening before touching this number.
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
    # Measured: this voice at +0% reads ~150 wpm, slower than the cloned voice
    # that `max_script_words` is calibrated against, so the same script runs
    # longer on this backend. Raising the rate shortens the video; if you want
    # a longer one, raise max_script_words instead.
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
    # "mps" on Apple silicon, "cpu" everywhere else, and chosen by the platform
    # rather than pinned, because the wrong one does not degrade, it fails:
    # asking for mps on Linux raises `Storage device not recognized: mps` at
    # model load, after the run has already paid for a script. Measured at
    # roughly 35s of compute for 25s of audio on mps once warm, and about 10
    # minutes for the same clip on six CPU cores.
    chatterbox_device: str = Field(default_factory=lambda: _default_torch_device())
    # Generous because the first call in a cold process pays for MPS kernel
    # compilation, which took ~200s more than a warm one when measured. A run
    # that hangs past this is broken, not slow.
    chatterbox_timeout_s: int = 900
    # Recording of the voice the clone is built from. Gitignored, so a fresh
    # checkout has to re-record it (tools/chatterbox/ref/RECORD-THIS.txt).
    #
    # Left unset it resolves to the selected account's own `ref/voice.wav`,
    # because PROFILE.md is explicit that sharing one cloned voice across two
    # accounts meant to look unrelated is the strongest link between them.
    # See `_resolve_account_paths`.
    chatterbox_ref: Path = Field(default=LEGACY_VOICE_REF)

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

    @model_validator(mode="after")
    def _resolve_account_paths(self) -> Settings:
        """Point the voice reference at the selected account, unless told not to.

        Done here rather than as a property because `chatterbox_ref` is a
        settable field that `tools/chatterbox/synth.py` is handed directly, and
        a machine with one account and an existing recording should keep
        working while `accounts/` is still being populated.

        `model_fields_set` is what makes "unless told not to" honest: a value
        that arrived from `CHATTERBOX_REF` in either `.env` counts as chosen
        and is never second guessed.
        """
        if self.account and "chatterbox_ref" not in self.model_fields_set:
            own = self.account_dir / "ref" / "voice.wav"
            if own.exists():
                self.chatterbox_ref = own
            elif LEGACY_VOICE_REF.exists():
                # The migration has not been run yet. Loud enough to notice,
                # quiet enough that a batch still speaks tonight. Two accounts
                # sharing one voice is a real problem and it is the operator's
                # to fix, not this function's to fail over.
                log.info(
                    "No voice recording at %s; falling back to %s. "
                    "python main.py --migrate-account <name> moves it into place.",
                    own, LEGACY_VOICE_REF,
                )
            else:
                self.chatterbox_ref = own
        return self

    # --- Derived paths -----------------------------------------------------
    @property
    def account_dir(self) -> Path:
        """This account's own half of the checkout.

        Never created on access, unlike the directories below it. An account is
        something the operator declares by making the directory and putting an
        `.env` in it, and a typo in `--account` that silently created an empty
        profile would be a run against no credentials rather than an error.
        """
        if not self.account:
            raise ConfigError(
                "No account is selected, so there is no account directory.\n"
                "Pass --account <name>, or set REELSMITH_ACCOUNT in .env."
            )
        return ACCOUNTS_DIR / self.account

    @property
    def data_dir(self) -> Path:
        """The cooldown store, the star history and the live Instagram token.

        Per account and not shareable: `data/used_repos.json` holds a 30 day
        cooldown per repo, and a second account pointed at the same file
        inherits every repo the first one ever covered. F9.

        Falls back to the pre `accounts/` location while an account directory
        has no `data/` yet, so a checkout mid migration reads the store it
        already has rather than starting an empty one. `data` has to stay a
        *directory* symlink on a host that keeps it on a share, because
        `StarHistory.save()` renames a temp file over the target and that
        rename replaces a file symlink with a real file.
        """
        if self.account:
            own = self.account_dir / "data"
            if own.exists() or not LEGACY_DATA_DIR.exists():
                own.mkdir(parents=True, exist_ok=True)
                return own
            return LEGACY_DATA_DIR
        p = LEGACY_DATA_DIR
        p.mkdir(exist_ok=True)
        return p

    @property
    def build_dir(self) -> Path:
        """Where this account's run folders live: `build/<account>/`.

        Everything that takes a `<date>/<slug>` argument -- `--resume`,
        `--publish`, `--enqueue` -- is resolved against this, so those arguments
        keep their old shape and gain the account from `--account` rather than
        from the path. `--recover` scans this too, which is why it needs no
        account level of its own.
        """
        p = ROOT / "build" / self.account if self.account else ROOT / "build"
        p.mkdir(parents=True, exist_ok=True)
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


_selected_account: str = ""


def available_accounts() -> list[str]:
    """Every account this checkout can see, sorted.

    A directory under `accounts/` holding an `.env`. The `.env` is what makes
    it an account rather than a leftover: `accounts/nightlybuild/data/` can
    survive a profile being deleted, and offering it back as a name would be
    offering a run with no credentials.
    """
    if not ACCOUNTS_DIR.is_dir():
        return []
    return sorted(
        p.name for p in ACCOUNTS_DIR.iterdir() if p.is_dir() and (p / ".env").is_file()
    )


def resolve_account(explicit: str | None = None) -> str:
    """Which account this process is for, or fail saying what it could see.

    Order is `--account`, then `REELSMITH_ACCOUNT` from the environment or the
    root `.env`. There is deliberately no third rule. Resolving by "there is
    only one" is how the gateway lost a working schedule the first time a
    second account existed, and the pipeline's version of that mistake posts a
    video to the wrong audience, which no amount of noticing afterwards undoes.

    The bootstrap `Settings()` here reads the root `.env` alone, which is the
    only file that can name an account without already being one.
    """
    name = (explicit or "").strip() or Settings().account.strip()
    known = available_accounts()
    if not name:
        raise ConfigError(
            "No account selected.\n"
            f"{_accounts_line(known)}\n"
            "Pass --account <name>, or set REELSMITH_ACCOUNT in .env.\n"
            "There is no default: one checkout serves several accounts and "
            "guessing wrong publishes to the wrong audience."
        )
    if name not in known:
        raise ConfigError(
            f"No account directory at accounts/{name}/ with an .env in it.\n"
            f"{_accounts_line(known)}\n"
            "python main.py --migrate-account <name> builds one from the "
            "single account layout this repo used before."
        )
    return name


def _accounts_line(known: list[str]) -> str:
    if not known:
        return "This checkout has no accounts/ directory yet."
    return f"Accounts in this checkout: {', '.join(known)}."


def select_account(name: str) -> Settings:
    """Bind this process to one account, and hand back its settings.

    Called once, from `main.py`, before any stage runs. Every stage already
    reads a single `Settings`, so binding here is what keeps `--account` from
    becoming a parameter on twenty signatures.
    """
    global _selected_account
    _selected_account = name
    get_settings.cache_clear()
    return get_settings()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """The settings for the selected account, or the account-less checkout.

    Two `.env` files, root first and the account's own second, so the account
    overrides the checkout and anything it does not mention is inherited. That
    is the whole of what "a profile is an `.env` fragment plus a data
    directory" means, and it is why no stage signature changes.
    """
    if not _selected_account:
        return Settings()
    fragment = ACCOUNTS_DIR / _selected_account / ".env"
    return Settings(
        _env_file=(ROOT / ".env", fragment),
        account=_selected_account,
    )


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
