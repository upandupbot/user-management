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
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .audit import AUDIT_COLUMNS

log = logging.getLogger("swap_assignee.audit.sheets")

# Read/write a single spreadsheet the service account has been shared on.
_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def _cell(value: Any) -> Any:
    """Coerce a record value into something the Sheets API accepts."""
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


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
        if not header.get("values"):
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
    try:
        from google.oauth2.service_account import Credentials  # lazy import
    except ImportError:
        log.warning(
            "Google API libraries not installed; the Sheets audit sink is disabled. "
            "Run: pip install -r requirements.txt"
        )
        return None
    try:
        if service_account_json:
            import json

            info = json.loads(service_account_json)
            creds = Credentials.from_service_account_info(info, scopes=_SCOPES)
        else:
            path = Path(service_account_file).expanduser()
            if not path.exists():
                log.warning("Audit service_account_file not found: %s", path)
                return None
            creds = Credentials.from_service_account_file(str(path), scopes=_SCOPES)
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
