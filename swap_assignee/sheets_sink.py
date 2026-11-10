"""Direct Google Sheets audit sink (service-account auth).

Use this when your Google Workspace blocks anonymous Apps Script web apps
(the "You need access" page). Instead of POSTing to a public web app, the tool
authenticates as a service account and appends rows straight to the spreadsheet
via the Google Sheets API.

One-time setup
--------------
1. Google Cloud Console -> create (or pick) a project.
2. APIs & Services -> Library -> enable **Google Sheets API**.
3. APIs & Services -> Credentials -> Create credentials -> **Service account**.
   Give it any name; no roles are needed.
4. Open the new service account -> **Keys** -> Add key -> Create new key ->
   **JSON**. A `.json` key file downloads — keep it private.
5. Open the key file and copy the `client_email`
   (looks like `something@your-project.iam.gserviceaccount.com`).
6. Open your audit Google Sheet -> **Share** -> paste that client_email ->
   give it **Editor** -> Send. (No Workspace admin needed; you're just sharing
   a doc you own.)
7. In config.ini:
       [Audit]
       sheet_id = <the id from the sheet URL, between /d/ and /edit>
       service_account_file = C:\\path\\to\\key.json

Everything here is best-effort: if the Sheet is unreachable or the libraries
aren't installed, the swap still succeeds and the record stays in the local
JSONL store.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .audit import AUDIT_COLUMNS

log = logging.getLogger("swap_assignee.audit.sheets")

# Read/write a single spreadsheet the service account has been shared on.
_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def load_sheet_credentials(
    service_account_file: Optional[str],
    service_account_json: Optional[str] = None,
):
    """Build service-account credentials for the Sheets API, or None.

    Shared by the audit sink and the cover store. Never raises — returns
    None when the Google libraries are missing, the key file doesn't exist,
    or the key content is invalid.
    """
    if not (service_account_file or service_account_json):
        return None
    try:
        from google.oauth2.service_account import Credentials  # lazy import
    except ImportError:
        log.warning(
            "Google API libraries not installed; Google Sheets features are disabled. "
            "Run: pip install -r requirements.txt"
        )
        return None
    try:
        if service_account_json:
            import json

            info = json.loads(service_account_json)
            return Credentials.from_service_account_info(info, scopes=_SCOPES)
        path = Path(service_account_file).expanduser()
        if not path.exists():
            log.warning("Audit service_account_file not found: %s", path)
            return None
        return Credentials.from_service_account_file(str(path), scopes=_SCOPES)
    except Exception as exc:  # noqa: BLE001 - never block on credential init
        log.warning("Could not load Google service-account credentials: %s", exc)
        return None


def _cell(value: Any) -> Any:
    """Coerce a record value into something the Sheets API accepts."""
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _ts_epoch(ts: Any) -> float:
    """Best-effort ISO-8601 -> epoch seconds; unparseable rows are kept (inf)."""
    try:
        return datetime.strptime(str(ts), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except Exception:  # noqa: BLE001 - hand-edited or legacy rows
        return float("inf")


class GoogleSheetSink:
    """Appends audit rows to a tab of a Google Sheet via the Sheets API."""

    def __init__(self, sheet_id: str, credentials: Any, *, tab: str = "Audit") -> None:
        from googleapiclient.discovery import build  # lazy import

        self.sheet_id = sheet_id
        self.tab = tab
        self._svc = build("sheets", "v4", credentials=credentials, cache_discovery=False)
        self._ensured = False

    def _ensure(self) -> None:
        """Make sure the target tab and a header row exist (idempotent)."""
        if self._ensured:
            return
        meta = self._svc.spreadsheets().get(spreadsheetId=self.sheet_id).execute()
        titles = [s["properties"]["title"] for s in meta.get("sheets", [])]
        if self.tab not in titles:
            self._svc.spreadsheets().batchUpdate(
                spreadsheetId=self.sheet_id,
                body={"requests": [{"addSheet": {"properties": {"title": self.tab}}}]},
            ).execute()
        header = (
            self._svc.spreadsheets()
            .values()
            .get(spreadsheetId=self.sheet_id, range=f"{self.tab}!A1:1")
            .execute()
        )
        current_header = (header.get("values") or [[]])[0]
        if list(current_header) != list(AUDIT_COLUMNS):
            self._svc.spreadsheets().values().update(
                spreadsheetId=self.sheet_id,
                range=f"{self.tab}!A1",
                valueInputOption="RAW",
                body={"values": [list(AUDIT_COLUMNS)]},
            ).execute()
        self._ensured = True

    def append(self, records: Sequence[Dict[str, Any]]) -> bool:
        """Append one row per record. Returns True on success."""
        records = list(records or [])
        if not records:
            return False
        self._ensure()
        rows: List[List[Any]] = [
            [_cell(rec.get(col)) for col in AUDIT_COLUMNS] for rec in records
        ]
        self._svc.spreadsheets().values().append(
            spreadsheetId=self.sheet_id,
            range=f"{self.tab}!A1",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": rows},
        ).execute()
        return True

    def recent(self, *, limit: int = 300, days: Optional[int] = 30) -> List[Dict[str, Any]]:
        """Read back the newest audit rows (newest first) from the shared tab.

        This is what makes History a *team* view on hosted deploys: the local
        JSONL store resets on redeploy and only ever has one machine's applies,
        while the Sheet has everyone's.
        """
        self._ensure()
        last_col = chr(ord("A") + len(AUDIT_COLUMNS) - 1)
        resp = (
            self._svc.spreadsheets()
            .values()
            .get(spreadsheetId=self.sheet_id, range=f"{self.tab}!A2:{last_col}")
            .execute()
        )
        cutoff = (datetime.now(timezone.utc).timestamp() - days * 86400) if days else float("-inf")
        out: List[Dict[str, Any]] = []
        for raw in resp.get("values") or []:
            rec = {col: (raw[i] if i < len(raw) else "") for i, col in enumerate(AUDIT_COLUMNS)}
            if _ts_epoch(rec.get("ts")) >= cutoff:
                out.append(rec)
        out.reverse()  # sheet appends chronologically -> newest first
        if limit and limit > 0:
            out = out[:limit]
        return out


def build_sheet_sink(
    sheet_id: Optional[str],
    service_account_file: Optional[str],
    *,
    service_account_json: Optional[str] = None,
    tab: str = "Audit",
) -> Optional[GoogleSheetSink]:
    """Construct a sink, or return None if not configured / unavailable.

    Credentials come from either a key file on disk (`service_account_file`)
    or the raw key-file JSON content (`service_account_json`, useful on hosts
    where secrets are passed as env vars instead of files).

    Never raises — a misconfigured Sheet sink must not stop the tool from
    running (the local JSONL store is always the durable record).
    """
    if not sheet_id or not (service_account_file or service_account_json):
        return None
    creds = load_sheet_credentials(service_account_file, service_account_json)
    if creds is None:
        return None
    try:
        return GoogleSheetSink(sheet_id, creds, tab=tab)
    except Exception as exc:  # noqa: BLE001 - never block on sink init
        log.warning("Could not initialise Google Sheets audit sink: %s", exc)
        return None


def safe_append(sink: Optional[GoogleSheetSink], records: Sequence[Dict[str, Any]]) -> bool:
    """Best-effort append that never raises."""
    if sink is None or not records:
        return False
    try:
        return sink.append(records)
    except Exception as exc:  # noqa: BLE001 - logging must not block applies
        log.warning("Audit Sheets append failed (record kept locally): %s", exc)
        return False
