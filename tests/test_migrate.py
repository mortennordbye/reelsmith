"""Making an empty `accounts/<name>/` profile, which is `--new-account`.

It cannot lose anything, so the claims worth pinning are that it makes what an
account needs, fills nothing in, and never overwrites what is already there.
"""

from __future__ import annotations

from pipeline import migrate


def test_a_new_account_gets_its_directories_and_a_template(tmp_path):
    home, made = migrate.create("chapterverse", root=tmp_path)

    assert (home / "data").is_dir()
    assert (home / "ref").is_dir()
    assert (home / "brand").is_dir()
    assert (home / ".env").is_file()
    assert len(made) == 5


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
    """The whole point: `--account` finds it, which is what turns a few
    directories into a profile."""
    import config

    monkeypatch.setattr(config, "ACCOUNTS_DIR", tmp_path / "accounts")
    migrate.create("chapterverse", root=tmp_path)

    assert config.available_accounts() == ["chapterverse"]
    assert config.resolve_account("chapterverse") == "chapterverse"
