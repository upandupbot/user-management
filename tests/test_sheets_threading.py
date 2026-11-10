"""The Google Sheets client is not thread-safe; each server thread must get
its own client, and concurrent History/notification reads share one read."""
import ssl
import sys
import threading
import types

from swap_assignee.audit import AUDIT_COLUMNS
from swap_assignee.sheets_sink import GoogleSheetSink


class _Req:
    def __init__(self, fn):
        self._fn = fn

    def execute(self):
        return self._fn()


class FakeService:
    def __init__(self, state):
        self.state = state
        self.thread = threading.get_ident()
        state["built"].append(self)

    def spreadsheets(self):
        return self

    def values(self):
        return self

    def get(self, spreadsheetId=None, range=None):  # noqa: A002 - mirrors the API
        def run():
            assert threading.get_ident() == self.thread, "client shared across threads"
            if range is None:  # spreadsheet metadata
                return {"sheets": [{"properties": {"title": "Audit"}}]}
            if range.endswith("A1:1"):
                return {"values": [list(AUDIT_COLUMNS)]}
            if range.startswith("Audit!A2") or "!A2:" in range:
                self.state["reads"] += 1
                if self.state.get("fail_next"):
                    self.state["fail_next"] = False
                    raise ssl.SSLError("decryption failed or bad record mac")
                return {"values": [["2026-11-10T10:00:00Z", "op@dydx.digital"]]}
            return {"sheets": [{"properties": {"title": "Audit"}}]}
        return _Req(run)

    def append(self, **kwargs):
        return _Req(lambda: {})


def _sink(monkeypatch, state):
    mod = types.ModuleType("googleapiclient.discovery")
    mod.build = lambda *a, **k: FakeService(state)
    monkeypatch.setitem(sys.modules, "googleapiclient", types.ModuleType("googleapiclient"))
    monkeypatch.setitem(sys.modules, "googleapiclient.discovery", mod)
    return GoogleSheetSink("sheet", object())


def test_each_thread_gets_its_own_client_and_reads_are_shared(monkeypatch):
    state = {"built": [], "reads": 0}
    sink = _sink(monkeypatch, state)
    errors = []

    def worker():
        try:
            assert sink.recent(limit=0, days=None)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len({svc.thread for svc in state["built"]}) == len(state["built"])
    assert state["reads"] <= 4  # cached afterwards
    reads = state["reads"]
    sink.recent(limit=0, days=None)
    assert state["reads"] == reads  # served from cache
    sink.append([{"ts": "2026-11-10T11:00:00Z"}])
    sink.recent(limit=0, days=None)
    assert state["reads"] == reads + 1  # append invalidates the cache


def test_read_retries_once_after_ssl_error(monkeypatch):
    state = {"built": [], "reads": 0, "fail_next": True}
    sink = _sink(monkeypatch, state)
    rows = sink.recent(limit=0, days=None)
    assert rows and rows[0]["operator_email"] == "op@dydx.digital"
    assert state["reads"] == 2
