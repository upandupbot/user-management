"""Plan + execute the assignee swap on a card."""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple


log = logging.getLogger(__name__)

ASSIGNEE_FIELD_TYPES = {"assignee_select", "user", "member"}

DEFAULT_EXCLUDED_PHASES = ("Cancelled", "Completed", "Finance Recon")


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


@dataclass
class PlannedAction:
    scope: str  # "card" or "field:<id>"
    field_id: Optional[str]
    old_ids: List[str]
    new_ids: List[str]


@dataclass
class CardPlan:
    card_id: str
    card_url: str
    title: str
    phase_name: str
    actions: List[PlannedAction] = field(default_factory=list)
    skipped_reason: Optional[str] = None


def plan_card(
    card: Dict[str, Any],
    *,
    old_id: str,
    new_id: str,
    excluded_phases: Sequence[str],
    include_card_level: bool,
    field_filter: Optional[Set[str]],
) -> CardPlan:
    phase = ((card.get("current_phase") or {}).get("name")) or ""
    plan = CardPlan(
        card_id=str(card.get("id")),
        card_url=card.get("url") or "",
        title=card.get("title") or "",
        phase_name=phase,
    )
    excluded_norm = {normalize_phase(p) for p in excluded_phases}
    if normalize_phase(phase) in excluded_norm:
        plan.skipped_reason = f"excluded phase: {phase}"
        return plan

    if include_card_level:
        ids = [str(a.get("id")) for a in (card.get("assignees") or []) if a and a.get("id") is not None]
        new_ids = compute_swap(ids, old_id, new_id)
        if new_ids is not None and new_ids != ids:
            plan.actions.append(PlannedAction("card", None, ids, new_ids))

    for fnode in card.get("fields") or []:
        meta = (fnode or {}).get("field") or {}
        ftype = meta.get("type")
        fid = str(meta.get("id")) if meta.get("id") is not None else None
        if not fid or ftype not in ASSIGNEE_FIELD_TYPES:
            continue
        if field_filter is not None and fid not in field_filter:
            continue
        ids = parse_field_user_ids(fnode)
        new_ids = compute_swap(ids, old_id, new_id)
        if new_ids is not None and new_ids != ids:
            plan.actions.append(PlannedAction(f"field:{fid}", fid, ids, new_ids))

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
