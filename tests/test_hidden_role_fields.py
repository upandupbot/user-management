"""The web app never offers or changes the Reviewer and blank_assignee fields,
and review tasks are recognised by either status field ID."""
import json

from swap_assignee import web
from swap_assignee.swap_engine import is_review_task_card

from tests.test_web_operations import _client


def _meta(fid, label):
    return {"id": fid, "label": label, "type": "assignee_select", "phase": "Start form"}


DISCOVERED = {
    "reviewer": _meta("reviewer", "Reviewer"),
    "blank_assignee": _meta("blank_assignee", "blank_assignee"),
    "responsible_reviewer": _meta("responsible_reviewer", "Responsible - Reviewer"),
    "creative_internal_reviewer_s": _meta("creative_internal_reviewer_s", "Creative internal reviewer(s)"),
    "trafficker": _meta("trafficker", "Traffic Manager"),
}


def test_only_the_exact_fields_are_hidden():
    assert set(web.visible_role_fields(DISCOVERED)) == {
        "responsible_reviewer", "creative_internal_reviewer_s", "trafficker"}
    assert web.is_hidden_role_field("x9", "Reviewer")          # by label too
    assert web.is_hidden_role_field("x9", "blank assignee")
    assert not web.is_hidden_role_field("reviewer_1", "Reviewers")


def _card_with(fid, label):
    return {
        "id": "C1", "title": "Job", "url": "", "current_phase": {"id": "p", "name": "Doing"}, "assignees": [],
        "fields": [{"name": fid, "value": '["300111"]', "array_value": [{"id": "300111", "name": "Kate"}],
                    "field": {"id": fid, "label": label, "type": "assignee_select"}}],
    }


def test_scan_never_plans_reviewer_or_blank_assignee_changes(monkeypatch):
    client = _client(monkeypatch)
    monkeypatch.setattr(web, "discover_assignee_fields", lambda c, pid: dict(DISCOVERED))
    seen_fields = []

    def fake_cards(*a, **k):
        seen_fields.append(set(k.get("field_ids") or ()))
        return iter([_card_with("reviewer", "Reviewer"), dict(_card_with("blank_assignee", "blank_assignee"), id="C2")])

    monkeypatch.setattr(web, "iter_cards_fast_union", fake_cards)
    resp = client.post("/api/wizard/scan/stream", json={
        "operation": "replace", "old_user": "kate@dydx.digital", "new_user": "robyn@dydx.digital",
        "pipe_ids": ["1"], "exclude_phases_by_pipe": {"1": []},
    })
    events = [json.loads(line) for line in resp.get_data(as_text=True).splitlines()]
    assert not [e for e in events if e["type"] == "plan"]
    assert seen_fields and not ({"reviewer", "blank_assignee"} & seen_fields[0])


def test_hidden_ids_are_stripped_even_if_a_request_names_them(monkeypatch):
    client = _client(monkeypatch)
    seen = []
    monkeypatch.setattr(web, "iter_cards_fast_union",
                        lambda *a, **k: (seen.append(set(k.get("field_ids") or ())), iter([]))[1])
    client.post("/api/preview/stream", json={
        "pipe_id": "1", "old_user": "kate@dydx.digital", "new_user": "robyn@dydx.digital",
        "field_ids": ["reviewer", "blank_assignee", "trafficker"], "include_card_level": False,
    }).get_data()
    assert seen == [{"trafficker"}]


def test_review_task_recognised_by_online_review_status_id():
    card = {"labels": [], "fields": [{"name": "online_review_status", "value": '["Review Task"]',
                                      "field": {"id": "online_review_status", "label": "Status"}}]}
    assert is_review_task_card(card)
    card["fields"][0]["value"] = '["In Review: Client Presentation"]'
    assert not is_review_task_card(card)
