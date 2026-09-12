"""The pipeline's side of per brand state on the gateway.

What is worth pinning: reads name the brand and still name the account, so an
older gateway scopes the way it always did; the queue ceiling counts one
destination and never the brand; `.env` wins over a brand setting; the
cooldown and run calls say which brand; and a settings read that fails is a
None the caller can refuse on, never an empty row.
"""

from __future__ import annotations

import httpx
import pytest

import config
from config import Settings
from pipeline import gateway

IG = "17841400000000000"
YT = "UCq0Ff3lJ7dK2sWnEv8mXtLp"


@pytest.fixture
def cfg() -> Settings:
    return Settings(
        github_token="x",
        ig_user_id=IG,
        brand="thenightlybuild",
        gateway_url="https://gate.example.test",
        gateway_token="test-token",
        _env_file=None,
    )


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_reads_name_the_brand_and_still_the_account(cfg):
    """An older gateway ignores `brand` and scopes by account; a newer one lets
    the brand win. Sending only the brand would make an older one answer for
    everyone, which is F8."""
    seen = []

    def handler(request):
        seen.append(dict(request.url.params))
        return httpx.Response(200, json={"covered": []})

    gateway.fetch_covered(cfg, client=_client(handler))

    assert seen == [{"account_id": IG, "brand": "thenightlybuild"}]


def test_no_brand_reads_exactly_as_before(cfg):
    plain = cfg.model_copy(update={"brand": ""})
    seen = []

    def handler(request):
        seen.append(dict(request.url.params))
        return httpx.Response(200, json={"rendered": []})

    gateway.fetch_rendered(plain, client=_client(handler))

    assert seen == [{"account_id": IG}]


def test_the_queue_ceiling_counts_one_destination_never_the_brand(cfg):
    """A brand wide count multiplies by the number of platforms and pins the
    batch at zero against a ceiling calibrated on one feed."""
    seen = []

    def handler(request):
        seen.append(dict(request.url.params))
        return httpx.Response(200, json={"queue": [{"state": "approved"}, {"state": "draft"}]})

    assert gateway.fetch_pending_count(cfg, client=_client(handler)) == 2
    assert seen == [{"account_id": IG}]


def test_an_account_with_no_instagram_counts_its_own_first_destination(cfg):
    """It used to send nothing and count every account's queue."""
    no_ig = cfg.model_copy(update={"ig_user_id": "", "youtube_channel_id": YT})
    seen = []

    def handler(request):
        seen.append(dict(request.url.params))
        return httpx.Response(200, json={"queue": []})

    gateway.fetch_pending_count(no_ig, client=_client(handler))

    assert seen == [{"account_id": YT}]


def test_env_wins_over_a_brand_setting(tmp_path):
    """A render host that has not been updated carries these lines in its
    projected .env, and nothing from the gateway may override them."""
    cfg = Settings(github_token="x", episode_crf=24, _env_file=None)

    applied = config.apply_brand_settings(
        cfg, {"episode_crf": 30, "endcard_tagline": "from the gateway", "batch": 3}
    )

    assert cfg.episode_crf == 24
    assert cfg.endcard_tagline == "from the gateway"
    assert applied == ["endcard_tagline"]


def test_a_missing_settings_row_is_none_not_an_empty_row(cfg):
    handler = lambda request: httpx.Response(404, json={"detail": "no settings"})  # noqa: E731

    assert gateway.fetch_brand_settings(cfg, client=_client(handler)) is None


def test_an_unreachable_gateway_is_none_too(cfg):
    def handler(request):
        raise httpx.ConnectError("down")

    assert gateway.fetch_brand_settings(cfg, client=_client(handler)) is None


def test_a_settings_write_is_pinned_and_a_refusal_raises(cfg):
    seen = []

    def handler(request):
        seen.append(request.read())
        return httpx.Response(409, text="thenightlybuild is at version 2")

    with pytest.raises(RuntimeError, match="version 2"):
        gateway.put_brand_settings(cfg, {"batch": 3}, if_version=1, client=_client(handler))
    assert b'"if_version":1' in seen[0].replace(b" ", b"")


def test_a_commitment_names_the_brand_and_the_subject(cfg):
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path, request.read()))
        return httpx.Response(200, json={"ok": True})

    assert gateway.record_covered(cfg, "repo:a/b", source="posted", client=_client(handler))
    assert gateway.forget_covered(cfg, "repo:a/b", client=_client(handler))

    assert seen[0][0:2] == ("POST", "/api/covered")
    assert b"thenightlybuild" in seen[0][2] and b"repo:a/b" in seen[0][2]
    assert seen[1][0:2] == ("DELETE", "/api/covered/repo:a/b")


def test_no_brand_writes_nothing_to_the_cooldown_table(cfg):
    def handler(request):  # pragma: no cover - must never be reached
        raise AssertionError("a brandless commitment has no table row to go to")

    plain = cfg.model_copy(update={"brand": ""})

    assert gateway.record_covered(plain, "repo:a/b", client=_client(handler)) is False


def test_a_run_report_returns_its_id_and_never_raises(cfg):
    ok = lambda request: httpx.Response(200, json={"id": 7, "outcome": "running"})  # noqa: E731

    def down(request):
        raise httpx.ConnectError("down")

    assert gateway.report_run(cfg, "batch", client=_client(ok)) == 7
    assert gateway.report_run(cfg, "batch", outcome="ok", run_id=7, client=_client(down)) is None


# --- The startup read --------------------------------------------------------


def _row(**settings) -> dict:
    return {"brand": "thenightlybuild", "settings": settings, "version": 4}


def test_a_brand_whose_settings_cannot_be_read_refuses_the_run(cfg, monkeypatch):
    """Rendering on defaults is how one identity takes another's end card."""
    import typer

    import main

    monkeypatch.setattr(main.gateway, "fetch_brand_settings", lambda cfg: None)

    with pytest.raises(typer.Exit):
        main._apply_brand(cfg, batch=None, max_queue=None)


def test_no_brand_or_no_gateway_changes_nothing(cfg, monkeypatch):
    """A host that has not been set up for brand settings behaves as before."""
    import main

    def unreachable(cfg):  # pragma: no cover - must never be reached
        raise AssertionError("no brand means no read")

    monkeypatch.setattr(main.gateway, "fetch_brand_settings", unreachable)

    assert main._apply_brand(cfg.model_copy(update={"brand": ""}), batch=None, max_queue=None) == (
        None, None,
    )
    assert main._apply_brand(
        cfg.model_copy(update={"gateway_url": ""}), batch=2, max_queue=5
    ) == (2, 5)


def test_the_brand_fills_the_ceiling_but_never_turns_a_run_into_a_batch(cfg, monkeypatch):
    """`batch` absent is what makes `--recover` or `--publish` a single run."""
    import main

    monkeypatch.setattr(
        main.gateway, "fetch_brand_settings",
        lambda cfg: _row(batch=3, max_queue=6, endcard_tagline="from the gateway"),
    )

    assert main._apply_brand(cfg, batch=None, max_queue=None) == (None, 6)
    assert cfg.endcard_tagline == "from the gateway"


def test_a_flag_wins_over_the_brand(cfg, monkeypatch):
    import main

    monkeypatch.setattr(main.gateway, "fetch_brand_settings", lambda cfg: _row(max_queue=6))

    assert main._apply_brand(cfg, batch=2, max_queue=10) == (2, 10)
