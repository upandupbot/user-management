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
