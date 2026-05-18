import pytest

from swap_assignee.user_resolver import User, resolve_user, UserResolutionError


def make(uid, name, email):
    return User(id=str(uid), name=name, email=email)


MEMBERS = [
    make(1, "Kate Smith", "kate.smith@upandup.com"),
    make(2, "Robyn Seth", "robyn.seth@upandup.com"),
    make(5, "Kate Smith", "kate.smith.alt@upandup.com"),  # name dup inside members
]
ORG = MEMBERS + [
    make(3, "Other Person", "other@upandup.com"),
    make(4, "Kate Smith", "kate.smith.dup@upandup.com"),  # name dup, email different
]


def test_email_resolution_prefers_pipe_members():
    u = resolve_user("kate.smith@upandup.com", MEMBERS, ORG)
    assert u.id == "1"


def test_email_resolution_falls_back_to_org():
    u = resolve_user("other@upandup.com", MEMBERS, ORG)
    assert u.id == "3"


def test_name_resolution_unique():
    u = resolve_user("Robyn Seth", MEMBERS, ORG)
    assert u.id == "2"


def test_name_resolution_ambiguous_fails():
    with pytest.raises(UserResolutionError) as exc:
        resolve_user("Kate Smith", MEMBERS, ORG)
    assert "ambiguous" in str(exc.value).lower() or "ambiguous" in repr(exc.value).lower()


def test_email_unknown_fails():
    with pytest.raises(UserResolutionError):
        resolve_user("nobody@upandup.com", MEMBERS, ORG)


def test_email_resolution_is_case_insensitive():
    u = resolve_user("  KATE.SMITH@upandup.com  ", MEMBERS, ORG)
    assert u.id == "1"
