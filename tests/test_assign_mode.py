"""Tests for the client-match assign mode (plan_assign_card + card_matches_text)."""
import json

import swap_assignee.web as web
from swap_assignee.swap_engine import card_matches_text, is_review_task_card, plan_assign_card
from swap_assignee.user_resolver import User


NEW = "300222"


def _card(title="Acme — Q3 banners", phase="Doing", fields=None):
    return {
        "id": "C1", "title": title, "url": "https://example.test/C1",
        "current_phase": {"id": "p", "name": phase},
        "assignees": [{"id": "300111", "name": "Kate"}],
        "fields": fields if fields is not None else [],
    }


def _role_field(ids_names, fid="creative_specialist", label="Creative Specialist"):
    return {
        "name": fid,
        "value": json.dumps([i for i, _ in ids_names]),
        "array_value": [{"id": i, "name": n} for i, n in ids_names],
        "field": {"id": fid, "label": label, "type": "assignee_select"},
    }


# ---------- card_matches_text ----------

def test_matches_title_case_insensitive():
    assert card_matches_text(_card(title="ACME rebrand"), "acme")
    assert card_matches_text(_card(title="Something for Acme Corp"), "ACME")


def test_matches_client_field():
    card = _card(title="Q3 banners", fields=[
        {"name": "client", "value": "Acme Corp", "array_value": None,
         "field": {"id": "client", "label": "Client", "type": "short_text"}},
    ])
    assert card_matches_text(card, "acme")


def test_no_match_and_empty_text():
    assert not card_matches_text(_card(title="Globex refresh"), "acme")
    assert not card_matches_text(_card(), "")
    assert not card_matches_text(_card(), "   ")


# ---------- plan_assign_card ----------

def test_assign_appends_to_role_field_keeping_existing():
    card = _card(fields=[_role_field([("300999", "Existing")])])
    plan = plan_assign_card(card, new_id=NEW, field_ids={"creative_specialist"})
    assert len(plan.actions) == 1
    a = plan.actions[0]
    assert a.scope == "field:creative_specialist"
    assert a.mode == "add_only"
    assert a.old_ids == ["300999"]
    assert a.new_ids == ["300999", NEW]


def test_assign_idempotent_when_already_present():
    card = _card(fields=[_role_field([(NEW, "Robyn")])])
    plan = plan_assign_card(card, new_id=NEW, field_ids={"creative_specialist"})
    assert plan.actions == []


def test_assign_never_touches_card_level():
    card = _card(fields=[_role_field([])])
    plan = plan_assign_card(card, new_id=NEW, field_ids={"creative_specialist"})
    assert not any(a.scope == "card" for a in plan.actions)


def test_assign_plans_empty_field_missing_from_card_via_meta():
    # Pipefy omits empty fields from the card payload; field_meta fills the gap.
    card = _card(fields=[])
    meta = {"creative_specialist": {"id": "creative_specialist", "label": "Creative Specialist"}}
    plan = plan_assign_card(
        card, new_id=NEW, field_ids={"creative_specialist"}, field_meta=meta,
        id_names={NEW: "Robyn"},
    )
    assert len(plan.actions) == 1
    a = plan.actions[0]
    assert a.old_ids == []
    assert a.new_ids == [NEW]
    assert a.field_label == "Creative Specialist"
    assert a.new_names == ["Robyn"]


def test_assign_skips_excluded_phase():
    card = _card(phase="Completed", fields=[_role_field([])])
    plan = plan_assign_card(
        card, new_id=NEW, field_ids={"creative_specialist"},
        excluded_phases=["Completed"],
        field_meta={"creative_specialist": {"label": "Creative Specialist"}},
    )
    assert plan.skipped_reason
    assert plan.actions == []


def test_assign_ignores_unselected_fields():
    card = _card(fields=[_role_field([("300999", "Existing")], fid="other_role", label="Other")])
    plan = plan_assign_card(card, new_id=NEW, field_ids={"creative_specialist"})
    assert plan.actions == []


# ---------- web endpoint ----------

def _assign_client(monkeypatch, cards):
    user = User(id=NEW, name="Robyn", email="robyn@dydx.digital")
    monkeypatch.setattr(web, "KNOWN_PIPES", [{"id": "1", "name": "Creative"}])
    monkeypatch.setattr(
        web, "_discover_resolve_user",
        lambda client, pipes, user_input, org_id: user,
    )
    monkeypatch.setattr(
        web, "discover_assignee_fields",
        lambda client, pipe_id: {
            "creative_specialist": {
                "id": "creative_specialist", "label": "Creative Specialist",
                "type": "assignee_select", "phase": "Doing",
            },
        },
    )
    monkeypatch.setattr(
        web, "fetch_active_phases",
        lambda client, pipe_id, **kwargs: {
            "active": [{"id": "p", "name": "Doing", "done": False, "cards_count": len(cards)}],
            "skipped": [],
        },
    )
    monkeypatch.setattr(web, "iter_cards", lambda *args, **kwargs: iter(cards))
    # Simulate Pipefy's server-side title search over the same card list.
    monkeypatch.setattr(
        web, "iter_cards_by_title",
        lambda client, pipe_id, title, **kwargs: iter(
            [c for c in cards if title.lower() in (c.get("title") or "").lower()]
        ),
    )
    return web.create_app().test_client()


def test_assign_users_endpoint(monkeypatch):
    client = _assign_client(monkeypatch, [])
    resp = client.post("/api/assign/users", json={"user": "robyn@dydx.digital"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["user"]["id"] == NEW
    assert data["pipes"] == [{"id": "1", "name": "Creative"}]


def test_assign_scan_stream_matches_title_and_client_only(monkeypatch):
    matching = _card(title="Acme — Q3 banners", fields=[_role_field([("300999", "Existing")])])
    non_matching = dict(_card(title="Globex refresh", fields=[]), id="C2")
    client = _assign_client(monkeypatch, [matching, non_matching])
    resp = client.post("/api/assign/scan/stream", json={
        "user": "robyn@dydx.digital",
        "match_text": "acme",
        "pipe_ids": ["1"],
        "roles_by_pipe": {"1": ["creative specialist"]},
        "exclude_phases_by_pipe": {"1": []},
    })
    events = [json.loads(line) for line in resp.get_data(as_text=True).splitlines()]
    plans = [e["plan"] for e in events if e["type"] == "plan"]
    assert len(plans) == 1
    assert plans[0]["card_id"] == "C1"
    action = plans[0]["actions"][0]
    assert action["mode"] == "add_only"
    assert action["old_ids"] == ["300999"]
    assert action["new_ids"] == ["300999", NEW]
    done = next(e for e in events if e["type"] == "pipe_done")
    assert done["operation"] == "add"
    assert done["plans"] == 1
    assert done["token"]


def test_assign_scan_stream_requires_inputs(monkeypatch):
    client = _assign_client(monkeypatch, [])
    resp = client.post("/api/assign/scan/stream", json={"user": "x@dydx.digital"})
    assert resp.status_code == 400


def _scan_body(**extra):
    return {
        "user": "robyn@dydx.digital", "match_text": "acme", "pipe_ids": ["1"],
        "roles_by_pipe": {"1": ["creative specialist"]},
        "exclude_phases_by_pipe": {"1": []}, **extra,
    }


def _plans_from(resp):
    events = [json.loads(line) for line in resp.get_data(as_text=True).splitlines()]
    return [e["plan"] for e in events if e["type"] == "plan"]


def test_assign_client_field_no_longer_matches(monkeypatch):
    # Title-only matching: a client name only in the Client field is not found.
    card = _card(title="Q3 banners", fields=[
        {"name": "client", "value": "Acme Corp", "array_value": None,
         "field": {"id": "client", "label": "Client", "type": "short_text"}},
        _role_field([]),
    ])
    client = _assign_client(monkeypatch, [card])
    assert _plans_from(client.post("/api/assign/scan/stream", json=_scan_body())) == []


def test_assign_title_search_failure_falls_back_to_full_scan(monkeypatch):
    card = _card(title="Acme — Q3 banners", fields=[_role_field([])])
    client = _assign_client(monkeypatch, [card])

    def _boom(*args, **kwargs):
        raise RuntimeError("title search unsupported")

    monkeypatch.setattr(web, "iter_cards_by_title", _boom)
    plans = _plans_from(client.post("/api/assign/scan/stream", json=_scan_body()))
    assert len(plans) == 1


# ---------- review-task exclusion ----------

def _review_status_field(value='["Review Task"]'):
    # Shape observed in prod: value is a JSON-encoded list, array_value holds label ids.
    return {
        "name": "Online review status", "value": value, "array_value": ["316297298"],
        "field": {"id": "ziflow_status", "label": "Online review status", "type": "label_select"},
    }


def test_is_review_task_card_via_online_review_field():
    assert is_review_task_card(_card(fields=[_review_status_field()]))
    assert not is_review_task_card(_card(fields=[_review_status_field('["Approved"]')]))
    assert not is_review_task_card(_card(fields=[]))


def test_is_review_task_card_via_card_label():
    card = _card()
    card["labels"] = [{"id": "1", "name": "Review Task"}]
    assert is_review_task_card(card)
    card["labels"] = [{"id": "1", "name": "Urgent"}]
    assert not is_review_task_card(card)


def test_assign_scan_excludes_review_task_cards(monkeypatch):
    review = _card(title="Acme — review", fields=[_role_field([]), _review_status_field()])
    real = dict(_card(title="Acme — Q3 banners", fields=[_role_field([])]), id="C2")
    client = _assign_client(monkeypatch, [review, real])
    plans = _plans_from(client.post("/api/assign/scan/stream", json=_scan_body()))
    assert [p["card_id"] for p in plans] == ["C2"]


def test_apply_cancel_leaves_plans_planned(monkeypatch):
    import time
    from swap_assignee.swap_engine import CardPlan, PlannedAction

    client = _assign_client(monkeypatch, [])
    plan = CardPlan(
        card_id="C1", card_url="", title="t", phase_name="Doing",
        actions=[PlannedAction("field:f", "f", [], [NEW], field_label="F", mode="add_only")],
    )
    web._PREVIEW_STORE["tok-cancel"] = {
        "created_at": time.time(), "plans": [plan], "throttle_ms": 0,
        "summary": {}, "pipe_id": "1", "pipe_name": "Creative",
        "old_user": "", "new_user": "Robyn", "old_user_id": "", "new_user_id": NEW,
        "new_user_name": "Robyn", "new_user_email": "robyn@dydx.digital", "operation": "add",
    }
    resp = client.post("/api/apply/cancel", json={"apply_id": "ap1"})
    assert resp.status_code == 200
    resp = client.post("/api/apply", json={
        "token": "tok-cancel", "apply_id": "ap1", "operator_email": "op@dydx.digital",
    })
    data = resp.get_json()
    assert data["cancelled"] is True
    assert data["cards_changed"] == 0
    assert all(p["status"] == "planned" for p in data["plans"])
    assert data["remaining"] == 1
    # Flag is cleared, so a follow-up apply of the same id would run normally.
    assert "ap1" not in web._APPLY_CANCEL


def test_apply_cancel_requires_id(monkeypatch):
    client = _assign_client(monkeypatch, [])
    resp = client.post("/api/apply/cancel", json={})
    assert resp.status_code == 400
