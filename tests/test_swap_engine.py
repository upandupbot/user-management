from tests._fakes import FakeClient, load_fixture

from swap_assignee.swap_engine import (
    DEFAULT_EXCLUDED_PHASES,
    compute_add_only,
    compute_swap,
    execute_plan,
    extract_client,
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


def test_extract_client_from_string_field():
    card = {
        "id": "X", "title": "t", "url": "", "current_phase": {"name": "InProg"},
        "assignees": [], "fields": [
            {"name": "client_name", "value": "Acme Corp", "array_value": None,
             "field": {"id": "client_name", "label": "Client", "type": "short_text"}},
        ],
    }
    assert extract_client(card) == "Acme Corp"


def test_extract_client_from_connection_array():
    card = {
        "id": "X", "title": "t", "url": "", "current_phase": {"name": "InProg"},
        "assignees": [], "fields": [
            {"name": "brand", "value": None,
             "array_value": [{"id": "9", "name": "Globex"}],
             "field": {"id": "brand", "label": "Brand", "type": "connector"}},
        ],
    }
    assert extract_client(card) == "Globex"


def test_extract_client_ignores_assignee_fields_and_returns_blank():
    card = {
        "id": "X", "title": "t", "url": "", "current_phase": {"name": "InProg"},
        "assignees": [], "fields": [
            {"name": "account_manager", "value": None,
             "array_value": [{"id": OLD, "name": "Kate"}],
             "field": {"id": "account_manager", "label": "Account Manager", "type": "assignee_select"}},
        ],
    }
    # "Account Manager" is an assignee field, so it must not be read as a client.
    assert extract_client(card) == ""


def test_extract_client_suppresses_numeric_ids():
    # A connection field that only resolves to a raw record id must not surface
    # the id as a client name.
    card = {
        "id": "X", "title": "t", "url": "", "current_phase": {"name": "InProg"},
        "assignees": [], "fields": [
            {"name": "client", "value": "1048576", "array_value": None,
             "field": {"id": "client", "label": "Client", "type": "connector"}},
        ],
    }
    assert extract_client(card) == ""
    card["fields"][0]["array_value"] = [{"id": "999"}]
    card["fields"][0]["value"] = None
    assert extract_client(card) == ""


def test_plan_exposes_client_on_card_plan():
    card = {
        "id": "X", "title": "t", "url": "", "current_phase": {"name": "InProg"},
        "assignees": [{"id": OLD, "name": "Kate"}],
        "fields": [
            {"name": "client", "value": "Acme Corp", "array_value": None,
             "field": {"id": "client", "label": "Client", "type": "short_text"}},
        ],
    }
    plan = _plan(card)
    assert plan.client == "Acme Corp"


def test_plan_action_carries_before_after_names():
    card = {
        "id": "X", "title": "t", "url": "", "current_phase": {"name": "InProg"},
        "assignees": [{"id": OLD, "name": "Kate Smith"}, {"id": "X1", "name": "Sam Jones"}],
        "fields": [],
    }
    plan = _plan(card, id_names={OLD: "Kate Smith", NEW: "Robyn Seth"})
    a = plan.actions[0]
    assert a.old_names == ["Kate Smith", "Sam Jones"]
    # Bystander preserved, old swapped for new — by name.
    assert a.new_names == ["Sam Jones", "Robyn Seth"]


def test_no_include_card_level_skips_card_action_even_when_old_present():
    """Field-only swaps must NOT touch the card-level assignee list, even when
    the old user is also a card-level assignee. Touching it triggers Pipefy's
    notification storm via updateCard."""
    card = {
        "id": "X", "title": "t", "url": "", "current_phase": {"name": "InProg"},
        "assignees": [{"id": OLD}, {"id": "X1"}, {"id": "Y1"}],
        "fields": [
            {
                "name": "trafficker",
                "value": None,
                "array_value": [{"id": OLD}, {"id": "Z9"}],
                "field": {"id": "trafficker", "label": "Traffic Manager", "type": "assignee_select"},
            }
        ],
    }
    plan = _plan(card, include_card_level=False, field_filter={"trafficker"})
    # Only the field action — no card-level action at all.
    assert len(plan.actions) == 1
    a = plan.actions[0]
    assert a.scope == "field:trafficker"
    assert OLD not in a.new_ids
    assert NEW in a.new_ids
    assert "Z9" in a.new_ids  # bystander on field preserved
    # Card-level assignees are completely untouched (no "card" action).
    assert not any(act.scope == "card" for act in plan.actions)


def test_card_add_only_ignored_when_card_level_excluded():
    """card_add_only only matters when card-level is included; with it excluded,
    no card action is produced regardless."""
    card = {
        "id": "X", "title": "t", "url": "", "current_phase": {"name": "InProg"},
        "assignees": [{"id": OLD}, {"id": "X1"}], "fields": [],
    }
    plan = _plan(card, include_card_level=False, card_add_only=True)
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


# ---------- card_add_only mode ----------

def test_compute_add_only_appends_new_keeps_old():
    assert compute_add_only(["A", OLD], OLD, NEW) == ["A", OLD, NEW]


def test_compute_add_only_returns_none_when_old_absent():
    assert compute_add_only(["A", "B"], OLD, NEW) is None


def test_compute_add_only_returns_none_when_new_already_present():
    assert compute_add_only([OLD, NEW, "A"], OLD, NEW) is None


def test_card_add_only_card_level_keeps_old_user():
    card = {
        "id": "X", "title": "t", "url": "", "current_phase": {"name": "InProg"},
        "assignees": [{"id": "X1"}, {"id": OLD}, {"id": "Y1"}], "fields": [],
    }
    plan = _plan(card, card_add_only=True)
    assert len(plan.actions) == 1
    a = plan.actions[0]
    assert a.scope == "card"
    assert a.mode == "add_only"
    # Old user is preserved; new user is appended.
    assert a.new_ids == ["X1", OLD, "Y1", NEW]
    assert OLD in a.new_ids
    assert NEW in a.new_ids


def test_card_add_only_skips_when_new_already_present():
    card = {
        "id": "X", "title": "t", "url": "", "current_phase": {"name": "InProg"},
        "assignees": [{"id": OLD}, {"id": NEW}], "fields": [],
    }
    plan = _plan(card, card_add_only=True)
    assert plan.actions == []


def test_card_add_only_does_not_affect_field_level():
    """Field-level swaps still remove the old user even when card-add-only is on."""
    card = load_fixture("card_with_field_assignee.json")
    plan = _plan(card, card_add_only=True)
    field_actions = [a for a in plan.actions if a.scope.startswith("field:")]
    assert len(field_actions) == 1
    a = field_actions[0]
    assert a.mode == "swap"
    assert OLD not in a.new_ids
    assert NEW in a.new_ids


def test_card_add_only_apply_sends_appended_list():
    """The mutation payload preserves the old user and adds the new one."""
    card = {
        "id": "C9", "title": "t", "url": "", "current_phase": {"name": "InProg"},
        "assignees": [{"id": "X1"}, {"id": OLD}], "fields": [],
    }
    plan = _plan(card, card_add_only=True, include_card_level=True)
    seen = []

    def router(query, variables):
        seen.append((query, variables))
        if "updateCard" in query:
            return {"updateCard": {"card": {"id": "C9", "assignees": []}}}
        raise AssertionError("unexpected query")

    fake = FakeClient(router=router)
    results = execute_plan(fake, plan, throttle_ms=0)
    assert all(err is None for _, err in results)
    card_payloads = [v for q, v in seen if "updateCard" in q and "updateCardField" not in q]
    assert len(card_payloads) == 1
    sent_ids = card_payloads[0]["assigneeIds"]
    assert OLD in sent_ids  # NOT removed
    assert NEW in sent_ids  # IS appended
    assert sent_ids == ["X1", OLD, NEW]
