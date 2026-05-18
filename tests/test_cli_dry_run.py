import io
import sys
from typing import Any, Dict, List
from unittest import mock

import pytest

from swap_assignee.cli import main
from tests._fakes import load_fixture


OLD_ID = "300111"
NEW_ID = "300222"


def make_router(cards: List[Dict[str, Any]], fields_meta: List[Dict[str, str]]):
    org_users = [
        {"id": OLD_ID, "name": "Kate Smith", "email": "kate.smith@upandup.com"},
        {"id": NEW_ID, "name": "Robyn Seth", "email": "robyn.seth@upandup.com"},
    ]
    pipe_org = {
        "pipe": {
            "id": "999",
            "name": "Test Pipe",
            "organization": {"id": "ORG1", "name": "Up and Up"},
            "members": [{"user": u} for u in org_users],
        }
    }
    org_payload = {"organization": {"users": org_users}}

    state = {"mutations": []}

    def router(query: str, variables: Dict[str, Any]):
        if "PipeOrg" in query or "organization { id name }" in query:
            return pipe_org
        if "OrgUsers" in query or "organization(id: $orgId)" in query:
            return org_payload
        if "PipeFields" in query or "start_form_fields" in query:
            return {
                "pipe": {
                    "id": "999",
                    "name": "Test Pipe",
                    "start_form_fields": [],
                    "phases": [{"id": "P1", "name": "x", "fields": fields_meta}],
                }
            }
        if "PipeCards" in query or "cards(pipe_id" in query:
            return {
                "cards": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "edges": [{"node": c} for c in cards],
                }
            }
        if "updateCardField" in query:
            state["mutations"].append(("field", variables))
            return {"updateCardField": {"card": {"id": variables["cardId"]}, "success": True}}
        if "updateCard" in query:
            state["mutations"].append(("card", variables))
            return {"updateCard": {"card": {"id": variables["cardId"], "assignees": []}}}
        raise AssertionError(f"unexpected query: {query[:80]}")

    return router, state


@pytest.fixture
def patched_client(monkeypatch, tmp_path):
    cfg = tmp_path / "config.ini"
    cfg.write_text(
        "[Pipefy]\n"
        "pipefy_api_url = https://api.pipefy.com/graphql\n"
        "pipefy_api_token = test-token-1234\n"
        "[PaperTrail]\n"
        "papertrail_log_token =\n",
        encoding="utf-8",
    )
    return cfg


def _run_cli(args, router, monkeypatch, *, stdin=None):
    from swap_assignee import cli as cli_mod

    class _C:
        def __init__(self, *a, **k):
            self._router = router

        def execute(self, query, variables=None):
            return self._router(query, variables or {})

    monkeypatch.setattr(cli_mod, "PipefyClient", _C)
    if stdin is not None:
        monkeypatch.setattr(sys, "stdin", stdin)
    return main(args)


def test_dry_run_makes_no_mutation_calls(patched_client, monkeypatch, capsys):
    cards = [load_fixture("card_with_old_user_only.json")]
    fields_meta = [{"id": "creative_specialist", "label": "Creative Specialist", "type": "assignee_select"}]
    router, state = make_router(cards, fields_meta)

    rc = _run_cli(
        [
            "--pipe-id", "999",
            "--old-user", "kate.smith@upandup.com",
            "--new-user", "robyn.seth@upandup.com",
            "--config", str(patched_client),
            "--all-fields",
        ],
        router, monkeypatch,
    )
    assert rc == 0
    assert state["mutations"] == []
    out = capsys.readouterr().out
    assert "DRY-RUN" in out
    assert "Cards to swap:        1" in out


def test_dry_run_preview_lists_affected_cards(patched_client, monkeypatch, capsys):
    cards = [load_fixture("card_with_old_user_only.json"), load_fixture("card_with_field_assignee.json")]
    fields_meta = [{"id": "creative_specialist", "label": "Creative Specialist", "type": "assignee_select"}]
    router, state = make_router(cards, fields_meta)

    rc = _run_cli(
        [
            "--pipe-id", "999",
            "--old-user", "kate.smith@upandup.com",
            "--new-user", "robyn.seth@upandup.com",
            "--config", str(patched_client),
            "--all-fields",
        ],
        router, monkeypatch,
    )
    assert rc == 0
    assert state["mutations"] == []
    out = capsys.readouterr().out
    # Preview header + both card titles + per-action line
    assert "Cards that would be affected (2)" in out
    assert "Only old user, card-level" in out
    assert "Field-level assignee swap" in out
    assert "card-level:" in out
    assert "field creative_specialist" in out
    assert f"add {NEW_ID}" in out


def test_no_preview_flag_suppresses_table(patched_client, monkeypatch, capsys):
    cards = [load_fixture("card_with_old_user_only.json")]
    router, _ = make_router(cards, [])
    rc = _run_cli(
        [
            "--pipe-id", "999",
            "--old-user", "kate.smith@upandup.com",
            "--new-user", "robyn.seth@upandup.com",
            "--config", str(patched_client),
            "--all-fields",
            "--no-preview",
        ],
        router, monkeypatch,
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "Cards that would be affected" not in out


def test_preview_limit_truncates(patched_client, monkeypatch, capsys):
    base = load_fixture("card_with_old_user_only.json")
    cards = []
    for i in range(4):
        c = dict(base)
        c["id"] = f"C{i}"
        c["title"] = f"Card {i}"
        cards.append(c)
    router, _ = make_router(cards, [])
    rc = _run_cli(
        [
            "--pipe-id", "999",
            "--old-user", "kate.smith@upandup.com",
            "--new-user", "robyn.seth@upandup.com",
            "--config", str(patched_client),
            "--all-fields",
            "--preview-limit", "2",
        ],
        router, monkeypatch,
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "Card 0" in out
    assert "Card 1" in out
    assert "2 more not shown" in out


def test_apply_invokes_correct_mutations(patched_client, monkeypatch, capsys):
    cards = [load_fixture("card_with_old_user_only.json")]
    fields_meta = []
    router, state = make_router(cards, fields_meta)

    rc = _run_cli(
        [
            "--pipe-id", "999",
            "--old-user", "kate.smith@upandup.com",
            "--new-user", "robyn.seth@upandup.com",
            "--config", str(patched_client),
            "--all-fields",
            "--apply", "--yes",
            "--throttle-ms", "0",
        ],
        router, monkeypatch,
    )
    assert rc == 0
    assert any(scope == "card" for scope, _ in state["mutations"])
    payload = [v for s, v in state["mutations"] if s == "card"][0]
    assert payload["assigneeIds"] == [NEW_ID]


def test_old_equals_new_refused(patched_client, monkeypatch, capsys):
    router, _ = make_router([], [])
    rc = _run_cli(
        [
            "--pipe-id", "999",
            "--old-user", "kate.smith@upandup.com",
            "--new-user", "kate.smith@upandup.com",
            "--config", str(patched_client),
            "--all-fields",
        ],
        router, monkeypatch,
    )
    assert rc != 0


def test_unknown_field_id_fails_fast(patched_client, monkeypatch, capsys):
    router, state = make_router([load_fixture("card_with_old_user_only.json")], [])
    rc = _run_cli(
        [
            "--pipe-id", "999",
            "--old-user", "kate.smith@upandup.com",
            "--new-user", "robyn.seth@upandup.com",
            "--config", str(patched_client),
            "--field", "does_not_exist",
        ],
        router, monkeypatch,
    )
    assert rc != 0
    assert state["mutations"] == []


def test_non_tty_without_field_flags_errors(patched_client, monkeypatch, capsys):
    fields_meta = [{"id": "creative_specialist", "label": "Creative Specialist", "type": "assignee_select"}]
    router, state = make_router([], fields_meta)

    fake_stdin = io.StringIO("")  # not a TTY
    rc = _run_cli(
        [
            "--pipe-id", "999",
            "--old-user", "kate.smith@upandup.com",
            "--new-user", "robyn.seth@upandup.com",
            "--config", str(patched_client),
        ],
        router, monkeypatch, stdin=fake_stdin,
    )
    assert rc != 0
    err = capsys.readouterr().err
    assert "Field selection required" in err
    assert "creative_specialist" in err


def test_field_prompt_lists_discovered_fields(patched_client, monkeypatch, capsys):
    cards = [load_fixture("card_with_field_assignee.json")]
    fields_meta = [
        {"id": "creative_specialist", "label": "Creative Specialist", "type": "assignee_select"},
        {"id": "qa_reviewer", "label": "QA Reviewer", "type": "assignee_select"},
    ]
    router, _ = make_router(cards, fields_meta)

    class TTYStdin(io.StringIO):
        def isatty(self): return True

    stdin = TTYStdin("1\n")
    rc = _run_cli(
        [
            "--pipe-id", "999",
            "--old-user", "kate.smith@upandup.com",
            "--new-user", "robyn.seth@upandup.com",
            "--config", str(patched_client),
            "--no-include-card-level",
        ],
        router, monkeypatch, stdin=stdin,
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "creative_specialist" in out
    assert "qa_reviewer" in out


def test_field_prompt_accepts_multi_select(patched_client, monkeypatch, capsys):
    cards = [load_fixture("card_with_field_assignee.json")]
    fields_meta = [
        {"id": "creative_specialist", "label": "Creative Specialist", "type": "assignee_select"},
        {"id": "qa_reviewer", "label": "QA Reviewer", "type": "assignee_select"},
        {"id": "account_manager", "label": "Account Manager", "type": "assignee_select"},
    ]
    router, _ = make_router(cards, fields_meta)

    # Sorted by label: Account Manager [1], Creative Specialist [2], QA Reviewer [3]
    class TTYStdin(io.StringIO):
        def isatty(self): return True

    from swap_assignee.cli import _interactive_field_selection
    stdin = TTYStdin("1,3\n")
    chosen = _interactive_field_selection(
        {f["id"]: f for f in fields_meta}, stdin=stdin, stdout=io.StringIO()
    )
    assert chosen == {"account_manager", "qa_reviewer"}


def test_field_prompt_rejects_invalid_then_quits():
    from swap_assignee.cli import _interactive_field_selection
    fields_meta = [{"id": "creative_specialist", "label": "Creative Specialist", "type": "assignee_select"}]

    class TTYStdin(io.StringIO):
        def isatty(self): return True

    stdin = TTYStdin("banana\nq\n")
    out = io.StringIO()
    chosen = _interactive_field_selection(
        {f["id"]: f for f in fields_meta}, stdin=stdin, stdout=out
    )
    assert chosen is None
    assert "Invalid input" in out.getvalue()


def test_pagination_continues_until_done(patched_client, monkeypatch, capsys):
    cards1 = [load_fixture("card_with_old_user_only.json")]
    c2 = dict(load_fixture("card_with_old_user_only.json"))
    c2["id"] = "C1B"
    cards2 = [c2]

    pages = iter([
        {"cards": {"pageInfo": {"hasNextPage": True, "endCursor": "CUR1"}, "edges": [{"node": c} for c in cards1]}},
        {"cards": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "edges": [{"node": c} for c in cards2]}},
    ])

    base_router, _ = make_router([], [])

    def router(query, variables):
        if "PipeCards" in query or "cards(pipe_id" in query:
            return next(pages)
        return base_router(query, variables)

    rc = _run_cli(
        [
            "--pipe-id", "999",
            "--old-user", "kate.smith@upandup.com",
            "--new-user", "robyn.seth@upandup.com",
            "--config", str(patched_client),
            "--all-fields",
        ],
        router, monkeypatch,
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "Cards scanned:        2" in out


def test_rate_limit_retry_then_succeed(monkeypatch):
    from swap_assignee.pipefy_client import PipefyClient

    calls = {"n": 0}

    class FakeResp:
        def __init__(self, status, body):
            self.status_code = status
            self._body = body
        def json(self):
            return self._body

    class FakeSession:
        def post(self, url, json=None, headers=None, timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:
                return FakeResp(429, {"errors": [{"message": "rate limit hit"}]})
            return FakeResp(200, {"data": {"ok": True}})

    sleeps = []
    client = PipefyClient(
        "http://x/graphql", "tok",
        session=FakeSession(),
        sleep=lambda s: sleeps.append(s),
        backoff_base=2.0, max_retries=3,
    )
    out = client.execute("query { ok }", {})
    assert out == {"ok": True}
    assert calls["n"] == 2
    assert sleeps  # at least one backoff sleep happened
