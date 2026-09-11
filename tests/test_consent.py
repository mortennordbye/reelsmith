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


# --- The Instagram trip, and the id it must not pick -------------------------


def _ig_me(monkeypatch, payload):
    from scripts import instagram_authorise as ig

    monkeypatch.setattr(ig, "_graph", lambda *a, **k: payload)
    return ig


def test_the_instagram_trip_registers_the_business_id_not_the_app_scoped_one(monkeypatch):
    """`/me` returns two seventeen digit ids and the obvious one is wrong.

    On the Instagram Login path `id` is the app-scoped user id and `user_id` is
    the Instagram Business account id. The publisher addresses
    `graph.instagram.com/{id}/media` with the second, so it is `user_id` that
    belongs in `IG_USER_ID` and on the account row.

    Neither looks more correct than the other, and nothing about the wrong one
    is visible until the first publish fails against a node that does not
    exist. Measured on account 1's own token: `id` is 37342907808657598 and
    `user_id` is 17841441696714445, and it is the second that has been
    publishing since 2026-08-01.
    """
    ig = _ig_me(monkeypatch, {
        "id": "37342907808657598",
        "user_id": "17841441696714445",
        "username": "thenightlybuild",
        "account_type": "BUSINESS",
    })
    found = ig.account_of("https://graph.instagram.com", "tok", "v23.0")
    assert found["user_id"] == "17841441696714445"


def test_a_token_with_no_user_id_is_refused_and_says_why(monkeypatch):
    """A token minted through Facebook Login answers on graph.facebook.com and
    reaches its Instagram account through a Page, which is a different flow.
    Refusing here beats registering a Facebook user id as an Instagram one."""
    ig = _ig_me(monkeypatch, {"id": "123", "username": "someone"})
    with pytest.raises(SystemExit) as raised:
        ig.account_of("https://graph.instagram.com", "tok", "v23.0")
    assert "Facebook Login" in str(raised.value)


def test_a_personal_account_is_refused(monkeypatch):
    """The Content Publishing API does not work with Personal and neither do
    full insights. Switching is free and reversible, so this is worth stopping
    for rather than discovering at the first publish."""
    ig = _ig_me(monkeypatch, {
        "id": "1", "user_id": "2", "username": "x", "account_type": "PERSONAL",
    })
    with pytest.raises(SystemExit) as raised:
        ig.account_of("https://graph.instagram.com", "tok", "v23.0")
    assert "Personal" in str(raised.value)


def test_the_prerequisites_are_printed_and_not_opened(monkeypatch, capsys):
    """The complaint that produced this, in one test.

    Opening every page the setup touches, at once, put four tabs on screen with
    nothing saying which was current. A page is opened at the moment its value
    is asked for, by the prompt that needs it, and nowhere else. These two have
    no prompt behind them.
    """
    from scripts import instagram_authorise as ig

    opened = []
    monkeypatch.setattr(consent.webbrowser, "open", opened.append)
    consent.list_prerequisites(ig.prerequisites("1234567890"))

    printed = capsys.readouterr().out
    assert opened == [], "a prerequisite was opened rather than printed"
    assert "https://developers.facebook.com/apps/1234567890/roles/roles/" in printed
    assert ig.INVITES_URL in printed


def test_asking_for_a_secret_opens_the_page_it_is_on(tmp_path, monkeypatch):
    """The rule, stated as a test. The page and the prompt arrive together, so
    what is on screen is always the thing being asked for."""
    monkeypatch.setattr(consent, "ROOT", tmp_path)
    (tmp_path / ".env").write_text("")
    monkeypatch.setattr(consent.getpass, "getpass", lambda _p: "s")
    monkeypatch.setattr("builtins.input", lambda _p: "n")
    opened = []
    monkeypatch.setattr(consent.webbrowser, "open", opened.append)

    consent.ask_secret("K", what="Behind Show.", where="https://example.test/basic/")

    assert opened == ["https://example.test/basic/"]


def test_no_app_id_falls_back_to_the_apps_list():
    """Still one click from the right place rather than a file path."""
    from scripts import instagram_authorise as ig

    assert ig.prerequisites("")[0][0] == ig.APPS_URL


def test_every_flow_refuses_before_opening_anything_without_a_terminal(monkeypatch):
    """All four, because the check only helps where it is actually called.

    Every trip prompts: three for a paste and all four for a yes or no at the
    end. The worst case is the YouTube one, which prompts only to confirm and
    so would discover the missing terminal after the consent had been granted,
    and Google hands out a refresh token once per authorisation.
    """
    import argparse

    from scripts import authorise

    monkeypatch.setattr(consent.sys.stdin, "isatty", lambda: False)
    opened = []
    monkeypatch.setattr(consent.webbrowser, "open", opened.append)

    for name, module in authorise.FLOWS.items():
        # Only the flows that open tabs import webbrowser; YouTube's browser is
        # opened by google-auth-oauthlib inside the flow library.
        if hasattr(module, "webbrowser"):
            monkeypatch.setattr(module.webbrowser, "open", opened.append)
        args = argparse.Namespace(
            account="x", brand="", no_browser=False, no_subscribe=False,
            username="", secrets_file=None,
        )
        with pytest.raises(SystemExit) as raised:
            module.trip(args)
        assert "real terminal" in str(raised.value), f"{name} refused for another reason"

    assert opened == [], "a browser was opened before the refusal"


def test_the_terminal_refusal_says_nothing_was_done(monkeypatch):
    """Three tabs opening and then the prompt dying on EOF is worse than not
    starting.

    `getpass` needs a terminal it can turn echo off on. A pipe is not one and
    neither is Claude Code's `!` prefix: it warns that echo cannot be
    controlled, falls back to a plain read, and raises EOFError on the empty
    stdin behind it, after the browser has already been opened.
    """
    monkeypatch.setattr(consent.sys.stdin, "isatty", lambda: False)
    with pytest.raises(SystemExit) as raised:
        consent.require_terminal()
    assert "Nothing was opened and nothing was registered" in str(raised.value)


# --- Asking for an app credential rather than requiring it up front ----------


def test_a_missing_app_secret_is_asked_for_and_can_be_saved(tmp_path, monkeypatch, capsys):
    """The alternative is a prerequisite, and a prerequisite is the step
    somebody discovers they have not done after a browser has been opened.

    Every one of these trips is most expensive to restart at exactly that
    point, and the page the credential comes from is open in a tab by then, so
    the answer to the prompt is a copy and a paste.
    """
    monkeypatch.setattr(consent, "ROOT", tmp_path)
    (tmp_path / ".env").write_text("GITHUB_TOKEN=x\n")
    monkeypatch.setattr(consent.getpass, "getpass", lambda _p: "  the-secret  ")
    monkeypatch.setattr("builtins.input", lambda _p: "y")

    got = consent.ask_secret("FACEBOOK_APP_SECRET", what="Behind Show.", where="https://x")

    assert got == "the-secret"
    assert "FACEBOOK_APP_SECRET=the-secret" in (tmp_path / ".env").read_text()
    assert "not echoed" in capsys.readouterr().out


def test_declining_to_save_still_returns_it_and_says_so(tmp_path, monkeypatch, capsys):
    """Offered rather than written. A script that silently appends a secret to
    a file is one nobody can predict."""
    monkeypatch.setattr(consent, "ROOT", tmp_path)
    (tmp_path / ".env").write_text("")
    monkeypatch.setattr(consent.getpass, "getpass", lambda _p: "the-secret")
    monkeypatch.setattr("builtins.input", lambda _p: "n")

    assert consent.ask_secret("X", what="w", where="https://x") == "the-secret"
    assert "X=" not in (tmp_path / ".env").read_text()
    assert "will ask again" in capsys.readouterr().out


def test_pasting_nothing_stops_rather_than_going_on_with_an_empty_secret(tmp_path, monkeypatch):
    monkeypatch.setattr(consent, "ROOT", tmp_path)
    monkeypatch.setattr(consent.getpass, "getpass", lambda _p: "   ")

    with pytest.raises(SystemExit) as raised:
        consent.ask_secret("X", what="w", where="https://x")
    assert "Nothing pasted" in str(raised.value)


# --- Reading the code out of whatever was pasted -----------------------------


DIALOG = (
    "https://www.facebook.com/v23.0/dialog/oauth?client_id=325&"
    "redirect_uri=https%3A%2F%2Fgate.example.test%2Ffacebook%2Fcallback&"
    "state=STATE&scope=pages_show_list&response_type=code"
)
LANDED = "https://gate.example.test/facebook/callback?code=AQJcX2iw&state=STATE#_=_"


def test_both_urls_pasted_together_still_finds_the_code():
    """The paste that failed on a live trip, and it was not carelessness.

    This prompt is printed directly under a fallback URL the trip prints in
    case the browser does not open, so picking up both is the normal accident.
    The first is the authorisation dialog, whose query carries
    `response_type=code` and no `code`, so the refusal read "that address
    carries no code" while the code sat in the same paste.
    """
    query = consent._query_carrying_the_code(f"{DIALOG} {LANDED}")

    assert query["code"] == ["AQJcX2iw"]
    assert query["state"] == ["STATE"]


def test_the_landed_address_alone_still_works():
    assert consent._query_carrying_the_code(LANDED)["code"] == ["AQJcX2iw"]


def test_the_dialog_alone_carries_no_code_and_says_so():
    """`response_type=code` is not a code. Falling back to the first URL is
    what keeps that error describing the real problem."""
    assert not consent._query_carrying_the_code(DIALOG).get("code")


def test_an_error_redirect_is_found_rather_than_buried_by_the_dialog():
    """A refusal comes back on the callback with no code at all, and its
    message is the only useful thing in the paste.

    Looking for a code and then falling back to the first URL would hand back
    the dialog's query here and report a mismatched state, which describes
    nothing. So `error` is looked for too, before that fallback.
    """
    denied = "https://gate.example.test/facebook/callback?error=access_denied&state=STATE"

    assert consent._query_carrying_the_code(f"{DIALOG} {denied}")["error"] == ["access_denied"]


# --- Confirming before registering -------------------------------------------


def test_registering_is_confirmed_against_the_account_it_is_for(monkeypatch, capsys):
    """The Facebook trip was the only one of the four without this, and that
    was the gap rather than an inconsistency.

    Meta offers a "continue with your previous settings" shortcut to an app you
    have connected before, and the previous settings belong to whichever
    identity connected it last. Take it while onboarding a second identity and
    the consent grants only the first one's Page, /me/accounts returns exactly
    one row, a chooser that resolves by count takes it, and the second
    account's Facebook row points at the first account's audience.
    """
    monkeypatch.setattr("builtins.input", lambda _p: "y")
    consent.confirm(
        what="About to register this Page:",
        lines=[("Page", "The Whole Quote"), ("id", "123")],
        account="secondaccount",
        brand="secondbrand",
    )
    printed = capsys.readouterr().out

    # The pair is what is stored, so the pair is what is shown: either half can
    # be right while the pairing is wrong.
    for expected in ("The Whole Quote", "123", "secondaccount", "secondbrand"):
        assert expected in printed


def test_anything_but_yes_stores_nothing(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _p: "")
    with pytest.raises(SystemExit) as raised:
        consent.confirm(what="x", lines=[], account="a", brand="b")
    assert "Nothing was stored" in str(raised.value)


def test_a_derived_brand_is_labelled_rather_than_shown_blank(monkeypatch, capsys):
    """Empty is a real answer here, meaning the gateway derives from the
    handle. A blank line reads as a bug."""
    monkeypatch.setattr("builtins.input", lambda _p: "y")
    consent.confirm(what="x", lines=[], account="a", brand="")
    assert "derived from the handle" in capsys.readouterr().out


def test_facebook_refuses_a_missing_app_id_before_printing_any_url(monkeypatch, capsys):
    """A list of addresses that 404 is worse than no list.

    Without an app id every prerequisite URL carried a `<app-id>` placeholder,
    which is a dead link presented as an instruction. The refusal moved above
    the printing rather than below it.
    """
    import argparse

    from scripts import facebook_authorise as fb

    monkeypatch.setattr(fb.consent, "require_terminal", lambda: None)
    monkeypatch.setattr(fb.consent, "brand_for", lambda *_a: "")
    bare = fb.consent.Settings(_env_file=None)
    monkeypatch.setattr(fb.consent, "account_settings", lambda _n: bare)
    monkeypatch.setattr(fb.os.environ, "get", lambda *_a: "")

    args = argparse.Namespace(account="x", brand="", no_browser=False)
    with pytest.raises(SystemExit) as raised:
        fb.trip(args)

    assert "FACEBOOK_APP_ID" in str(raised.value)
    assert "<app-id>" not in capsys.readouterr().out
