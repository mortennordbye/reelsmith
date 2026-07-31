"""tech-ig -- automated Instagram Reels for trending AI/dev tooling.

    python main.py                      full run
    python main.py --stop-after script  stop early to inspect an artifact
    python main.py --repo astral-sh/uv  skip discovery, use a specific repo
    python main.py --resume 2026-07-30/astral-sh-uv   re-render from artifacts

    python main.py --candidates         rank today's repos, generate nothing
    python main.py --snapshot           record star counts only (run daily)
    python main.py --posted astral-sh/uv    start the 30-day cooldown
    python main.py --unmark astral-sh/uv    undo that

Every stage writes its output to build/<date>/<owner-repo>/ before the next
one starts, so a failure late in the pipeline never costs you the earlier work.
"""

from __future__ import annotations

import json
import logging
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.logging import RichHandler

from config import ConfigError, get_settings, require_github_token, resolve_claude_cli
from pipeline import captions as captions_mod
from pipeline import publisher, renderer, scraper, screenshot, tts
from pipeline import spec as spec_mod
from pipeline.models import Caption, RepoCandidate, VideoScript, VideoSpec
from pipeline.scriptwriter import write_script

app = typer.Typer(add_completion=False, help=__doc__)
console = Console()
log = logging.getLogger("tech-ig")


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


def _preflight(*, need_github: bool, need_claude: bool) -> None:
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

    # Everything from here is about making the one remaining manual step --
    # dropping the file into Instagram -- as short as possible.
    if script.caption_text:
        # On the clipboard for an immediate post, and on disk because the
        # clipboard is gone the moment anything else copies, and a run you come
        # back to tomorrow still needs its caption.
        caption_path = run_dir / "caption.txt"
        caption_path.write_text(script.caption_text.strip() + "\n")
        console.print(f"\n[bold]Instagram caption[/]\n{script.caption_text}")
        if publisher.copy_to_clipboard(script.caption_text):
            console.print("[dim]  (copied to the clipboard)[/]")
        console.print(f"[dim]  (also written to {caption_path.name})[/]")
    publisher.reveal(run_dir / "out.mp4")

    console.print(
        f"\n[bold]Once it is actually posted:[/]  "
        f"python main.py --posted {repo.full_name}"
    )
    console.print(
        f"[dim]That is what starts the {cfg.repo_cooldown_days}-day cooldown. "
        f"Rendering alone does not, so a video you reject costs you nothing.[/]"
    )
    console.print(
        f"[dim]Iterate on the look:  cd video && npm run studio  "
        f"(then load {run_dir / 'video.json'})[/]"
    )


def _finish(run_dir: Path | None) -> None:
    if run_dir:
        console.print(f"\n[dim]Artifacts in {run_dir}[/]")


if __name__ == "__main__":
    app()
