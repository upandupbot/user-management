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
