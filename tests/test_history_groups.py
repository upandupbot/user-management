from swap_assignee import web
from swap_assignee.history_groups import change_mode, group_changes

from tests.test_web_operations import _client


def _row(ts, card, field, **kw):
    base = {
        "ts": ts, "operator_email": "robyn.seymour@mcsaatchiabel.co.za",
        "pipe_id": "7", "pipe_name": "Account Management", "card_id": card,
        "title": f"Job {card}", "client": "Standard Bank", "scope": "field",
        "field_label": field, "old_user": "Katekani Nyamandi <katekani@dydx.digital>",
        "new_user": "Simphiwe Zungu <simphiwe@dydx.digital>", "status": "ok",
        "operation": "replace",
    }
    base.update(kw)
    return base


def test_rows_from_one_apply_become_one_readable_change():
    rows = [_row("2026-11-06T10:30:%02dZ" % i, f"C{i // 2}", "Account Manager" if i % 2 else "Job Owner")
            for i in range(10)]
    changes = group_changes(rows)
    assert len(changes) == 1
    c = changes[0]
    assert c["job_count"] == 5
    assert c["mode"] == "replace"
    assert c["sentence"] == (
        "Katekani Nyamandi was replaced with Simphiwe Zungu on 5 jobs in Account Management."
    )
    assert c["roles_text"] == "as Account Manager and Job Owner"
    assert c["operator"] == "Robyn Seymour"


def test_modes_are_told_apart():
    client = _row("2026-11-06T10:30:00Z", "C1", "Account Manager", operation="add",
                  old_user="client match “Standard Bank”", new_user="Nate Ndlovu <nate@x.co.za>")
    coassign = _row("2026-11-06T09:00:00Z", "C2", "Job Owner", operation="add")
    undo = _row("2026-11-06T08:00:00Z", "C3", "Job Owner", operation="remove",
                old_user="Simphiwe Zungu <simphiwe@dydx.digital>", new_user="")
    assert [change_mode(r) for r in (client, coassign, undo)] == ["client", "coassign", "undo"]
    sentences = [c["sentence"] for c in group_changes([client, coassign, undo])]
    assert sentences == [
        "Nate Ndlovu was added to 1 Standard Bank job in Account Management.",
        "Simphiwe Zungu was added alongside Katekani Nyamandi on 1 job in Account Management.",
        "Simphiwe Zungu was removed from 1 job in Account Management (undoing a saved change).",
    ]


def test_run_id_splits_back_to_back_applies_and_gap_splits_legacy_rows():
    a = [_row("2026-11-06T10:30:00Z", "C1", "Job Owner", run_id="r1"),
         _row("2026-11-06T10:30:05Z", "C2", "Job Owner", run_id="r2")]
    assert len(group_changes(a)) == 2
    legacy = [_row("2026-11-06T10:30:00Z", "C1", "Job Owner"),
              _row("2026-11-06T13:30:00Z", "C2", "Job Owner")]
    assert len(group_changes(legacy)) == 2


def test_failed_cards_are_counted():
    rows = [_row("2026-11-06T10:30:00Z", "C1", "Job Owner"),
            _row("2026-11-06T10:30:01Z", "C2", "Job Owner", status="error", error="Card was locked")]
    c = group_changes(rows)[0]
    assert (c["ok_count"], c["failed_count"], c["status"]) == (1, 1, "partial")


def test_history_api_and_notifications_use_grouped_changes(monkeypatch):
    rows = [_row("2026-11-06T10:30:%02dZ" % i, f"C{i}", "Account Manager") for i in range(3)]

    class FakeAuditStore:
        retention_days = 365

        def recent(self, *, limit=0, days=None):
            return list(rows)

    monkeypatch.setattr(web, "AuditStore", FakeAuditStore)
    client = _client(monkeypatch)
    payload = client.get("/api/history?days=all&type=replace").get_json()
    assert payload["change_count"] == 1
    assert payload["changes"][0]["job_count"] == 3
    assert client.get("/api/history?days=all&type=client").get_json()["change_count"] == 0

    monkeypatch.setattr(web.time, "time", lambda: 1794000000.0)  # 2026-11-06
    client.post("/api/auth", json={"email": "someone@dydx.digital"})
    notes = [n for n in client.get("/api/notifications").get_json()["notifications"] if n["kind"] == "change"]
    assert len(notes) == 1
    assert notes[0]["message"] == (
        "Katekani Nyamandi was replaced with Simphiwe Zungu on 3 jobs in Account Management."
    )
    assert notes[0]["by"] == "Robyn Seymour"


def test_bell_hides_your_own_successful_changes_only(monkeypatch):
    mine_ok = _row("2026-11-06T10:30:00Z", "C1", "Job Owner", run_id="mine-ok",
                   operator_email="robyn.seymour@mcsaatchiabel.co.za")
    mine_failed = _row("2026-11-06T10:20:00Z", "C2", "Job Owner", run_id="mine-failed",
                       operator_email="robyn.seymour@mcsaatchiabel.co.za", status="error", error="Card was locked")
    other_involving_me = _row("2026-11-06T10:10:00Z", "C3", "Job Owner", run_id="other",
                              operator_email="sam@mcsaatchiabel.co.za",
                              old_user="Robyn Seymour <robyn.seymour@mcsaatchiabel.co.za>")
    dev_own = _row("2026-11-06T10:00:00Z", "C4", "Job Owner", run_id="dev-own",
                   operator_email="katekani@dydx.digital")
    rows = [mine_ok, mine_failed, other_involving_me, dev_own]

    class FakeAuditStore:
        retention_days = 365

        def recent(self, *, limit=0, days=None):
            return list(rows)

    monkeypatch.setattr(web, "AuditStore", FakeAuditStore)
    monkeypatch.setattr(web.time, "time", lambda: 1794000000.0)  # 2026-11-06

    def change_ids(email):
        client = _client(monkeypatch)
        client.post("/api/auth", json={"email": email})
        notes = client.get("/api/notifications").get_json()["notifications"]
        return {n["id"] for n in notes if n["kind"] == "change"}

    robyn = change_ids("robyn.seymour@mcsaatchiabel.co.za")
    assert "change:mine-ok" not in robyn          # own success: hidden
    assert "change:mine-failed" in robyn          # own failure: kept
    assert "change:other" in robyn                # someone else's change involving her: kept

    kate = change_ids("katekani@dydx.digital")
    assert "change:dev-own" not in kate           # dydx users too
    assert "change:mine-ok" in kate               # but they still see everyone else's

    # History is unchanged: your own changes are all still there.
    client = _client(monkeypatch)
    client.post("/api/auth", json={"email": "robyn.seymour@mcsaatchiabel.co.za"})
    assert client.get("/api/history?days=all").get_json()["change_count"] == 4
