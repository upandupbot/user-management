import json
import time

import pytest

from swap_assignee import audit
from swap_assignee.audit import (
    AuditStore,
    build_records,
    is_allowed_email,
    normalize_email,
    record_swap,
)


# ---------- email allow-list ----------

@pytest.mark.parametrize(
    "email",
    [
        "alice@upandupgroup.com",
        "Bob.Jones@dydx.digital",
        "  carla@mcsaatchiabel.co.za  ",
        "dee@upandupconnect.com",
        "ed@inblackandwhite.co.za",
        "fay@levergy.co.za",
        "gus@razorpr.co.za",
    ],
)
def test_allowed_emails_pass(email):
    assert is_allowed_email(email) is True


@pytest.mark.parametrize(
    "email",
    [
        "",
        None,
        "nodomain",
        "person@gmail.com",
        "person@upandupgroup.com.evil.com",
        "person@evil.upandupgroup.com",  # subdomain is not the allowed domain
        "@upandupgroup.com",
        "person@",
        "two@@dydx.digital",
    ],
)
def test_disallowed_emails_fail(email):
    assert is_allowed_email(email) is False


def test_normalize_email_lowercases_and_trims():
    assert normalize_email("  ALICE@UpAndUpGroup.com ") == "alice@upandupgroup.com"
    assert normalize_email(None) == ""


# ---------- build_records ----------

def _plan_dict():
    return {
        "card_id": "C1",
        "card_url": "https://app.pipefy.com/open-cards/C1",
        "title": "Acme retainer",
        "phase_name": "In Progress",
        "client": "Acme",
        "actions": [
            {
                "scope": "card",
                "field_label": None,
                "old_ids": ["300111"],
                "new_ids": ["300222"],
                "old_names": ["Old User"],
                "new_names": ["New User"],
                "mode": "swap",
            },
            {
                "scope": "field:creative",
                "field_label": "Creative Specialist",
                "old_ids": ["300111"],
                "new_ids": ["300222"],
                "old_names": ["Old User"],
                "new_names": ["New User"],
                "mode": "swap",
            },
        ],
    }


def test_build_records_one_per_action():
    recs = build_records(
        operator_email="alice@upandupgroup.com",
        source="cli",
        pipe_id="305",
        pipe_name="Delivery",
        old_user="Old User <old@x.com>",
        new_user="New User <new@x.com>",
        plan_dict=_plan_dict(),
        status="ok",
    )
    assert len(recs) == 2
    card_rec = next(r for r in recs if r["scope"] == "card")
    assert card_rec["operator_email"] == "alice@upandupgroup.com"
    assert card_rec["card_id"] == "C1"
    assert card_rec["client"] == "Acme"
    assert card_rec["before"] == "Old User"
    assert card_rec["after"] == "New User"
    assert card_rec["operation"] == "replace"
    field_rec = next(r for r in recs if r["scope"].startswith("field:"))
    assert field_rec["field_label"] == "Creative Specialist"
    # Every record carries a timestamp + status.
    assert all(r["ts"] and r["status"] == "ok" for r in recs)


def test_build_records_captures_add_operation():
    plan = _plan_dict()
    plan["actions"] = [{
        "scope": "field:creative",
        "field_label": "Creative Specialist",
        "old_names": ["Old User", "Existing User"],
        "new_names": ["Old User", "Existing User", "New User"],
        "mode": "add_only",
    }]
    recs = build_records(
        operator_email="alice@upandupgroup.com",
        source="web",
        pipe_id="305",
        pipe_name="Delivery",
        old_user="Old User <old@x.com>",
        new_user="New User <new@x.com>",
        plan_dict=plan,
        status="ok",
        operation="add",
    )
    assert recs[0]["operation"] == "add"
    assert recs[0]["before"] == "Old User, Existing User"
    assert recs[0]["after"] == "Old User, Existing User, New User"


def test_build_records_no_actions_still_logs_summary():
    pd = {"card_id": "C9", "title": "x", "phase_name": "P", "actions": []}
    recs = build_records(
        operator_email="a@dydx.digital",
        source="web",
        pipe_id="1",
        pipe_name="",
        old_user="",
        new_user="",
        plan_dict=pd,
        status="error",
        error="boom",
    )
    assert len(recs) == 1
    assert recs[0]["status"] == "error"
    assert recs[0]["error"] == "boom"


# ---------- AuditStore ----------

def test_store_append_and_recent(tmp_path):
    store = AuditStore(tmp_path / "history.jsonl")
    recs = build_records(
        operator_email="alice@upandupgroup.com",
        source="cli",
        pipe_id="305",
        pipe_name="Delivery",
        old_user="Old",
        new_user="New",
        plan_dict=_plan_dict(),
        status="ok",
    )
    n = store.append(recs)
    assert n == 2
    rows = store.recent(limit=10)
    assert len(rows) == 2
    # Newest first: the file is appended in order, recent() reverses.
    assert rows[0]["card_id"] == "C1"


def test_store_prunes_old_records(tmp_path):
    path = tmp_path / "history.jsonl"
    store = AuditStore(path, retention_days=30)
    old_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 40 * 86400))
    new_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    # Seed an old record directly.
    path.write_text(json.dumps({"ts": old_ts, "card_id": "OLD", "status": "ok"}) + "\n", encoding="utf-8")
    # Appending a fresh record should prune the 40-day-old one.
    store.append([{"ts": new_ts, "card_id": "NEW", "status": "ok"}])
    rows = store.recent(limit=10)
    ids = {r["card_id"] for r in rows}
    assert "NEW" in ids
    assert "OLD" not in ids


def test_store_recent_days_filter(tmp_path):
    path = tmp_path / "history.jsonl"
    store = AuditStore(path, retention_days=3650)  # don't auto-prune
    old_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 10 * 86400))
    new_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    store.append([{"ts": old_ts, "card_id": "OLD", "status": "ok"}])
    store.append([{"ts": new_ts, "card_id": "NEW", "status": "ok"}])
    rows = store.recent(limit=10, days=3)
    ids = {r["card_id"] for r in rows}
    assert ids == {"NEW"}


# ---------- record_swap (no network) ----------

def test_record_swap_writes_local_and_skips_empty_webhook(tmp_path, monkeypatch):
    store = AuditStore(tmp_path / "history.jsonl")
    calls = {"n": 0}

    def _fake_post(url, records, timeout=5.0):
        calls["n"] += 1
        return False

    monkeypatch.setattr(audit, "post_to_webhook", _fake_post)
    out = record_swap(
        store=store,
        webhook_url=None,
        operator_email="alice@upandupgroup.com",
        source="web",
        pipe_id="305",
        pipe_name="Delivery",
        old_user="Old",
        new_user="New",
        plan_dict=_plan_dict(),
        status="ok",
    )
    assert len(out) == 2
    assert store.recent(limit=10)
    # post_to_webhook is always attempted (it no-ops internally on empty URL).
    assert calls["n"] == 1


# ---------- Google Sheets sink ----------

def test_build_sheet_sink_returns_none_when_unconfigured():
    from swap_assignee.sheets_sink import build_sheet_sink

    assert build_sheet_sink(None, None) is None
    assert build_sheet_sink("sheet-id", None) is None
    assert build_sheet_sink(None, "key.json") is None


def test_build_sheet_sink_returns_none_for_missing_key_file(tmp_path):
    from swap_assignee.sheets_sink import build_sheet_sink

    missing = tmp_path / "does-not-exist.json"
    assert build_sheet_sink("sheet-id", str(missing)) is None


def test_record_swap_mirrors_to_sheet_sink(tmp_path, monkeypatch):
    store = AuditStore(tmp_path / "history.jsonl")
    captured = {}

    class FakeSink:
        def append(self, records):
            captured["records"] = list(records)
            return True

    # No webhook so we isolate the sheet path.
    monkeypatch.setattr(audit, "post_to_webhook", lambda *a, **k: False)
    out = record_swap(
        store=store,
        webhook_url=None,
        operator_email="alice@upandupgroup.com",
        source="web",
        pipe_id="305",
        pipe_name="Delivery",
        old_user="Old",
        new_user="New",
        plan_dict=_plan_dict(),
        status="ok",
        sheet_sink=FakeSink(),
    )
    assert len(out) == 2
    assert len(captured["records"]) == 2
    assert captured["records"][0]["operator_email"] == "alice@upandupgroup.com"


def test_record_swap_survives_sheet_sink_failure(tmp_path, monkeypatch):
    """A failing Sheets sink must never break the apply or lose the local row."""
    store = AuditStore(tmp_path / "history.jsonl")

    class BoomSink:
        def append(self, records):
            raise RuntimeError("sheets down")

    monkeypatch.setattr(audit, "post_to_webhook", lambda *a, **k: False)
    out = record_swap(
        store=store,
        webhook_url=None,
        operator_email="alice@upandupgroup.com",
        source="cli",
        pipe_id="305",
        pipe_name="Delivery",
        old_user="Old",
        new_user="New",
        plan_dict=_plan_dict(),
        status="ok",
        sheet_sink=BoomSink(),
    )
    assert len(out) == 2
    # Local store still has the records despite the sink blowing up.
    assert len(store.recent(limit=10)) == 2

