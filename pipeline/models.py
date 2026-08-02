"""Data contracts shared by every pipeline stage.

These models are the *only* interface between stages. Each stage reads one
model from disk and writes another, which means any stage can be re-run in
isolation against the previous run's artifacts. That matters a lot when
iterating on visuals -- you should never have to re-scrape GitHub and re-run
Whisper just to nudge a font size.

`VideoSpec` is mirrored one-to-one as a zod schema in video/src/schema.ts, and
the renderer parses video.json through it in `calculateMetadata` before the
first frame. Keep the two in sync: a field renamed here and not there fails the
render immediately, naming the field, instead of painting an `undefined` into
the middle of a finished MP4.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, Field, ValidationInfo, field_validator

from config import get_settings

# Read once, at import, so the JSON Schema description handed to Claude and the
# validator that checks his answer can never disagree about the number.
MAX_HOOK_CHARS = get_settings().max_hook_chars
# Same reason. This one was a literal 80 in the schema description while the
# prompt interpolated the setting, so raising the setting left the schema still
# asking for 80 words in the same request.
MAX_SCRIPT_WORDS = get_settings().max_script_words

# Colons and every dash variant Claude reaches for, including the ones a model
# emits without being asked (en dash, em dash, non-breaking hyphen).
_BANNED_PUNCTUATION = frozenset(":-‐‑‒–—―−")

# --------------------------------------------------------------------------
# Step 1 -- topic research
# --------------------------------------------------------------------------


class RepoCandidate(BaseModel):
    """A trending repository, scored and ready to become a video."""

    full_name: str  # "owner/name"
    name: str
    owner: str
    url: str
    description: str = ""
    homepage: str | None = None

    stars: int
    forks: int = 0
    stars_gained_today: int | None = None  # None on a cold start
    velocity: float = 0.0  # stars/day, measured or proxied
    velocity_is_measured: bool = False

    language: str | None = None
    license_spdx: str | None = None
    topics: list[str] = Field(default_factory=list)
    created_at: datetime | None = None
    pushed_at: datetime | None = None

    readme: str = ""
    hn_points: int | None = None
    hn_url: str | None = None

    score: float = 0.0
    score_breakdown: dict[str, float] = Field(default_factory=dict)

    @property
    def slug(self) -> str:
        """Filesystem-safe identifier: 'owner/repo.js' -> 'owner-repo-js'."""
        safe = self.full_name.replace("/", "-").replace(".", "-").lower()
        return "".join(c if (c.isalnum() or c == "-") else "-" for c in safe).strip("-")

    @property
    def age_days(self) -> float:
        if not self.created_at:
            return 365.0
        delta = datetime.now(self.created_at.tzinfo) - self.created_at
        return max(delta.total_seconds() / 86400.0, 1.0)


# --------------------------------------------------------------------------
# Step 2 -- script
# --------------------------------------------------------------------------


class CueKind(StrEnum):
    """What to render behind a given beat of the voiceover.

    Kept deliberately small. Every value maps to exactly one React scene
    component in video/src/scenes/, so adding a value here means adding a
    component there.
    """

    REPO_CARD = "repo_card"  # name, stars, language, license
    CODE = "code"  # syntax-highlighted snippet
    STAT = "stat"  # one big number with a label
    BULLETS = "bullets"  # 2-4 short lines, staggered in
    TERMINAL = "terminal"  # install/run command, typed out
    # Real GitHub page screenshot in browser chrome. Inserted by the pipeline
    # as the opening shot, not requested by Claude -- so it is intentionally
    # absent from the scriptwriter prompt.
    SCREENSHOT = "screenshot"


class VisualCue(BaseModel):
    """One beat of the video. Ordered; durations are allocated proportionally
    to the spoken text they accompany, so cues never drift out of sync."""

    kind: CueKind
    # Roughly the words being spoken while this is on screen. Used only to
    # weight the cue's share of the timeline, never rendered.
    spoken_excerpt: str = ""

    title: str | None = None
    subtitle: str | None = None
    bullets: list[str] = Field(default_factory=list)
    code: str | None = None
    code_language: str | None = None
    stat_value: str | None = None
    stat_label: str | None = None


class VideoScript(BaseModel):
    """Exactly the JSON we ask Claude Code to produce.

    The JSON Schema handed to `claude --json-schema` is generated from this
    model, so the prompt contract and the parser can never drift apart.
    """

    hook: str = Field(
        description=(
            f"Text overlay for the first 3 seconds. "
            f"Max {MAX_HOOK_CHARS} characters, no period, "
            f"no colons and no hyphens or dashes."
        )
    )
    spoken_script: str = Field(
        description=(
            f"The voiceover. Under {MAX_SCRIPT_WORDS} words, "
            f"no colons, hyphens or dashes."
        )
    )
    visual_cues: list[VisualCue] = Field(
        description="5-8 ordered beats describing what to show behind the voiceover."
    )
    caption_text: str = Field(
        default="",
        description="Instagram caption with hashtags. Not rendered into the video.",
    )

    @field_validator("hook")
    @classmethod
    def _hook_length(cls, v: str) -> str:
        v = v.strip().rstrip(".")
        if len(v) > MAX_HOOK_CHARS:
            raise ValueError(
                f"hook is {len(v)} chars; keep it under {MAX_HOOK_CHARS} so it fits on screen"
            )
        return v

    @field_validator("hook", "spoken_script")
    @classmethod
    def _no_colons_or_dashes(cls, v: str, info: ValidationInfo) -> str:
        """Colons and dashes are invisible to a listener and clutter the screen.

        The captions burned into the video are generated from `spoken_script`,
        so punctuation that does nothing aloud still costs screen legibility.
        Rejecting here rather than stripping keeps the rewrite with Claude,
        which can find a phrasing that reads naturally without them; silently
        deleting a hyphen would turn "seven-word" into "sevenword".
        """
        found = sorted({c for c in v if c in _BANNED_PUNCTUATION})
        if found:
            raise ValueError(
                f"{info.field_name} contains {', '.join(repr(c) for c in found)}; "
                f"rewrite without colons or dashes "
                f'("92k stars" not "92k-star", split a colon into two sentences)'
            )
        return v

    @property
    def word_count(self) -> int:
        return len(self.spoken_script.split())


# --------------------------------------------------------------------------
# Step 4 -- captions
# --------------------------------------------------------------------------


class Caption(BaseModel):
    """One word with its timing. Matches the shape @remotion/captions expects."""

    text: str
    startMs: float  # noqa: N815 - deliberately camelCase to match Remotion
    endMs: float  # noqa: N815
    timestampMs: float | None = None  # noqa: N815
    confidence: float | None = None


# --------------------------------------------------------------------------
# Step 5 -- render spec
# --------------------------------------------------------------------------


class Scene(BaseModel):
    """A resolved visual cue with concrete frame timings."""

    kind: CueKind
    fromFrame: int  # noqa: N815
    durationInFrames: int  # noqa: N815

    title: str | None = None
    subtitle: str | None = None
    bullets: list[str] = Field(default_factory=list)
    code: str | None = None
    codeLanguage: str | None = None  # noqa: N815
    statValue: str | None = None  # noqa: N815
    statLabel: str | None = None  # noqa: N815
    imageSrc: str | None = None  # noqa: N815  - path relative to video/public/


class RepoMeta(BaseModel):
    """The subset of RepoCandidate the renderer actually needs."""

    fullName: str  # noqa: N815
    owner: str
    name: str
    stars: int
    starsGainedToday: int | None = None  # noqa: N815
    language: str | None = None
    license: str | None = None
    url: str


class VideoSpec(BaseModel):
    """Everything Remotion needs, and nothing it doesn't.

    Deliberately renderer-agnostic: no React, no CSS, no Remotion types. A
    different backend could consume this file unchanged.
    """

    version: int = 1
    slug: str
    createdOn: date  # noqa: N815

    width: int = 1080
    height: int = 1920
    fps: int = 30
    durationInFrames: int  # noqa: N815

    hook: str
    audioSrc: str  # noqa: N815  - path relative to video/public/
    repo: RepoMeta
    scenes: list[Scene]
    captions: list[Caption]

    # The word to comment for the link, shown as an end card. None when no
    # gateway is configured, because asking for a comment nothing is listening
    # for is a promise the account cannot keep. The caption carries the same
    # ask, but a caption sits behind a "more" tap and most viewers never open
    # it, so the video has to say it too or the mechanic depends on a tap that
    # does not happen.
    ctaKeyword: str | None = None  # noqa: N815
