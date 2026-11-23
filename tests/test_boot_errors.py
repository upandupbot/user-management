"""A settings problem stops the app with gunicorn's 'worker failed to boot'
code (3), so gunicorn halts instead of respawning the worker in a tight loop."""
import pathlib

import pytest

from swap_assignee import web


def _exit_code(monkeypatch, capsys, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(SystemExit) as exc:
        web.create_app()
    return exc.value.code, capsys.readouterr().err


def test_bad_swap_pipes_exits_with_boot_error(monkeypatch, capsys):
    code, err = _exit_code(monkeypatch, capsys, SWAP_PIPES="307433006")
    assert code == web.GUNICORN_WORKER_BOOT_ERROR == 3
    assert "SWAP_PIPES entry '307433006' must look like" in err


def test_bad_prefix_exits_with_boot_error(monkeypatch, capsys):
    code, err = _exit_code(monkeypatch, capsys, SWAP_URL_PREFIX="/a b")
    assert code == 3 and "SWAP_URL_PREFIX" in err


def test_missing_token_exits_with_boot_error(monkeypatch, capsys):
    monkeypatch.delenv("PIPEFY_API_TOKEN", raising=False)
    code, err = _exit_code(monkeypatch, capsys)
    assert code == 3 and "Pipefy API token missing" in err


def test_good_settings_still_build_the_app():
    assert web.create_app().test_client().get("/api/health").status_code == 200


def test_service_files_back_off_when_failing():
    # Read from the repo, not the test's temp working directory.
    root = pathlib.Path(__file__).resolve().parent.parent / "deploy"
    for name in ("user-management.service", "user-management-staging.service"):
        unit = (root / name).read_text(encoding="utf-8")
        assert "Restart=always" in unit and "RestartSteps=10" in unit and "RestartMaxDelaySec=5min" in unit
