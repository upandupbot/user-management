"""Resolve email/name to a Pipefy user ID using pipe + org membership."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class User:
    id: str
    name: str
    email: str
    role: Optional[str] = None  # pipe role (admin/member/restricted_member); None for org-only users


class UserResolutionError(RuntimeError):
    pass


PIPE_ORG_QUERY = """
query PipeOrg($pipeId: ID!) {
  pipe(id: $pipeId) {
    id
    name
    organization { id name }
    members { role_name user { id name email } }
  }
}
"""

ORG_USERS_QUERY = """
query OrgUsers($orgId: ID!) {
  organization(id: $orgId) {
    users { id name email }
  }
}
"""


def _norm_email(s: Optional[str]) -> str:
    return (s or "").strip().lower()


def _norm_name(s: Optional[str]) -> str:
    return " ".join((s or "").strip().lower().split())


def _to_user(d: Dict[str, Any], role: Optional[str] = None) -> User:
    return User(
        id=str(d.get("id")),
        name=d.get("name") or "",
        email=d.get("email") or "",
        role=role,
    )


def fetch_pipe_context(client, pipe_id: str) -> Dict[str, Any]:
    data = client.execute(PIPE_ORG_QUERY, {"pipeId": str(pipe_id)})
    pipe = data.get("pipe") or {}
    members = [
        _to_user(m["user"], role=m.get("role_name"))
        for m in (pipe.get("members") or [])
        if m and m.get("user")
    ]
    org = pipe.get("organization") or {}
    return {
        "pipe_id": str(pipe.get("id") or pipe_id),
        "pipe_name": pipe.get("name") or "",
        "org_id": str(org.get("id")) if org.get("id") else None,
        "org_name": org.get("name") or "",
        "members": members,
    }


def fetch_org_users(client, org_id: str) -> List[User]:
    data = client.execute(ORG_USERS_QUERY, {"orgId": str(org_id)})
    org = data.get("organization") or {}
    return [_to_user(u) for u in (org.get("users") or [])]


def _match_email(users: List[User], email: str) -> List[User]:
    return [u for u in users if _norm_email(u.email) == email]


def _match_name(users: List[User], name: str) -> List[User]:
    return [u for u in users if _norm_name(u.name) == name]


def resolve_user(input_value: str, members: List[User], org_users: List[User]) -> User:
    """Resolve an email or name against pipe members first, then org users."""
    if not input_value or not input_value.strip():
        raise UserResolutionError("Empty user value")

    looks_like_email = "@" in input_value
    if looks_like_email:
        target = _norm_email(input_value)
        for pool, label in ((members, "pipe members"), (org_users, "org users")):
            hits = _match_email(pool, target)
            if len(hits) == 1:
                return hits[0]
            if len(hits) > 1:
                ids = ", ".join(sorted({u.id for u in hits}))
                raise UserResolutionError(
                    f"Email '{input_value}' matches {len(hits)} users in {label} (ids: {ids})."
                )
        raise UserResolutionError(
            f"Could not resolve user '{input_value}'. No matching email in pipe members "
            f"({len(members)} searched) or organization ({len(org_users)} searched). "
            f"Check spelling, or pass --org-id so the org user list can be searched."
        )

    target = _norm_name(input_value)
    for pool, label in ((members, "pipe members"), (org_users, "org users")):
        hits = _match_name(pool, target)
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            ids = ", ".join(sorted({u.id for u in hits}))
            raise UserResolutionError(
                f"Name '{input_value}' is ambiguous in {label} (ids: {ids}). Provide an email instead of a name."
            )
    raise UserResolutionError(
        f"Could not resolve user '{input_value}'. Provide an email instead of a name."
    )


def scan_pipe_cards_for_user(client, pipe_id: str, input_value: str, *, max_pages: int = 200) -> Optional[User]:
    """Last-resort fallback: page through cards and look for an assignee whose
    email or name matches `input_value`. Returns the User on first match, or None.

    This catches users who are currently assignable on the pipe (because they're
    actually assigned to a card) but who aren't in `pipe.members`.
    """
    from .card_iterator import iter_cards  # local import to avoid circular

    looks_like_email = "@" in input_value
    target = _norm_email(input_value) if looks_like_email else _norm_name(input_value)
    seen = 0
    for card in iter_cards(client, pipe_id):
        seen += 1
        for a in (card.get("assignees") or []):
            if not a:
                continue
            email = _norm_email(a.get("email"))
            name = _norm_name(a.get("name"))
            if (looks_like_email and email == target) or (not looks_like_email and name == target):
                return _to_user(a)
        # Cap how far we'll scan to avoid extremely long lookups on huge pipes.
        if max_pages and seen >= max_pages * 50:
            break
    return None
