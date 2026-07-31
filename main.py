"""reelsmith -- automated Instagram Reels for trending AI/dev tooling.

    python main.py                      full run
    python main.py --stop-after script  stop early to inspect an artifact
    python main.py --repo astral-sh/uv  skip discovery, use a specific repo
    python main.py --resume 2026-07-30/astral-sh-uv   re-render from artifacts

    python main.py --post               render, then publish it unattended
    python main.py --publish 2026-07-30/astral-sh-uv  publish a run you approved

    python main.py --candidates         rank today's repos, generate nothing
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
from datetime import UTC, datetime
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

    if preview_voice:
        sample = (
            "Eden is a homelab run entirely from Git. Talos Linux on Proxmox, "
            "ArgoCD on top. The useful part is the deploy pattern."
        )
        out = cfg.build_dir / f"voice-preview-{tts.voice_name(cfg)}{tts.audio_suffix(cfg)}"
        tts.synthesize(sample, out, cfg)
        console.print(f"[bold green]Preview:[/] {out}")
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
    if existing is None:
        console.rule("[bold]3/5  Synthesizing the voiceover")
        tts.synthesize(script.spoken_script, audio_path, cfg)
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
            caps = captions_mod.transcribe(audio_path, cfg, script.spoken_script)
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

    # Meta fetches cover_url when the container is created, so this has to
    # happen before the publish, not after. An explicit --cover-url always wins.
    if cover_url is None:
        with console.status("Hosting the cover..."):
            cover_url = gateway.upload_cover(run_dir / "cover.png", run_dir.name, cfg)
        if cover_url:
            console.print(f"[dim]Cover hosted at {cover_url}[/]")

    try:
        with console.status("Uploading and waiting for Instagram to process..."):
            result = publisher.publish_reel(video_path, caption, cfg, cover_url=cover_url)
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


def _finish(run_dir: Path | None) -> None:
    if run_dir:
        console.print(f"\n[dim]Artifacts in {run_dir}[/]")


if __name__ == "__main__":
    app()
