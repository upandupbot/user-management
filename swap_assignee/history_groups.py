"""Turn per-field audit rows into one readable entry per change.

The audit log stores one row per role field per card, so a single
"replace Katekani with Simphiwe on Account Management" apply can write
hundreds of rows. History and notifications show these grouped back
into the change the operator actually made:

    "Katekani Nyamandi was replaced with Simphiwe Zungu on 189 jobs in
     Account Management."

Rows written since `run_id` was added are grouped by it. Older rows are
grouped by operator, board, change type and people, as long as each row
is within GAP_SECONDS of the previous one.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

GAP_SECONDS = 15 * 60

MODE_LABELS = {
    "replace": "Replaced",
    "coassign": "Co-assignee added",
    "client": "Added to client cards",
    "undo": "Saved change undone",
}

_CLIENT_MATCH_RE = re.compile(r'^\s*client match\s*[“"](.*?)[”"]\s*$', re.IGNORECASE)


def _epoch(ts: Any) -> float:
    try:
        return datetime.strptime(str(ts), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except Exception:  # noqa: BLE001 - hand-edited or legacy rows
        return 0.0


def person_label(value: Any, fallback: str = "") -> str:
    """"Magda Smit <magda@x>" -> "Magda Smit"; "simphiwe.zungu@x" -> "Simphiwe Zungu"."""
    text = str(value or "").strip()
    text = re.sub(r"\s*<[^>]+>", "", text).strip()
    if "@" in text:
        text = text.split("@", 1)[0]
    if text and " " not in text and text == text.lower():
        # Email local parts and Pipefy handles: "mbali-zwane" -> "Mbali Zwane".
        text = " ".join(part[:1].upper() + part[1:] for part in re.split(r"[._-]+", text) if part)
    return text or fallback


def _email_of(value: Any) -> str:
    m = re.search(r"<([^>]+@[^>]+)>", str(value or "")) or re.search(r"(\S+@\S+)", str(value or ""))
    return m.group(1) if m else ""


def client_match_text(record: Dict[str, Any]) -> str:
    m = _CLIENT_MATCH_RE.match(str(record.get("old_user") or ""))
    return m.group(1).strip() if m else ""


def change_mode(record: Dict[str, Any]) -> str:
    """replace | coassign | client | undo, from what the audit row stores."""
    op = str(record.get("operation") or "replace").strip().casefold()
    if op == "remove":
        return "undo"
    if op in ("add", "assign"):
        return "client" if client_match_text(record) else "coassign"
    return "replace"


def _jobs(n: int) -> str:
    return f"{n} job" if n == 1 else f"{n} jobs"


def _join(items: List[str]) -> str:
    items = [i for i in items if i]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def change_sentence(group: Dict[str, Any]) -> str:
    """Plain-English summary of one grouped change."""
    mode = group["mode"]
    n = group["job_count"]
    where = f" in {group['board']}" if group.get("board") else ""
    old = group.get("old_person") or "someone"
    new = group.get("new_person") or "someone"
    if mode == "replace":
        return f"{old} was replaced with {new} on {_jobs(n)}{where}."
    if mode == "coassign":
        return f"{new} was added alongside {old} on {_jobs(n)}{where}."
    if mode == "client":
        client = group.get("client_match") or "client"
        return f"{new} was added to {n} {client} job{'' if n == 1 else 's'}{where}."
    return f"{old} was removed from {_jobs(n)}{where} (undoing a saved change)."


def _group_key(record: Dict[str, Any]) -> tuple:
    return (
        str(record.get("operator_email") or record.get("os_user") or "").casefold(),
        str(record.get("pipe_id") or record.get("pipe_name") or ""),
        change_mode(record),
        str(record.get("old_user") or "").casefold(),
        str(record.get("new_user") or "").casefold(),
    )


def group_changes(records: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Group audit rows (any order) into changes, newest first."""
    rows = sorted(records, key=lambda r: _epoch(r.get("ts")), reverse=True)
    groups: List[Dict[str, Any]] = []
    by_run: Dict[str, Dict[str, Any]] = {}
    open_by_key: Dict[tuple, Dict[str, Any]] = {}

    for rec in rows:
        run_id = str(rec.get("run_id") or "").strip()
        key = _group_key(rec)
        ts = _epoch(rec.get("ts"))
        group: Optional[Dict[str, Any]] = None
        if run_id:
            group = by_run.get(run_id)
        else:
            candidate = open_by_key.get(key)
            if candidate is not None and candidate["_oldest"] - ts <= GAP_SECONDS:
                group = candidate
        if group is None:
            group = {
                "_cards": {},
                "_oldest": ts,
                "ts": rec.get("ts"),
                "started_ts": rec.get("ts"),
                "operator_email": rec.get("operator_email") or rec.get("os_user") or "",
                "operator": person_label(rec.get("operator_email") or rec.get("os_user"), "Someone"),
                "mode": change_mode(rec),
                "board": rec.get("pipe_name") or "",
                "pipe_id": rec.get("pipe_id") or "",
                "old_user": rec.get("old_user") or "",
                "new_user": rec.get("new_user") or "",
                "client_match": client_match_text(rec),
                "run_id": run_id,
            }
            group["old_person"] = "" if group["mode"] == "client" else person_label(group["old_user"])
            group["new_person"] = person_label(group["new_user"])
            if group["old_person"] and group["old_person"] == group["new_person"]:
                # Same display name on two accounts: say which account is which.
                old_email, new_email = _email_of(group["old_user"]), _email_of(group["new_user"])
                if old_email and new_email and old_email != new_email:
                    group["old_person"] += f" ({old_email})"
                    group["new_person"] += f" ({new_email})"
            groups.append(group)
            if run_id:
                by_run[run_id] = group
            else:
                open_by_key[key] = group
        group["_oldest"] = min(group["_oldest"], ts)
        group["started_ts"] = rec.get("ts") if ts <= _epoch(group["started_ts"]) else group["started_ts"]

        card_id = str(rec.get("card_id") or rec.get("title") or len(group["_cards"]))
        card = group["_cards"].setdefault(card_id, {
            "card_id": rec.get("card_id") or "",
            "title": rec.get("title") or "",
            "card_url": rec.get("card_url") or "",
            "client": rec.get("client") or "",
            "phase": rec.get("phase") or "",
            "roles": [],
            "status": "ok",
            "error": "",
        })
        role = "Card assignee" if rec.get("scope") == "card" else (rec.get("field_label") or "")
        if role and role not in card["roles"]:
            card["roles"].append(role)
        status = str(rec.get("status") or "ok")
        if status != "ok" and card["status"] == "ok":
            card["status"] = status
        if rec.get("error") and not card["error"]:
            card["error"] = rec.get("error")

    out: List[Dict[str, Any]] = []
    for g in groups:
        cards = list(g.pop("_cards").values())
        g.pop("_oldest", None)
        roles: List[str] = []
        clients: List[str] = []
        for c in cards:
            for r in c["roles"]:
                if r not in roles:
                    roles.append(r)
            if c["client"] and c["client"] not in clients:
                clients.append(c["client"])
        g["cards"] = cards
        g["job_count"] = len(cards)
        g["ok_count"] = sum(1 for c in cards if c["status"] == "ok")
        g["failed_count"] = g["job_count"] - g["ok_count"]
        g["roles"] = roles
        g["clients"] = clients
        g["mode_label"] = MODE_LABELS.get(g["mode"], "Changed")
        g["sentence"] = change_sentence(g)
        g["roles_text"] = ("as " + _join(roles)) if roles else ""
        g["status"] = "ok" if g["failed_count"] == 0 else ("error" if g["ok_count"] == 0 else "partial")
        out.append(g)
    return out
