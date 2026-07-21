"""Google Sheet-backed store for "cover" batches (co-assignee undo).

When an operator adds a co-assignee to cover for someone on leave, they can
opt in to *remembering* the cover: which cards/fields the person was added
to, under a named batch ("Sam covering Alice — 24 Jul to 4 Aug"). When the
leave ends, the site lists active covers and the operator removes the
co-assignee from exactly those cards — nothing inferred, nothing guessed.

Storage lives in two tabs of the existing audit spreadsheet (same
service-account credentials as the audit sink):

- ``Covers``      one row per batch (name, who, dates, status)
- ``CoverCards``  one row per recorded card action (scope/field, status)

Rows are marked ``removed`` as the undo is applied; a batch flips to
``ended`` when no active card rows remain. Everything here is best-effort
on the *write* path (a storage failure must never block a Pipefy apply),
but read/update failures during the undo flow surface as errors so the
operator knows the state is unclear.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Set

from .sheets_sink import load_sheet_credentials

log = logging.getLogger("swap_assignee.covers")

BATCH_TAB = "Covers"
CARD_TAB = "CoverCards"

# Append new columns at the END to stay compatible with existing sheets.
BATCH_COLUMNS = (
    "batch_id",
    "name",
    "leaver",
    "cover_user_id",
    "cover_user_name",
    "cover_user_email",
    "start_date",
    "end_date",
    "created_by",
    "created_ts",
    "status",
)

CARD_COLUMNS = (
    "batch_id",
    "ts",
    "pipe_id",
    "pipe_name",
    "card_id",
    "card_url",
    "title",
    "client",
    "product",
    "scope",
    "field_id",
    "field_label",
    "status",
)

STATUS_ACTIVE = "active"
STATUS_REMOVED = "removed"
STATUS_ENDED = "ended"

_LOCK = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _col_letter(idx: int) -> str:
    """0-based column index -> A1 letter (supports beyond Z for safety)."""
    out = ""
    n = idx + 1
    while n:
        n, rem = divmod(n - 1, 26)
        out = chr(ord("A") + rem) + out
    return out


class CoverStore:
    """Reads/writes cover batches in a Google Sheet via the Sheets API."""

    def __init__(self, sheet_id: str, credentials: Any) -> None:
        from googleapiclient.discovery import build  # lazy import

        self.sheet_id = sheet_id
        self._svc = build("sheets", "v4", credentials=credentials, cache_discovery=False)
        self._ensured = False

    # ---------- low-level sheet I/O (overridden by tests) ----------

    def _ensure(self) -> None:
        """Create both tabs + header rows if missing (idempotent)."""
        if self._ensured:
            return
        meta = self._svc.spreadsheets().get(spreadsheetId=self.sheet_id).execute()
        titles = [s["properties"]["title"] for s in meta.get("sheets", [])]
        requests = [
            {"addSheet": {"properties": {"title": tab}}}
            for tab in (BATCH_TAB, CARD_TAB)
            if tab not in titles
        ]
        if requests:
            self._svc.spreadsheets().batchUpdate(
                spreadsheetId=self.sheet_id, body={"requests": requests}
            ).execute()
        for tab, cols in ((BATCH_TAB, BATCH_COLUMNS), (CARD_TAB, CARD_COLUMNS)):
            header = (
                self._svc.spreadsheets()
                .values()
                .get(spreadsheetId=self.sheet_id, range=f"{tab}!A1:1")
                .execute()
            )
            current = (header.get("values") or [[]])[0]
            if list(current) != list(cols):
                self._svc.spreadsheets().values().update(
                    spreadsheetId=self.sheet_id,
                    range=f"{tab}!A1",
                    valueInputOption="RAW",
                    body={"values": [list(cols)]},
                ).execute()
        self._ensured = True

    def _read_rows(self, tab: str, columns: Sequence[str]) -> List[Dict[str, Any]]:
        """All data rows of a tab as dicts; each carries its 1-based sheet row in `_row`."""
        self._ensure()
        resp = (
            self._svc.spreadsheets()
            .values()
            .get(spreadsheetId=self.sheet_id, range=f"{tab}!A2:{_col_letter(len(columns) - 1)}")
            .execute()
        )
        out: List[Dict[str, Any]] = []
        for i, raw in enumerate(resp.get("values") or []):
            rec = {col: (raw[j] if j < len(raw) else "") for j, col in enumerate(columns)}
            rec["_row"] = i + 2  # 1-based + header row
            out.append(rec)
        return out

    def _append_rows(self, tab: str, columns: Sequence[str], records: Sequence[Dict[str, Any]]) -> None:
        self._ensure()
        rows = [[str(rec.get(col) or "") for col in columns] for rec in records]
        self._svc.spreadsheets().values().append(
            spreadsheetId=self.sheet_id,
            range=f"{tab}!A1",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": rows},
        ).execute()

    def _update_cells(self, tab: str, col_idx: int, row_values: Dict[int, str]) -> None:
        """Set one column's value on specific sheet rows ({row_number: value})."""
        if not row_values:
            return
        letter = _col_letter(col_idx)
        data = [
            {"range": f"{tab}!{letter}{row}", "values": [[value]]}
            for row, value in sorted(row_values.items())
        ]
        self._svc.spreadsheets().values().batchUpdate(
            spreadsheetId=self.sheet_id,
            body={"valueInputOption": "RAW", "data": data},
        ).execute()

    # ---------- public API ----------

    def record(self, batch: Dict[str, Any], card_rows: Sequence[Dict[str, Any]]) -> int:
        """Store cover rows for an apply. Creates the batch row on first use.

        The same batch_id may arrive several times (the wizard applies pipe
        by pipe); the batch row is only written once.
        """
        card_rows = list(card_rows or [])
        if not card_rows:
            return 0
        with _LOCK:
            existing = {r["batch_id"] for r in self._read_rows(BATCH_TAB, BATCH_COLUMNS)}
            if batch["batch_id"] not in existing:
                rec = dict(batch)
                rec.setdefault("created_ts", _now_iso())
                rec.setdefault("status", STATUS_ACTIVE)
                self._append_rows(BATCH_TAB, BATCH_COLUMNS, [rec])
            ts = _now_iso()
            for r in card_rows:
                r.setdefault("ts", ts)
                r.setdefault("status", STATUS_ACTIVE)
                r["batch_id"] = batch["batch_id"]
            self._append_rows(CARD_TAB, CARD_COLUMNS, card_rows)
        return len(card_rows)

    def list_batches(self, *, status: Optional[str] = STATUS_ACTIVE) -> List[Dict[str, Any]]:
        """Batches (optionally filtered by status), each with card counts."""
        batches = self._read_rows(BATCH_TAB, BATCH_COLUMNS)
        if status:
            batches = [b for b in batches if (b.get("status") or STATUS_ACTIVE) == status]
        if not batches:
            return []
        cards = self._read_rows(CARD_TAB, CARD_COLUMNS)
        by_batch: Dict[str, Dict[str, Any]] = {}
        for c in cards:
            agg = by_batch.setdefault(c["batch_id"], {"active": 0, "removed": 0, "pipes": set()})
            key = "active" if (c.get("status") or STATUS_ACTIVE) == STATUS_ACTIVE else "removed"
            agg[key] += 1
            if c.get("pipe_name"):
                agg["pipes"].add(c["pipe_name"])
        out = []
        for b in batches:
            agg = by_batch.get(b["batch_id"], {"active": 0, "removed": 0, "pipes": set()})
            rec = {k: v for k, v in b.items() if k != "_row"}
            rec["cards_active"] = agg["active"]
            rec["cards_removed"] = agg["removed"]
            rec["pipe_names"] = sorted(agg["pipes"])
            out.append(rec)
        return out

    def list_cards(self, batch_id: str, *, status: Optional[str] = STATUS_ACTIVE) -> List[Dict[str, Any]]:
        rows = [r for r in self._read_rows(CARD_TAB, CARD_COLUMNS) if r["batch_id"] == batch_id]
        if status:
            rows = [r for r in rows if (r.get("status") or STATUS_ACTIVE) == status]
        return [{k: v for k, v in r.items() if k != "_row"} for r in rows]

    def mark_cards_removed(self, batch_id: str, card_ids: Set[str]) -> bool:
        """Flip CoverCards rows to removed; end the batch if none stay active.

        Returns True when the batch was ended.
        """
        wanted = {str(c) for c in card_ids}
        with _LOCK:
            rows = self._read_rows(CARD_TAB, CARD_COLUMNS)
            updates: Dict[int, str] = {}
            remaining_active = 0
            for r in rows:
                if r["batch_id"] != batch_id:
                    continue
                active = (r.get("status") or STATUS_ACTIVE) == STATUS_ACTIVE
                if active and str(r.get("card_id")) in wanted:
                    updates[r["_row"]] = STATUS_REMOVED
                elif active:
                    remaining_active += 1
            self._update_cells(CARD_TAB, CARD_COLUMNS.index("status"), updates)
            if remaining_active == 0:
                b_updates = {
                    r["_row"]: STATUS_ENDED
                    for r in self._read_rows(BATCH_TAB, BATCH_COLUMNS)
                    if r["batch_id"] == batch_id and (r.get("status") or STATUS_ACTIVE) == STATUS_ACTIVE
                }
                self._update_cells(BATCH_TAB, BATCH_COLUMNS.index("status"), b_updates)
                return bool(b_updates)
        return False


def build_cover_store(
    sheet_id: Optional[str],
    service_account_file: Optional[str],
    *,
    service_account_json: Optional[str] = None,
) -> Optional[CoverStore]:
    """Construct a CoverStore, or None if not configured / unavailable.

    Never raises — with no store the web UI simply hides the cover features.
    """
    if not sheet_id:
        return None
    creds = load_sheet_credentials(service_account_file, service_account_json)
    if creds is None:
        return None
    try:
        return CoverStore(sheet_id, creds)
    except Exception as exc:  # noqa: BLE001 - never block app start
        log.warning("Could not initialise cover store: %s", exc)
        return None
