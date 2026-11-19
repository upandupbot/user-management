"""The health endpoint answers without a session or outside calls, and a
restart made by deploy/healthcheck.sh is reported when the app starts."""
import logging

from swap_assignee import web
from swap_assignee import papertrail_logger as pt


def test_health_needs_no_session_even_with_a_password(monkeypatch):
    monkeypatch.setenv("SWAP_WEB_PASSWORD", "pw")
    client = web.create_app().test_client()
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True and body["uptime_seconds"] >= 0
    # Other API routes are still locked.
    assert client.post("/api/info").status_code == 401


def test_health_works_under_the_live_prefix(monkeypatch):
    monkeypatch.delenv("SWAP_URL_PREFIX", raising=False)
    client = web.create_app().test_client()
    assert client.get("/user-management/api/health").get_json()["ok"] is True


def test_health_makes_no_pipefy_calls(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("health check must not call Pipefy")

    monkeypatch.setattr(web.PipefyClient, "execute", boom, raising=False)
    assert web.create_app().test_client().get("/api/health").status_code == 200


def test_restart_marker_is_logged_once_and_removed(tmp_path, caplog):
    marker = tmp_path / ".healthcheck-restart"
    marker.write_text("2026-11-19T10:00:00Z, 3 failed checks in a row\n", encoding="utf-8")
    with caplog.at_level(logging.INFO, logger=pt.activity_log.name):
        assert web.report_health_check_restart(str(marker)) is True
    rec = next(r for r in caplog.records if "Restarted by the health check" in r.getMessage())
    assert rec.levelno == logging.WARNING
    assert "3 failed checks in a row" in rec.getMessage()
    assert not marker.exists()
    assert web.report_health_check_restart(str(marker)) is False


def test_marker_in_working_directory_is_reported_on_start(caplog):
    # conftest chdirs to a temp folder, which stands in for the app folder.
    with open(web.HEALTH_RESTART_MARKER, "w", encoding="utf-8") as fh:
        fh.write("2026-11-19T10:00:00Z, 3 failed checks in a row")
    with caplog.at_level(logging.INFO, logger=pt.activity_log.name):
        web.create_app()
    assert any("Restarted by the health check" in r.getMessage() for r in caplog.records)
