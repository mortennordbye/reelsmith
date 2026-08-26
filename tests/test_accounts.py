"""Which account a run belongs to, and what an account owns.

One checkout serves several accounts and nothing about which one is in play is
recoverable after the fact: a video published to the wrong audience cannot be
taken back, and a cooldown store shared between two accounts silently hands one
of them the other's 30 day exclusions on every repo it ever covered.

So the two claims worth pinning are that selection is explicit, and that
selecting an account moves everything an account owns.
"""

from __future__ import annotations

import pytest

import config
from config import ConfigError, Settings


@pytest.fixture
def accounts(tmp_path, monkeypatch):
    """An `accounts/` directory this test owns, with nothing in it yet."""
    root = tmp_path / "accounts"
    root.mkdir()
    monkeypatch.setattr(config, "ACCOUNTS_DIR", root)
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(config, "LEGACY_DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "LEGACY_VOICE_REF", tmp_path / "ref" / "morten.wav")
    monkeypatch.setattr(config, "_selected_account", "")
    config.get_settings.cache_clear()
    yield root
    config.get_settings.cache_clear()


def make(accounts, name: str, env: str = "") -> None:
    home = accounts / name
    home.mkdir(parents=True, exist_ok=True)
    (home / ".env").write_text(env)


# --- Selection is explicit, and there is no third rule ----------------------


def test_no_account_and_none_configured_fails_saying_so(accounts, monkeypatch):
    monkeypatch.delenv("REELSMITH_ACCOUNT", raising=False)
    with pytest.raises(ConfigError) as exc:
        config.resolve_account(None)

    assert "No account selected" in str(exc.value)


def test_one_account_is_still_not_resolved_by_counting(accounts, monkeypatch):
    """The rule this whole design turns on.

    The gateway resolves an unnamed slot line to the single registered
    Instagram account, and a second account registering was enough to delete a
    working schedule (F0). The pipeline's version of the same shortcut posts to
    the wrong audience, so it does not have one, and a single account is not a
    special case.
    """
    monkeypatch.delenv("REELSMITH_ACCOUNT", raising=False)
    make(accounts, "nightlybuild")

    with pytest.raises(ConfigError) as exc:
        config.resolve_account(None)

    assert "nightlybuild" in str(exc.value)


def test_the_error_names_what_it_could_see(accounts, monkeypatch):
    monkeypatch.delenv("REELSMITH_ACCOUNT", raising=False)
    make(accounts, "chapterverse")
    make(accounts, "nightlybuild")

    with pytest.raises(ConfigError) as exc:
        config.resolve_account(None)

    assert "chapterverse, nightlybuild" in str(exc.value)


def test_an_unknown_name_fails_rather_than_creating_a_profile(accounts, monkeypatch):
    """A typo that silently made an empty directory would be a run with no
    credentials, which fails much later and much less clearly."""
    monkeypatch.delenv("REELSMITH_ACCOUNT", raising=False)
    make(accounts, "nightlybuild")

    with pytest.raises(ConfigError):
        config.resolve_account("nightlybiuld")

    assert not (accounts / "nightlybiuld").exists()


def test_the_environment_names_an_account_when_the_flag_does_not(accounts, monkeypatch):
    """The one line fix for the nightly, which cannot pass a flag: the render
    host's own .env carries REELSMITH_ACCOUNT."""
    make(accounts, "nightlybuild")
    monkeypatch.setenv("REELSMITH_ACCOUNT", "nightlybuild")

    assert config.resolve_account(None) == "nightlybuild"


def test_the_flag_beats_the_environment(accounts, monkeypatch):
    make(accounts, "nightlybuild")
    make(accounts, "chapterverse")
    monkeypatch.setenv("REELSMITH_ACCOUNT", "nightlybuild")

    assert config.resolve_account("chapterverse") == "chapterverse"


def test_a_directory_without_an_env_is_not_an_account(accounts):
    """`accounts/x/data/` can outlive a profile being deleted, and offering the
    name back would be offering a run with no credentials."""
    (accounts / "leftovers" / "data").mkdir(parents=True)

    assert config.available_accounts() == []


# --- What selecting an account actually moves -------------------------------


def test_the_account_env_overrides_the_root_one(accounts, monkeypatch):
    """A profile is an .env fragment plus a data directory, which is the whole
    of F4's answer and the reason no stage signature changes."""
    (config.ROOT / ".env").write_text(
        "GITHUB_TOKEN=shared\nIG_USER_ID=17841400000000000\n"
    )
    make(accounts, "chapterverse", "IG_USER_ID=17841400000000009\n")
    monkeypatch.delenv("IG_USER_ID", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    cfg = config.select_account("chapterverse")

    assert cfg.ig_user_id == "17841400000000009"
    assert cfg.github_token == "shared"


def test_two_accounts_do_not_share_a_cooldown_store(accounts):
    """F9. One `used_repos.json` between two accounts hands the second one the
    first's 30 day exclusion on every repo it has ever covered."""
    one = Settings(account="nightlybuild", _env_file=None)
    two = Settings(account="chapterverse", _env_file=None)

    assert one.used_repos_path != two.used_repos_path
    assert one.used_repos_path.parent == accounts / "nightlybuild" / "data"


def test_two_accounts_do_not_share_a_build_subtree(accounts):
    one = Settings(account="nightlybuild", _env_file=None)
    two = Settings(account="chapterverse", _env_file=None)

    assert one.build_dir != two.build_dir
    assert one.build_dir.name == "nightlybuild"


def test_a_run_folder_argument_keeps_its_shape(accounts):
    """`--resume 2026-08-01/astral-sh-uv` gains the account from --account
    rather than from the path, which is what keeps every one of those arguments
    the same length as it was."""
    cfg = Settings(account="nightlybuild", _env_file=None)

    assert cfg.build_dir / "2026-08-01/astral-sh-uv" == (
        cfg.build_dir / "2026-08-01" / "astral-sh-uv"
    )
    assert cfg.run_dir("astral-sh-uv").parent.parent.name == "nightlybuild"


def test_the_token_follows_the_account(accounts):
    """`data/ig_token.json` holds the live long lived token. Two accounts
    reading one file is two accounts publishing as whichever refreshed last."""
    one = Settings(account="nightlybuild", _env_file=None)
    two = Settings(account="chapterverse", _env_file=None)

    assert one.ig_token_path != two.ig_token_path


def test_the_voice_follows_the_account(accounts):
    """PROFILE.md is explicit that one cloned voice across two accounts meant to
    look unrelated is the strongest link between them."""
    ref = accounts / "chapterverse" / "ref"
    ref.mkdir(parents=True)
    (ref / "voice.wav").write_bytes(b"")

    cfg = Settings(account="chapterverse", _env_file=None)

    assert cfg.chatterbox_ref == ref / "voice.wav"


def test_an_explicitly_set_voice_is_never_second_guessed(accounts, tmp_path):
    elsewhere = tmp_path / "somewhere.wav"

    cfg = Settings(account="chapterverse", chatterbox_ref=elsewhere, _env_file=None)

    assert cfg.chatterbox_ref == elsewhere


def test_a_checkout_mid_migration_reads_the_store_it_already_has(accounts):
    """The account directory exists but nothing has been moved into it yet.

    Falling back rather than starting an empty store, because an empty
    `used_repos.json` means every repo ever covered becomes eligible again on
    the same night.
    """
    legacy = config.LEGACY_DATA_DIR
    legacy.mkdir()
    (legacy / "used_repos.json").write_text("{}")
    make(accounts, "nightlybuild")

    cfg = Settings(account="nightlybuild", _env_file=None)

    assert cfg.data_dir == legacy


def test_no_account_selected_still_reads_the_global_half(accounts):
    """`pipeline/models.py` reads the validator limits at import time, long
    before a flag has been parsed, so this has to keep working."""
    cfg = Settings(_env_file=None)

    assert cfg.max_hook_chars > 0
    assert cfg.account == ""
    with pytest.raises(ConfigError):
        _ = cfg.account_dir
