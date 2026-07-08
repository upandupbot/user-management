"""Resolve email/name to a Pipefy user ID using pipe + org membership."""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional


log = logging.getLogger(__name__)


# --- Disk cache for pipe context + org users -------------------------------
#
# Pipefy's `organization.users` endpoint returns hundreds of users on every
# call and can take 5-15 seconds. The same pipe/org context is fetched on
# every CLI invocation and every web request, so we cache it on disk with a
# short TTL. Pass use_cache=False to bypass.

_CACHE_DIR = Path(os.environ.get("SWAP_ASSIGNEE_CACHE_DIR") or (Path(tempfile.gettempdir()) / "swap_assignee_cache"))
_CACHE_TTL_SECONDS = int(os.environ.get("SWAP_ASSIGNEE_CACHE_TTL", "600"))  # 10 min default


def _cache_path(key: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in key)
    return _CACHE_DIR / f"{safe}.json"


def _cache_get(key: str, ttl: int = _CACHE_TTL_SECONDS) -> Optional[Any]:
    try:
        p = _cache_path(key)
        if not p.exists():
            return None
        if time.time() - p.stat().st_mtime > ttl:
            return None
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        log.debug("cache read failed for %s: %s", key, exc)
        return None


def _cache_put(key: str, value: Any) -> None:
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _cache_path(key).write_text(json.dumps(value), encoding="utf-8")
    except Exception as exc:
        log.debug("cache write failed for %s: %s", key, exc)


def clear_cache() -> int:
    """Delete every cached JSON file. Returns the number removed."""
    if not _CACHE_DIR.exists():
        return 0
    n = 0
    for p in _CACHE_DIR.glob("*.json"):
        try:
            p.unlink()
            n += 1
        except Exception:
            pass
    return n


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


def fetch_pipe_context(client, pipe_id: str, *, use_cache: bool = True) -> Dict[str, Any]:
    cache_key = f"pipe_ctx_{pipe_id}"
    if use_cache:
        hit = _cache_get(cache_key)
        if hit is not None:
            log.info("fetch_pipe_context(%s): cache hit (%d members)", pipe_id, len(hit.get("members") or []))
            ctx = dict(hit)
            ctx["members"] = [User(**m) if isinstance(m, dict) else m for m in ctx.get("members") or []]
            return ctx

    t0 = time.time()
    data = client.execute(PIPE_ORG_QUERY, {"pipeId": str(pipe_id)})
    elapsed = time.time() - t0
    pipe = data.get("pipe") or {}
    members = [
        _to_user(m["user"], role=m.get("role_name"))
        for m in (pipe.get("members") or [])
        if m and m.get("user")
    ]
    org = pipe.get("organization") or {}
    ctx = {
        "pipe_id": str(pipe.get("id") or pipe_id),
        "pipe_name": pipe.get("name") or "",
        "org_id": str(org.get("id")) if org.get("id") else None,
        "org_name": org.get("name") or "",
        "members": members,
    }
    log.info("fetch_pipe_context(%s): %.2fs, %d members", pipe_id, elapsed, len(members))
    if use_cache:
        _cache_put(cache_key, {**ctx, "members": [u.__dict__ for u in members]})
    return ctx


def fetch_org_users(client, org_id: str, *, use_cache: bool = True) -> List[User]:
    cache_key = f"org_users_{org_id}"
    if use_cache:
        hit = _cache_get(cache_key)
        if hit is not None:
            log.info("fetch_org_users(%s): cache hit (%d users)", org_id, len(hit))
            return [User(**u) for u in hit]

    t0 = time.time()
    data = client.execute(ORG_USERS_QUERY, {"orgId": str(org_id)})
    elapsed = time.time() - t0
    org = data.get("organization") or {}
    users = [_to_user(u) for u in (org.get("users") or [])]
    log.info("fetch_org_users(%s): %.2fs, %d users", org_id, elapsed, len(users))
    if use_cache:
        _cache_put(cache_key, [u.__dict__ for u in users])
    return users


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
    results = scan_pipe_cards_for_users(client, pipe_id, [input_value], max_pages=max_pages)
    return results.get(input_value)


def scan_pipe_cards_for_users(
    client, pipe_id: str, input_values: List[str], *, max_pages: int = 200
) -> Dict[str, Optional[User]]:
    """Scan the pipe once looking for any of the provided email/name inputs.

    Returns a dict mapping each input_value to the matched User (or None).
    Exits early as soon as every input has been found.
    """
    from .card_iterator import iter_cards  # local import to avoid circular

    out: Dict[str, Optional[User]] = {v: None for v in input_values}
    if not input_values:
        return out

    # Pre-normalize each target for fast comparison.
    targets: List[tuple] = []  # (original_value, looks_like_email, normalized)
    for v in input_values:
        is_email = "@" in v
        targets.append((v, is_email, _norm_email(v) if is_email else _norm_name(v)))

    t0 = time.time()
    seen = 0
    for card in iter_cards(client, pipe_id):
        seen += 1
        for a in (card.get("assignees") or []):
            if not a:
                continue
            email = _norm_email(a.get("email"))
            name = _norm_name(a.get("name"))
            for original, is_email, target in targets:
                if out[original] is not None:
                    continue
                if (is_email and email == target) or (not is_email and name == target):
                    out[original] = _to_user(a)
            if all(v is not None for v in out.values()):
                log.info(
                    "scan_pipe_cards_for_users(%s): all %d target(s) found after %d card(s) in %.2fs",
                    pipe_id, len(input_values), seen, time.time() - t0,
                )
                return out
        if max_pages and seen >= max_pages * 50:
            break
    log.info(
        "scan_pipe_cards_for_users(%s): scanned %d card(s) in %.2fs; matched %d/%d",
        pipe_id, seen, time.time() - t0,
        sum(1 for v in out.values() if v is not None), len(input_values),
    )
    return out
