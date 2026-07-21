"""Cover (co-assignee undo) tests: remove planning, store logic, web endpoints."""
from __future__ import annotations

import copy

import swap_assignee.web as web
from swap_assignee.cover_store import (
    BATCH_COLUMNS,
    BATCH_TAB,
    CARD_COLUMNS,
    CARD_TAB,
    CoverStore,
)
from swap_assignee.swap_engine import PlannedAction, compute_remove, plan_remove_card
from swap_assignee.user_resolver import User


# ---------- compute_remove ----------

def test_compute_remove_takes_user_out():
    assert compute_remove(["1", "2", "3"], "2") == ["1", "3"]


def test_compute_remove_absent_user_is_noop():
    assert compute_remove(["1", "3"], "2") is None


def test_compute_remove_can_empty_the_list():
    assert compute_remove(["2"], "2") == []


# ---------- plan_remove_card ----------

def _card(field_ids=("300111", "300222"), assignees=("300222",)):
    return {
        "id": "C1",
        "title": "Client job",
        "url": "https://example.test/C1",
        "current_phase": {"id": "p", "name": "Doing"},
        "assignees": [{"id": a, "name": f"User {a}"} for a in assignees],
        "fields": [
            {
                "name": "creative_specialist",
                "value": None,
                "array_value": [{"id": i, "name": f"User {i}"} for i in field_ids],
                "field": {"id": "creative_specialist", "label": "Creative Specialist", "type": "assignee_select"},
            },
            {
                "name": "reviewer",
                "value": None,
                "array_value": [{"id": "300222", "name": "User 300222"}],
                "field": {"id": "reviewer", "label": "Reviewer", "type": "assignee_select"},
            },
        ],
    }


def test_plan_remove_only_touches_recorded_fields():
    plan = plan_remove_card(_card(), user_id="300222", field_ids={"creative_specialist"})
    assert len(plan.actions) == 1
    a = plan.actions[0]
    assert a.scope == "field:creative_specialist"
    assert a.mode == "remove"
    assert a.old_ids == ["300111", "300222"]
    assert a.new_ids == ["300111"]  # everyone else stays


def test_plan_remove_card_level_when_recorded():
    plan = plan_remove_card(
        _card(), user_id="300222", field_ids=set(), include_card_level=True
    )
    assert [a.scope for a in plan.actions] == ["card"]
    assert plan.actions[0].new_ids == []


def test_plan_remove_is_noop_when_user_already_gone():
    plan = plan_remove_card(
        _card(field_ids=("300111",), assignees=()),
        user_id="300222",
        field_ids={"creative_specialist"},
        include_card_level=True,
    )
    assert plan.actions == []


# ---------- CoverStore logic (sheet I/O faked in memory) ----------

class MemStore(CoverStore):
    def __init__(self):  # deliberately no super().__init__ (no Google client)
        self.sheet_id = "test-sheet"
        self._ensured = True
        self.tabs = {BATCH_TAB: [], CARD_TAB: []}

    def _ensure(self):
        pass

    def _read_rows(self, tab, columns):
        out = []
        for i, rec in enumerate(self.tabs[tab]):
            r = dict(rec)
            r["_row"] = i + 2
            out.append(r)
        return out

    def _append_rows(self, tab, columns, records):
        for rec in records:
            self.tabs[tab].append({c: str(rec.get(c) or "") for c in columns})

    def _update_cells(self, tab, col_idx, row_values):
        cols = BATCH_COLUMNS if tab == BATCH_TAB else CARD_COLUMNS
        for row, val in row_values.items():
            self.tabs[tab][row - 2][cols[col_idx]] = val


def _batch(bid="b1"):
    return {
        "batch_id": bid,
        "name": "Sam covering Alice",
        "leaver": "Alice <alice@x.com>",
        "cover_user_id": "300222",
        "cover_user_name": "Sam",
        "cover_user_email": "sam@x.com",
        "start_date": "2026-07-24",
        "end_date": "2026-08-04",
        "created_by": "op@upandupgroup.com",
    }


def _rows(card_ids=("C1", "C2")):
    return [
        {
            "pipe_id": "1", "pipe_name": "Creative", "card_id": cid,
            "card_url": f"https://x/{cid}", "title": f"Job {cid}", "client": "Acme",
            "product": "", "scope": "field:creative_specialist",
            "field_id": "creative_specialist", "field_label": "Creative Specialist",
        }
        for cid in card_ids
    ]


def test_store_record_writes_batch_once_across_applies():
    store = MemStore()
    store.record(_batch(), _rows(("C1",)))
    store.record(_batch(), _rows(("C2",)))  # second pipe apply, same batch
    assert len(store.tabs[BATCH_TAB]) == 1
    assert len(store.tabs[CARD_TAB]) == 2
    assert all(r["status"] == "active" for r in store.tabs[CARD_TAB])


def test_store_list_batches_counts_and_pipes():
    store = MemStore()
    store.record(_batch(), _rows(("C1", "C2")))
    batches = store.list_batches()
    assert len(batches) == 1
    b = batches[0]
    assert b["cards_active"] == 2
    assert b["cards_removed"] == 0
    assert b["pipe_names"] == ["Creative"]


def test_store_mark_removed_partial_keeps_batch_active():
    store = MemStore()
    store.record(_batch(), _rows(("C1", "C2")))
    ended = store.mark_cards_removed("b1", {"C1"})
    assert ended is False
    assert [r["status"] for r in store.tabs[CARD_TAB]] == ["removed", "active"]
    assert store.tabs[BATCH_TAB][0]["status"] == "active"
    assert [c["card_id"] for c in store.list_cards("b1")] == ["C2"]


def test_store_mark_removed_all_ends_batch():
    store = MemStore()
    store.record(_batch(), _rows(("C1", "C2")))
    ended = store.mark_cards_removed("b1", {"C1", "C2"})
    assert ended is True
    assert store.tabs[BATCH_TAB][0]["status"] == "ended"
    assert store.list_batches() == []  # active filter hides ended batches


# ---------- web endpoints ----------

def _fake_execute_plan(client, plan, **kwargs):
    return [(action, None) for action in plan.actions]


def _web_client(monkeypatch, store):
    old_user = User(id="300111", name="Alice", email="alice@dydx.digital")
    new_user = User(id="300222", name="Sam", email="sam@dydx.digital")
    card = _card(field_ids=("300111",), assignees=())
    monkeypatch.setattr(web, "KNOWN_PIPES", [{"id": "1", "name": "Creative"}])
    monkeypatch.setattr(
        web, "_resolve_users",
        lambda client, pipe_id, old, new, org, **kw: (
            {"pipe_id": pipe_id, "pipe_name": "Creative", "org_id": "9",
             "members": [old_user, new_user]},
            old_user, new_user,
        ),
    )
    monkeypatch.setattr(web, "iter_cards_fast_union", lambda *a, **kw: iter([copy.deepcopy(card)]))
    monkeypatch.setattr(
        web, "fetch_active_phases",
        lambda client, pipe_id, **kw: {"active": [{"id": "p", "name": "Doing", "done": False, "cards_count": 1}], "skipped": []},
    )
    monkeypatch.setattr(web, "execute_plan", _fake_execute_plan)
    monkeypatch.setattr(web, "record_swap", lambda **kw: [])
    monkeypatch.setattr(web, "build_cover_store", lambda *a, **kw: store)
    monkeypatch.setattr(web, "build_sheet_sink", lambda *a, **kw: None)
    return web.create_app().test_client()


def test_covers_endpoint_reports_disabled_without_store(monkeypatch):
    c = _web_client(monkeypatch, None)
    data = c.get("/api/covers").get_json()
    assert data == {"covers": [], "enabled": False}


def test_apply_with_cover_records_rows_and_remove_undoes_them(monkeypatch):
    store = MemStore()
    c = _web_client(monkeypatch, store)

    # Preview an "add co-assignee" on the field, then apply with cover saving.
    prev = c.post("/api/preview", json={
        "pipe_id": "1", "old_user": "alice@dydx.digital", "new_user": "sam@dydx.digital",
        "operation": "add", "include_card_level": False,
        "field_ids": ["creative_specialist"],
    }).get_json()
    assert prev["plans"], prev
    applied = c.post("/api/apply", json={
        "token": prev["token"],
        "operator_email": "op@upandupgroup.com",
        "cover": {"save": True, "batch_id": "b1", "name": "Sam covering Alice",
                  "start_date": "2026-07-24", "end_date": "2026-08-04"},
    }).get_json()
    assert applied["cards_changed"] == 1
    assert applied["cover_saved"] == 1
    assert store.tabs[CARD_TAB][0]["card_id"] == "C1"
    assert store.tabs[BATCH_TAB][0]["cover_user_id"] == "300222"

    # The cover shows up as active.
    covers = c.get("/api/covers").get_json()
    assert covers["enabled"] is True
    assert covers["covers"][0]["cards_active"] == 1

    # End the cover: Sam is on the card now; remove flips the record + batch.
    monkeypatch.setattr(web, "fetch_card", lambda client, cid: _card())
    out = c.post("/api/covers/remove", json={
        "batch_id": "b1", "operator_email": "op@upandupgroup.com",
    }).get_json()
    assert out["removed"] == 1
    assert out["errors"] == []
    assert out["batch_ended"] is True
    assert out["results"][0]["status"] == "ok"
    assert store.tabs[CARD_TAB][0]["status"] == "removed"


def test_cover_remove_skips_cards_where_user_already_gone(monkeypatch):
    store = MemStore()
    store.record(_batch(), _rows(("C1",)))
    c = _web_client(monkeypatch, store)
    # Card no longer has Sam on the recorded field.
    monkeypatch.setattr(web, "fetch_card",
                        lambda client, cid: _card(field_ids=("300111",), assignees=()))
    out = c.post("/api/covers/remove", json={
        "batch_id": "b1", "operator_email": "op@upandupgroup.com",
    }).get_json()
    assert out["removed"] == 0
    assert out["skipped"] == 1
    assert out["results"][0]["status"] == "already_removed"
    assert out["batch_ended"] is True  # record cleaned up anyway


def test_cover_remove_requires_company_email(monkeypatch):
    store = MemStore()
    store.record(_batch(), _rows(("C1",)))
    c = _web_client(monkeypatch, store)
    r = c.post("/api/covers/remove", json={"batch_id": "b1", "operator_email": "x@gmail.com"})
    assert r.status_code == 403
