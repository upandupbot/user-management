from tests._fakes import FakeClient, load_fixture

from swap_assignee.swap_engine import (
    DEFAULT_EXCLUDED_PHASES,
    compute_swap,
    execute_plan,
    plan_card,
)


OLD = "300111"
NEW = "300222"


def _plan(card, **kw):
    defaults = dict(
        old_id=OLD,
        new_id=NEW,
        excluded_phases=DEFAULT_EXCLUDED_PHASES,
        include_card_level=True,
        field_filter=None,
    )
    defaults.update(kw)
    return plan_card(card, **defaults)


def test_swap_when_only_old_user_card_level():
    card = load_fixture("card_with_old_user_only.json")
    plan = _plan(card)
    assert len(plan.actions) == 1
    a = plan.actions[0]
    assert a.scope == "card"
    assert a.old_ids == [OLD]
    assert a.new_ids == [NEW]


def test_swap_keeps_other_assignees():
    card = {
        "id": "X", "title": "t", "url": "", "current_phase": {"name": "InProg"},
        "assignees": [{"id": OLD}, {"id": "X1"}, {"id": "Y1"}], "fields": [],
    }
    plan = _plan(card)
    a = plan.actions[0]
    assert set(a.new_ids) == {"X1", "Y1", NEW}
    assert OLD not in a.new_ids


def test_idempotent_when_only_new_user():
    card = {
        "id": "X", "title": "t", "url": "", "current_phase": {"name": "InProg"},
        "assignees": [{"id": NEW}], "fields": [],
    }
    plan = _plan(card)
    assert plan.actions == []


def test_dedup_when_both_present():
    card = {
        "id": "X", "title": "t", "url": "", "current_phase": {"name": "InProg"},
        "assignees": [{"id": OLD}, {"id": NEW}, {"id": "X1"}], "fields": [],
    }
    plan = _plan(card)
    a = plan.actions[0]
    assert a.new_ids.count(NEW) == 1
    assert OLD not in a.new_ids
    assert set(a.new_ids) == {NEW, "X1"}


def test_excluded_phase_skipped():
    card = load_fixture("card_in_excluded_phase.json")
    plan = _plan(card)
    assert plan.skipped_reason
    assert plan.actions == []


def test_excluded_phase_case_insensitive():
    # The fixture's phase is "  finance RECON " — already weird casing/whitespace.
    card = load_fixture("card_in_excluded_phase.json")
    plan = _plan(card, excluded_phases=["finance recon"])
    assert plan.skipped_reason


def test_field_assignee_swap():
    card = load_fixture("card_with_field_assignee.json")
    plan = _plan(card, include_card_level=False)
    assert len(plan.actions) == 1
    a = plan.actions[0]
    assert a.scope == "field:creative_specialist"
    assert OLD not in a.new_ids
    assert NEW in a.new_ids
    assert "300999" in a.new_ids  # bystander preserved


def test_field_filter_restricts_scope():
    card = load_fixture("card_with_field_assignee.json")
    plan = _plan(card, include_card_level=False, field_filter={"some_other_field"})
    assert plan.actions == []


def test_dry_run_makes_no_mutation_calls():
    # Building plans does not call the client at all.
    fake = FakeClient()
    cards = [load_fixture("card_with_old_user_only.json")]
    for c in cards:
        _plan(c)
    assert fake.calls == []


def test_apply_invokes_correct_mutations():
    card = load_fixture("card_with_field_assignee.json")
    plan = _plan(card, include_card_level=False)
    seen = []

    def router(query, variables):
        seen.append((query, variables))
        if "updateCardField" in query:
            return {"updateCardField": {"card": {"id": "C3"}, "success": True}}
        if "updateCard" in query:
            return {"updateCard": {"card": {"id": "C3", "assignees": []}}}
        raise AssertionError("unexpected query")

    fake = FakeClient(router=router)
    results = execute_plan(fake, plan, throttle_ms=0)
    assert all(err is None for _, err in results)
    assert any("updateCardField" in q for q, _ in seen)
    payload = [v for q, v in seen if "updateCardField" in q][0]
    assert payload["fieldId"] == "creative_specialist"
    assert OLD not in payload["value"]
    assert NEW in payload["value"]


def test_compute_swap_returns_none_when_old_absent():
    assert compute_swap(["A", "B"], OLD, NEW) is None
