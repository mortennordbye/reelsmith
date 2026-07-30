"""Step 6 -- hand the finished video off to a human.

Instagram has no unattended posting path worth taking here. The Graph API can
publish Reels, but only for a business or creator account, and only from an MP4
already sitting at a public URL -- which means standing up object storage and a
Meta app just to avoid a drag-and-drop. Not worth it for one video a day.

So this closes the gap the cheap way: the moment the render finishes, the
caption is on the clipboard and the folder is open. The remaining manual step is
dropping the file into Instagram and pressing paste.

Everything here is best-effort. A failed clipboard write must never fail a run
that already produced a video.
"""

from __future__ import annotations

import logging
import platform
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


def copy_to_clipboard(text: str) -> bool:
    """Put `text` on the system clipboard. Returns whether it worked."""
    if not text:
        return False

    system = platform.system()
    if system == "Darwin":
        cmd = ["pbcopy"]
    elif system == "Linux":
        # Wayland first: on a Wayland session xclip talks to an X server that
        # may not exist, and fails in a way that is confusing to debug.
        cmd = _first_available(["wl-copy"], ["xclip", "-selection", "clipboard"])
        if cmd is None:
            log.debug("No clipboard tool found (tried wl-copy, xclip).")
            return False
    else:
        log.debug("No clipboard support for platform %s.", system)
        return False

    try:
        subprocess.run(  # noqa: S603 - argv list, no shell
            cmd, input=text, text=True, check=True, timeout=10, capture_output=True
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("Clipboard copy failed (%s)", exc)
        return False
    return True


def reveal(path: Path) -> bool:
    """Open `path` in the desktop file manager. Returns whether it worked."""
    system = platform.system()
    opener = {"Darwin": "open", "Linux": "xdg-open", "Windows": "explorer"}.get(system)
    if not opener:
        return False

    try:
        subprocess.run(  # noqa: S603 - argv list, no shell
            [opener, str(path)], check=True, timeout=10, capture_output=True
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("Could not open %s (%s)", path, exc)
        return False
    return True


def _first_available(*candidates: list[str]) -> list[str] | None:
    from shutil import which

    return next((c for c in candidates if which(c[0])), None)
