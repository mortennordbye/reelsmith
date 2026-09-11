"""The shared half of a consent trip, which is the half that can be tested.

The flows themselves are a browser, a paste and three live APIs, and none of
that belongs in a test. What is testable is everything the four trips have in
common, and it is worth pinning because two of its failures are silent:

- **The account key not being written.** The fan-out skips a destination whose
  id is missing from `accounts/<name>/.env` and queues the rest, deliberately,
  so a key that never landed looks exactly like a night that meant to publish
  to three platforms. Nothing fails, nothing logs, and the symptom is a
  platform quietly missing from a feed.
- **The brand being wrong.** It is a label rather than a foreign key, so a bad
  one puts an identity's board in a group of its own and is invisible until
  somebody opens the panel. `brand_for` deciding to derive one where it should
  have sent nothing is how a corrected grouping gets silently put back.
"""

from __future__ import annotations

import pytest

from scripts import consent

# --- Writing the account key into the account's .env ------------------------


def test_setting_a_key_uncomments_the_template_line_rather_than_appending():
    """The case that is normal rather than exceptional.

    `--new-account` writes every line commented out on purpose, so the line
    this is about to set almost always exists as `# YOUTUBE_CHANNEL_ID=`.
    Appending would leave the commented line above the live one, which reads on
    the next visit as a setting somebody disabled.
    """
    before = "# --- Destinations ---\n# YOUTUBE_CHANNEL_ID=\n# TIKTOK_OPEN_ID=\n"
    after, what = consent.set_env_key(before, "YOUTUBE_CHANNEL_ID", "UC123")
    assert after == "# --- Destinations ---\nYOUTUBE_CHANNEL_ID=UC123\n# TIKTOK_OPEN_ID=\n"
    assert what == "set"


def test_replacing_a_live_value_says_what_it_replaced():
    """A re-authorisation that lands on a different channel is the mistake
    worth naming out loud, because the wrong pick at a consent screen is not
    visible again until the first publish."""
    after, what = consent.set_env_key("YOUTUBE_CHANNEL_ID=UC_old\n", "YOUTUBE_CHANNEL_ID", "UC_new")
    assert after == "YOUTUBE_CHANNEL_ID=UC_new\n"
    assert what == "replaced 'UC_old'"


def test_re_running_a_trip_on_the_same_destination_changes_nothing():
    """Re-authorising is how a broken credential chain is recovered, so it has
    to be the boring case rather than one that rewrites the file."""
    before = "YOUTUBE_CHANNEL_ID=UC123\n"
    after, what = consent.set_env_key(before, "YOUTUBE_CHANNEL_ID", "UC123")
    assert after == before
    assert what == "unchanged"


def test_a_key_the_file_predates_is_appended():
    """An account scaffolded before a platform existed has no line for it.
    `accounts/nightlybuild/.env` had no FACEBOOK_PAGE_ID for exactly this
    reason: the template was a platform behind for a month."""
    after, what = consent.set_env_key("IG_USER_ID=178\n", "FACEBOOK_PAGE_ID", "104")
    assert after == "IG_USER_ID=178\nFACEBOOK_PAGE_ID=104\n"
    assert what == "added"


def test_a_file_with_no_trailing_newline_does_not_lose_its_last_line():
    after, _ = consent.set_env_key("IG_USER_ID=178", "FACEBOOK_PAGE_ID", "104")
    assert after == "IG_USER_ID=178\nFACEBOOK_PAGE_ID=104\n"


def test_nothing_else_in_the_file_is_touched():
    """The file holds hand written notes and a comment block per key. Rewriting
    it from a template would be how those disappear."""
    before = (
        "# a note somebody left\n"
        "CHATTERBOX_EXAGGERATION=0.5\n"
        "\n"
        "# --- Destinations ---\n"
        "# IG_USER_ID=\n"
    )
    after, _ = consent.set_env_key(before, "IG_USER_ID", "178")
    assert after.splitlines()[:4] == before.splitlines()[:4]


def test_a_commented_key_elsewhere_in_the_file_is_not_confused_for_another():
    """`IG_USER_ID` is a prefix of nothing, but `YOUTUBE_CHANNEL_ID` sitting
    under a comment mentioning `YOUTUBE_CHANNEL_ID_OLD` would be, if the match
    were a `startswith` on the name rather than on `NAME=`."""
    before = "# YOUTUBE_CHANNEL_IDS are UC-prefixed\n# YOUTUBE_CHANNEL_ID=\n"
    after, what = consent.set_env_key(before, "YOUTUBE_CHANNEL_ID", "UC1")
    assert after == "# YOUTUBE_CHANNEL_IDS are UC-prefixed\nYOUTUBE_CHANNEL_ID=UC1\n"
    assert what == "set"


# --- Which route and which key a platform uses ------------------------------


@pytest.mark.parametrize(
    ("platform", "endpoint", "env_key"),
    [
        ("instagram", "/api/accounts", "IG_USER_ID"),
        ("youtube", "/api/accounts/youtube", "YOUTUBE_CHANNEL_ID"),
        ("tiktok", "/api/accounts/tiktok", "TIKTOK_OPEN_ID"),
        ("facebook", "/api/accounts/facebook", "FACEBOOK_PAGE_ID"),
    ],
)
def test_every_platform_knows_its_route_and_its_key(platform, endpoint, env_key):
    """Instagram's route is the original and unsuffixed, which is the one
    exception to the pattern and the reason `endpoint` is derived rather than
    configured: a registration route that stops following it is a thing to
    notice."""
    trip = consent.Trip(platform=platform, account_id="x", payload={})
    assert trip.endpoint == endpoint
    assert trip.env_key == env_key


# --- The brand, which is what groups an identity ----------------------------


def test_an_explicit_brand_wins(tmp_path, monkeypatch):
    monkeypatch.setattr(consent, "ACCOUNTS_DIR", tmp_path)
    (tmp_path / "acct").mkdir()
    (tmp_path / "acct" / ".env").write_text("BRAND=fromfile\n")
    assert consent.brand_for("acct", "fromflag") == "fromflag"


def test_the_brand_comes_from_the_accounts_own_env(tmp_path, monkeypatch):
    """Written down once, where the rest of that identity already lives, rather
    than remembered as a flag on four separate consent trips."""
    monkeypatch.setattr(consent, "ACCOUNTS_DIR", tmp_path)
    (tmp_path / "acct").mkdir()
    (tmp_path / "acct" / ".env").write_text("BRAND=thenightlybuild\n")
    assert consent.brand_for("acct", "") == "thenightlybuild"


def test_no_brand_anywhere_sends_nothing_rather_than_the_account_name(tmp_path, monkeypatch):
    """The one that would have been a bug.

    Defaulting to the directory name looks obviously right and is not: account
    1's directory is `nightlybuild` and every one of its gateway rows is
    grouped under `thenightlybuild`, because the first three were registered
    before `brand` existed and took the handle-derived value. Since
    `upsert_account` overwrites a stored brand whenever a non-empty one is
    sent, a default here would regroup all of them on the next
    re-authorisation, which is the failure `CLAUDE.md` already records costing
    a re-registration.
    """
    monkeypatch.setattr(consent, "ACCOUNTS_DIR", tmp_path)
    (tmp_path / "nightlybuild").mkdir()
    (tmp_path / "nightlybuild" / ".env").write_text("IG_USER_ID=178\n")
    assert consent.brand_for("nightlybuild", "") == ""


def test_an_unknown_account_is_refused_before_a_browser_opens(tmp_path, monkeypatch):
    """Checked first in every flow, because the alternative is discovering the
    typo after a consent screen has been spent and the recovery for that is
    another trip through it."""
    monkeypatch.setattr(consent, "ACCOUNTS_DIR", tmp_path)
    (tmp_path / "nightlybuild").mkdir()
    with pytest.raises(SystemExit) as raised:
        consent.brand_for("nightlybiuld", "")
    assert "nightlybuild" in str(raised.value)


# --- Which .env a flow reads a credential out of -----------------------------


def test_the_account_env_is_layered_over_the_root_one(tmp_path, monkeypatch):
    """The bug that stopped the second account's first consent trip.

    `YOUTUBE_CLIENT_ID` and its secret identify the Google Cloud project, and
    one project authorises every channel, so they are an app credential. They
    were lumped into `accounts/nightlybuild/.env` alongside that channel's
    refresh token, which worked for exactly as long as there was one account.
    Reading the root file alone then found no pair when account 2 asked for
    one, and said to set a variable that was already set one directory away.
    """
    monkeypatch.setattr(consent, "ACCOUNTS_DIR", tmp_path)
    monkeypatch.setattr(consent, "ROOT", tmp_path)
    (tmp_path / ".env").write_text("YOUTUBE_CLIENT_ID=shared-app\nGITHUB_TOKEN=root\n")
    (tmp_path / "acct").mkdir()
    (tmp_path / "acct" / ".env").write_text("YOUTUBE_CLIENT_SECRET=from-the-account\n")

    cfg = consent.account_settings("acct")
    assert cfg.youtube_client_id == "shared-app"
    assert cfg.youtube_client_secret == "from-the-account"
    assert cfg.github_token == "root"


def test_an_account_may_override_the_shared_app(tmp_path, monkeypatch):
    """An identity with its own Cloud project is a real case, which is why this
    layers rather than simply reading the root file."""
    monkeypatch.setattr(consent, "ACCOUNTS_DIR", tmp_path)
    monkeypatch.setattr(consent, "ROOT", tmp_path)
    (tmp_path / ".env").write_text("YOUTUBE_CLIENT_ID=shared-app\n")
    (tmp_path / "acct").mkdir()
    (tmp_path / "acct" / ".env").write_text("YOUTUBE_CLIENT_ID=its-own-app\n")

    assert consent.account_settings("acct").youtube_client_id == "its-own-app"
