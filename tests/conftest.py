import sys
from pathlib import Path

import pytest

# Make the package importable when running `pytest` from repo root.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _isolate_audit_store(tmp_path, monkeypatch):
    """Redirect the audit history store to a temp file so tests never write to
    the developer's real ~/.swap_assignee/history.jsonl."""
    monkeypatch.setenv("SWAP_AUDIT_STORE", str(tmp_path / "audit-history.jsonl"))


# Settings a developer (or the server) may have in their environment that
# would change how the app behaves under test.
_REAL_SETTINGS = (
    "PIPEFY_API_TOKEN", "SWAP_WEB_PASSWORD", "SWAP_WEB_SECRET_KEY",
    "SWAP_AUDIT_SHEET_ID", "SWAP_AUDIT_SERVICE_ACCOUNT_FILE",
    "SWAP_AUDIT_SERVICE_ACCOUNT_JSON", "SWAP_AUDIT_WEBHOOK_URL",
    "PAPERTRAIL_LOG_TOKEN", "SWAP_URL_PREFIX", "SWAP_ENV_LABEL",
)


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path, monkeypatch):
    """Never read the developer's real config.ini or environment.

    Each test runs from an empty temp folder (so `config.ini` isn't found)
    with a fake Pipefy token. Without this, tests silently used the local
    config.ini: they passed on a laptop but failed in CI, and could pick up
    a real Google Sheet or password.
    """
    for name in _REAL_SETTINGS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PIPEFY_API_TOKEN", "test-token")
    monkeypatch.chdir(tmp_path)
