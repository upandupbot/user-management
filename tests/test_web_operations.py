import json

import swap_assignee.web as web
from swap_assignee.user_resolver import User


def _client(monkeypatch):
    old_user = User(id="300111", name="Kate", email="kate@dydx.digital")
    new_user = User(id="300222", name="Robyn", email="robyn@dydx.digital")
    monkeypatch.setattr(web, "KNOWN_PIPES", [{"id": "1", "name": "Creative"}])
    monkeypatch.setattr(
        web,
        "fetch_pipe_context",
        lambda client, pipe_id, **kwargs: {
            "pipe_id": pipe_id,
            "pipe_name": "Creative",
            "org_id": "9",
            "members": [old_user, new_user],
        },
    )
    monkeypatch.setattr(web, "fetch_org_users", lambda client, org_id, **kwargs: [old_user, new_user])
    monkeypatch.setattr(
        web,
        "_resolve_users",
        lambda client, pipe_id, old, new, org, **kwargs: (
            {
                "pipe_id": pipe_id,
                "pipe_name": "Creative",
                "org_id": "9",
                "members": [old_user, new_user],
            },
            old_user,
            new_user,
        ),
    )
    monkeypatch.setattr(
        web,
        "discover_assignee_fields",
        lambda client, pipe_id: {
            "creative_specialist": {
                "id": "creative_specialist",
                "label": "Creative Specialist",
                "type": "assignee_select",
                "phase": "Doing",
            }
        },
    )
    monkeypatch.setattr(
        web,
        "fetch_active_phases",
        lambda client, pipe_id, **kwargs: {
            "active": [{"id": "p", "name": "Doing", "done": False, "cards_count": 1}],
            "skipped": [],
        },
    )
    card = {
        "id": "C1",
        "title": "Client job",
        "url": "https://example.test/C1",
        "current_phase": {"id": "p", "name": "Doing"},
        "assignees": [],
        "fields": [{
            "name": "creative_specialist",
            "value": '["300111", "300999"]',
            "array_value": [
                {"id": "300111", "name": "Kate"},
                {"id": "300999", "name": "Existing"},
            ],
            "field": {
                "id": "creative_specialist",
                "label": "Creative Specialist",
                "type": "assignee_select",
            },
        }],
    }
    monkeypatch.setattr(web, "iter_cards_fast_union", lambda *args, **kwargs: iter([card]))
    return web.create_app().test_client()


def test_wizard_scan_rejects_invalid_operation(monkeypatch):
    client = _client(monkeypatch)
    response = client.post("/api/wizard/scan/stream", json={
        "operation": "merge",
        "old_user": "kate@dydx.digital",
        "new_user": "robyn@dydx.digital",
        "pipe_ids": ["1"],
    })
    assert response.status_code == 400
    assert response.get_json()["error"] == "operation must be 'replace' or 'add'."


def test_wizard_add_stream_preserves_existing_assignees(monkeypatch):
    client = _client(monkeypatch)
    response = client.post("/api/wizard/scan/stream", json={
        "operation": "add",
        "old_user": "kate@dydx.digital",
        "new_user": "robyn@dydx.digital",
        "pipe_ids": ["1"],
        "roles_by_pipe": {"1": ["creative specialist"]},
        "exclude_phases_by_pipe": {"1": []},
    })
    events = [json.loads(line) for line in response.get_data(as_text=True).splitlines()]
    plan = next(event["plan"] for event in events if event["type"] == "plan")
    action = plan["actions"][0]
    done = next(event for event in events if event["type"] == "pipe_done")
    assert action["mode"] == "add_only"
    assert action["old_ids"] == ["300111", "300999"]
    assert action["new_ids"] == ["300111", "300999", "300222"]
    assert done["operation"] == "add"


def test_history_api_exposes_filters_and_retention(monkeypatch):
    client = _client(monkeypatch)
    response = client.get("/api/history?days=365&board=Creative&client=Acme&user=Kate")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["retention_days"] >= 365
    assert payload["records"] == []
    assert payload["source"] == "local"


def _history_records():
    return [
        {"ts": "2026-11-09T10:00:00Z", "pipe_name": "Creative", "pipe_id": "1", "client": "Acme",
         "old_user": "Magda <magda@levergy.co.za>", "new_user": "Sim <sim@levergy.co.za>", "status": "ok"},
        {"ts": "2025-01-15T10:00:00Z", "pipe_name": "Media", "pipe_id": "2", "client": "Globex",
         "old_user": "", "new_user": "Kate <kate@dydx.digital>", "status": "ok"},
    ]


def test_history_all_time_and_custom_dates(monkeypatch):
    records = _history_records()

    class FakeAuditStore:
        retention_days = 365

        def recent(self, *, limit=0, days=None):
            return list(records)

    monkeypatch.setattr(web, "AuditStore", FakeAuditStore)
    client = _client(monkeypatch)
    all_rows = client.get("/api/history?days=all").get_json()["records"]
    assert len(all_rows) == 2
    custom = client.get("/api/history?from=2025-01-01&to=2025-01-31").get_json()["records"]
    assert [r["client"] for r in custom] == ["Globex"]
    by_user = client.get("/api/history?days=all&user=magda").get_json()["records"]
    assert [r["client"] for r in by_user] == ["Acme"]


def test_history_reads_shared_sheet_when_configured(monkeypatch):
    class FakeSheet:
        def recent(self, *, limit=0, days=None):
            return _history_records()

        def append(self, rows):
            return True

    monkeypatch.setattr(web, "build_sheet_sink", lambda *a, **kw: FakeSheet())
    client = _client(monkeypatch)
    payload = client.get("/api/history?days=all").get_json()
    assert payload["source"] == "sheet"
    assert len(payload["records"]) == 2


def test_developer_access_is_limited_to_dydx_domain(monkeypatch):
    client = _client(monkeypatch)

    assert client.post("/api/auth", json={"email": "operator@upandupgroup.com"}).status_code == 200
    session = client.get("/api/session").get_json()
    assert session["developer"] is False
    assert client.post("/api/logs", json={"since": 0}).status_code == 403

    client = _client(monkeypatch)
    assert client.post("/api/auth", json={"email": "developer@dydx.digital"}).status_code == 200
    session = client.get("/api/session").get_json()
    assert session["developer"] is True
    assert client.post("/api/logs", json={"since": 0}).status_code == 200


def test_notifications_api_returns_notification_collection(monkeypatch):
    client = _client(monkeypatch)
    response = client.get("/api/notifications")
    assert response.status_code == 200
    payload = response.get_json()
    assert isinstance(payload["notifications"], list)


def test_notifications_are_scoped_by_viewer_agency(monkeypatch):
    records = [
        {
            "ts": "2026-11-10T10:00:00Z", "operator_email": "simphiwe@levergy.co.za",
            "old_user": "Magda <magda@levergy.co.za>", "new_user": "Simphiwe <simphiwe@levergy.co.za>",
            "pipe_id": "1", "card_id": "C1", "pipe_name": "Creative", "client": "Acme",
            "operation": "replace", "status": "ok",
        },
        {
            "ts": "2026-11-10T09:00:00Z", "operator_email": "operator@upandupgroup.com",
            "old_user": "Old <old@upandupgroup.com>", "new_user": "New <new@upandupgroup.com>",
            "pipe_id": "1", "card_id": "C2", "pipe_name": "Creative", "client": "Globex",
            "operation": "replace", "status": "ok",
        },
    ]

    class FakeAuditStore:
        retention_days = 365

        def recent(self, **kwargs):
            return records

    monkeypatch.setattr(web, "AuditStore", FakeAuditStore)

    client = _client(monkeypatch)
    client.post("/api/auth", json={"email": "person@levergy.co.za"})
    levergy = client.get("/api/notifications").get_json()["notifications"]
    assert len(levergy) == 1
    assert "Magda" in levergy[0]["message"]
    assert "Globex" not in levergy[0]["message"]

    client = _client(monkeypatch)
    client.post("/api/auth", json={"email": "developer@dydx.digital"})
    dydx = client.get("/api/notifications").get_json()["notifications"]
    assert len(dydx) >= 2


def test_wizard_scan_age_cutoff_skips_old_cards(monkeypatch):
    client = _client(monkeypatch)
    old_card = {
        "id": "C8",
        "title": "Ancient job",
        "url": "",
        "created_at": "2021-03-01T00:00:00Z",
        "current_phase": {"id": "p", "name": "Doing"},
        "assignees": [],
        "fields": [{
            "name": "creative_specialist",
            "value": '["300111"]',
            "array_value": [{"id": "300111", "name": "Kate"}],
            "field": {"id": "creative_specialist", "label": "Creative Specialist", "type": "assignee_select"},
        }],
    }
    monkeypatch.setattr(web, "iter_cards_fast_union", lambda *args, **kwargs: iter([old_card]))
    response = client.post("/api/wizard/scan/stream", json={
        "operation": "replace",
        "old_user": "kate@dydx.digital",
        "new_user": "robyn@dydx.digital",
        "pipe_ids": ["1"],
        "roles_by_pipe": {"1": ["creative specialist"]},
        "exclude_phases_by_pipe": {"1": []},
        "max_age_days": 365,
    })
    events = [json.loads(line) for line in response.get_data(as_text=True).splitlines()]
    assert not [e for e in events if e["type"] == "plan"]
    summary = next(e for e in events if e["type"] == "summary")
    assert summary["cards_in_excluded"] == 1


def test_wizard_scan_excludes_review_task_cards(monkeypatch):
    client = _client(monkeypatch)
    review_card = {
        "id": "C9",
        "title": "Review this",
        "url": "",
        "current_phase": {"id": "p", "name": "Doing"},
        "assignees": [],
        "fields": [
            {
                "name": "creative_specialist",
                "value": '["300111"]',
                "array_value": [{"id": "300111", "name": "Kate"}],
                "field": {"id": "creative_specialist", "label": "Creative Specialist", "type": "assignee_select"},
            },
            {
                "name": "Online review status",
                "value": '["Review Task"]',
                "array_value": ["316297298"],
                "field": {"id": "ziflow_status", "label": "Online review status", "type": "label_select"},
            },
        ],
    }
    monkeypatch.setattr(web, "iter_cards_fast_union", lambda *args, **kwargs: iter([review_card]))
    response = client.post("/api/wizard/scan/stream", json={
        "operation": "replace",
        "old_user": "kate@dydx.digital",
        "new_user": "robyn@dydx.digital",
        "pipe_ids": ["1"],
        "roles_by_pipe": {"1": ["creative specialist"]},
        "exclude_phases_by_pipe": {"1": []},
    })
    events = [json.loads(line) for line in response.get_data(as_text=True).splitlines()]
    assert not [e for e in events if e["type"] == "plan"]
    summary = next(e for e in events if e["type"] == "summary")
    assert summary["cards_in_excluded"] == 1
