"""Replace mode warns when the leaver is Account Manager / Traffic Manager on
jobs whose child cards sit on boards that weren't ticked."""
import json

from swap_assignee import web

from tests.test_web_operations import _client

KNOWN = [{"id": "1", "name": "Account Management"}, {"id": "2", "name": "Creative"},
         {"id": "3", "name": "Production"}]


def _role_field(fid, label):
    return {fid: {"id": fid, "label": label, "type": "assignee_select", "phase": "Doing"}}


def _job(card_id, fid, label):
    return {
        "id": card_id, "title": f"Job {card_id}", "url": "",
        "current_phase": {"id": "p", "name": "Doing"}, "assignees": [],
        "fields": [{"name": fid, "value": '["300111"]', "array_value": [{"id": "300111", "name": "Kate"}],
                    "field": {"id": fid, "label": label, "type": "assignee_select"}}],
    }


def _scan(client, operation="replace"):
    resp = client.post("/api/wizard/scan/stream", json={
        "operation": operation, "old_user": "kate@dydx.digital", "new_user": "robyn@dydx.digital",
        "pipe_ids": ["1"], "exclude_phases_by_pipe": {"1": []},
    })
    return [json.loads(line) for line in resp.get_data(as_text=True).splitlines()]


def test_crucial_roles_detected_by_slug_or_label():
    plan = {"actions": [{"field_id": "trafficker", "field_label": "Trafficker"},
                        {"field_id": "x1", "field_label": "Account  Manager"},
                        {"field_id": "creative_specialist", "field_label": "Creative Specialist"}]}
    assert web.crucial_roles_in_plan(plan) == ["Traffic Manager", "Account Manager"]
    assert web.crucial_roles_in_plan({"actions": [{"field_id": "job_owner", "field_label": "Job Owner"}]}) == []


class FakePipefy:
    def __init__(self, children_by_card):
        self.children = children_by_card
        self.queries = []

    def execute(self, query, variables=None):
        assert query.startswith("query")
        self.queries.append(query)
        out = {}
        for i in range(60):
            key = f'c{i}: card(id: "'
            if key not in query:
                continue
            cid = query.split(key, 1)[1].split('"', 1)[0]
            out[f"c{i}"] = {"id": cid, "child_relations": [{"cards": self.children.get(cid, [])}]}
        return out


def test_child_boards_ignores_selected_boards_lookup_dbs_and_review_tasks():
    fake = FakePipefy({
        "101": [{"id": "c1", "pipe": {"id": "2"}, "labels": []},                 # Creative: unticked -> warn
                {"id": "c2", "pipe": {"id": "1"}, "labels": []},                 # same board, ticked
                {"id": "c3", "pipe": {"id": "306759746"}, "labels": []},         # Agency DB lookup
                {"id": "c4", "pipe": {"id": "3"}, "labels": [{"name": "Review Task"}]}],
        "102": [{"id": "c5", "pipe": {"id": "2"}, "labels": []},
                {"id": "c6", "pipe": {"id": "3"}, "labels": []}],
        "103": [],
    })
    boards = web.find_unselected_child_boards(fake, ["101", "102", "103"], {"1"}, KNOWN)
    assert boards == [{"id": "2", "name": "Creative", "jobs": 2, "cards": 2},
                      {"id": "3", "name": "Production", "jobs": 1, "cards": 1}]


def test_child_boards_batches_25_jobs_per_query():
    fake = FakePipefy({})
    web.find_unselected_child_boards(fake, [str(1000 + i) for i in range(60)], set(), KNOWN)
    assert len(fake.queries) == 3


def test_replace_scan_sends_warning_for_traffic_manager_jobs(monkeypatch):
    client = _client(monkeypatch)
    monkeypatch.setattr(web, "KNOWN_PIPES", KNOWN)
    monkeypatch.setattr(web, "discover_assignee_fields", lambda c, pid: _role_field("trafficker", "Traffic Manager"))
    monkeypatch.setattr(web, "iter_cards_fast_union", lambda *a, **k: iter([_job("101", "trafficker", "Traffic Manager")]))
    seen = {}

    def fake_find(c, card_ids, selected, known, **kw):
        seen.update(card_ids=list(card_ids), selected=set(selected))
        return [{"id": "2", "name": "Creative", "jobs": 1, "cards": 3}]

    monkeypatch.setattr(web, "find_unselected_child_boards", fake_find)
    events = _scan(client)
    warn = next(e for e in events if e["type"] == "child_boards_warning")
    assert warn["boards"][0]["name"] == "Creative" and warn["roles"] == ["Traffic Manager"]
    assert seen == {"card_ids": ["101"], "selected": {"1"}}
    assert [e["type"] for e in events][-1] == "all_done"


def test_no_warning_for_other_roles_or_add_mode(monkeypatch):
    client = _client(monkeypatch)
    monkeypatch.setattr(web, "KNOWN_PIPES", KNOWN)
    calls = []
    monkeypatch.setattr(web, "find_unselected_child_boards", lambda *a, **k: calls.append(a) or [{"id": "2"}])
    # Creative Specialist (not a crucial role) in replace mode
    monkeypatch.setattr(web, "iter_cards_fast_union",
                        lambda *a, **k: iter([_job("101", "creative_specialist", "Creative Specialist")]))
    assert not [e for e in _scan(client) if e["type"] == "child_boards_warning"]
    # Traffic Manager, but in add (co-assignee) mode
    monkeypatch.setattr(web, "discover_assignee_fields", lambda c, pid: _role_field("trafficker", "Traffic Manager"))
    monkeypatch.setattr(web, "iter_cards_fast_union", lambda *a, **k: iter([_job("101", "trafficker", "Traffic Manager")]))
    assert not [e for e in _scan(client, operation="add") if e["type"] == "child_boards_warning"]
    assert calls == []


def test_a_failing_check_never_breaks_the_scan(monkeypatch):
    client = _client(monkeypatch)
    monkeypatch.setattr(web, "KNOWN_PIPES", KNOWN)
    monkeypatch.setattr(web, "discover_assignee_fields", lambda c, pid: _role_field("trafficker", "Traffic Manager"))
    monkeypatch.setattr(web, "iter_cards_fast_union", lambda *a, **k: iter([_job("101", "trafficker", "Traffic Manager")]))

    def boom(*a, **k):
        raise RuntimeError("Pipefy timeout")

    monkeypatch.setattr(web, "find_unselected_child_boards", boom)
    events = _scan(client)
    assert any(e["type"] == "plan" for e in events)
    assert events[-1]["type"] == "all_done"
    assert not any(e["type"] == "child_boards_warning" for e in events)
