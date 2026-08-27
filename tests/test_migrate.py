"""Moving one account's half of a checkout into `accounts/<name>/`.

The files involved are the only copy of a cloned voice, a cooldown store that
exists nowhere else, and about a month of run folders the feedback loop reads
its hooks out of. So the planner is pure and the executor takes a plan, and
`main.py` prints one and moves nothing unless told twice.
"""

from __future__ import annotations

import pytest

from pipeline import migrate


@pytest.fixture
def checkout(tmp_path):
    """The single account layout, as it stood before `accounts/` existed."""
    (tmp_path / ".env").write_text("GITHUB_TOKEN=x\nIG_USER_ID=1784\n")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "used_repos.json").write_text("{}")
    ref = tmp_path / "tools" / "chatterbox" / "ref"
    ref.mkdir(parents=True)
    (ref / "morten.wav").write_bytes(b"riff")
    for day, runs in (("2026-08-01", ["a-one"]), ("2026-08-02", ["b-two", "b-two.prev"])):
        for run in runs:
            (tmp_path / "build" / day / run).mkdir(parents=True)
    return tmp_path


def test_the_plan_names_the_four_places_an_identity_lived(checkout):
    moves = migrate.plan("nightlybuild", root=checkout)
    sources = [m.source.name for m in moves]

    assert ".env" in sources
    assert "data" in sources
    assert "morten.wav" in sources
    assert "2026-08-01" in sources and "2026-08-02" in sources


def test_a_run_folder_a_human_set_aside_moves_too(checkout):
    """The dot marks a run somebody rejected, which `_runs_by_repo` ranks below
    its unsuffixed sibling and `--recover` skips. That meaning is per folder and
    survives the move, so leaving them behind would silently drop the losing
    half of every regenerated script."""
    migrate.apply(migrate.plan("nightlybuild", root=checkout))

    moved = checkout / "build" / "nightlybuild" / "2026-08-02"
    assert sorted(p.name for p in moved.iterdir()) == ["b-two", "b-two.prev"]


def test_an_already_migrated_subtree_is_not_read_as_a_date(checkout):
    """Otherwise a second run of this nests `build/x/x/` and the run folders
    stop being where anything looks for them."""
    (checkout / "build" / "nightlybuild" / "2026-08-01").mkdir(parents=True)

    moves = migrate.plan("nightlybuild", root=checkout)

    assert "nightlybuild" not in [m.source.name for m in moves]


def test_a_move_whose_target_exists_is_blocked_rather_than_overwriting(checkout):
    (checkout / "accounts" / "nightlybuild").mkdir(parents=True)
    (checkout / "accounts" / "nightlybuild" / ".env").write_text("do not lose me")

    moves = migrate.plan("nightlybuild", root=checkout)
    blocked = [m for m in moves if m.blocked]

    assert [m.source.name for m in blocked] == [".env"]
    assert (checkout / "accounts" / "nightlybuild" / ".env").read_text() == "do not lose me"


def test_the_root_env_is_copied_rather_than_moved(checkout):
    """It holds the global half too -- the GitHub token, the gateway URL -- and
    a host may be reading it right now. Which lines are global is a judgement
    the operator makes afterwards, not one to guess at here."""
    migrate.apply(migrate.plan("nightlybuild", root=checkout))

    assert (checkout / ".env").exists()
    assert (checkout / "accounts" / "nightlybuild" / ".env").read_text() == (
        "GITHUB_TOKEN=x\nIG_USER_ID=1784\n"
    )


def test_data_moves_as_a_directory_symlink_rather_than_its_contents(tmp_path):
    """`StarHistory.save()` renames a temp file over its target, and that rename
    replaces a *file* symlink with a real file, so per file links silently send
    writes to local disk instead of the share. Moving the link itself keeps
    whatever it already was."""
    share = tmp_path / "share" / "data"
    share.mkdir(parents=True)
    (share / "used_repos.json").write_text("{}")
    checkout = tmp_path / "repo"
    checkout.mkdir()
    (checkout / ".env").write_text("")
    (checkout / "data").symlink_to(share, target_is_directory=True)

    migrate.apply(migrate.plan("nightlybuild", root=checkout))

    moved = checkout / "accounts" / "nightlybuild" / "data"
    assert moved.is_symlink()
    assert moved.resolve() == share.resolve()


def test_nothing_there_is_reported_rather_than_failing(tmp_path):
    """A checkout with no voice recording is normal: it is gitignored, so a
    fresh clone has never had one."""
    (tmp_path / ".env").write_text("")

    moves = migrate.plan("nightlybuild", root=tmp_path)
    done = migrate.apply(moves)

    assert [m.source.name for m in moves if m.blocked == "nothing there"] == [
        "data", "morten.wav",
    ]
    assert [m.source.name for m in done] == [".env"]


def test_a_tracked_placeholder_is_left_where_git_expects_it(checkout):
    """`data/.gitkeep` is tracked, and moving the directory carries it off,
    which leaves a `D` in `git status` that stays until somebody works out
    whether it matters.

    It does not, and on a host where `pod-setup.sh` has set `skip-worktree` the
    deletion is invisible, which is how it went unnoticed the first time. A
    migration that leaves a dirty tree behind invites exactly that question at
    exactly the wrong moment.
    """
    (checkout / "data" / ".gitkeep").touch()

    migrate.apply(migrate.plan("nightlybuild", root=checkout))

    assert (checkout / "data" / ".gitkeep").is_file()
    assert (checkout / "accounts" / "nightlybuild" / "data" / "used_repos.json").is_file()


def test_nothing_is_put_back_where_there_was_no_placeholder(checkout):
    """Only files git tracks come back. Recreating the directory otherwise
    would leave `data/` looking like a store that had been emptied."""
    (checkout / "data" / ".gitkeep").unlink(missing_ok=True)

    migrate.apply(migrate.plan("nightlybuild", root=checkout))

    assert not (checkout / "data").exists()


def test_running_it_twice_moves_nothing_the_second_time(checkout):
    migrate.apply(migrate.plan("nightlybuild", root=checkout))

    again = migrate.plan("nightlybuild", root=checkout)

    assert [m for m in again if not m.blocked] == []


# --- Making one from nothing -------------------------------------------------


def test_a_new_account_gets_the_three_directories_and_a_template(tmp_path):
    home, made = migrate.create("chapterverse", root=tmp_path)

    assert (home / "data").is_dir()
    assert (home / "ref").is_dir()
    assert (home / ".env").is_file()
    assert len(made) == 4


def test_every_line_of_the_template_is_commented_out(tmp_path):
    """A profile with a blank IG_USER_ID looks configured and fails at the
    first publish. One with nothing set fails at `require_instagram`, which
    says what is missing and where to set it."""
    home, _ = migrate.create("chapterverse", root=tmp_path)

    lines = [
        line for line in (home / ".env").read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]

    assert lines == []


def test_the_template_carries_only_the_per_account_half(tmp_path):
    """Repeating a global value in a fragment layered over the root .env is how
    the two drift apart."""
    home, _ = migrate.create("chapterverse", root=tmp_path)
    text = (home / ".env").read_text()

    assert "IG_USER_ID" in text
    assert "CHATTERBOX_REF" in text
    # Global, and named in F4's table as such.
    assert "GITHUB_TOKEN" not in text
    assert "GATEWAY_URL" not in text
    assert "MAX_HOOK_CHARS" not in text


def test_creating_it_twice_changes_nothing(tmp_path):
    """It cannot lose anything, which is why it needs no --yes, and an .env
    somebody has already filled in is exactly what it must not overwrite."""
    home, _ = migrate.create("chapterverse", root=tmp_path)
    (home / ".env").write_text("IG_USER_ID=17841400000000009\n")

    _, made = migrate.create("chapterverse", root=tmp_path)

    assert made == []
    assert (home / ".env").read_text() == "IG_USER_ID=17841400000000009\n"


def test_a_created_account_is_one_config_resolve_can_see(tmp_path, monkeypatch):
    """The whole point: `--account` finds it, which is what turns three
    directories into a profile."""
    import config

    monkeypatch.setattr(config, "ACCOUNTS_DIR", tmp_path / "accounts")
    migrate.create("chapterverse", root=tmp_path)

    assert config.available_accounts() == ["chapterverse"]
    assert config.resolve_account("chapterverse") == "chapterverse"
