import json

from swap_assignee.swap_engine import parse_field_user_ids


def make_field(value=None, array_value=None, ftype="assignee_select"):
    return {
        "value": value,
        "array_value": array_value,
        "field": {"id": "f1", "label": "F", "type": ftype},
    }


def test_array_value_dicts():
    f = make_field(array_value=[{"id": "111", "name": "A"}, {"id": "222", "name": "B"}])
    assert parse_field_user_ids(f) == ["111", "222"]


def test_value_json_array_of_strings():
    f = make_field(value='["111", "222"]')
    assert parse_field_user_ids(f) == ["111", "222"]


def test_value_json_array_of_objects():
    f = make_field(value=json.dumps([{"id": "111"}, {"id": 222}]))
    assert parse_field_user_ids(f) == ["111", "222"]


def test_value_single_id_string():
    f = make_field(value="123")
    assert parse_field_user_ids(f) == ["123"]


def test_integer_ids_coerced_to_strings():
    f = make_field(array_value=[{"id": 111}, {"id": 222}])
    assert parse_field_user_ids(f) == ["111", "222"]


def test_empty_field():
    assert parse_field_user_ids(make_field(value=None, array_value=None)) == []
    assert parse_field_user_ids(make_field(value="", array_value=[])) == []
