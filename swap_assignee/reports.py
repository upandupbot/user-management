"""CSV reporting."""
from __future__ import annotations

import csv
from typing import Iterable, Optional, Sequence


COLUMNS = [
    "card_id",
    "card_url",
    "title",
    "current_phase",
    "scope",
    "action",
    "old_assignees",
    "new_assignees",
    "error_message",
]


class ReportWriter:
    def __init__(self, path: Optional[str]) -> None:
        self.path = path
        self._fh = None
        self._writer = None
        if path:
            self._fh = open(path, "w", encoding="utf-8", newline="")
            self._writer = csv.writer(self._fh)
            self._writer.writerow(COLUMNS)

    def write(
        self,
        *,
        card_id: str,
        card_url: str,
        title: str,
        current_phase: str,
        scope: str,
        action: str,
        old_assignees: Sequence[str],
        new_assignees: Sequence[str],
        error_message: str = "",
    ) -> None:
        if not self._writer:
            return
        self._writer.writerow([
            card_id,
            card_url,
            title,
            current_phase,
            scope,
            action,
            ",".join(old_assignees),
            ",".join(new_assignees),
            error_message,
        ])

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None
            self._writer = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
