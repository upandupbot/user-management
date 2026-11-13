"""Papertrail activity logging: format, what's sent, and what's never sent."""
import logging
import re
import time

import pytest

from swap_assignee import papertrail_logger as pt
from swap_assignee import web
from swap_assignee.swap_engine import CardPlan, PlannedAction

from tests.test_assign_mode import NEW, _assign_client, _scan_body, _plans_from, _card, _role_field

LINE_RE = re.compile(r"^(?P<ident>[a-z-]+): (?P<stamp>\d{9}), card_id (?P<card>[^ ].*?) : (?P<msg>.*)$")


@pytest.fixture
def sent(monkeypatch):
    """Turn Papertrail on with a fake endpoint and capture every line sent."""
    lines = []
    monkeypatch.setattr(pt.PapertrailHTTPHandler, "_post",
                        staticmethod(lambda url, token, line: lines.append(line)))
    monkeypatch.setenv("PAPERTRAIL_LOG_URL", "https://logs.example.invalid/v1/log")
    monkeypatch.setenv("PAPERTRAIL_LOG_TOKEN", "test-papertrail-token")
    yield lines
    root = logging.getLogger()
    for h in list(root.handlers):
        if isinstance(h, pt.PapertrailHTTPHandler):
            h.flush()
            root.removeHandler(h)


def _flush():
    for h in logging.getLogger().handlers:
        if isinstance(h, pt.PapertrailHTTPHandler):
            h.flush()


def test_line_format_matches_the_other_integrations():
    rec = logging.LogRecord("swap_assignee.activity", logging.INFO, __file__, 1, "Signed in: %s", ("a@b.c",), None)
    rec.card_id = "1445558588"
    m = LINE_RE.match(pt.format_line("user-management", rec))
    assert m and m["ident"] == "user-management" and m["card"] == "1445558588"
    assert m["msg"] == "Signed in: a@b.c"
    rec2 = logging.LogRecord("swap_assignee.web", logging.WARNING, __file__, 1, "Sheet down", (), None)
    assert pt.format_line("user-management", rec2).endswith("card_id no card_id found : WARNING: Sheet down")


def test_only_activity_and_problems_go_to_papertrail():
    def rec(name, level):
        return logging.LogRecord(name, level, __file__, 1, "m", (), None)
    assert pt._papertrail_filter(rec("swap_assignee.activity", logging.INFO))
    assert not pt._papertrail_filter(rec("swap_assignee.web", logging.INFO))  # e.g. scan progress
    assert pt._papertrail_filter(rec("swap_assignee.web", logging.WARNING))
    assert pt._papertrail_filter(rec("swap_assignee.web", logging.ERROR))


def test_sign_ins_are_logged_without_the_password(monkeypatch, sent):
    monkeypatch.setenv("SWAP_WEB_PASSWORD", "s3cret-team-pw")
    monkeypatch.setenv("SWAP_WEB_ALLOW_INSECURE_COOKIE", "1")
    client = web.create_app().test_client()
    client.post("/api/auth", json={"email": "kate@dydx.digital", "password": "wrong-guess"})
    client.post("/api/auth", json={"email": "someone@gmail.com", "password": "x"})
    client.post("/api/auth", json={"email": "kate@dydx.digital", "password": "s3cret-team-pw"})
    client.post("/api/logout")
    _flush()
    text = "\n".join(sent)
    assert all(LINE_RE.match(line) for line in sent), sent
    assert "App started" in text
    assert "Sign-in failed: wrong password for kate@dydx.digital" in text
    assert "Sign-in refused: someone@gmail.com is not on an approved domain" in text
    assert "Signed in: kate@dydx.digital" in text
    assert "Signed out: kate@dydx.digital" in text
    for secret in ("s3cret-team-pw", "wrong-guess", "test-papertrail-token", "test-token"):
        assert secret not in text


def test_successful_scans_stay_out_but_applies_log_each_card(monkeypatch, sent):
    card = _card(fields=[_role_field([])])
    client = _assign_client(monkeypatch, [card])
    assert _plans_from(client.post("/api/assign/scan/stream", json=_scan_body()))
    _flush()
    assert not any("scan" in line.lower() for line in sent), sent

    monkeypatch.setattr(web, "execute_plan",
                        lambda client_, plan, throttle_ms=0: [(a, None) for a in plan.actions])
    plan = CardPlan(card_id="C42", card_url="", title="Acme banners", phase_name="Doing",
                    actions=[PlannedAction("field:f", "f", [], [NEW], field_label="Job Owner", mode="add_only")])
    web._PREVIEW_STORE["tok-log"] = {
        "created_at": time.time(), "plans": [plan], "throttle_ms": 0, "summary": {},
        "pipe_id": "1", "pipe_name": "Creative", "old_user": "Kate <kate@dydx.digital>",
        "new_user": "Robyn <robyn@dydx.digital>", "old_user_id": "", "new_user_id": NEW,
        "new_user_name": "Robyn", "new_user_email": "robyn@dydx.digital", "operation": "add",
    }
    resp = client.post("/api/apply", json={"token": "tok-log", "operator_email": "op@dydx.digital"})
    assert resp.get_json()["cards_changed"] == 1
    _flush()
    text = "\n".join(sent)
    assert "Apply started by op@dydx.digital: add Robyn alongside Kate on 1 card(s) in Creative" in text
    card_lines = [line for line in sent if "card_id C42 :" in line]
    assert card_lines and "Acme banners: add Robyn alongside Kate (Job Owner) done" in card_lines[0]
    assert "Apply finished by op@dydx.digital in Creative: 1 changed, 0 failed" in text


def test_failed_card_is_logged_as_an_error(monkeypatch, sent):
    client = _assign_client(monkeypatch, [])
    monkeypatch.setattr(web, "execute_plan",
                        lambda client_, plan, throttle_ms=0: [(a, "Card is locked") for a in plan.actions])
    plan = CardPlan(card_id="C7", card_url="", title="Locked job", phase_name="Doing",
                    actions=[PlannedAction("field:f", "f", ["1"], [NEW], field_label="Account Manager")])
    web._PREVIEW_STORE["tok-fail"] = {
        "created_at": time.time(), "plans": [plan], "throttle_ms": 0, "summary": {},
        "pipe_id": "1", "pipe_name": "Creative", "old_user": "Kate <kate@dydx.digital>",
        "new_user": "Robyn <robyn@dydx.digital>", "old_user_id": "1", "new_user_id": NEW,
        "new_user_name": "Robyn", "new_user_email": "robyn@dydx.digital", "operation": "replace",
    }
    client.post("/api/apply", json={"token": "tok-fail", "operator_email": "op@dydx.digital"})
    _flush()
    line = next(line for line in sent if "card_id C7 :" in line)
    assert "ERROR: Locked job: replace Kate with Robyn (Account Manager) failed: Card is locked" in line
    assert any("WARNING: Apply finished" in s and "0 changed, 1 failed" in s for s in sent)


def test_activity_lines_stay_out_of_the_notification_bell(monkeypatch, sent):
    monkeypatch.setenv("SWAP_WEB_PASSWORD", "pw")
    monkeypatch.setenv("SWAP_WEB_ALLOW_INSECURE_COOKIE", "1")
    web._NOTIFICATION_RING.clear()
    client = web.create_app().test_client()
    client.post("/api/auth", json={"email": "kate@dydx.digital", "password": "nope"})
    assert not any("Sign-in failed" in str(n.get("message")) for n in web._NOTIFICATION_RING)
