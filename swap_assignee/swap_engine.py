"""Plan + execute the assignee swap on a card."""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple


log = logging.getLogger(__name__)

ASSIGNEE_FIELD_TYPES = {"assignee_select", "user", "member"}

DEFAULT_EXCLUDED_PHASES = ("Cancelled", "Completed", "Finance Recon", "Completed Jobs", "Completed Review Tasks")

# Substrings (case-insensitive) used to locate the "client" of a card among
# its non-assignee fields. Pipefy pipes label this differently across orgs
# (Client, Client Name, Brand, Customer, Account…), so we match the most
# common ones. Assignee-type fields are never treated as the client.
DEFAULT_CLIENT_FIELD_PATTERNS = ("client", "brand", "customer", "account name", "company")


UPDATE_CARD_MUT = """
mutation SetAssignees($cardId: ID!, $assigneeIds: [ID!]!) {
  updateCard(input: { id: $cardId, assignee_ids: $assigneeIds }) {
    card { id assignees { id email } }
  }
}
"""

UPDATE_FIELD_MUT = """
mutation UpdateField($cardId: ID!, $fieldId: ID!, $value: [UndefinedInput]) {
  updateCardField(input: { card_id: $cardId, field_id: $fieldId, new_value: $value }) {
    card { id }
    success
  }
}
"""


def normalize_phase(name: Optional[str]) -> str:
    return " ".join((name or "").strip().lower().split())


def parse_field_user_ids(field_node: Dict[str, Any]) -> List[str]:
    """Coerce a Pipefy field's value/array_value into a list of user-id strings (digits)."""
    out: List[str] = []
    av = field_node.get("array_value")
    if isinstance(av, list):
        for item in av:
            if isinstance(item, dict) and item.get("id") is not None:
                out.append(str(item["id"]))
            elif isinstance(item, (str, int)):
                s = str(item).strip()
                if s.isdigit():
                    out.append(s)
        if out:
            return _dedup_preserve(out)

    val = field_node.get("value")
    if val is None:
        return []
    if isinstance(val, list):
        for item in val:
            if isinstance(item, dict) and item.get("id") is not None:
                out.append(str(item["id"]))
            elif isinstance(item, (str, int)):
                s = str(item).strip()
                if s.isdigit():
                    out.append(s)
        return _dedup_preserve(out)
    if isinstance(val, (int,)):
        return [str(val)]
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return []
        # Try JSON first
        if s.startswith("[") or s.startswith("{") or s.startswith('"'):
            try:
                decoded = json.loads(s)
            except Exception:
                decoded = None
            if isinstance(decoded, list):
                for item in decoded:
                    if isinstance(item, dict) and item.get("id") is not None:
                        out.append(str(item["id"]))
                    elif isinstance(item, (str, int)):
                        ss = str(item).strip()
                        if ss.isdigit():
                            out.append(ss)
                return _dedup_preserve(out)
            if isinstance(decoded, dict) and decoded.get("id") is not None:
                return [str(decoded["id"])]
            if isinstance(decoded, (str, int)):
                ss = str(decoded).strip()
                if ss.isdigit():
                    return [ss]
        if s.isdigit():
            return [s]
    return []


def _dedup_preserve(seq: Iterable[str]) -> List[str]:
    seen: Set[str] = set()
    out: List[str] = []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _field_display_value(field_node: Dict[str, Any]) -> str:
    """Best-effort human-readable string for a non-assignee field value.

    Handles connection/database fields (array_value of objects with names),
    plain strings, and JSON-encoded string values.
    """
    def _label(item: Any) -> str:
        if isinstance(item, dict):
            for key in ("name", "title", "label", "value"):
                v = item.get(key)
                if v:
                    return str(v).strip()
            v = item.get("id")
            return str(v).strip() if v is not None else ""
        return str(item).strip()

    av = field_node.get("array_value")
    if isinstance(av, list) and av:
        parts = [p for p in (_label(it) for it in av) if p]
        if parts:
            return ", ".join(parts)

    val = field_node.get("value")
    if val is None:
        return ""
    if isinstance(val, list):
        parts = [p for p in (_label(it) for it in val) if p]
        return ", ".join(parts)
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return ""
        if s[:1] in "[{\"":
            try:
                decoded = json.loads(s)
            except Exception:
                decoded = None
            if isinstance(decoded, list):
                parts = [p for p in (_label(it) for it in decoded) if p]
                return ", ".join(parts)
            if isinstance(decoded, dict):
                return _label(decoded)
            if isinstance(decoded, str):
                return decoded.strip()
        return s
    return str(val).strip()


def extract_client(
    card: Dict[str, Any],
    *,
    patterns: Sequence[str] = DEFAULT_CLIENT_FIELD_PATTERNS,
) -> str:
    """Return the card's client/brand as a display string, or "" if none found.

    Scans non-assignee fields for one whose label or internal name contains a
    client-like keyword and returns its display value. Purely numeric values
    (raw record ids that leak through connection fields) are treated as "no
    client" so internal ids never surface in the UI.
    """
    return _extract_field_by_patterns(card, patterns)


# Substrings (case-insensitive) used to locate the "product" of a card.
DEFAULT_PRODUCT_FIELD_PATTERNS = ("product",)


def extract_product(
    card: Dict[str, Any],
    *,
    patterns: Sequence[str] = DEFAULT_PRODUCT_FIELD_PATTERNS,
) -> str:
    """Return the card's product as a display string, or "" if none found."""
    return _extract_field_by_patterns(card, patterns)


def _extract_field_by_patterns(card: Dict[str, Any], patterns: Sequence[str]) -> str:
    for fnode in card.get("fields") or []:
        meta = (fnode or {}).get("field") or {}
        if meta.get("type") in ASSIGNEE_FIELD_TYPES:
            continue
        hay = f"{meta.get('label') or ''} {fnode.get('name') or ''}".lower()
        if any(p in hay for p in patterns):
            val = _field_display_value(fnode)
            if val and not _looks_like_id(val):
                return val
    return ""


def _looks_like_id(value: str) -> bool:
    """True when a string is just an id (or comma-separated ids) rather than a name."""
    parts = [p.strip() for p in str(value).split(",")]
    parts = [p for p in parts if p]
    return bool(parts) and all(p.isdigit() for p in parts)


def build_card_name_lookup(card: Dict[str, Any]) -> Dict[str, str]:
    """Map user-id -> display name from a card's assignees and assignee fields."""
    out: Dict[str, str] = {}
    for a in card.get("assignees") or []:
        if isinstance(a, dict) and a.get("id") is not None and a.get("name"):
            out[str(a["id"])] = str(a["name"])
    for fnode in card.get("fields") or []:
        av = (fnode or {}).get("array_value")
        if isinstance(av, list):
            for item in av:
                if isinstance(item, dict) and item.get("id") is not None and item.get("name"):
                    out[str(item["id"])] = str(item["name"])
    return out


def compute_swap(current_ids: Sequence[str], old_id: str, new_id: str) -> Optional[List[str]]:
    """Return new id list if a swap is needed, else None.

    - Removes `old_id` if present.
    - Adds `new_id` only if not already present.
    - Returns None when `old_id` is absent (nothing to do).
    """
    current = [str(x) for x in current_ids]
    if old_id not in current:
        return None
    new_list = [x for x in current if x != old_id]
    if new_id not in new_list:
        new_list.append(new_id)
    return new_list


def compute_remove(current_ids: Sequence[str], user_id: str) -> Optional[List[str]]:
    """Return the id list without `user_id`, or None when they're absent.

    Pipefy has no "remove one assignee" mutation — both updateCard and
    updateCardField SET the whole list — so removal is always
    "current list minus one id", computed from a fresh card read.
    """
    current = [str(x) for x in current_ids]
    uid = str(user_id)
    if uid not in current:
        return None
    return [x for x in current if x != uid]


def card_matches_text(card: Dict[str, Any], text: str) -> bool:
    """True when `text` appears (case-insensitive) in the card title or its client field."""
    needle = " ".join((text or "").strip().lower().split())
    if not needle:
        return False
    title = (card.get("title") or "").lower()
    if needle in title:
        return True
    return needle in extract_client(card).lower()


# Ziflow/online-review cards are process artifacts, not real jobs — every scan
# mode skips them. Marked either by a card label or the "Online review status"
# (ziflow_status) label_select field being set to "Review Task".
REVIEW_TASK_MARKER = "review task"
_REVIEW_STATUS_FIELD_PATTERNS = ("online review status", "ziflow_status")


def _field_text_candidates(fnode: Dict[str, Any]) -> List[str]:
    """Possible display strings for a field value (handles JSON-encoded lists)."""
    out: List[str] = []
    val = fnode.get("value")
    if isinstance(val, str):
        s = val.strip()
        if s[:1] in '[{"':
            try:
                dec = json.loads(s)
            except Exception:
                dec = None
            if isinstance(dec, list):
                out.extend(str(x) for x in dec if isinstance(x, (str, int)))
            elif isinstance(dec, str):
                out.append(dec)
            elif dec is None and s:
                out.append(s)
        elif s:
            out.append(s)
    elif isinstance(val, list):
        out.extend(str(x) for x in val if isinstance(x, (str, int)))
    av = fnode.get("array_value")
    if isinstance(av, list):
        for item in av:
            if isinstance(item, dict) and item.get("name"):
                out.append(str(item["name"]))
            elif isinstance(item, str):
                out.append(item)
    return out


def is_review_task_card(card: Dict[str, Any]) -> bool:
    """True when the card is a Review Task (card label or online-review field)."""
    for lab in card.get("labels") or []:
        name = lab.get("name") if isinstance(lab, dict) else lab
        if normalize_phase(str(name or "")) == REVIEW_TASK_MARKER:
            return True
    for fnode in card.get("fields") or []:
        meta = (fnode or {}).get("field") or {}
        hay = f"{meta.get('label') or ''} {fnode.get('name') or ''} {meta.get('id') or ''}".lower()
        if not any(p in hay for p in _REVIEW_STATUS_FIELD_PATTERNS):
            continue
        for text in _field_text_candidates(fnode):
            if normalize_phase(text) == REVIEW_TASK_MARKER:
                return True
    return False


def compute_add_only(current_ids: Sequence[str], old_id: str, new_id: str) -> Optional[List[str]]:
    """Return new id list when we want to *add* new_id without removing old_id.

    Use this for card-level updates where Pipefy's `updateCard` mutation
    re-broadcasts notifications to every assignee whenever the assignee
    list changes. By keeping the existing list intact and only appending
    the new user, Pipefy only fires a notification for the genuinely new
    assignment — the old user can then be removed manually in the admin
    panel without spamming everyone again.

    - Only acts when `old_id` is present (matches discovery scope).
    - Returns None if there is nothing to do (old absent, or new already there).
    """
    current = [str(x) for x in current_ids]
    if old_id not in current:
        return None
    if new_id in current:
        return None
    return current + [new_id]


@dataclass
class PlannedAction:
    scope: str  # "card" or "field:<id>"
    field_id: Optional[str]
    old_ids: List[str]
    new_ids: List[str]
    field_label: Optional[str] = None  # human-readable, e.g. "Assigned Team Member"
    mode: str = "swap"  # "swap" (default) or "add_only" (card-level only, keeps old user)
    old_names: List[str] = field(default_factory=list)  # before-state display names
    new_names: List[str] = field(default_factory=list)  # after-state display names


@dataclass
class CardPlan:
    card_id: str
    card_url: str
    title: str
    phase_name: str
    actions: List[PlannedAction] = field(default_factory=list)
    skipped_reason: Optional[str] = None
    client: str = ""  # client/brand display string, when discoverable
    product: str = ""  # product display string, when discoverable


def plan_card(
    card: Dict[str, Any],
    *,
    old_id: str,
    new_id: str,
    excluded_phases: Sequence[str],
    include_card_level: bool,
    field_filter: Optional[Set[str]],
    card_add_only: bool = False,
    operation: str = "replace",
    id_names: Optional[Dict[str, str]] = None,
) -> CardPlan:
    if operation not in {"replace", "add"}:
        raise ValueError(f"Unsupported assignment operation: {operation}")
    phase = ((card.get("current_phase") or {}).get("name")) or ""
    plan = CardPlan(
        card_id=str(card.get("id")),
        card_url=card.get("url") or "",
        title=card.get("title") or "",
        phase_name=phase,
        client=extract_client(card),
        product=extract_product(card),
    )
    excluded_norm = {normalize_phase(p) for p in excluded_phases}
    if normalize_phase(phase) in excluded_norm:
        plan.skipped_reason = f"excluded phase: {phase}"
        return plan

    # Build an id -> display-name map from the card itself, then layer in any
    # caller-supplied names (e.g. the resolved old/new users, since the new
    # user is not yet present on the card). Used to show before/after names.
    name_map = build_card_name_lookup(card)
    if id_names:
        for k, v in id_names.items():
            if v:
                name_map[str(k)] = str(v)

    def _names(ids: Sequence[str]) -> List[str]:
        return [name_map.get(str(i), str(i)) for i in ids]

    # Card-level assignee handling is gated by `include_card_level`. When it
    # is False we do NOT touch the card-level assignee list at all — not even
    # to remove the old user. This matters because Pipefy's `updateCard`
    # mutation replaces the whole assignee list and re-broadcasts a
    # notification to EVERY assignee on the card (previous + new), regardless
    # of how small the net change is. A field-only swap must therefore avoid
    # calling `updateCard` entirely, so the only people notified are those the
    # field change actually concerns.
    #
    # When card-level IS included and `card_add_only` is True we only append
    # the new user (keeping the old user) so the operator can remove the old
    # user manually in Pipefy. Note: even add-only still triggers Pipefy's
    # per-assignee notification on `updateCard`; the only fully spam-free
    # option is to leave card-level untouched (`include_card_level=False`).
    if include_card_level:
        ids = [str(a.get("id")) for a in (card.get("assignees") or []) if a and a.get("id") is not None]
        if operation == "add" or card_add_only:
            new_ids = compute_add_only(ids, old_id, new_id)
            if new_ids is not None and new_ids != ids:
                plan.actions.append(PlannedAction(
                    "card", None, ids, new_ids, mode="add_only",
                    old_names=_names(ids), new_names=_names(new_ids),
                ))
        else:
            new_ids = compute_swap(ids, old_id, new_id)
            if new_ids is not None and new_ids != ids:
                plan.actions.append(PlannedAction(
                    "card", None, ids, new_ids,
                    old_names=_names(ids), new_names=_names(new_ids),
                ))

    for fnode in card.get("fields") or []:
        meta = (fnode or {}).get("field") or {}
        ftype = meta.get("type")
        fid = str(meta.get("id")) if meta.get("id") is not None else None
        if not fid or ftype not in ASSIGNEE_FIELD_TYPES:
            continue
        if field_filter is not None and fid not in field_filter:
            continue
        ids = parse_field_user_ids(fnode)
        new_ids = compute_add_only(ids, old_id, new_id) if operation == "add" else compute_swap(ids, old_id, new_id)
        if new_ids is not None and new_ids != ids:
            label = (meta.get("label") or "").strip() or None
            plan.actions.append(PlannedAction(
                f"field:{fid}", fid, ids, new_ids, field_label=label,
                mode="add_only" if operation == "add" else "swap",
                old_names=_names(ids), new_names=_names(new_ids),
            ))

    return plan


def plan_remove_card(
    card: Dict[str, Any],
    *,
    user_id: str,
    field_ids: Optional[Set[str]] = None,
    include_card_level: bool = False,
    id_names: Optional[Dict[str, str]] = None,
) -> CardPlan:
    """Plan removal of one user from a card's recorded scopes only.

    Used by the cover-undo flow: `field_ids` / `include_card_level` come from
    the stored cover rows, so we never touch a field the tool didn't add the
    user to. Cards/fields where the user is already gone yield no actions
    (idempotent — safe to re-run).
    """
    plan = CardPlan(
        card_id=str(card.get("id")),
        card_url=card.get("url") or "",
        title=card.get("title") or "",
        phase_name=((card.get("current_phase") or {}).get("name")) or "",
        client=extract_client(card),
        product=extract_product(card),
    )
    name_map = build_card_name_lookup(card)
    if id_names:
        for k, v in id_names.items():
            if v:
                name_map[str(k)] = str(v)

    def _names(ids: Sequence[str]) -> List[str]:
        return [name_map.get(str(i), str(i)) for i in ids]

    if include_card_level:
        ids = [str(a.get("id")) for a in (card.get("assignees") or []) if a and a.get("id") is not None]
        new_ids = compute_remove(ids, user_id)
        if new_ids is not None:
            plan.actions.append(PlannedAction(
                "card", None, ids, new_ids, mode="remove",
                old_names=_names(ids), new_names=_names(new_ids),
            ))

    wanted = {str(f) for f in (field_ids or set())}
    for fnode in card.get("fields") or []:
        meta = (fnode or {}).get("field") or {}
        fid = str(meta.get("id")) if meta.get("id") is not None else None
        if not fid or meta.get("type") not in ASSIGNEE_FIELD_TYPES or fid not in wanted:
            continue
        ids = parse_field_user_ids(fnode)
        new_ids = compute_remove(ids, user_id)
        if new_ids is not None:
            label = (meta.get("label") or "").strip() or None
            plan.actions.append(PlannedAction(
                f"field:{fid}", fid, ids, new_ids, field_label=label, mode="remove",
                old_names=_names(ids), new_names=_names(new_ids),
            ))

    return plan


def plan_assign_card(
    card: Dict[str, Any],
    *,
    new_id: str,
    field_ids: Set[str],
    excluded_phases: Sequence[str] = (),
    id_names: Optional[Dict[str, str]] = None,
    field_meta: Optional[Dict[str, Dict[str, str]]] = None,
) -> CardPlan:
    """Plan adding one user to specific assignee fields, no old user required.

    Used by the client-match assign flow: the card was selected by title/client
    text, so we simply append `new_id` to each target field where they're not
    already present. Card-level assignees are never touched (avoids Pipefy's
    updateCard notification storm). Idempotent — already-assigned fields yield
    no action.

    Empty fields are often omitted from the card payload entirely; when
    `field_meta` ({field_id: {"label":...}}) is supplied, wanted fields missing
    from the card are still planned as [] -> [new_id].
    """
    phase = ((card.get("current_phase") or {}).get("name")) or ""
    plan = CardPlan(
        card_id=str(card.get("id")),
        card_url=card.get("url") or "",
        title=card.get("title") or "",
        phase_name=phase,
        client=extract_client(card),
        product=extract_product(card),
    )
    excluded_norm = {normalize_phase(p) for p in excluded_phases}
    if normalize_phase(phase) in excluded_norm:
        plan.skipped_reason = f"excluded phase: {phase}"
        return plan

    name_map = build_card_name_lookup(card)
    if id_names:
        for k, v in id_names.items():
            if v:
                name_map[str(k)] = str(v)

    def _names(ids: Sequence[str]) -> List[str]:
        return [name_map.get(str(i), str(i)) for i in ids]

    wanted = {str(f) for f in (field_ids or set())}
    uid = str(new_id)
    seen: Set[str] = set()
    for fnode in card.get("fields") or []:
        meta = (fnode or {}).get("field") or {}
        fid = str(meta.get("id")) if meta.get("id") is not None else None
        if not fid or meta.get("type") not in ASSIGNEE_FIELD_TYPES or fid not in wanted:
            continue
        seen.add(fid)
        ids = parse_field_user_ids(fnode)
        if uid in ids:
            continue
        new_ids = ids + [uid]
        label = (meta.get("label") or "").strip() or None
        plan.actions.append(PlannedAction(
            f"field:{fid}", fid, ids, new_ids, field_label=label, mode="add_only",
            old_names=_names(ids), new_names=_names(new_ids),
        ))

    if field_meta:
        for fid in sorted(wanted - seen):
            meta = field_meta.get(fid) or {}
            label = (meta.get("label") or "").strip() or None
            plan.actions.append(PlannedAction(
                f"field:{fid}", fid, [], [uid], field_label=label, mode="add_only",
                old_names=[], new_names=_names([uid]),
            ))

    return plan


def execute_plan(client, plan: CardPlan, *, throttle_ms: int = 250, sleep=time.sleep) -> List[Tuple[PlannedAction, Optional[str]]]:
    """Execute every action in the plan. Returns [(action, error_message_or_None), ...]."""
    results: List[Tuple[PlannedAction, Optional[str]]] = []
    for action in plan.actions:
        try:
            if action.scope == "card":
                client.execute(UPDATE_CARD_MUT, {"cardId": plan.card_id, "assigneeIds": action.new_ids})
            else:
                client.execute(
                    UPDATE_FIELD_MUT,
                    {"cardId": plan.card_id, "fieldId": action.field_id, "value": action.new_ids},
                )
            results.append((action, None))
        except Exception as exc:
            log.error("Mutation failed for card %s scope=%s: %s", plan.card_id, action.scope, exc)
            results.append((action, str(exc)))
        if throttle_ms > 0:
            sleep(throttle_ms / 1000.0)
    return results
