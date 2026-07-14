"""Audit trail for *real* assignee swaps.

Two sinks, both best-effort and non-blocking (a logging failure must never
block or revert a Pipefy change):

1. A local append-only JSONL store with 30-day retention. This is the durable
   source of truth and works fully offline.
2. An optional Google Sheet mirror via an Apps Script webhook URL. When the URL
   is configured we POST a batch of records; if it is missing or the network
   fails we simply skip it (the local store still has the record).

Every operator must identify themselves with a company email before they can
apply changes. The allow-list below is enforced both client-side (for UX) and
server-side (for real). Anything outside these domains is rejected.
"""
from __future__ import annotations

import getpass
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence


log = logging.getLogger("swap_assignee.audit")


# Company domains permitted to use the tool. Lower-case, no leading "@".
ALLOWED_EMAIL_DOMAINS = frozenset({
    "upandupgroup.com",
    "dydx.digital",
    "mcsaatchiabel.co.za",
    "upandupconnect.com",
    "inblackandwhite.co.za",
    "levergy.co.za",
    "razorpr.co.za",
})

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

RETENTION_DAYS = 30
_SECONDS_PER_DAY = 86400

_STORE_LOCK = threading.Lock()


# Canonical column order for the shared Google Sheet. Both the Apps Script
# webhook and the direct service-account sink write rows in this order. Append
# new columns at the END to stay backwards-compatible with existing sheets.
AUDIT_COLUMNS = (
    "ts",
    "operator_email",
    "os_user",
    "source",
    "pipe_id",
    "pipe_name",
    "card_id",
    "card_url",
    "title",
    "client",
    "phase",
    "scope",
    "field_label",
    "old_user",
    "new_user",
    "before",
    "after",
    "status",
    "error",
    "operation",
)


def normalize_email(email: Optional[str]) -> str:
    return (email or "").strip().lower()


def email_domain(email: Optional[str]) -> str:
    e = normalize_email(email)
    return e.split("@", 1)[1] if "@" in e else ""


def is_allowed_email(email: Optional[str]) -> bool:
    """True only for a syntactically valid email on an allow-listed domain."""
    e = normalize_email(email)
    if not _EMAIL_RE.match(e):
        return False
    return email_domain(e) in ALLOWED_EMAIL_DOMAINS


def default_store_path() -> Path:
    """Per-user store location, overridable via SWAP_AUDIT_STORE."""
    override = os.environ.get("SWAP_AUDIT_STORE")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".swap_assignee" / "history.jsonl"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def os_user() -> str:
    try:
        return getpass.getuser()
    except Exception:  # pragma: no cover - platform edge
        return ""


def build_records(
    *,
    operator_email: str,
    source: str,
    pipe_id: str,
    pipe_name: str,
    old_user: str,
    new_user: str,
    plan_dict: Dict[str, Any],
    status: str,
    error: str = "",
    operation: str = "replace",
) -> List[Dict[str, Any]]:
    """One audit record per planned action on a card (card-level + each field)."""
    ts = _now_iso()
    user = os_user()
    base = {
        "ts": ts,
        "operator_email": normalize_email(operator_email),
        "os_user": user,
        "source": source,
        "pipe_id": str(pipe_id or ""),
        "pipe_name": pipe_name or "",
        "card_id": str(plan_dict.get("card_id") or ""),
        "card_url": plan_dict.get("card_url") or "",
        "title": plan_dict.get("title") or "",
        "client": plan_dict.get("client") or "",
        "phase": plan_dict.get("phase_name") or "",
        "old_user": old_user or "",
        "new_user": new_user or "",
        "status": status,
        "error": error or "",
        "operation": operation,
    }
    out: List[Dict[str, Any]] = []
    for a in plan_dict.get("actions") or []:
        before = a.get("old_names") or a.get("old_ids") or []
        after = a.get("new_names") or a.get("new_ids") or []
        rec = dict(base)
        rec["scope"] = a.get("scope") or ""
        rec["field_label"] = a.get("field_label") or ""
        rec["before"] = ", ".join(str(x) for x in before)
        rec["after"] = ", ".join(str(x) for x in after)
        out.append(rec)
    if not out:
        # No actions (shouldn't normally happen for an applied card) — still
        # log a single summary row so the apply isn't invisible.
        rec = dict(base)
        rec["scope"] = ""
        rec["field_label"] = ""
        rec["before"] = ""
        rec["after"] = ""
        out.append(rec)
    return out


class AuditStore:
    """Append-only JSONL store with rolling 30-day retention."""

    def __init__(self, path: Optional[os.PathLike[str] | str] = None, *, retention_days: int = RETENTION_DAYS) -> None:
        self.path = Path(path) if path else default_store_path()
        self.retention_days = retention_days

    def append(self, records: Sequence[Dict[str, Any]]) -> int:
        """Append records, then prune anything older than the retention window."""
        records = list(records or [])
        if not records:
            return 0
        with _STORE_LOCK:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                for rec in records:
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            self._prune_locked()
        return len(records)

    def recent(self, *, limit: int = 200, days: Optional[int] = None) -> List[Dict[str, Any]]:
        """Return the most recent records (newest first), within `days`."""
        cutoff = None
        if days is not None:
            cutoff = time.time() - days * _SECONDS_PER_DAY
        rows = self._read_all()
        if cutoff is not None:
            rows = [r for r in rows if _record_epoch(r) >= cutoff]
        rows.reverse()
        if limit and limit > 0:
            rows = rows[:limit]
        return rows

    # --- internals ---

    def _read_all(self) -> List[Dict[str, Any]]:
        if not self.path.exists():
            return []
        out: List[Dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
        return out

    def _prune_locked(self) -> None:
        cutoff = time.time() - self.retention_days * _SECONDS_PER_DAY
        rows = self._read_all()
        kept = [r for r in rows if _record_epoch(r) >= cutoff]
        if len(kept) == len(rows):
            return
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            for rec in kept:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        tmp.replace(self.path)


def _record_epoch(rec: Dict[str, Any]) -> float:
    ts = rec.get("ts")
    if not ts:
        return 0.0
    try:
        dt = datetime.strptime(str(ts), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return 0.0


def post_to_webhook(url: Optional[str], records: Sequence[Dict[str, Any]], *, timeout: float = 5.0) -> bool:
    """Best-effort POST of records to the Apps Script webhook. Never raises."""
    if not url or not records:
        return False
    try:
        import urllib.request

        payload = json.dumps({"records": list(records)}).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec - user-configured URL
            resp.read()
        return True
    except Exception as exc:  # noqa: BLE001 - logging must not block applies
        log.warning("Audit webhook POST failed (record kept locally): %s", exc)
        return False


def record_swap(
    *,
    store: AuditStore,
    webhook_url: Optional[str],
    operator_email: str,
    source: str,
    pipe_id: str,
    pipe_name: str,
    old_user: str,
    new_user: str,
    plan_dict: Dict[str, Any],
    status: str,
    error: str = "",
    operation: str = "replace",
    sheet_sink: Any = None,
) -> List[Dict[str, Any]]:
    """Build records for one applied card, persist locally, mirror to webhook/sheet."""
    records = build_records(
        operator_email=operator_email,
        source=source,
        pipe_id=pipe_id,
        pipe_name=pipe_name,
        old_user=old_user,
        new_user=new_user,
        plan_dict=plan_dict,
        status=status,
        error=error,
        operation=operation,
    )
    try:
        store.append(records)
    except Exception as exc:  # noqa: BLE001
        log.error("Failed to write local audit store: %s", exc)
    post_to_webhook(webhook_url, records)
    if sheet_sink is not None:
        from .sheets_sink import safe_append  # lazy import (optional dependency)

        safe_append(sheet_sink, records)
    return records
