"""Tests for the cheap per-pipe discovery probes ("Find their pipes")."""
from __future__ import annotations

from swap_assignee.card_iterator import (
    probe_card_level_assignee,
    probe_field_value,
)

from _fakes import FakeClient


def test_probe_card_level_hit():
    client = FakeClient(responses={
        "ProbeCardsByAssignee": {"cards": {"edges": [{"node": {"id": "1"}}]}},
    })
    assert probe_card_level_assignee(client, "305705781", "111") is True
    assert len(client.calls) == 1
    assert client.calls[0]["variables"] == {"pipeId": "305705781", "assigneeId": "111"}


def test_probe_card_level_miss():
    client = FakeClient(responses={
        "ProbeCardsByAssignee": {"cards": {"edges": []}},
    })
    assert probe_card_level_assignee(client, "305705781", "111") is False


def test_probe_card_level_error_returns_unknown():
    def _boom(query, variables):
        raise RuntimeError("timeout")

    client = FakeClient(router=_boom)
    assert probe_card_level_assignee(client, "305705781", "111") is None


def test_probe_field_value_hit_and_miss():
    client = FakeClient(responses={
        "ProbeFindCards": lambda: {"findCards": {"edges": [{"node": {"id": "9"}}]}},
    })
    assert probe_field_value(client, "305705781", "account_manager_1", "111") is True

    client2 = FakeClient(responses={"ProbeFindCards": {"findCards": {"edges": []}}})
    assert probe_field_value(client2, "305705781", "account_manager_1", "111") is False


def test_probe_field_value_error_returns_unknown():
    def _boom(query, variables):
        raise RuntimeError("findCards timed out")

    client = FakeClient(router=_boom)
    assert probe_field_value(client, "305705781", "account_manager_1", "111") is None
