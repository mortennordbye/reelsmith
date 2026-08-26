"""reelsmith -- automated Instagram Reels for trending AI/dev tooling.

    python main.py                      full run
    python main.py --stop-after script  stop early to inspect an artifact
    python main.py --repo astral-sh/uv  skip discovery, use a specific repo
    python main.py --resume 2026-07-30/astral-sh-uv   re-render from artifacts

    python main.py --post               render, then publish it unattended
    python main.py --publish 2026-07-30/astral-sh-uv  publish a run you approved

    python main.py --batch 3            render the top 3 repos in one sitting
    python main.py --recover            finish and queue what a killed batch left behind

    python main.py --candidates         rank today's repos, generate nothing
    python main.py --covered            list every repo already made into a Reel
    python main.py --history            every repo we have touched, and when it went out
    python main.py --cohorts slot       compare the published Reels by slot, or by recipe
    python main.py --results            how the published Reels did, worst hook last
    python main.py --backfill           find Reels posted by hand; --yes measures them
    python main.py --snapshot           record star counts only (run daily)
    python main.py --refresh-token      renew the Instagram token by hand
                                        (--snapshot already does it when due)
    python main.py --posted astral-sh/uv    start the 30-day cooldown
    python main.py --unmark astral-sh/uv    undo that

    python main.py --migrate-account nightlybuild   plan the move into accounts/

Every run belongs to one account. Pass `--account <name>`, or set
REELSMITH_ACCOUNT in .env; there is no default, and a run without one fails at
startup naming the accounts it can see. An account is a directory under
accounts/<name>/ holding its own .env, data/ store and ref/ voice recording.

Every stage writes its output to build/<account>/<date>/<owner-repo>/ before the
next one starts, so a failure late in the pipeline never costs you the earlier
work. The <date>/<owner-repo> arguments to --resume, --publish and --enqueue are
resolved under the selected account, so they keep the shape they always had.
"""

from __future__ import annotations

import contextlib
import json
import logging
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from statistics import median
from typing import Annotated

import typer
from rich.console import Console
from rich.logging import RichHandler

from config import (
    ROOT,
    ConfigError,
    Settings,
    get_settings,
    require_github_token,
    require_instagram,
    resolve_account,
    resolve_claude_cli,
    select_account,
)
from pipeline import backfill as backfill_mod
from pipeline import captions as captions_mod
from pipeline import gateway, migrate, publisher, renderer, scraper, screenshot, tts
from pipeline import results as results_mod
from pipeline import spec as spec_mod
from pipeline.models import Caption, RepoCandidate, VideoScript, VideoSpec
from pipeline.scriptwriter import write_script

app = typer.Typer(add_completion=False, help=__doc__)
console = Console()
log = logging.getLogger("reelsmith")


class Stage(StrEnum):
    SCRAPE = "scrape"
    SCRIPT = "script"
    AUDIO = "audio"
    CAPTIONS = "captions"
    RENDER = "render"


ORDER = [Stage.SCRAPE, Stage.SCRIPT, Stage.AUDIO, Stage.CAPTIONS, Stage.RENDER]


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=console, rich_tracebacks=True, show_path=False)],
    )
    # These are chatty at INFO and drown out our own progress.
    for noisy in ("httpx", "httpcore", "faster_whisper", "huggingface_hub", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _preflight(*, need_github: bool, need_claude: bool, need_instagram: bool = False) -> None:
    """Fail on missing prerequisites before spending any time or tokens.

    Exits rather than raising: every caller wants the same readable message and
    a non-zero status, not a traceback.
    """
    cfg = get_settings()
    try:
        if need_github:
            require_github_token(cfg)
        if need_claude:
            resolve_claude_cli()
        if need_instagram:
            require_instagram(cfg)
    except ConfigError as exc:
        console.print(f"[bold red]Setup problem[/]\n{exc}")
        raise typer.Exit(1) from exc


@app.command()
def run(
    candidates: Annotated[
        bool,
        typer.Option("--candidates", help="Only rank today's repos; generate nothing"),
    ] = False,
    snapshot: Annotated[
        bool,
        typer.Option("--snapshot", help="Record star counts only; generate nothing"),
    ] = False,
    covered: Annotated[
        bool,
        typer.Option("--covered", help="List every repo already made into a Reel"),
    ] = False,
    history: Annotated[
        bool,
        typer.Option("--history", help="Every repo we have touched, and when it last went out"),
    ] = False,
    cohorts: Annotated[
        str | None,
        typer.Option("--cohorts", help="Compare the published Reels by 'recipe' or 'slot'"),
    ] = None,
    show_results: Annotated[
        bool,
        typer.Option("--results", help="How the published Reels did, worst hook last"),
    ] = False,
    backfill: Annotated[
        bool,
        typer.Option("--backfill", help="Find Reels posted by hand and measure them too"),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option("--yes", help="With --backfill, register instead of only listing"),
    ] = False,
    batch: Annotated[
        int | None,
        typer.Option("--batch", help="Render this many distinct repos in one sitting"),
    ] = None,
    max_queue: Annotated[
        int | None,
        typer.Option("--max-queue", help="Skip the batch when this many posts are already waiting"),
    ] = None,
    recover: Annotated[
        bool,
        typer.Option("--recover", help="Finish and queue any run a killed batch left half-built"),
    ] = False,
    posted: Annotated[
        str | None,
        typer.Option("--posted", help="Mark a repo as posted, starting its cooldown"),
    ] = None,
    unmark: Annotated[
        str | None,
        typer.Option("--unmark", help="Clear a repo's cooldown"),
    ] = None,
    repo_full_name: Annotated[
        str | None,
        typer.Option("--repo", help="Skip discovery, e.g. 'astral-sh/uv'"),
    ] = None,
    stop_after: Annotated[
        Stage | None,
        typer.Option("--stop-after", help="Stop after this stage"),
    ] = None,
    resume: Annotated[
        str | None,
        typer.Option("--resume", help="Re-run from artifacts, e.g. '2026-07-30/astral-sh-uv'"),
    ] = None,
    post: Annotated[
        bool,
        typer.Option("--post", help="Publish to Instagram once the render finishes"),
    ] = False,
    publish: Annotated[
        str | None,
        typer.Option("--publish", help="Publish an existing run, e.g. '2026-07-30/astral-sh-uv'"),
    ] = None,
    enqueue: Annotated[
        str | None,
        typer.Option(
            "--enqueue", help="Send an existing run to the gateway's schedule"
        ),
    ] = None,
    approve: Annotated[
        bool,
        typer.Option("--approve", help="Arm the queued post, so a slot may publish it"),
    ] = False,
    refresh_token: Annotated[
        bool,
        typer.Option("--refresh-token", help="Renew the Instagram token; run at least monthly"),
    ] = False,
    cover_url: Annotated[
        str | None,
        typer.Option("--cover-url", help="Public URL of the cover image; else a video frame"),
    ] = None,
    no_research: Annotated[
        bool, typer.Option("--no-research", help="Skip Claude's web search (faster, cheaper)")
    ] = False,
    preview_voice: Annotated[
        bool,
        typer.Option("--preview-voice", help="Synthesize one sample line and exit"),
    ] = False,
    account: Annotated[
        str | None,
        typer.Option("--account", help="Which account under accounts/ this run is for"),
    ] = None,
    migrate_account: Annotated[
        str | None,
        typer.Option(
            "--migrate-account",
            help="Plan the move of the single account layout into accounts/<name>/",
        ),
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    _setup_logging(verbose)

    # Before anything reads a Settings, because `get_settings()` is cached and
    # every stage downstream reads whichever one it hands back. This is the
    # whole of `--account`: bind the process, and no stage signature changes.
    if migrate_account:
        return _migrate_account(migrate_account, apply=yes)
    try:
        cfg = select_account(resolve_account(account))
    except ConfigError as exc:
        console.print(f"[bold red]Setup problem[/]\n{exc}")
        raise typer.Exit(1) from exc
    log.debug("Account %s, build dir %s", cfg.account, cfg.build_dir)

    if no_research:
        cfg.claude_research = False

    # --- Bookkeeping commands: do one thing, then stop. --------------------
    if posted:
        scraper.mark_featured(cfg, posted)
        console.print(
            f"[bold green]Marked posted:[/] {posted} "
            f"[dim](on cooldown for {cfg.repo_cooldown_days} days)[/]"
        )
        return

    if unmark:
        was = scraper.unmark_featured(cfg, unmark)
        # The gateway holds two separate reasons to skip a repo, a commitment
        # and a render, and a rejected video usually has both. Clearing one and
        # leaving the other is how a repo stays invisible to discovery with
        # nothing local left to explain it.
        forgotten = gateway.forget_rendered(unmark, cfg)
        if was:
            console.print(f"[bold green]Cooldown cleared:[/] {unmark} [dim](was set {was})[/]")
        elif forgotten:
            console.print(f"[bold green]Render record cleared:[/] {unmark}")
        else:
            console.print(f"[yellow]{unmark} was not on cooldown.[/]")
        return

    if candidates:
        _preflight(need_github=True, need_claude=False)
        scraper.inspect_candidates(cfg)
        return

    if covered:
        _show_covered(cfg)
        return

    if history:
        _show_history(cfg)
        return

    if cohorts:
        _show_cohorts(cfg, cohorts)
        return

    if show_results:
        _show_results(cfg)
        return

    if backfill:
        _preflight(need_github=False, need_claude=False, need_instagram=True)
        _backfill(cfg, apply=yes)
        return

    if snapshot:
        _preflight(need_github=True, need_claude=False)
        count = scraper.snapshot_stars(cfg)
        console.print(f"[bold green]Snapshotted[/] {count} repos")
        # Piggybacked on the daily job because the failure it prevents is a slow
        # one: a token nobody refreshed for 60 days is dead and needs a browser
        # to replace. A no-op on almost every run is the intended behaviour.
        try:
            if state := publisher.refresh_token_if_due(cfg):
                console.print(f"[dim]Instagram token renewed, {state.days_left:.0f} days left[/]")
        except publisher.PublishError as exc:
            console.print(f"[yellow]Instagram token not renewed:[/] {exc}")
        return

    if refresh_token:
        _preflight(need_github=False, need_claude=False, need_instagram=True)
        try:
            state = publisher.refresh_token(cfg)
        except publisher.PublishError as exc:
            console.print(f"[bold red]Could not refresh the token[/]\n{exc}")
            raise typer.Exit(1) from exc
        left = f"{state.days_left:.0f} days" if state.days_left is not None else "unknown"
        console.print(f"[bold green]Token renewed[/] [dim](valid for {left})[/]")
        console.print(f"[dim]Stored in {cfg.ig_token_path}[/]")
        return

    if publish:
        _preflight(need_github=False, need_claude=False, need_instagram=True)
        _publish_run(cfg, cfg.build_dir / publish, cover_url=cover_url)
        return

    if enqueue:
        _enqueue_run(cfg, cfg.build_dir / enqueue, approved=approve)
        return

    if preview_voice:
        sample = (
            "Eden is a homelab run entirely from Git. Talos Linux on Proxmox, "
            "ArgoCD on top. The useful part is the deploy pattern."
        )
        out = cfg.build_dir / f"voice-preview-{tts.voice_name(cfg)}{tts.audio_suffix(cfg)}"
        tts.synthesize(sample, out, cfg)
        console.print(f"[bold green]Preview:[/] {out}")
        return

    if recover:
        # No GitHub and no Claude: recovery re-runs stages, it never decides to
        # spend. A run with no script is reported and left for discovery.
        _preflight(need_github=False, need_claude=False, need_instagram=post)
        _recover(cfg, approve=approve, max_queue=max_queue)
        return

    if batch is not None:
        if batch < 1:
            console.print("[bold red]--batch needs a positive number.[/]")
            raise typer.Exit(1)
        _preflight(need_github=True, need_claude=True, need_instagram=post)
        _run_batch(
            cfg, batch, stop_after=stop_after, post=post, cover_url=cover_url,
            max_queue=max_queue,
        )
        return

    _preflight(
        need_github=resume is None and repo_full_name is None,
        need_claude=stop_after != Stage.SCRAPE,
        # Checked now rather than after the render, so a missing token costs a
        # config error instead of ten minutes of Remotion.
        need_instagram=post,
    )

    run_dir = cfg.build_dir / resume if resume else None
    if resume and not (run_dir and run_dir.exists()):
        console.print(f"[bold red]No such build dir:[/] {run_dir}")
        raise typer.Exit(1)

    def done(stage: Stage) -> bool:
        return stop_after is not None and ORDER.index(stage) >= ORDER.index(stop_after)

    # ---- 1. Scrape ------------------------------------------------------
    if run_dir and (run_dir / "repo.json").exists():
        repo = RepoCandidate.model_validate_json((run_dir / "repo.json").read_text())
        console.rule(f"[dim]resuming {repo.full_name}")
    else:
        console.rule("[bold]1/5  Finding today's repository")
        if repo_full_name:
            import httpx

            from sources.github import GitHubClient

            # No token required here: one named repo costs a single core
            # request, which fits inside the anonymous budget.
            with GitHubClient(cfg.github_token) as gh:
                try:
                    item = gh.fetch_repo(repo_full_name)
                except httpx.HTTPStatusError as exc:
                    console.print(
                        f"[bold red]Could not fetch {repo_full_name}[/] "
                        f"(HTTP {exc.response.status_code}). "
                        f"Check the name, and that the repo is public."
                    )
                    raise typer.Exit(1) from exc
                repo = scraper._to_candidate(item)
                repo.readme = gh.fetch_readme(repo.full_name, cfg.readme_char_budget)
            # Discovery snapshots every repo it sees; this path skips discovery,
            # so record it by hand or a later run has no history for this repo.
            scraper.record_snapshot(cfg, repo)
        else:
            repo = scraper.find_trending_repo(cfg)

        run_dir = cfg.run_dir(repo.slug)
        (run_dir / "repo.json").write_text(repo.model_dump_json(indent=2))

    console.print(
        f"  [cyan]{repo.full_name}[/]  {repo.stars:,}★  "
        f"{repo.language or '?'}  {repo.license_spdx or '?'}"
    )
    console.print(f"  [dim]{repo.description[:110]}[/]")
    if done(Stage.SCRAPE):
        return _finish(run_dir)

    script = _render_one(cfg, repo, run_dir, stop_after=stop_after)
    if script is None:
        return _finish(run_dir)

    if post:
        console.rule("[bold]Publishing to Instagram")
        _publish_run(cfg, run_dir, cover_url=cover_url)
        console.print(
            f"[dim]Iterate on the look:  cd video && npm run studio  "
            f"(then load {run_dir / 'video.json'})[/]"
        )
        return

    # Otherwise, make the one remaining manual step -- dropping the file into
    # Instagram -- as short as possible.
    if script.caption_text:
        console.print(f"\n[bold]Instagram caption[/]\n{script.caption_text}")
        if publisher.copy_to_clipboard(script.caption_text):
            console.print("[dim]  (copied to the clipboard)[/]")
        console.print("[dim]  (also written to caption.txt)[/]")
    publisher.reveal(run_dir / "out.mp4")

    console.print(
        f"\n[bold]Post it from here:[/]  "
        f"python main.py --publish {run_dir.parent.name}/{run_dir.name}"
    )
    console.print(
        f"[dim]That uploads it and starts the {cfg.repo_cooldown_days}-day cooldown in one "
        f"step. If you posted it by hand instead: python main.py --posted {repo.full_name}[/]"
    )
    console.print(
        "[dim]Rendering alone starts no cooldown, so a video you reject costs you nothing.[/]"
    )
    console.print(
        f"[dim]Iterate on the look:  cd video && npm run studio  "
        f"(then load {run_dir / 'video.json'})[/]"
    )


def _render_one(
    cfg: Settings, repo: RepoCandidate, run_dir: Path, *, stop_after: Stage | None = None
) -> VideoScript | None:
    """Stages 2 to 5 for one repo, from an existing run dir holding repo.json.

    Shared by the single run and by `--batch`, so a batch cannot drift from
    what a hand-driven run produces. Returns the script once the video and its
    covers exist, or None if `--stop-after` ended it early.
    """

    def done(stage: Stage) -> bool:
        return stop_after is not None and ORDER.index(stage) >= ORDER.index(stop_after)

    # ---- 2. Script ------------------------------------------------------
    script_path = run_dir / "script.json"
    if script_path.exists():
        script = VideoScript.model_validate_json(script_path.read_text())
        console.rule("[bold]2/5  Script [dim](cached)")
    else:
        console.rule("[bold]2/5  Writing the script with Claude Code")
        with console.status("Claude is researching and writing..."):
            script, envelope = write_script(repo, cfg)
        script_path.write_text(script.model_dump_json(indent=2))
        # Keep the envelope: it carries the web-search count and cost, which is
        # how you audit whether research actually happened.
        (run_dir / "claude_envelope.json").write_text(json.dumps(envelope, indent=2))
        # And what wrote it. Three posts a day go out while the prompt is being
        # edited, so without this "did the hook change work" cannot be answered
        # afterwards: the numbers attach to a video and nothing says which
        # version of the rules produced it.
        results_mod.write_recipe(run_dir, cfg)

    console.print(f'  [bold]"{script.hook}"[/]')
    console.print(f"  [dim]{script.word_count} words · {len(script.visual_cues)} cues[/]")
    if done(Stage.SCRIPT):
        return _finish(run_dir)

    # ---- 3. Audio -------------------------------------------------------
    audio_path = run_dir / f"voice{tts.audio_suffix(cfg)}"
    # A previous run may have used the other backend; reuse whatever is there.
    existing = next(
        (p for p in (audio_path, run_dir / "voice.mp3", run_dir / "voice.wav") if p.exists()),
        None,
    )
    # The voice reads the ask too. The caption carries it and so does the end
    # card, but the caption is behind a "more" tap and the card can be scrolled
    # past, so the one channel that reaches everyone who watches is the audio.
    # Strip any ask the model wrote itself before appending ours, or the voice
    # reads two of them back to back.
    spoken = gateway.strip_written_cta(script.spoken_script)
    # The cues quote that same ask, and an excerpt describing speech we just
    # stripped can never be found in the transcript, which costs the whole
    # video its word level scene timing rather than just that one cue.
    script = script.model_copy(
        update={"visual_cues": gateway.strip_cta_cues(script.visual_cues)}
    )
    cta_line = gateway.spoken_cta(cfg)
    if cta_line:
        spoken = f"{spoken.rstrip()} {cta_line}"

    if existing is None:
        console.rule("[bold]3/5  Synthesizing the voiceover")
        tts.synthesize(spoken, audio_path, cfg)
    else:
        audio_path = existing
        console.rule("[bold]3/5  Voiceover [dim](cached)")
    console.print(f"  [dim]{cfg.tts_backend} · {tts.voice_name(cfg)}[/]")

    duration = tts.audio_duration_seconds(audio_path)
    flag = "[green]" if 25 <= duration <= 50 else "[yellow]"
    console.print(f"  {flag}{duration:.1f}s[/]  (target 30-45s)")
    if done(Stage.AUDIO):
        return _finish(run_dir)

    # ---- 4. Captions ----------------------------------------------------
    captions_path = run_dir / "captions.json"
    if captions_path.exists():
        caps = [Caption.model_validate(c) for c in json.loads(captions_path.read_text())]
        console.rule("[bold]4/5  Captions [dim](cached)")
    else:
        console.rule("[bold]4/5  Aligning captions with Whisper")
        with console.status(f"Transcribing with {cfg.whisper_model}..."):
            caps = captions_mod.transcribe(audio_path, cfg, spoken)
        captions_path.write_text(
            json.dumps([json.loads(c.model_dump_json()) for c in caps], indent=2)
        )
    console.print(f"  [dim]{len(caps)} words aligned[/]")
    if done(Stage.CAPTIONS):
        return _finish(run_dir)

    # ---- 5. Render ------------------------------------------------------
    console.rule("[bold]5/5  Rendering")

    # public/ is a staging area, not a store. Clear out other runs' copies
    # before adding ours, so it doesn't grow an audio file and a screenshot per
    # video forever.
    renderer.prune_staged_assets(cfg.video_dir, repo.slug)

    # Opening shot: the real GitHub page. Cached across re-runs, and entirely
    # optional -- a capture failure just means the video opens on a card.
    shot_path = run_dir / "repo.png"
    if not shot_path.exists():
        with console.status("Capturing the GitHub page..."):
            screenshot.capture_repo(repo.url, shot_path)
    screenshot_src = (
        renderer.stage_asset(shot_path, cfg.video_dir, repo.slug)
        if shot_path.exists()
        else None
    )
    console.print(
        f"  [dim]screenshot: {'captured' if screenshot_src else 'unavailable, opening on card'}[/]"
    )

    audio_src = renderer.stage_asset(audio_path, cfg.video_dir, repo.slug)
    video_spec: VideoSpec = spec_mod.build_spec(
        repo, script, caps, duration, audio_src, cfg,
        screenshot_src=screenshot_src,
        # The ask is audio no visual cue was written for, so the spec needs to
        # know its words to give it a scene of its own.
        spoken_cta=cta_line,
    )
    # The end card. Shown only when the voice actually read the ask, so the two
    # cannot disagree about whether there is one.
    video_spec = video_spec.model_copy(update={"showFollowCta": bool(cta_line)})
    renderer.write_spec(video_spec, run_dir / "video.json")

    out_path = run_dir / "out.mp4"
    with console.status("Remotion is rendering..."):
        renderer.render(video_spec, out_path, cfg)

    # An MP4 now exists, which is the fact worth recording. Rendering still
    # starts no cooldown; this only stops tomorrow's discovery spending a
    # script, a voiceover and a render rebuilding it. `--unmark` takes it back.
    gateway.register_rendered(
        repo.full_name,
        cfg,
        run_folder=f"{run_dir.parent.name}/{run_dir.name}",
        # From the candidate that was ranked, so the panel can say why this repo
        # won rather than only that it did. Empty on a run started from a
        # `repo.json` written before the scorer recorded a breakdown.
        score=repo.score,
        score_breakdown=repo.score_breakdown,
    )

    with console.status("Rendering cover stills..."):
        covers = renderer.render_covers(video_spec, run_dir, cfg)

    console.rule("[bold green]Done")
    console.print(f"  [bold]{out_path}[/]")
    console.print(f"  [dim]{video_spec.durationInFrames / video_spec.fps:.1f}s · "
                  f"{video_spec.width}x{video_spec.height}[/]")
    if covers:
        console.print(f"  [dim]covers: {', '.join(p.name for p in covers)}[/]")

    # The caption is written either way: --post needs it to send, and a run you
    # come back to tomorrow needs it because the clipboard is long gone.
    if script.caption_text:
        caption_out = gateway.add_caption_cta(script.caption_text.strip(), cfg)
        (run_dir / "caption.txt").write_text(caption_out.rstrip() + "\n")

    return script


def _run_batch(
    cfg: Settings,
    count: int,
    *,
    stop_after: Stage | None,
    post: bool,
    cover_url: str | None,
    max_queue: int | None = None,
) -> None:
    """Render `count` distinct repos back to back.

    The gateway holds three slots a day and the launchd job renders one, so
    without this two of three slots starve every day. Discovery runs once for
    the whole batch: nothing marks a repo as taken until it is published or
    queued, so ranking per video would pick the same winner every time.

    One repo failing does not stop the others. A batch is a day's worth of
    posting, and losing all of it because the third script tripped the dash
    validator is worse than losing one.
    """
    # Asked before discovery, because the cheapest batch is the one that never
    # starts. A scheduled render with no ceiling fills the queue faster than
    # three slots a day drain it, and the back of a long line goes out stale.
    if max_queue is not None:
        pending = gateway.fetch_pending_count(cfg)
        if pending is None:
            console.print(
                "[yellow]Cannot read the gateway queue, so --max-queue cannot be honoured.[/] "
                "[dim]Refusing rather than guessing; a batch is expensive to undo.[/]"
            )
            return
        if pending >= max_queue:
            console.print(
                f"[bold]Nothing to do.[/] {pending} posts already waiting "
                f"[dim](--max-queue {max_queue}). Approve or cancel some first.[/]"
            )
            return
        # Never queue past the ceiling, however big a batch was asked for.
        count = min(count, max_queue - pending)
        console.print(f"[dim]{pending} waiting, room for {count}.[/]")

    console.rule(f"[bold]Finding the top {count} repositories")
    repos = scraper.find_trending_repos(cfg, count=count)
    if len(repos) < count:
        console.print(
            f"[yellow]Only {len(repos)} of {count} repos are available[/] "
            f"[dim](the rest are already covered; see --covered).[/]"
        )
    for i, repo in enumerate(repos, 1):
        console.print(f"  [dim]{i}.[/] [cyan]{repo.full_name}[/]  {repo.stars:,}★  "
                      f"[dim]{repo.description[:80]}[/]")

    done: list[tuple[RepoCandidate, Path]] = []
    failed: list[tuple[RepoCandidate, str]] = []

    for i, repo in enumerate(repos, 1):
        console.rule(f"[bold]Video {i}/{len(repos)}  {repo.full_name}")
        run_dir = cfg.run_dir(repo.slug)
        (run_dir / "repo.json").write_text(repo.model_dump_json(indent=2))
        try:
            script = _render_one(cfg, repo, run_dir, stop_after=stop_after)
        except Exception as exc:  # noqa: BLE001 -- one bad repo must not end the batch
            log.exception("Video %d failed", i)
            failed.append((repo, f"{type(exc).__name__}: {exc}"))
            continue
        if script is None:
            console.print(f"[dim]Stopped after {stop_after}; artifacts in {run_dir}[/]")
            continue
        done.append((repo, run_dir))
        if post:
            console.rule(f"[bold]Publishing {repo.full_name}")
            _publish_run(cfg, run_dir, cover_url=cover_url)

    console.rule("[bold green]Batch done" if not failed else "[bold yellow]Batch done")
    for repo, run_dir in done:
        console.print(f"  [green]✓[/] {repo.full_name}  [dim]{run_dir / 'out.mp4'}[/]")
    for repo, why in failed:
        console.print(f"  [red]✗[/] {repo.full_name}  [dim]{why}[/]")

    if done and not post:
        console.print("\n[bold]Queue them for the gateway's slots:[/]")
        for _, run_dir in done:
            console.print(
                f"  python main.py --enqueue {run_dir.parent.name}/{run_dir.name} --approve"
            )
        console.print(
            "[dim]Watch each one first. --enqueue starts the cooldown, because a queued "
            "post goes out days later with nobody here to catch it.[/]"
        )


# Today and yesterday. An unfinished render older than that is stale news, and
# posting a two week old "trending" repo is worse than dropping it.
_RECOVER_DAYS = 2


def _recover(cfg: Settings, *, approve: bool, max_queue: int | None) -> None:
    """Finish what a killed run left behind.

    The nightly renders inside a container that can go away mid-batch. On
    2026-08-14 the voice model took it past its 8 GiB limit and the OOM killer
    took the session with it, three stages into the third video; the script was
    on disk and paid for, the batch process was not. Nothing picks that up,
    because the thing that would have is what died.

    So this is a separate pass over what is already on disk. No discovery, no
    ranking, no Claude: it only runs the stages a half-finished folder still
    owes, and hands the result to the gateway. Run it after the batch, and
    again from somewhere that was not inside the batch.

    Narrow on purpose, because the build tree is full of videos that were never
    meant to ship:

    - `queued.json` or `published.json` means the run is committed already, and
      those are the same two guards `--enqueue` and `--publish` read.
    - A dot in the folder name (`.prev`, `.v2`) means a person moved it aside,
      which is the documented way to reject a render. `RepoCandidate.slug`
      turns every dot into a hyphen, so a dot can only have come from a human.
    - A repo already on the cooldown list shipped from some other folder. The
      receipt is per folder and cannot see a sibling, and the cooldown can.
    - No `script.json` means the run died before Claude answered. Writing one
      now is discovery's decision to spend, not recovery's, so it is reported
      and left.
    """
    if not cfg.build_dir.is_dir():
        console.print("[dim]Nothing built yet.[/]")
        return

    # The ceiling matters more here than in a batch: recovery is the path that
    # runs when something already went wrong, and a pass that queues four days
    # of salvage into three slots a day is its own outage.
    room: int | None = None
    if max_queue is not None:
        pending = gateway.fetch_pending_count(cfg)
        if pending is None:
            console.print(
                "[yellow]Cannot read the gateway queue, so --max-queue cannot be honoured.[/] "
                "[dim]Refusing rather than guessing.[/]"
            )
            return
        room = max_queue - pending
        if room <= 0:
            console.print(
                f"[bold]Nothing to do.[/] {pending} posts already waiting "
                f"[dim](--max-queue {max_queue}).[/]"
            )
            return

    stamps = {
        (date.today() - timedelta(days=n)).isoformat() for n in range(_RECOVER_DAYS)
    }
    covered = {name for name, _ in scraper.covered_repos(cfg)}

    pending_runs: list[Path] = []
    for day_dir in sorted(p for p in cfg.build_dir.iterdir() if p.name in stamps):
        for run_dir in sorted(p for p in day_dir.iterdir() if p.is_dir()):
            if "." in run_dir.name:
                continue
            if (run_dir / "queued.json").exists() or (run_dir / "published.json").exists():
                continue
            if not (run_dir / "repo.json").exists():
                continue
            pending_runs.append(run_dir)

    if not pending_runs:
        console.print("[bold green]Nothing to recover.[/] [dim]Every recent run is committed.[/]")
        return

    console.rule(f"[bold]Recovering {len(pending_runs)} unfinished run(s)")
    recovered: list[Path] = []
    skipped: list[tuple[Path, str]] = []

    for run_dir in pending_runs:
        rel = f"{run_dir.parent.name}/{run_dir.name}"
        repo = RepoCandidate.model_validate_json((run_dir / "repo.json").read_text())

        if repo.full_name in covered:
            skipped.append((run_dir, "repo already on cooldown from another run"))
            continue
        if room is not None and len(recovered) >= room:
            skipped.append((run_dir, "queue ceiling reached"))
            continue

        if not (run_dir / "out.mp4").exists():
            if not (run_dir / "script.json").exists():
                skipped.append((run_dir, "no script; would need a fresh Claude run"))
                continue
            console.rule(f"[dim]finishing {repo.full_name}")
            try:
                if _render_one(cfg, repo, run_dir) is None:
                    skipped.append((run_dir, "render produced nothing"))
                    continue
            except Exception as exc:  # noqa: BLE001 -- one bad run must not end the sweep
                log.exception("Recovering %s failed", rel)
                skipped.append((run_dir, f"{type(exc).__name__}: {exc}"))
                continue

        try:
            _enqueue_run(cfg, run_dir, approved=approve)
        except typer.Exit:
            # _enqueue_run already said why. A failure to hand it over leaves
            # the video on disk with no receipt, which is exactly the state
            # this pass exists to find, so the next one tries again.
            skipped.append((run_dir, "gateway would not take it"))
            continue
        recovered.append(run_dir)
        covered.add(repo.full_name)

    console.rule("[bold green]Recovery done" if not skipped else "[bold yellow]Recovery done")
    for run_dir in recovered:
        console.print(f"  [green]✓[/] {run_dir.parent.name}/{run_dir.name}  [dim]queued[/]")
    for run_dir, why in skipped:
        console.print(f"  [dim]·[/] {run_dir.parent.name}/{run_dir.name}  [dim]{why}[/]")


def _show_covered(cfg: Settings) -> None:
    """Print every repo we have committed to a post about.

    The store has always existed as a filter; this makes it readable, because
    "have we done this one already" is a question you ask far more often than
    the scorer does.
    """
    from rich.table import Table

    rows = scraper.covered_repos(cfg)
    if not rows:
        console.print("[dim]Nothing covered yet.[/]")
        return

    today = datetime.now(UTC).date()
    table = Table(title=f"Covered repositories ({len(rows)})", header_style="bold")
    table.add_column("Repository", style="cyan", no_wrap=True)
    table.add_column("Covered")
    table.add_column("Status")

    for full_name, used_on in rows:
        age = (today - date.fromisoformat(used_on)).days
        left = cfg.repo_cooldown_days - age
        status = f"[yellow]blocked, {left}d left[/]" if left > 0 else "[green]free again[/]"
        table.add_row(full_name, used_on, status)

    console.print(table)
    console.print(
        f"[dim]Blocked repos are dropped during discovery, before any README is fetched. "
        f"Cooldown is {cfg.repo_cooldown_days} days (REPO_COOLDOWN_DAYS).[/]"
    )


def _show_history(cfg: Settings) -> None:
    """Every repo this account has touched, and how far it got.

    `--covered` answers "may discovery pick this", which is the scorer's
    question rather than the one a person asks. This answers "have we talked
    about this, and when did it go out", which needs three records that live in
    three places and have never been read together.

    The three are deliberately different strengths and stay distinguishable
    rather than being flattened into one date. Covered is a commitment and is
    the only one that blocks discovery, for `repo_cooldown_days`. Rendered is
    only "a video exists", so a repo can sit there having cost a script, a
    voiceover and a render and never have gone out. Posted is the last time a
    Reel about it actually published.

    **Made and Posted are separate columns because they are days apart.** The
    queue is meant to sit about three days deep, so a Reel published this
    morning was written from whatever the code said earlier in the week. Read
    together with Recipe, that is what makes "did the change work" answerable:
    rows are comparable when their recipes match and not when they do not, and
    the publish date says nothing about either.

    Publish dates come from the queue rather than from `/api/results`, which
    omits a post until it has a retention reading. A Reel that went out this
    afternoon has none, and reporting it as never posted would be wrong in
    exactly the window someone is most likely to be looking.

    Degrades the way everything else that reads the gateway does. With it
    unreachable this still prints the local store, which is the half that
    decides what happens tomorrow morning.
    """
    from rich.table import Table

    # Merged in memory, not written back. `data/used_repos.json` is one file on
    # one laptop and the nightly marks its repos on the render host, so reading
    # the local store alone reports a repo this account committed to days ago as
    # never covered. Discovery already folds the two together with
    # `_sync_covered`; a listing command has no business writing the store as a
    # side effect of being run, so it does the same merge and keeps it.
    #
    # The earlier date wins on a conflict, matching `UsedRepos.merge`: taking the
    # later one would extend the cooldown by however long the two disagree.
    covered = dict(scraper.covered_repos(cfg))
    for name, when in gateway.fetch_covered(cfg).items():
        day = when[:10]
        if day and (name not in covered or day < covered[name]):
            covered[name] = day
    rendered = gateway.fetch_rendered(cfg)
    readings = {
        row["repo_full_name"]: row
        for row in gateway.fetch_results(cfg)
        if row.get("repo_full_name")
    }

    # Last publish wins per repo. The cooldown stops two inside its window, but
    # `--unmark` and a re-post can put one either side of it.
    posts: dict[str, dict] = {}
    for row in gateway.fetch_queue(cfg) or []:
        name, when = row.get("repo_full_name"), row.get("published_at")
        if not name or not when:
            continue
        if when > (posts.get(name, {}).get("published_at") or ""):
            posts[name] = row
    # A Reel put out with `--publish` never made a queue row, so the readings
    # are the only trace it left. Fewer columns is worth far more than absence.
    for name, row in readings.items():
        if name not in posts:
            posts[name] = row

    names = sorted(set(covered) | set(rendered) | set(posts))
    if not names:
        console.print("[dim]Nothing touched yet.[/]")
        return

    def _day(value: str | None) -> str:
        return (value or "")[:10]

    def _latest(name: str) -> str:
        return max(
            _day(covered.get(name)),
            _day(rendered.get(name)),
            _day((posts.get(name) or {}).get("published_at")),
        )

    names.sort(key=_latest, reverse=True)

    today = datetime.now(UTC).date()
    table = Table(title=f"Repositories touched ({len(names)})", header_style="bold")
    table.add_column("Repository", style="cyan", no_wrap=True, overflow="ellipsis")
    table.add_column("Made")
    table.add_column("Posted")
    table.add_column("Views", justify="right")
    table.add_column("Skip", justify="right")
    table.add_column("Recipe", style="dim")
    table.add_column("Status")

    posted_count = 0
    for name in names:
        post = posts.get(name) or {}
        reading = readings.get(name) or {}
        published = _day(post.get("published_at"))
        if published:
            posted_count += 1

        used_on = covered.get(name)
        if used_on:
            left = cfg.repo_cooldown_days - (today - date.fromisoformat(used_on)).days
            status = f"[yellow]{left}d left[/]" if left > 0 else "[green]free again[/]"
        elif name in rendered:
            # Rendered and never committed to, so nothing blocks it and a video
            # may be sitting on a disk having cost a full run.
            status = "[dim]not committed[/]"
        else:
            status = ""

        # The commit half only. The digest after it separates two runs from one
        # checkout with a prompt edited in between, which matters to `--results`
        # and is more than a column this wide can carry.
        recipe = str(post.get("recipe") or reading.get("recipe") or "").split(".")[0]
        skip, views = reading.get("skip_rate"), reading.get("views")
        table.add_row(
            name,
            _day(post.get("created_at") or rendered.get(name)),
            published,
            f"{int(views):,}" if views else "",
            f"{float(skip):.1f}%" if skip else "",
            recipe or ("[dim]before[/]" if published else ""),
            status,
        )

    console.print(table)

    stranded = sum(1 for n in names if n in rendered and n not in posts)
    console.print(
        f"[dim]{posted_count} of {len(names)} have published. {stranded} have a render "
        f"and no post, which is a draft, a queued post waiting for its slot, or one "
        f"that failed quietly.\n"
        f"Made is when the video reached the queue, Posted is when its slot fired, and "
        f"the gap between them is how deep the queue was. Two rows are only comparable "
        f"when their recipes match.[/]"
    )


def _show_cohorts(cfg: Settings, dimension: str) -> None:
    """Group the published Reels by something and compare the groups.

    Every number in `IDEAS.md` was computed by hand in a session and pasted in,
    and `CLAUDE.md` already names what that costs: a fact about the numbers goes
    stale silently and the only symptom is worse decisions. This recomputes.

    Two dimensions, because they are the two questions the account keeps asking
    and neither could be answered from anything the pipeline printed.

    `recipe` is "did the change work". Rows are comparable when their recipes
    match and not when they do not, and until the recipe travelled with the
    video there was no way to group by it. Publish dates cannot stand in: the
    queue runs days deep, so eight changes landing in one evening are
    indistinguishable from the four videos already queued when they landed.

    `slot` is "does it matter when it goes out". The slot a post lands in is
    decided by queue position rather than by anything about the video, so the
    groups are close to randomly assigned with respect to content, which is
    rare enough here to be worth using.

    **Read the breakouts column, not the median.** Eight of the first 58 posts
    carried a third of all views, so the median describes the post that failed
    and the tail is where everything actually is. Two cohorts can have identical
    medians and completely different value.

    Posts that have not finished arriving are held back rather than shown with
    an age column and a warning. A Reel reaches about 99 percent of its final
    views by its third daily reading, so a cohort holding yesterday's post is
    not reporting a worse slot, it is reporting a younger post. The count that
    was dropped is printed, because a table that silently lost four posts reads
    as one that covered everything.
    """
    from rich.table import Table

    # A post reaches about 99 percent of its final views by its third daily
    # reading and 56 percent by its first, measured on this account's own
    # history and shown on the panel's Insights page. Counting one that has not
    # finished arriving does not report a worse slot, it reports a younger post.
    #
    # The number is duplicated from `gateway/analysis.py` rather than imported,
    # because the pipeline holds no gateway code and the gateway holds no
    # pipeline code. Both read it from the same `readings` field, so the two
    # views hold back the same posts, which is the property worth having.
    settled_after = 3
    everything = [r for r in gateway.fetch_results(cfg) if r.get("skip_rate")]
    # A gateway too old to send `readings` sends nothing rather than zero, and
    # no history is not evidence that a post is unsettled.
    rows = [
        r for r in everything
        if r.get("readings") is None or int(r["readings"]) >= settled_after
    ]
    held_back = len(everything) - len(rows)
    if not rows:
        console.print(
            "[dim]No results yet. Either the gateway is unreachable, or no post has a "
            "retention reading, which takes a few hours after publishing.[/]"
        )
        return

    def _slot(row: dict) -> str:
        """The hour a post went out, not the minute.

        The scheduler jitters each slot by an offset derived from the slot id
        and the date, so no two posts share a publish time and grouping on the
        exact stamp gives 43 cohorts of one. The hour is the slot. A slot that
        straddles an hour boundary splits into two rows, which is visible and
        honest; clustering nearby times would be guessing at the schedule from
        its output.
        """
        stamp = row.get("published_at") or ""
        with contextlib.suppress(ValueError):
            return f"{datetime.fromisoformat(stamp).astimezone(UTC):%H}:00 UTC"
        return "unknown"

    def _recipe(row: dict) -> str:
        return str(row.get("recipe") or "") or "before recipes"

    key = {"slot": _slot, "recipe": _recipe}.get(dimension)
    if key is None:
        console.print(
            f"[bold red]No such dimension:[/] {dimension} [dim](recipe or slot)[/]"
        )
        raise typer.Exit(1)

    cohorts: dict[str, list[dict]] = {}
    for row in rows:
        cohorts.setdefault(key(row), []).append(row)

    table = Table(
        title=f"Cohorts by {dimension} ({len(rows)} posts with readings)",
        header_style="bold",
    )
    table.add_column(dimension.title(), style="cyan", no_wrap=True)
    table.add_column("n", justify="right")
    table.add_column("Skip", justify="right")
    table.add_column("Views", justify="right")
    table.add_column("Best", justify="right")
    table.add_column("Over 500", justify="right")
    table.add_column("Under 60%", justify="right")

    # Slots read in time order, because the question is about the shape of the
    # day. Recipes have no meaningful order, so the biggest cohort leads.
    order = (
        (lambda kv: kv[0]) if dimension == "slot" else (lambda kv: (-len(kv[1]), kv[0]))
    )
    for name, group in sorted(cohorts.items(), key=order):
        skips = [float(r["skip_rate"]) for r in group]
        views = [int(r.get("views") or 0) for r in group]
        # The threshold the whole file is judged against: under it, median views
        # jumped several fold. Counted rather than averaged, because it is a
        # question about how often a post clears a bar.
        good = sum(1 for s in skips if s < 60)
        big = sum(1 for v in views if v > 500)
        table.add_row(
            name,
            str(len(group)),
            f"{median(skips):.1f}%",
            f"{median(views):,.0f}",
            f"{max(views):,}",
            f"{big} ({100 * big / len(group):.0f}%)",
            f"{good} ({100 * good / len(group):.0f}%)",
        )

    console.print(table)
    console.print(
        "[dim]Skip is the share who scrolled past inside the first three seconds. "
        "Under 60% is the threshold below which median views jumped several fold, "
        "and Over 500 is how often a post actually reached anybody.\n"
        + (
            f"{held_back} post(s) held back, having fewer than {settled_after} readings "
            "and so not finished arriving.\n"
            if held_back
            else ""
        )
        + "n is small here, so treat a gap of a few points as noise.[/]"
    )


def _show_results(cfg: Settings) -> None:
    """What the published Reels did, worst opening last.

    The same data the scriptwriter is now given, for the person deciding what
    to change next. Sorted by skip rate rather than by date, because the
    question is which opening worked rather than what happened when.
    """
    from rich.table import Table

    posts = results_mod.past_posts(cfg, limit=50)
    if not posts:
        console.print(
            "[dim]No results yet. Either the gateway is unreachable, or no post "
            "has a retention reading, which takes a few hours after publishing.[/]"
        )
        return

    table = Table(title=f"Published Reels ({len(posts)})", header_style="bold")
    table.add_column("Skip", justify="right")
    table.add_column("Watch", justify="right")
    table.add_column("Views", justify="right")
    table.add_column("Hook", style="cyan")
    table.add_column("Recipe", style="dim")

    for post in posts:
        # 30 to 40 percent is the published average for this format. Colour is
        # against that rather than against the account's own spread, which
        # would make the least bad of a bad set look green.
        colour = "green" if post.skip_rate < 50 else "yellow" if post.skip_rate < 70 else "red"
        table.add_row(
            f"[{colour}]{post.skip_rate:.1f}%[/]",
            f"{post.avg_watch_s:.1f}s",
            f"{post.views:,}",
            post.hook,
            post.recipe or "before recipes",
        )

    console.print(table)
    console.print(
        "[dim]Skip is the share who scrolled past inside the first three seconds, so it "
        "scores the hook alone. Educational Reels average 30 to 40 percent.\n"
        "Recipe is the checkout and settings that wrote the script. Two rows with "
        "different recipes are not comparable.[/]"
    )


def _backfill(cfg: Settings, *, apply: bool) -> None:
    """Bring a Reel posted outside the pipeline into the feedback loop.

    Lists by default and registers only with `--yes`, because it reads the whole
    account and matches on text that has been through a phone keyboard. Seeing
    which run folder it believes made which post costs one command; finding out
    afterwards that it registered a `.v2` draft costs the loop its credibility.
    """
    from rich.table import Table

    try:
        found = backfill_mod.plan(cfg)
    except publisher.PublishError as exc:
        console.print(f"[bold red]Could not read the account's media:[/] {exc}")
        raise typer.Exit(1) from exc

    if found.ambiguous:
        console.print(
            f"[yellow]{len(found.ambiguous)} caption(s) are claimed by more than one run "
            "folder and were skipped.[/] [dim]Rename or move aside the folders that did "
            "not ship.[/]"
        )
    if found.unmatched:
        console.print(
            f"[dim]{len(found.unmatched)} live post(s) match no run folder: "
            f"{', '.join(m.get('id', '?') for m in found.unmatched)}. A caption edited "
            "after posting will land here.[/]"
        )
    if not found.matched:
        console.print("[dim]Nothing to backfill.[/]")
        return

    table = Table(title=f"Matched ({len(found.matched)})", header_style="bold")
    table.add_column("Published")
    table.add_column("Repo", style="cyan")
    table.add_column("Run", style="dim")
    table.add_column("Hook")

    for match in found.matched:
        table.add_row(
            match.published_at.strftime("%Y-%m-%d %H:%M"),
            match.repo_full_name,
            f"{match.run.parent.name}/{match.run.name}",
            match.hook,
        )
    console.print(table)

    if not apply:
        console.print(
            "[dim]Nothing was registered. Re-run with --yes once the pairings above "
            "look right. Registering is for measurement only and never replies to a "
            "comment.[/]"
        )
        return

    done = backfill_mod.apply(cfg, found.matched)
    console.print(
        f"[bold green]Registered[/] {len(done)} of {len(found.matched)} "
        "[dim](numbers arrive on the gateway's next insights sweep, within six hours)[/]"
    )


def _publish_run(cfg: Settings, run_dir: Path, *, cover_url: str | None = None) -> None:
    """Upload a finished run and start its cooldown.

    Shared by `--post` and `--publish` so there is exactly one definition of
    what posting means, including the part that is easy to forget: the cooldown
    starts here and nowhere else.
    """
    if not run_dir.is_dir():
        console.print(f"[bold red]No such build dir:[/] {run_dir}")
        raise typer.Exit(1)

    video_path = run_dir / "out.mp4"
    if not video_path.exists():
        console.print(f"[bold red]No video in {run_dir}[/] [dim](expected out.mp4)[/]")
        raise typer.Exit(1)

    # A second --publish on the same folder would post the same Reel twice, and
    # unattended is exactly where that mistake goes unnoticed.
    receipt_path = run_dir / "published.json"
    if receipt_path.exists():
        prior = json.loads(receipt_path.read_text())
        console.print(
            f"[yellow]Already published[/] on {prior.get('published_at', '?')} "
            f"({prior.get('permalink') or prior.get('media_id')}).\n"
            f"[dim]Delete {receipt_path} to publish it again.[/]"
        )
        return

    repo_path = run_dir / "repo.json"
    repo = RepoCandidate.model_validate_json(repo_path.read_text()) if repo_path.exists() else None

    caption_path = run_dir / "caption.txt"
    caption = caption_path.read_text().strip() if caption_path.exists() else ""
    if not caption:
        console.print("[yellow]No caption.txt in this run; posting without a caption.[/]")

    # Meta fetches both of these from its own servers while the container is
    # created, so they have to be public before the publish, not after.
    if cover_url is None:
        with console.status("Hosting the cover..."):
            cover_url = gateway.upload_cover(run_dir / "cover.png", run_dir.name, cfg)
        if cover_url:
            console.print(f"[dim]Cover hosted at {cover_url}[/]")

    with console.status("Hosting the video..."):
        video_url = gateway.upload_media(video_path, run_dir.name, cfg)
    if not video_url:
        # Unlike the cover, there is no fallback. Meta will not take the bytes.
        console.print(
            "[bold red]Cannot publish[/] the video has nowhere public to live.\n"
            "[dim]Meta fetches the MP4 rather than accepting an upload on this API path. "
            "Set GATEWAY_URL and GATEWAY_TOKEN, or host it yourself.[/]"
        )
        raise typer.Exit(1)
    console.print(f"[dim]Video hosted at {video_url}[/]")

    try:
        with console.status("Waiting for Instagram to fetch and process..."):
            result = publisher.publish_reel(
                video_path, caption, cfg, video_url=video_url, cover_url=cover_url
            )
    except publisher.PublishError as exc:
        console.print(f"[bold red]Publish failed[/]\n{exc}")
        raise typer.Exit(1) from exc

    receipt_path.write_text(
        json.dumps(
            {
                "media_id": result.media_id,
                "permalink": result.permalink,
                "published_at": datetime.now(UTC).isoformat(),
                "repo": repo.full_name if repo else None,
            },
            indent=2,
        )
        + "\n"
    )

    console.print(f"[bold green]Published[/] {result.permalink or result.media_id}")

    if repo:
        # Only now does a media id exist, which is why this cannot happen
        # earlier. A failure here costs the keyword mechanic on one post and is
        # recoverable by hand for the seven days Meta allows a reply to a
        # comment.
        keyword = gateway.keyword_for(repo.full_name, cfg)
        if gateway.register_post(result.media_id, repo.url, cfg, keyword=keyword):
            console.print(f"[dim]Gateway is watching for comments matching '{keyword}'.[/]")
        scraper.mark_featured(cfg, repo.full_name)
        console.print(
            f"[dim]{repo.full_name} is on cooldown for {cfg.repo_cooldown_days} days.[/]"
        )
    else:
        console.print(
            "[yellow]No repo.json in this run, so no cooldown was started.[/] "
            "[dim]Run --posted <owner/repo> by hand.[/]"
        )


def _enqueue_run(cfg: Settings, run_dir: Path, *, approved: bool) -> None:
    """Hand a finished run to the gateway and start its cooldown.

    The unattended counterpart to `_publish_run`. The gateway publishes it on
    the next due slot, so the laptop is only needed for the render.

    **The cooldown starts here, not at publish.** `_publish_run` could mark the
    repo because it was standing there when the media id appeared; nothing on
    this machine is standing there when a queued post goes out days later.
    Queueing a repo is committing it, so that is the moment, and `--unmark`
    undoes it if the post is cancelled. This is the one place where posting and
    the cooldown deliberately came apart.
    """
    if not run_dir.is_dir():
        console.print(f"[bold red]No such build dir:[/] {run_dir}")
        raise typer.Exit(1)

    video_path = run_dir / "out.mp4"
    if not video_path.exists():
        console.print(f"[bold red]No video in {run_dir}[/] [dim](expected out.mp4)[/]")
        raise typer.Exit(1)

    if not cfg.gateway_url or not cfg.gateway_token:
        console.print(
            "[bold red]No gateway configured.[/]\n"
            "[dim]Set GATEWAY_URL and GATEWAY_TOKEN, or use --post to publish from here.[/]"
        )
        raise typer.Exit(1)

    # Same guard as --publish, for the same reason: unattended is exactly where
    # posting the same Reel twice goes unnoticed.
    receipt_path = run_dir / "published.json"
    if receipt_path.exists():
        prior = json.loads(receipt_path.read_text())
        console.print(
            f"[yellow]Already published[/] on {prior.get('published_at', '?')}. "
            f"[dim]Delete {receipt_path} to queue it anyway.[/]"
        )
        return

    queue_receipt = run_dir / "queued.json"
    if queue_receipt.exists():
        prior = json.loads(queue_receipt.read_text())
        console.print(
            f"[yellow]Already queued[/] as #{prior.get('id')} on "
            f"{prior.get('queued_at', '?')}.\n"
            f"[dim]Cancel it in the admin UI, or delete {queue_receipt} to queue it again.[/]"
        )
        return

    repo_path = run_dir / "repo.json"
    repo = RepoCandidate.model_validate_json(repo_path.read_text()) if repo_path.exists() else None
    caption_path = run_dir / "caption.txt"
    caption = caption_path.read_text().strip() if caption_path.exists() else ""

    with console.status("Uploading the video..."):
        video_url = gateway.upload_media(video_path, run_dir.name, cfg)
    if not video_url:
        console.print("[bold red]Upload failed[/] [dim](is the gateway reachable?)[/]")
        raise typer.Exit(1)

    with console.status("Uploading the cover..."):
        cover_url = gateway.upload_media(run_dir / "cover.png", run_dir.name, cfg)

    # Read from the folder rather than recomputed, because this can run days
    # after the render and `--recover` runs later still.
    recipe = results_mod.read_recipe(run_dir)
    # The hook goes with it, so the feedback loop reads the opening that was on
    # this video rather than looking one up in a build folder that may belong to
    # a different render on a different machine.
    hook_path = run_dir / "script.json"
    hook = (
        VideoScript.model_validate_json(hook_path.read_text()).hook
        if hook_path.exists()
        else ""
    )

    keyword = gateway.keyword_for(repo.full_name, cfg) if repo else cfg.gateway_keyword
    result = gateway.enqueue(
        video_url.rsplit("/", 1)[-1],
        repo.url if repo else "",
        cfg,
        caption=caption,
        keyword=keyword,
        cover_name=cover_url.rsplit("/", 1)[-1] if cover_url else None,
        repo_full_name=repo.full_name if repo else None,
        approved=approved,
        recipe=recipe,
        hook=hook,
    )
    if result is None:
        console.print(
            "[bold red]The gateway would not take it.[/] "
            "[dim]Nothing was queued and no cooldown was started.[/]"
        )
        raise typer.Exit(1)

    youtube_result = _enqueue_youtube(
        cfg,
        run_dir,
        fallback_video_name=video_url.rsplit("/", 1)[-1],
        link=repo.url if repo else "",
        caption=caption,
        repo_full_name=repo.full_name if repo else None,
        approved=approved,
        recipe=recipe,
        hook=hook,
    )

    queue_receipt.write_text(
        json.dumps(
            {
                "id": result.get("id"),
                "state": result.get("state"),
                "youtube_id": youtube_result.get("id") if youtube_result else None,
                "queued_at": datetime.now(UTC).isoformat(),
                "repo": repo.full_name if repo else None,
            },
            indent=2,
        )
        + "\n"
    )

    console.print(f"[bold green]Queued[/] as #{result.get('id')} [dim]({result.get('detail')})[/]")
    if not approved:
        console.print("[dim]Approve it in the admin UI, or re-run with --approve.[/]")

    if repo:
        scraper.mark_featured(cfg, repo.full_name)
        console.print(
            f"[dim]{repo.full_name} is on cooldown for {cfg.repo_cooldown_days} days. "
            f"Cancelling the post means running --unmark {repo.full_name}.[/]"
        )
    else:
        console.print(
            "[yellow]No repo.json in this run, so no cooldown was started.[/] "
            "[dim]Run --posted <owner/repo> by hand.[/]"
        )


def _youtube_video_name(cfg: Settings, run_dir: Path, fallback: str) -> str:
    """Upload the version without the ask, or fall back to the full one.

    The Reel says "comment ANYDOC if you want the link" out loud, in the
    captions, and on a chip that runs from halfway to the last frame. YouTube
    has no private replies, so that is a promise nothing on the surface can
    keep. `renderer.render_without_cta` renders it again without any of them,
    reusing the voiceover so nothing goes back through TTS.

    Falling back rather than failing when there is no such version: a Short
    carrying an ask it cannot honour is a smaller problem than no Short.
    """
    spec_path = run_dir / "video.json"
    if not spec_path.exists():
        return fallback

    spec = VideoSpec.model_validate_json(spec_path.read_text())
    with console.status("Rendering the version without the ask..."):
        trimmed = renderer.render_without_cta(spec, run_dir, cfg)
    if trimmed is None:
        console.print(
            "[yellow]No version without the ask.[/] "
            "[dim]The Short gets the full video, ask included.[/]"
        )
        return fallback

    with console.status("Uploading the version without the ask..."):
        url = gateway.upload_media(trimmed, f"{run_dir.name}-no-cta", cfg)
    if not url:
        console.print("[yellow]Could not upload the trimmed video.[/] [dim]Using the full one.[/]")
        return fallback
    return url.rsplit("/", 1)[-1]


def _enqueue_youtube(
    cfg: Settings,
    run_dir: Path,
    *,
    fallback_video_name: str,
    link: str,
    caption: str,
    repo_full_name: str | None,
    approved: bool,
    recipe: str = "",
    hook: str = "",
) -> dict | None:
    """Queue the same render on the YouTube channel, if one is configured.

    Not quite the same file: the Short gets a copy that stops before the ask,
    since the keyword mechanic has no equivalent there. Everything else is
    identical, and `/api/media` names files by their own digest, so nothing is
    uploaded twice.

    **Best effort, and loudly so.** The Reel is the primary surface and its row
    is already committed by the time this runs, so a YouTube failure must not
    fail the command or strand the Instagram post. But a channel that quietly
    stops receiving videos looks exactly like a channel nobody is posting to,
    which is why this says what went wrong rather than returning None in
    silence.
    """
    if not cfg.youtube_channel_id:
        return None

    script_path = run_dir / "script.json"
    if not script_path.exists():
        console.print(
            "[yellow]No script.json, so no YouTube title.[/] "
            "[dim]The Reel is queued; the Short is not.[/]"
        )
        return None
    title = VideoScript.model_validate_json(script_path.read_text()).hook

    result = gateway.enqueue(
        _youtube_video_name(cfg, run_dir, fallback_video_name),
        link,
        cfg,
        caption=gateway.youtube_description(caption, link),
        cover_name=None,
        repo_full_name=repo_full_name,
        approved=approved,
        account=cfg.youtube_channel_id,
        title=title,
        recipe=recipe,
        hook=hook,
    )
    if result is None:
        console.print(
            "[yellow]The gateway would not take the YouTube row.[/] "
            "[dim]The Reel is queued and the cooldown has started, so re-running "
            "--enqueue will not retry it. Queue it by hand in the admin UI.[/]"
        )
        return None

    console.print(
        f"[bold green]Queued on YouTube[/] as #{result.get('id')} "
        f"[dim]({result.get('detail')})[/]"
    )
    return result


def _migrate_account(name: str, *, apply: bool) -> None:
    """Move the single account layout into `accounts/<name>/`.

    Prints the plan and does nothing, unless `--yes` is also given. The files
    involved are the only copy of a cloned voice and every run folder the
    feedback loop reads its hooks out of, so this is one of the few things here
    worth asking twice about.
    """
    moves = migrate.plan(name)
    console.print(f"[bold]Moving this checkout into accounts/{name}/[/]\n")
    for move in moves:
        why = move.blocked
        mark = "[dim]·[/]" if why else "[green]→[/]"
        tail = f"  [dim]{why}[/]" if why else ""
        console.print(
            f"  {mark} {move.source.relative_to(ROOT)}  →  "
            f"{move.target.relative_to(ROOT)}  [dim]{move.what}[/]{tail}"
        )

    doable = [m for m in moves if not m.blocked]
    if not doable:
        console.print("\n[yellow]Nothing to move.[/]")
        return
    if not apply:
        console.print(
            f"\n[dim]{len(doable)} of {len(moves)} would move. "
            f"Re-run with --yes to do it.[/]"
        )
        return

    done = migrate.apply(doable)
    console.print(f"\n[bold green]Moved {len(done)} item(s).[/]")
    console.print(
        "[dim]The root .env was copied rather than moved, because it also holds "
        "the global half. Delete the per account lines from it by hand, then set "
        f"REELSMITH_ACCOUNT={name} there or pass --account {name}.[/]"
    )


def _finish(run_dir: Path | None) -> None:
    if run_dir:
        console.print(f"\n[dim]Artifacts in {run_dir}[/]")


if __name__ == "__main__":
    app()
