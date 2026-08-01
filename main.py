"""reelsmith -- automated Instagram Reels for trending AI/dev tooling.

    python main.py                      full run
    python main.py --stop-after script  stop early to inspect an artifact
    python main.py --repo astral-sh/uv  skip discovery, use a specific repo
    python main.py --resume 2026-07-30/astral-sh-uv   re-render from artifacts

    python main.py --post               render, then publish it unattended
    python main.py --publish 2026-07-30/astral-sh-uv  publish a run you approved

    python main.py --batch 3            render the top 3 repos in one sitting

    python main.py --candidates         rank today's repos, generate nothing
    python main.py --covered            list every repo already made into a Reel
    python main.py --snapshot           record star counts only (run daily)
    python main.py --refresh-token      renew the Instagram token by hand
                                        (--snapshot already does it when due)
    python main.py --posted astral-sh/uv    start the 30-day cooldown
    python main.py --unmark astral-sh/uv    undo that

Every stage writes its output to build/<date>/<owner-repo>/ before the next
one starts, so a failure late in the pipeline never costs you the earlier work.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.logging import RichHandler

from config import (
    ConfigError,
    Settings,
    get_settings,
    require_github_token,
    require_instagram,
    resolve_claude_cli,
)
from pipeline import captions as captions_mod
from pipeline import gateway, publisher, renderer, scraper, screenshot, tts
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
    batch: Annotated[
        int | None,
        typer.Option("--batch", help="Render this many distinct repos in one sitting"),
    ] = None,
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
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    _setup_logging(verbose)
    cfg = get_settings()
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
        if was:
            console.print(f"[bold green]Cooldown cleared:[/] {unmark} [dim](was set {was})[/]")
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

    if batch is not None:
        if batch < 1:
            console.print("[bold red]--batch needs a positive number.[/]")
            raise typer.Exit(1)
        _preflight(need_github=True, need_claude=True, need_instagram=post)
        _run_batch(cfg, batch, stop_after=stop_after, post=post, cover_url=cover_url)
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
    cta_keyword = gateway.keyword_for(repo.full_name, cfg)
    # Strip any ask the model wrote itself before appending ours, or the voice
    # reads two of them back to back asking for two different words.
    spoken = gateway.strip_written_cta(script.spoken_script)
    cta_line = gateway.spoken_cta(cta_keyword, cfg)
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
        repo, script, caps, duration, audio_src, cfg, screenshot_src=screenshot_src
    )
    # The end card. Same word the voice just read and the caption carries, so
    # the three cannot disagree about what to comment.
    video_spec = video_spec.model_copy(update={"ctaKeyword": cta_keyword if cta_line else None})
    renderer.write_spec(video_spec, run_dir / "video.json")

    out_path = run_dir / "out.mp4"
    with console.status("Remotion is rendering..."):
        renderer.render(video_spec, out_path, cfg)

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
        # The same derivation runs again at publish time, so the word burned
        # into the caption and the word the poller watches for cannot drift.
        caption_out = gateway.add_caption_cta(
            script.caption_text.strip(), cfg, keyword=gateway.keyword_for(repo.full_name, cfg)
        )
        (run_dir / "caption.txt").write_text(caption_out.rstrip() + "\n")

    return script


def _run_batch(
    cfg: Settings,
    count: int,
    *,
    stop_after: Stage | None,
    post: bool,
    cover_url: str | None,
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
    )
    if result is None:
        console.print(
            "[bold red]The gateway would not take it.[/] "
            "[dim]Nothing was queued and no cooldown was started.[/]"
        )
        raise typer.Exit(1)

    queue_receipt.write_text(
        json.dumps(
            {
                "id": result.get("id"),
                "state": result.get("state"),
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


def _finish(run_dir: Path | None) -> None:
    if run_dir:
        console.print(f"\n[dim]Artifacts in {run_dir}[/]")


if __name__ == "__main__":
    app()
