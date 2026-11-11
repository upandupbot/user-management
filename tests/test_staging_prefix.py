"""A staging copy runs under its own URL prefix with a banner and its own
session cookie; the live site's defaults stay exactly as they were."""
import pytest

from swap_assignee import web

from tests.test_web_operations import _client


def test_live_defaults_are_unchanged(monkeypatch):
    monkeypatch.delenv("SWAP_URL_PREFIX", raising=False)
    monkeypatch.delenv("SWAP_ENV_LABEL", raising=False)
    client = _client(monkeypatch)
    page = client.get("/user-management/").get_data(as_text=True)
    assert 'src="/user-management/static/slipstream-logo.png"' in page
    assert "const APP_PREFIX = '/user-management';" in page
    assert "__APP_PREFIX__" not in page and "__ENV_" not in page
    assert "env-banner" not in page.split("</style>", 1)[1]
    assert "<title>slipstream" in page
    resp = client.post("/user-management/api/auth", json={"email": "kate@dydx.digital"})
    assert resp.headers["Set-Cookie"].startswith("user_management_session=")


def test_staging_prefix_banner_and_cookie(monkeypatch):
    monkeypatch.setenv("SWAP_URL_PREFIX", "/user-management-staging")
    monkeypatch.setenv("SWAP_ENV_LABEL", "Staging")
    client = _client(monkeypatch)
    page = client.get("/user-management-staging/").get_data(as_text=True)
    assert 'src="/user-management-staging/static/slipstream-logo.png"' in page
    assert "const APP_PREFIX = '/user-management-staging';" in page
    assert '<div class="env-banner" role="status"><b>Staging</b>' in page
    assert "<title>[Staging] slipstream" in page
    assert client.get("/user-management-staging/static/swap-icon.svg").status_code == 200
    assert client.get("/user-management-staging/api/session").status_code == 200
    resp = client.post("/user-management-staging/api/auth", json={"email": "kate@dydx.digital"})
    assert resp.headers["Set-Cookie"].startswith("user_management_staging_session=")


def test_env_label_is_escaped(monkeypatch):
    monkeypatch.setenv("SWAP_ENV_LABEL", "<script>x</script>")
    page = _client(monkeypatch).get("/").get_data(as_text=True)
    assert "<b>&lt;script&gt;x&lt;/script&gt;</b>" in page


@pytest.mark.parametrize("bad", ["/a b", "/x'y", "/x<y", "//", "/user-management/../x"])
def test_unsafe_prefix_is_rejected(monkeypatch, bad):
    monkeypatch.setenv("SWAP_URL_PREFIX", bad)
    with pytest.raises(SystemExit):
        web.url_prefix_from_env()


def test_prefix_is_normalised(monkeypatch):
    monkeypatch.setenv("SWAP_URL_PREFIX", "user-management-staging/")
    assert web.url_prefix_from_env() == "/user-management-staging"
    assert web.session_cookie_name("/user-management") == "user_management_session"


def _client_with_pipes(monkeypatch, value):
    # create_app replaces web.KNOWN_PIPES when SWAP_PIPES is set; register the
    # original with monkeypatch first so it's restored after the test.
    monkeypatch.setattr(web, "KNOWN_PIPES", list(web.KNOWN_PIPES))
    monkeypatch.setenv("SWAP_PIPES", value)
    client = web.create_app().test_client()
    client.post("/api/auth", json={"email": "kate@dydx.digital"})
    return client


def test_swap_pipes_limits_the_board_list(monkeypatch):
    client = _client_with_pipes(monkeypatch, "306999999:Staging test pipe")
    assert client.get("/api/pipes").get_json()["pipes"] == [
        {"id": "306999999", "name": "Staging test pipe"}
    ]


def test_swap_pipes_refuses_other_pipes(monkeypatch):
    client = _client_with_pipes(monkeypatch, "306999999:Staging test pipe")
    real_board = "305705781"  # Account Management
    for path, body in (
        ("/api/wizard/phases", {"pipe_ids": [real_board]}),
        ("/api/wizard/scan/stream", {"operation": "replace", "old_user": "a@dydx.digital",
                                     "new_user": "b@dydx.digital", "pipe_ids": ["306999999", real_board]}),
        ("/api/assign/scan/stream", {"user": "b@dydx.digital", "match_text": "Acme", "pipe_ids": [real_board]}),
        ("/api/load", {"pipe_id": real_board}),
        ("/api/preview", {"pipe_id": real_board}),
        ("/api/preview/stream", {"pipe_id": real_board}),
    ):
        resp = client.post(path, json=body)
        assert resp.status_code == 403, path
        assert real_board in resp.get_json()["error"]


def test_without_swap_pipes_the_real_boards_are_listed(monkeypatch):
    client = web.create_app().test_client()
    names = [p["name"] for p in client.get("/api/pipes").get_json()["pipes"]]
    assert "Account Management" in names and len(names) == 9


@pytest.mark.parametrize("bad", ["abc:Name", "306999999", "306999999:", ":Name", ","])
def test_bad_swap_pipes_is_rejected(monkeypatch, bad):
    monkeypatch.setenv("SWAP_PIPES", bad)
    with pytest.raises(SystemExit):
        web.pipes_from_env()
