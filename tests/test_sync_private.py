"""scripts/sync-private.sh, and the one thing about it worth a test file.

The script moves the private half between this laptop and the share the render
host reads, and most of it is a file copy that is obvious when it breaks. One
part is not: `accounts/*/.env` is **projected** rather than copied, because
this laptop's copy holds `IG_ACCESS_TOKEN` and three `YOUTUBE_*` credentials it
needs in order to publish directly, and the render host neither publishes nor
has anywhere to put them.

That is a security boundary implemented in shell, so it is exactly the kind of
thing that stops working quietly. A leak here does not fail a run, does not
appear in a log and does not show up in the diff the script prints: it looks
like a successful sync. So the tests below assert on the absence of secrets
rather than on the presence of ids, and they run through `--via dir`, which is
the transport that needs neither a cluster nor a NAS password.

The other property pinned here is that `--pull` refuses that file. The far copy
is a projection and holds strictly less, so pulling it over the authored one
destroys credentials, and it destroys them on the machine that is the only
place they exist.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "sync-private.sh"

# What a real account .env looks like: the ids the render host needs, mixed in
# with the credentials it must never receive.
SECRETS = {
    "IG_ACCESS_TOKEN": "IGQVJsecret-instagram",
    "YOUTUBE_CLIENT_ID": "1234-secret.apps.googleusercontent.com",
    "YOUTUBE_CLIENT_SECRET": "GOCSPX-secret-google",
    "YOUTUBE_REFRESH_TOKEN": "1//0gsecret-refresh",
}
IDS = {
    "IG_USER_ID": "17841441696714445",
    "YOUTUBE_CHANNEL_ID": "UCH8RDOkbzDna2mDAlq4GaFw",
    # The leading hyphen is real and has bitten a config parser already, so it
    # is here rather than in a tidier fixture value.
    "TIKTOK_OPEN_ID": "-000y6NKYZ3EEbUGgXiyJ9nG66a_xqrF68Me",
    "FACEBOOK_PAGE_ID": "104739283746152",
}


@pytest.fixture
def world(tmp_path: Path) -> tuple[Path, Path]:
    """A throwaway repo and a throwaway share, wired the way the real ones are.

    The script resolves its own root with `git rev-parse`, so the fake repo has
    to be a real one. `--via dir` then points it at a directory instead of a
    mount, which is what makes this runnable anywhere.
    """
    repo = tmp_path / "repo"
    (repo / "accounts" / "acct" / "ref").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)

    (repo / "PROFILE.md").write_text("# The identity\n\nNot in git.\n")
    lines = [f"{k}={v}" for k, v in {**SECRETS, **IDS}.items()]
    (repo / "accounts" / "acct" / ".env").write_text(
        "# the per account half\n\n" + "\n".join(lines) + "\n"
    )
    # Binary, because the voice reference is and a base64 or text mangling bug
    # would not show up on the markdown.
    (repo / "accounts" / "acct" / "ref" / "voice.wav").write_bytes(bytes(range(256)) * 8)

    share = tmp_path / "nas" / "media" / "reelsmith"
    share.mkdir(parents=True)
    return repo, share


def run(repo: Path, share: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(SCRIPT), *args, "--via", "dir"],
        cwd=repo,
        capture_output=True,
        text=True,
        env={**os.environ, "NAS_DIR": str(share.parent.parent)},
    )


def far_env(share: Path) -> str:
    return (share / "accounts" / "acct" / ".env").read_text()


def test_no_credential_reaches_the_share(world):
    """The property the whole projection exists for.

    Asserted on the raw text rather than on parsed keys, so a value that leaks
    through a comment, a stray line or a future formatting change still fails.
    """
    repo, share = world
    result = run(repo, share, "--push", "--yes")

    assert result.returncode == 0, result.stderr
    body = far_env(share)
    for name, value in SECRETS.items():
        assert value not in body, f"{name} reached the share"
        assert name not in body, f"{name} named on the share"


def test_the_ids_do_reach_it(world):
    """The other half. A projection that dropped everything would pass the test
    above and be useless."""
    repo, share = world
    run(repo, share, "--push", "--yes")

    body = far_env(share)
    for name, value in IDS.items():
        assert f"{name}={value}" in body, f"{name} did not reach the share"


def test_everything_else_is_copied_byte_for_byte(world):
    """Only the account .env is special. A projection applied to the voice
    recording would be a corrupted file that still verified, since the script
    checksums whatever it decided to send."""
    repo, share = world
    run(repo, share, "--push", "--yes")

    for rel in ("PROFILE.md", "accounts/acct/ref/voice.wav"):
        assert (share / rel).read_bytes() == (repo / rel).read_bytes(), rel


def test_the_dropped_keys_are_named(world):
    """A key that stays behind looks exactly like a key that synced. The one
    that matters is a newly added id nobody allowlisted, which would otherwise
    show up as a render host quietly skipping a destination."""
    repo, share = world
    out = run(repo, share, "--push").stdout

    assert "staying here" in out
    for name in SECRETS:
        assert name in out, f"{name} was dropped without saying so"


def test_pull_refuses_the_account_env(world):
    """The footgun this replaced. The share's copy holds strictly less, so
    pulling it over the authored one destroys the credentials on the only
    machine that has them."""
    repo, share = world
    run(repo, share, "--push", "--yes")
    before = (repo / "accounts" / "acct" / ".env").read_text()

    result = run(repo, share, "--pull", "--yes")

    assert result.returncode == 0, result.stderr
    assert "refusing to pull" in result.stderr
    assert (repo / "accounts" / "acct" / ".env").read_text() == before
    for value in SECRETS.values():
        assert value in (repo / "accounts" / "acct" / ".env").read_text()


def test_pull_still_moves_the_other_files(world):
    """The refusal is one file, not the mode. A `--pull` that quietly stopped
    syncing PROFILE.md would be a worse bug than the one it fixed."""
    repo, share = world
    (share / "PROFILE.md").write_text("# Edited on the far side\n")

    run(repo, share, "--pull", "--yes")

    assert (repo / "PROFILE.md").read_text() == "# Edited on the far side\n"


def test_a_second_run_changes_nothing(world):
    """The projection is deterministic, which is what lets the script compare
    checksums across the boundary at all. If it were not, every run would
    report the .env as changed forever and nobody would read the output."""
    repo, share = world
    run(repo, share, "--push", "--yes")

    out = run(repo, share, "--push").stdout

    assert "Nothing differs" in out


def test_it_says_when_an_overwrite_would_remove_a_key(world):
    """A key on the share that the projection does not carry vanishes without
    trace, since the far file is replaced rather than merged. Saying so is what
    makes the allowlist's cost visible instead of silent."""
    repo, share = world
    run(repo, share, "--push", "--yes")
    far = share / "accounts" / "acct" / ".env"
    far.write_text(far.read_text() + "SOMETHING_ELSE=set-over-there\n")

    out = run(repo, share, "--push").stdout

    assert "would remove" in out
    assert "SOMETHING_ELSE" in out


def test_data_is_still_refused(world):
    """Unchanged by this, and the reason is in the header: the render host's
    cooldown store is the live one and pushing over it hands the nightly a list
    missing a month of repos."""
    repo, share = world
    data = repo / "accounts" / "acct" / "data"
    data.mkdir()
    (data / "used_repos.json").write_text("{}")

    run(repo, share, "--push", "--yes")

    assert not (share / "accounts" / "acct" / "data" / "used_repos.json").exists()


def test_smb_says_it_needs_a_terminal(world):
    """`mount_smbfs` asks for the password on the controlling terminal itself,
    so `--via smb` cannot work without one. It used to die on `/dev/tty: Device
    not configured`, which names a device rather than the problem and sends you
    looking at the mount.

    A pytest subprocess has no tty, which is what makes this testable at all.
    NAS_SHARE is set to something that cannot be mounted so the reuse-an-
    existing-mount path is never taken on a laptop that happens to have the real
    share mounted.
    """
    repo, share = world
    result = subprocess.run(
        [str(SCRIPT), "--push", "--via", "smb"],
        cwd=repo,
        capture_output=True,
        text=True,
        env={**os.environ, "NAS_SHARE": "no-such-share-for-tests"},
    )

    assert result.returncode != 0
    assert "needs a terminal" in result.stderr
    assert "--via pod" in result.stderr
    assert "Device not configured" not in result.stderr
