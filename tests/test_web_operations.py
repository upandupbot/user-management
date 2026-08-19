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
