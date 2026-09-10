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

import re
from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator

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


class SubjectCandidate(BaseModel):
    """A long dead person, scored and ready to become a video.

    The second niche's answer to `RepoCandidate`, and deliberately the same
    shape: a name, a signal that moved, the artefacts a shot can be cut from,
    and a score split into the parts that produced it. Everything downstream of
    discovery reads a scored candidate and does not care which catalogue it
    came out of, which is what makes one niche's discovery replaceable.

    Not yet mirrored in `video/src/schema.ts`, because nothing renders from one
    of these yet. `VideoSpec.repo` is still required and still a `RepoMeta`;
    breaking that is the interface change this model exists to argue for.
    """

    qid: str  # "Q41568", the Wikidata id and the stable identity
    name: str
    article: str  # the English Wikipedia title, which pageviews are keyed on
    description: str = ""
    born: int | None = None
    died: int | None = None

    # The format's first test is a real per item artefact nobody drew for it.
    portrait: str = ""  # a Commons filename
    artefacts: list[str] = Field(default_factory=list)
    usable_artefacts: int = 0

    # This niche's velocity, and the reason it needs no local history store:
    # Wikimedia publishes the daily series itself, where GitHub publishes only
    # today's star count and `StarHistory` has to remember the rest.
    views_recent: int = 0
    velocity: float = 0.0

    # The encyclopedia's own change feed, when it named this subject.
    sep_slug: str = ""
    sep_revised: date | None = None

    score: float = 0.0
    score_breakdown: dict[str, float] = Field(default_factory=dict)

    @property
    def key(self) -> str:
        """The cooldown key.

        Prefixed, because the cooldown store is one file per account keyed by
        strings and a bare `Q41568` beside `astral-sh/uv` says nothing about
        which catalogue it came from.
        """
        return f"wikidata:{self.qid}"

    @property
    def slug(self) -> str:
        safe = self.name.lower()
        return "".join(c if (c.isalnum() or c == "-") else "-" for c in safe).strip("-")


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
    # A named flow from the project's own README. Opt in: Claude asks for it
    # only when the project actually documents a pipeline worth drawing, and
    # `_diagram_nodes` refuses the generic version rather than rendering it.
    DIAGRAM = "diagram"


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
    diagram_nodes: list[str] = Field(default_factory=list)


# The words that turn a diagram into a slide. Every one of these is a box an
# LLM will draw for any project in any category, which is exactly the failure a
# diagram is supposed to avoid: "Input -> Tool -> Output" is the slide-deck
# motif this account already refuses, redrawn with arrows.
_GENERIC_NODES = frozenset(
    {
        "input", "inputs", "output", "outputs", "data", "result", "results",
        "user", "users", "tool", "tools", "model", "models", "api", "app",
        "client", "server", "database", "db", "start", "end", "process",
        "processing", "request", "response", "source", "sources", "step",
    }
)

MAX_DIAGRAM_NODES = 5
MAX_DIAGRAM_NODE_CHARS = 28


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

    @model_validator(mode="after")
    def _check_diagram_nodes(self) -> VideoScript:
        """A diagram has to name this project's own parts, or it is not one.

        The cue is opt in, which is what makes refusing here cheap: the honest
        response to a project that documents no pipeline is to not ask for a
        diagram, so the correction is to drop the cue rather than to invent
        better labels for it.

        Rejected rather than cleaned, for the reason the dash validator gives.
        A node reading "Input" cannot be fixed by editing the string; the whole
        cue is the problem, and a diagram of boxes that would be true of any
        project in the category is the slide-deck motif this account already
        refuses, redrawn with arrows.
        """
        for cue in self.visual_cues:
            if cue.kind is not CueKind.DIAGRAM:
                if cue.diagram_nodes:
                    raise ValueError(
                        f"a {cue.kind.value} cue carries diagram_nodes; "
                        "only a diagram cue may have them"
                    )
                continue

            nodes = [n.strip() for n in cue.diagram_nodes if n and n.strip()]
            if not 2 <= len(nodes) <= MAX_DIAGRAM_NODES:
                raise ValueError(
                    f"a diagram cue needs 2 to {MAX_DIAGRAM_NODES} nodes, got "
                    f"{len(nodes)}. If the project documents no pipeline worth "
                    "drawing, use a code or terminal cue instead."
                )
            for node in nodes:
                if len(node) > MAX_DIAGRAM_NODE_CHARS:
                    raise ValueError(
                        f"diagram node {node!r} is {len(node)} chars; keep each under "
                        f"{MAX_DIAGRAM_NODE_CHARS} so it stays legible on a phone"
                    )
                if node.casefold() in _GENERIC_NODES:
                    raise ValueError(
                        f"diagram node {node!r} is generic and would be true of any "
                        "project. Name this project's own components, from its "
                        "README, or drop the diagram cue and use code or terminal."
                    )
            cue.diagram_nodes = nodes
        return self


class EpisodeScript(BaseModel):
    """One episode of the second niche, with the format's arc in the model.

    `VideoScript` is a hook plus a block of prose, because a video about a
    repository is one argument. This niche's arc took a whole session to find
    and every failed cut failed the same way, so it is fields rather than
    prose: a model that returns an aphorism and stops cannot satisfy this
    schema, and the two beats that went missing longest, coming back to the
    viewer and the steps, are required rather than hoped for.

    **The steps have to come from the source.** Advice invented here and
    dressed in a historical costume is exactly the thing the account claims not
    to be, and it is the one rule no validator can enforce, so `source` is
    required and the prompt is written around it.
    """

    hook: str = Field(
        description=f"On screen for the first 3 seconds, under {MAX_HOOK_CHARS} chars"
    )
    situation: str = Field(
        description="The viewer's own situation, in the second person. Not the quote."
    )
    who: str = Field(
        description="Who they were and when, so the quote can mean something"
    )
    quote: str = Field(description="The quote, in their own words, verbatim")
    plain: str = Field(description="The same thing in plain words")
    did: str = Field(description="What they actually did about it, taken from the source")
    back: str = Field(description="Back to the viewer, in the second person")
    steps: list[str] = Field(
        description="Exactly 3 steps, derived from the source rather than invented"
    )
    source: str = Field(description="The primary source, named so a viewer could check it")
    caption_text: str = Field(default="", description="The post caption, with hashtags")

    @property
    def beats(self) -> list[tuple[str, str]]:
        """Every spoken line with the beat of the arc it belongs to.

        The renderer needs the beat, not just the words: a quotation is set
        differently from a step, and a step is numbered. Derived here rather
        than asked of the model, because the arc is already the schema and a
        second answer about it is a second thing that can disagree.
        """
        out: list[tuple[str, str]] = []
        for kind, part in (
            ("situation", self.situation),
            ("who", self.who),
            ("quote", self.quote),
            ("plain", self.plain),
            ("did", self.did),
            ("back", self.back),
        ):
            for sentence in _sentences(part):
                out.extend((kind, line) for line in _breathe(sentence))
        out.extend(("step", step) for step in self.steps)
        return out

    @property
    def lines(self) -> list[str]:
        """The spoken script in arc order, one line per sentence.

        Split per sentence because the shot boundaries are derived from where
        the lines actually end, which is what puts the first cut inside the
        three seconds `skip_rate` scores without anybody choosing a number.
        """
        return [line for _kind, line in self.beats]

    @property
    def word_count(self) -> int:
        return sum(len(line.split()) for line in self.lines)

    @field_validator("hook")
    @classmethod
    def _hook_length(cls, v: str) -> str:
        v = v.strip().rstrip(".")
        if len(v) > MAX_HOOK_CHARS:
            raise ValueError(f"hook is {len(v)} chars; keep it under {MAX_HOOK_CHARS}")
        return v

    @field_validator("steps")
    @classmethod
    def _three_steps(cls, v: list[str]) -> list[str]:
        if len(v) != 3:
            raise ValueError(f"steps must be exactly 3, got {len(v)}")
        return v

    @field_validator("hook", "situation", "who", "quote", "plain", "did", "back", "steps")
    @classmethod
    def _no_colons_or_dashes(cls, v, info: ValidationInfo):
        """The same rule as `VideoScript`, and it reaches the quote too.

        **That is a real editorial cost and it is taken knowingly.** A quotation
        is evidence and keeping its own words matters, but the captions burned
        into the video are generated from what is spoken, and a dash in a
        seventeenth century sentence is invisible to a listener and clutter on
        screen. The way out is choosing the clause that carries the point,
        which is what a 45 second video wanted anyway, rather than rewriting
        the quotation.
        """
        values = v if isinstance(v, list) else [v]
        for item in values:
            found = sorted({c for c in item if c in _BANNED_PUNCTUATION})
            if found:
                raise ValueError(
                    f"{info.field_name} contains {', '.join(repr(c) for c in found)}; "
                    f"rewrite without colons or dashes, or quote the clause that has none"
                )
        return v


# Longest a single spoken line may run before it is broken at a comma. One line
# is one shot, so a 36 word sentence is an 11 second shot, and the first live
# episode produced exactly that: a quotation from 1683 that the human edit of
# the prototype had split across two shots at its own comma.
MAX_LINE_WORDS = 18


def _breathe(sentence: str) -> list[str]:
    """Break an over long sentence at its own punctuation, keeping every word.

    **Not a rewrite.** A quotation is evidence and its words are not ours to
    change, but where it is spoken and where it is cut are ours, and the format
    cuts on where a line ends. Splitting at a comma is what a person reading it
    aloud does anyway.

    It splits at the boundary nearest the middle rather than filling greedily
    to the limit. Greedy filling broke "mends on, on, on" between the second and
    third "on", which is the one place in that sentence a reader would never
    pause.
    """
    words = sentence.split()
    if len(words) <= MAX_LINE_WORDS:
        return [sentence]

    breaks = [m.end() for m in re.finditer(r"[,;]\s+", sentence)]
    if not breaks:
        return [sentence]

    middle = len(sentence) / 2
    at = min(breaks, key=lambda pos: abs(pos - middle))
    left, right = sentence[:at].strip(), sentence[at:].strip()
    return _breathe(left) + _breathe(right)


def _sentences(text: str) -> list[str]:
    """Split on sentence ends, keeping the punctuation.

    Deliberately simple. The input is written to this account's own rules, so
    it has no abbreviations, no ellipses and no decimals to trip on.
    """
    out, current = [], ""
    for char in text.strip():
        current += char
        if char in ".?!":
            out.append(current.strip())
            current = ""
    if current.strip():
        out.append(current.strip())
    return out


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
    diagramNodes: list[str] = Field(default_factory=list)  # noqa: N815


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


class SpecArtefact(BaseModel):
    """One staged picture, with its own dimensions carried rather than measured.

    Same rule as `pageAspect`: a renderer that measures an image inside a frame
    render is one where a frame can differ from its neighbour for no visible
    reason. The licence and the credit travel too, because they are the reason
    the file was allowed to be used at all and a spec is the only place that
    record survives the run folder being deleted.
    """

    src: str  # relative to video/public/
    w: int
    h: int
    title: str = ""
    licence: str = ""
    credit: str = ""


class Crop(BaseModel):
    """A rectangle in the artefact's own pixel space."""

    sx: int
    sy: int
    sw: int
    sh: int


class Shot(BaseModel):
    """One line of the script, and what is on screen while it is spoken.

    A shot per line rather than per beat, because the cut lands where the line
    ends and the line boundaries are what the voice actually produced.
    """

    start: int = Field(description="First frame, in the video's own timeline")
    durationInFrames: int  # noqa: N815
    line: str
    kind: str  # situation, who, quote, plain, did, back, step
    art: int = 0  # index into EpisodeSpec.artefacts
    crop: Crop | None = None
    # "cover" crops to fill the frame, "contain" shows the whole artefact on
    # the paper ground. The opening shot of a landscape scan is contained,
    # because the first thing the format promises is the artefact itself and a
    # cropped one is a detail of something the viewer has not been shown yet.
    fit: str = "cover"
    step: int | None = None  # 1, 2 or 3 when this shot is a numbered step


class EpisodeSpec(BaseModel):
    """Everything the second niche's renderer needs, and nothing React shaped.

    The counterpart to `VideoSpec` rather than an extension of it. They share
    almost nothing: this one has no repository, no README capture and no
    scenes, and it has a quotation and its source, which is the whole format.
    Generalising `VideoSpec` to cover both was the alternative and it would
    have made every field optional for one of the two.
    """

    version: int = 1
    slug: str
    createdOn: date  # noqa: N815

    width: int = 1080
    height: int = 1920
    fps: int = 30
    durationInFrames: int  # noqa: N815

    hook: str
    audioSrc: str  # noqa: N815
    subject: str
    lived: str = ""
    source: str
    artefacts: list[SpecArtefact]
    shots: list[Shot]

    # The end card, which is the one place an episode says whose it is. Read
    # from the account rather than written here, so the public machinery holds
    # no identity.
    endcardName: str = ""  # noqa: N815
    endcardHandle: str = ""  # noqa: N815
    endcardTagline: str = ""  # noqa: N815


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

    # The whole README as one tall image, for the shot that scrolls it.
    #
    # On VideoSpec rather than on Scene because there is one page per video and
    # every screenshot scene shows the same one. Putting it on the scene would
    # mean the same filename repeated three times in a spec, with three chances
    # for two of them to disagree.
    #
    # Optional, and the renderer falls back to the framed hero without it, so a
    # spec written before this field renders exactly as it used to and a repo
    # whose README is too short to scroll is not a failure.
    pageSrc: str | None = None  # noqa: N815  - path relative to video/public/
    # height / width of that capture. Carried rather than measured, because
    # measuring an image inside a frame render is how one frame ends up
    # different from its neighbour for no visible reason.
    pageAspect: float | None = None  # noqa: N815

    # Whether the follow ask appears as an end card. The caption carries the
    # same ask, but a caption sits behind a "more" tap and most viewers never
    # open it, so the video has to say it too.
    #
    # This was `ctaKeyword`, the word to comment for the link. The ask is a
    # follow now and needs no word, so the field is a flag; see `SPOKEN_CTA` in
    # `pipeline/gateway.py` for why it changed.
    showFollowCta: bool = False  # noqa: N815

    # The frame the ask begins on, so a surface that cannot deliver it can cut
    # the video there rather than carry a promise it cannot keep.
    #
    # The ask is spoken, captioned and shown at once, so there is no render
    # flag that removes it: dropping it any other way means a second voiceover,
    # which is the step that holds four gigabytes and has taken the batch down
    # with it. Stopping the video before the ask starts costs one ffmpeg cut.
    #
    # Recorded rather than derived from scene lengths, because the ask only
    # gets a scene of its own when the split lands clear of a boundary. When it
    # does not, this stays None and the full video is the only version there
    # is, which is the honest answer rather than a cut in the wrong place.
    ctaFromFrame: int | None = None  # noqa: N815
